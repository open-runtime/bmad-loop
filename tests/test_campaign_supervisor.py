"""Single-writer supervision admits concurrent work and persists before effects.

Scripted drivers consume no model tokens. Real store revisions, command inboxes,
OS coordinator locks and thread executors expose restart and ordering defects.
"""

from __future__ import annotations

import threading
from dataclasses import replace

import pytest

from bmad_loop.campaign_model import (
    CampaignSnapshot,
    CampaignSpec,
    CampaignUnit,
    Hold,
    UnitSpec,
    UnitState,
    WorkerClaim,
)
from bmad_loop.campaign_scheduler import admit
from bmad_loop.campaign_store import CampaignStore, add_action, transition
from bmad_loop.campaign_supervisor import CampaignSupervisor, Observation, Proposal


@pytest.fixture
def campaign_store(project, tmp_path):
    units = [
        UnitSpec("s:e:first", "s", "e", "First", scopes=("src/a",)),
        UnitSpec("s:e:second", "s", "e", "Second", scopes=("src/b",)),
    ]
    spec = CampaignSpec(
        "campaign", units=tuple(units), max_workers=2, max_active_epics=2, poll_interval_s=0.001
    )
    snapshot = CampaignSnapshot(
        "supervisor-test",
        "a/b",
        spec,
        {unit.id: CampaignUnit(unit, UnitState.READY) for unit in units},
        common_dir=str(project.project / ".git"),
        root=str(project.project),
    )
    store = CampaignStore(tmp_path / "state")
    store.create(snapshot)
    return store, snapshot.id


class PipelineDriver:
    stages = {
        UnitState.LOCALLY_COMPLETE: UnitState.SYNCING,
        UnitState.SYNCING: UnitState.VERIFYING,
        UnitState.VERIFYING: UnitState.REVIEWING,
        UnitState.REVIEWING: UnitState.READY_TO_PUBLISH,
        UnitState.READY_TO_PUBLISH: UnitState.MERGE_REQUESTED,
        UnitState.MERGE_REQUESTED: UnitState.MERGED_PENDING_SYNC,
        UnitState.MERGED_PENDING_SYNC: UnitState.DELIVERED,
    }

    def __init__(self, store, *, barrier=None):
        self.store = store
        self.barrier = barrier
        self.performed = []
        self.reconciled = []
        self.max_start_parallel = 0
        self.start_parallel = 0
        self.lock = threading.Lock()
        self.deliveries = []

    def proposals(self, snapshot):
        proposals = [Proposal("start", key) for key in admit(snapshot).units]
        proposals += [
            Proposal("advance", key, {"state": self.stages[unit.state].value})
            for key, unit in snapshot.units.items()
            if unit.state in self.stages and not unit.hold
        ]
        return tuple(proposals)

    def perform(self, snapshot, action):
        persisted = self.store.load(snapshot.id)
        stored_action = next(row for row in persisted.outbox if row.id == action.id)
        assert stored_action.state == "sent_unknown"
        assert stored_action.generation == snapshot.generation
        assert persisted.revision >= snapshot.revision
        self.performed.append((action.kind, action.unit))
        if action.kind == "start":
            assert persisted.units[action.unit].claim is not None
            with self.lock:
                self.start_parallel += 1
                self.max_start_parallel = max(self.max_start_parallel, self.start_parallel)
            if self.barrier:
                self.barrier.wait(timeout=5)
            with self.lock:
                self.start_parallel -= 1
        return Observation("confirmed", {"kind": action.kind, **action.payload})

    def reconcile_action(self, snapshot, action):
        self.reconciled.append((action.id, snapshot.generation, action.generation))
        return Observation("unknown", reason="Ownership remains uncertain", retry_at=10**12)

    def apply(self, snapshot, action, observation):
        if action.kind == "start":
            transition(snapshot, action.unit, UnitState.RUNNING)
        elif action.kind == "finish":
            snapshot.units[action.unit].claim.quiescent = True
            transition(snapshot, action.unit, UnitState.LOCALLY_COMPLETE)
        elif action.kind == "advance":
            following = UnitState(action.payload["state"])
            transition(snapshot, action.unit, following)
            if following == UnitState.DELIVERED:
                self.deliveries.append(action.unit)

    def observe_workers(self, snapshot):
        return tuple(
            Proposal("finish", key)
            for key, unit in snapshot.units.items()
            if unit.state == UnitState.RUNNING and not unit.hold
        )

    def control(self, snapshot, command):
        kind = command["kind"]
        if kind == "prepare-effect":
            action = command["action"]
            if action["kind"] == "start":
                unit = snapshot.units[action["unit"]]
                unit.attempt += 1
                unit.claim = WorkerClaim(unit.id, unit.attempt, snapshot.generation, action["id"])
                transition(snapshot, unit.id, UnitState.CLAIMED)
        elif kind == "hold":
            snapshot.units[command["unit"]].hold = Hold("operator", command["reason"])
        elif kind == "release":
            snapshot.units[command["unit"]].hold = None
        elif kind == "stop":
            snapshot.phase = "draining"


def test_workers_in_one_epic_start_concurrently_with_durable_claims(campaign_store):
    store, campaign_id = campaign_store
    driver = PipelineDriver(store, barrier=threading.Barrier(2))
    result = CampaignSupervisor(store, driver, progress=lambda _: None).run(
        campaign_id, max_ticks=500
    )
    assert result.phase == "delivered", [
        (row.kind, row.unit, row.state, row.error) for row in result.outbox
    ]
    assert driver.max_start_parallel == 2
    assert all(unit.claim.quiescent for unit in result.units.values())
    assert len([row for row in driver.performed if row[0] == "start"]) == 2
    assert not driver.reconciled


def test_stop_preserves_uncertain_api_debt_without_starting_another_retry(campaign_store):
    store, campaign_id = campaign_store
    snapshot = store.load(campaign_id)
    action = add_action(snapshot, "metadata", "s:e:first", {})
    action.state = "sent_unknown"
    snapshot = store.save(snapshot, snapshot.revision)
    store.enqueue(campaign_id, "stop")

    class WaitingDriver(PipelineDriver):
        def reconcile_action(self, snapshot, action):
            assert snapshot.phase == "draining"
            self.reconciled.append(action.id)
            return Observation("unknown", reason="API unavailable", retry_at=99)

    driver = WaitingDriver(store)
    result = CampaignSupervisor(store, driver, clock=lambda: 100, progress=lambda _: None).run(
        campaign_id, max_ticks=5
    )
    assert result.phase == "stopped"
    assert driver.reconciled == [action.id]
    assert result.outbox[0].state == "sent_unknown"
    assert all(unit.claim is None for unit in result.units.values())


def test_hold_from_durable_inbox_blocks_one_unit_while_other_delivers(campaign_store):
    store, campaign_id = campaign_store
    store.enqueue(campaign_id, "hold", unit="s:e:first", reason="Owner deciding")
    driver = PipelineDriver(store)
    supervisor = CampaignSupervisor(store, driver, progress=lambda _: None)
    result = supervisor.run(campaign_id, max_ticks=100)
    assert result.units["s:e:first"].state == UnitState.READY
    assert result.units["s:e:first"].hold.kind == "operator"
    assert result.units["s:e:second"].state == UnitState.DELIVERED
    assert driver.deliveries == ["s:e:second"]
    store.enqueue(campaign_id, "release", unit="s:e:first")
    result = supervisor.run(campaign_id, max_ticks=200)
    assert result.phase == "delivered"
    assert driver.deliveries == ["s:e:second", "s:e:first"]


def test_restart_reconciles_unknown_create_before_generation_change_without_repeat(campaign_store):
    store, campaign_id = campaign_store
    snapshot = store.load(campaign_id)
    action = add_action(snapshot, "remote-create", "s:e:first", {}, action_id="create1")
    action.state = "sent_unknown"
    store.save(snapshot, snapshot.revision)
    driver = PipelineDriver(store)
    result = CampaignSupervisor(store, driver, progress=lambda _: None).run(
        campaign_id, max_ticks=2
    )
    assert driver.reconciled[0] == ("create1", 0, 0)
    assert not any(kind == "remote-create" for kind, _ in driver.performed)
    uncertain = next(row for row in result.outbox if row.id == "create1")
    assert uncertain.state == "sent_unknown"
    assert uncertain.next_retry_at == 10**12
    assert result.generation == 1


def test_unknown_live_claim_survives_restart_and_retains_capacity(campaign_store):
    store, campaign_id = campaign_store
    snapshot = store.load(campaign_id)
    unit = snapshot.units["s:e:first"]
    unit.state = UnitState.CLAIMED
    unit.claim = WorkerClaim(unit.id, 1, 0, "uncertain-launch", pid=None)
    snapshot.spec = replace(snapshot.spec, max_workers=1)
    snapshot.config_revision += 1
    store.save(snapshot, snapshot.revision)
    driver = PipelineDriver(store)
    result = CampaignSupervisor(store, driver, progress=lambda _: None).run(
        campaign_id, max_ticks=3
    )
    assert result.units["s:e:first"].claim.launch_id == "uncertain-launch"
    assert not result.units["s:e:first"].claim.quiescent
    assert result.units["s:e:second"].state == UnitState.READY
    assert not driver.performed


def test_delivery_tracking_debt_prevents_dependent_admission(campaign_store):
    store, campaign_id = campaign_store
    snapshot = store.load(campaign_id)
    snapshot.units["s:e:second"].spec = replace(
        snapshot.units["s:e:second"].spec, after=("s:e:first",)
    )
    snapshot.graph_revision += 1
    store.save(snapshot, snapshot.revision)

    class DebtDriver(PipelineDriver):
        def perform(self, snapshot, action):
            if action.payload.get("state") == UnitState.DELIVERED.value:
                return Observation(
                    "waiting", reason="GitHub tracking API unavailable", retry_at=10**12
                )
            return super().perform(snapshot, action)

    driver = DebtDriver(store)
    result = CampaignSupervisor(store, driver, progress=lambda _: None).run(
        campaign_id, max_ticks=100
    )
    assert result.units["s:e:first"].state == UnitState.MERGED_PENDING_SYNC
    assert result.units["s:e:second"].state == UnitState.READY
    assert driver.deliveries == []


def test_prepared_action_restart_is_revalidated_and_adopted_before_effect(campaign_store):
    store, campaign_id = campaign_store
    driver = PipelineDriver(store)
    snapshot = store.load(campaign_id)
    action = add_action(snapshot, "start", "s:e:first", {}, action_id="prepared-start")
    # Crash after claim+intent persistence, before sent_unknown or spawning.
    from dataclasses import asdict

    driver.control(snapshot, {"kind": "prepare-effect", "action": asdict(action)})
    store.save(snapshot, snapshot.revision)
    result = CampaignSupervisor(store, driver, progress=lambda _: None).run(
        campaign_id, max_ticks=200
    )
    adopted = next(row for row in result.outbox if row.id == "prepared-start")
    assert adopted.state == "confirmed", adopted.error
    assert result.phase == "delivered"
    assert sum(kind == "start" and unit == "s:e:first" for kind, unit in driver.performed) == 1
