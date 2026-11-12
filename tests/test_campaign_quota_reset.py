"""Native reset deadlines survive metadata refresh, replay and campaign restart."""

import copy
import dataclasses
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_campaign_checkpoint_runtime import checkpoint_case as checkpoint_case
from test_campaign_handoff_flow import campaign as campaign
from test_campaign_headless import prepared as prepared
from test_campaign_launch_preparation import pending as pending
from test_campaign_reasoning_controls import queued
from test_campaign_runtime import production_case as production_case
from test_campaign_runtime_admission import configured
from test_campaign_workers import owned_launch as owned_launch

from bmad_loop import campaign_capacity as capacity
from bmad_loop import campaign_handoff_flow as handoff
from bmad_loop import campaign_reasoning_flow as reasoning
from bmad_loop import campaign_workers as workers
from bmad_loop.campaign_model import CampaignError, CampaignSnapshot, CampaignSpec
from bmad_loop.campaign_supervisor import Observation
from bmad_loop.platform_util import LockUnavailableError, file_lock

SOURCE = "agy-native-individual-quota-v1"


def quota(**changes):
    return {"outcome": "quota", "quota_source": SOURCE, "quota_retry_at": 200, **changes}


def snapshot():
    return CampaignSnapshot("quota", "org/repo", CampaignSpec("quota"))


@pytest.mark.parametrize("bad", [True, 0, -1, "200", float("nan"), float("inf"), 10**1000])
def test_invalid_native_reset_is_refused_without_recording(bad):
    state = snapshot()
    with pytest.raises(CampaignError, match="reset evidence"):
        capacity.quota_retry_at(quota(quota_retry_at=bad))
    with pytest.raises(CampaignError, match="timestamp"):
        capacity.record_unavailable(
            state, "antigravity", "model", now=100, reason="quota", quota_retry_at=bad
        )
    assert state.bindings == {}


@pytest.mark.parametrize(
    "patch", [{"outcome": "completed"}, {"quota_source": "model-prose"}, {"quota_source": None}]
)
def test_reset_requires_native_quota_provenance(patch):
    assert capacity.quota_retry_at(quota()) == 200
    with pytest.raises(CampaignError, match="reset evidence"):
        capacity.quota_retry_at(quota(**patch))
    assert capacity.quota_retry_at({"outcome": "quota"}) is None


@pytest.mark.parametrize("later", [None, 150, 200, 250])
def test_fault_merge_never_shortens_a_known_reset(later):
    state = snapshot()
    capacity.record_unavailable(state, "a", "m", now=100, reason="known", quota_retry_at=200)
    capacity.record_unavailable(state, "a", "m", now=110, reason="later", quota_retry_at=later)
    fault = state.bindings["capacity_faults"]["a\0m"]
    assert fault["quota_retry_at"] == max(200, later or 0)
    assert fault["observed_at"] == 110
    capacity.record_unavailable(state, "a", "m", now=90, reason="old")
    assert state.bindings["capacity_faults"]["a\0m"] == fault
    assert capacity.unavailable_reason(state, "a", "m", observed_at=120, now=120)


def test_reset_survives_restart_and_selects_fallback_until_post_reset_observation(
    project, tmp_path
):
    runtime, state, preferred, fallback = configured(project, tmp_path)
    state.spec = dataclasses.replace(
        state.spec, planning=dataclasses.replace(state.spec.planning, enabled=True)
    )
    assert runtime._role(state, "planning") == preferred
    capacity.record_unavailable(
        state,
        preferred.profile,
        preferred.model,
        now=100,
        reason="native individual quota",
        quota_retry_at=200,
    )
    for row in state.bindings["capacity"]["observations"]:
        row["capacity"].update(observed_at=150, expires_at=270)
    runtime.clock = lambda: 150
    assert runtime._role(state, "planning") == fallback
    state = runtime.store.create(state)
    state = runtime.store.load(state.id)
    runtime.clock = lambda: 201
    assert runtime._role(state, "planning") == fallback
    state.bindings["capacity"]["observations"][0]["capacity"]["observed_at"] = 200
    assert runtime._role(state, "planning") == fallback
    state.bindings["capacity"]["observations"][0]["capacity"]["observed_at"] = 201
    assert runtime._role(state, "planning") == preferred


def test_no_deadline_retains_account_rerouting_behavior(project, tmp_path):
    runtime, state, preferred, fallback = configured(project, tmp_path)
    capacity.record_unavailable(state, preferred.profile, preferred.model, now=100, reason="quota")
    assert runtime._role(state, "planning") == fallback
    runtime.clock = lambda: 101
    state.bindings["capacity"]["observed_at"] = 101
    assert runtime._role(state, "planning") == preferred


def test_execution_quota_keeps_original_observation_across_repeat_and_reload(campaign):
    runtime, state, unit, receipt = campaign
    receipt["interruption"].update(quota_source=SOURCE, quota_retry_at=200)
    before = (unit.attempt, unit.failure_attempts, state.generation, state.config_revision)
    assert handoff.intercept_terminal(runtime, state, unit, receipt)
    fault = copy.deepcopy(state.bindings["capacity_faults"])
    assert next(iter(fault.values()))["quota_retry_at"] == 200
    state = runtime.store.save(state, state.revision)
    state = runtime.store.load(state.id)
    unit = state.units[unit.id]
    runtime.clock = lambda: 250
    assert handoff.intercept_terminal(runtime, state, unit, receipt)
    assert state.bindings["capacity_faults"] == fault
    assert (unit.attempt, unit.failure_attempts, state.generation, state.config_revision) == before
    changed = copy.deepcopy(receipt)
    changed["interruption"]["quota_retry_at"] = 300
    with pytest.raises(CampaignError, match="interruption changed"):
        handoff.intercept_terminal(runtime, state, unit, changed)


def test_reasoning_quota_persists_deadline_and_does_not_charge_or_repeat(tmp_path, prepared):
    state, runtime, _, clock, action = queued(tmp_path, prepared)
    row = state.jobs["job-0"]
    before = (row["attempt"], row.get("failure_attempts", 0), row["request_digest"])
    receipt = quota(
        job_id=row["id"],
        request_digest=row["request_digest"],
        launch_id=row["claim"]["launch_id"],
        generation=row["claim"]["generation"],
        quiescent=True,
        usage={"output_tokens": 3},
    )
    observed = Observation(
        "confirmed",
        {"state": "finished", "receipt": receipt, "effect_binding": reasoning._binding(row)},
    )
    reasoning.apply(runtime, state, action, observed)
    assert row["state"] == "capacity_wait" and row["claim"]["quiescent"]
    assert (row["attempt"], row.get("failure_attempts", 0), row["request_digest"]) == before
    assert next(iter(state.bindings["capacity_faults"].values()))["quota_retry_at"] == 200
    fault = copy.deepcopy(state.bindings["capacity_faults"])
    row_before = copy.deepcopy(row)
    state = runtime.store.save(state, state.revision)
    state = runtime.store.load(state.id)
    clock[0] = 250
    reasoning.apply(runtime, state, action, observed)
    assert state.bindings["capacity_faults"] == fault
    assert state.jobs["job-0"] == row_before


def test_prepared_reasoning_rechecks_new_quota_denial_before_native_intent(tmp_path, prepared):
    state, runtime, _, clock, action = queued(tmp_path, prepared)
    frozen = state.clone()
    row = state.jobs["job-0"]
    selected = row["selection"]
    capacity.record_unavailable(
        state,
        selected["profile"],
        selected["model"],
        now=100,
        reason="sibling quota",
        quota_retry_at=200,
    )
    state = runtime.store.save(state, state.revision)
    clock[0] = 101
    result = reasoning.perform(runtime, frozen, action)
    assert result.data["state"] == "deferred" and "quota reset" in result.data["reason"]
    assert not prepared[0].mux.processes
    assert not (Path(row["claim"]["run_dir"]) / "launch-intent.json").exists()
    reasoning.apply(runtime, state, action, result)
    current = state.jobs["job-0"]
    assert current["attempt"] == 1 and current.get("failure_attempts", 0) == 0
    assert current["state"] == "proposed" and current["claim"]["quiescent"]


@pytest.mark.parametrize("selection_key", ["candidate", "review_candidate"])
def test_prepared_worker_quota_refuses_reservation_and_readmits_after_reset(
    pending, monkeypatch, selection_key
):
    case, action = pending
    selected = {"profile": "claude", "model": "fixture"}
    if selection_key == "review_candidate":
        action.payload["candidate"] = {"profile": "codex", "model": "other"}
        action.payload["review_candidate"] = selected
    case.snapshot = case.store.save(case.snapshot, case.snapshot.revision)
    frozen = case.snapshot.clone()
    capacity.record_unavailable(
        case.snapshot, **selected, now=100, reason="sibling quota", quota_retry_at=200
    )
    case.snapshot = case.store.save(case.snapshot, case.snapshot.revision)
    case.runtime.clock = lambda: 101
    before = (
        case.snapshot.units[action.unit].attempt,
        case.snapshot.units[action.unit].failure_attempts,
    )
    calls = []

    def popen(argv, **kwargs):
        # A slow process constructor must not retain the campaign admission
        # lock: unrelated durable state can advance after this reservation.
        with file_lock(case.store.root / ".state.lock", blocking=False):
            pass
        latest = case.store.load(case.snapshot.id)
        latest.bindings["unrelated_writer_progress"] = True
        case.store.save(latest, latest.revision)
        calls.append(argv)
        return SimpleNamespace(pid=12345)

    monkeypatch.setattr(workers, "subprocess", SimpleNamespace(Popen=popen, DEVNULL=-3, STDOUT=-2))
    observed = case.runtime._perform_launch(frozen, action)
    assert observed.state == "confirmed" and observed.data["not_launched"]
    assert "quota reset" in observed.data["quota_wait"]
    directory = case.store.directory(case.snapshot.id) / "workers" / action.id
    assert directory.resolve() not in workers._OWNED_LAUNCHES and calls == []
    case.runtime.apply(case.snapshot, action, observed)
    unit = case.snapshot.units[action.unit]
    assert unit.claim.quiescent and unit.claim.status == "not_launched"
    assert (unit.attempt, unit.failure_attempts) == before
    event = case.snapshot.events[-1]
    assert event["kind"] == "quota-held-launch-suppressed"
    assert event["reason"] == observed.data["quota_wait"]
    old_request = (directory / "request.json").read_bytes()
    case.snapshot.bindings["capacity"] = {
        "observed_at": 201,
        "observations": [{**selected, "capacity": {"observed_at": 201}}],
    }
    case.runtime.clock = lambda: 201
    fresh = dataclasses.replace(action, id="after-reset")
    unit.claim = dataclasses.replace(
        unit.claim, launch_id=fresh.id, status="prepared", quiescent=False
    )
    case.snapshot.outbox.append(fresh)
    case.snapshot = case.store.save(case.snapshot, case.snapshot.revision)
    fresh_directory = case.store.directory(case.snapshot.id) / "workers" / fresh.id
    try:
        result = case.runtime._perform_launch(case.snapshot, fresh)
        assert result.data["launched"] and len(calls) == 1
        latest = case.store.load(case.snapshot.id)
        assert latest.bindings["unrelated_writer_progress"]
        assert (
            latest.units[action.unit].attempt,
            latest.units[action.unit].failure_attempts,
        ) == before
        assert (directory / "request.json").read_bytes() == old_request
    finally:
        workers._OWNED_LAUNCHES.pop(fresh_directory.resolve(), None)


@pytest.mark.parametrize("uncertain", [False, True])
def test_quota_admission_does_not_relabel_an_uncertain_owned_spawn(owned_launch, uncertain):
    path, _ = owned_launch.prepare()
    if uncertain:

        def lost_ack(*args, **kwargs):
            raise KeyboardInterrupt

        owned_launch.module.Popen = lost_ack
        with pytest.raises(KeyboardInterrupt):
            workers.spawn(path)
    else:
        workers.spawn(path)

    @contextmanager
    def held():
        yield "native quota reset"

    with pytest.raises(CampaignError, match="owned process or uncertain spawn") as error:
        workers.spawn(path, admission=held)
    assert not isinstance(error.value, workers.LaunchAdmissionHeld)
    assert len(owned_launch.processes) == (0 if uncertain else 1)


def test_admission_lock_covers_reservation_but_not_process_start(owned_launch, monkeypatch):
    path, _ = owned_launch.prepare()
    lock = path.parent / "state.lock"
    reserved = []
    original = workers._OwnedLaunch

    def reservation(*args, **kwargs):
        with pytest.raises(LockUnavailableError):
            with file_lock(lock, blocking=False):
                pass
        reserved.append(True)
        return original(*args, **kwargs)

    @contextmanager
    def admission():
        with file_lock(lock):
            yield ""

    popen = owned_launch.module.Popen

    def startup(*args, **kwargs):
        with file_lock(lock, blocking=False):
            assert reserved == [True]
        return popen(*args, **kwargs)

    monkeypatch.setattr(workers, "_OwnedLaunch", reservation)
    owned_launch.module.Popen = startup
    workers.spawn(path, admission=admission)
    assert len(owned_launch.processes) == 1


def test_reset_requires_observation_newer_than_later_fault_too():
    state = snapshot()
    capacity.record_unavailable(state, "a", "m", now=100, reason="known", quota_retry_at=200)
    capacity.record_unavailable(state, "a", "m", now=250, reason="still denied")
    assert capacity.unavailable_reason(state, "a", "m", observed_at=220, now=260) == "still denied"
    assert capacity.unavailable_reason(state, "a", "m", observed_at=250, now=260) == "still denied"
    assert capacity.unavailable_reason(state, "a", "m", observed_at=251, now=260) == ""


def test_supervisor_readmits_ready_execution_after_reset_without_resetting_debt(
    production_case, monkeypatch
):
    from concurrent.futures import Future

    from bmad_loop.campaign_model import OutboxAction, UnitState
    from bmad_loop.campaign_supervisor import CampaignSupervisor, Proposal

    case = production_case
    runtime, state = case.runtime, case.snapshot
    clock = [100]
    runtime.clock = lambda: clock[0]
    supervisor = CampaignSupervisor(case.store, runtime, clock=runtime.clock)
    key = "source:project:first"
    state.phase = "active"
    state.bindings["inventory"] = {"complete": True, "observed_at": 100}
    state = case.store.save(state, state.revision)

    def harvest(snapshot, action, result):
        future = Future()
        future.set_result(result)
        supervisor._active[action.id] = future
        return supervisor._harvest(snapshot)

    state = supervisor._queue(state, (Proposal("prepare-unit", key),))
    allocation = next(a for a in state.outbox if a.kind == "prepare-unit")
    state = harvest(state, allocation, runtime.perform(state, allocation))
    # Establish an admitted READY fixture, before the behavior under test.
    unit = state.units[key]
    unit.state = UnitState.READY
    unit.evidence["admission"] = {"accepted": True}
    unit.failure_attempts = 1
    state.bindings["capacity"] = runtime._perform_capacity(state, None).data
    state = case.store.save(state, state.revision)
    proposal = next(p for p in runtime.proposals(state) if p.kind == "launch" and p.unit == key)
    assert proposal.payload["kind"] == "execution"
    state = supervisor._queue(state, (proposal,))
    unit = state.units[key]
    assert unit.state == UnitState.CLAIMED
    action = next(a for a in state.outbox if a.id == unit.claim.launch_id)
    first_attempt, failure_debt = unit.attempt, unit.failure_attempts
    frozen = state.clone()
    selected = action.payload["candidate"]
    capacity.record_unavailable(
        state,
        selected["profile"],
        selected["model"],
        now=100,
        reason="native denial",
        quota_retry_at=200,
    )
    state = case.store.save(state, state.revision)
    clock[0] = 101
    observed = runtime.perform(frozen, action)
    assert observed.data["not_launched"] and not case.processes
    old_dir = case.store.directory(state.id) / "workers" / action.id
    old_request = (old_dir / "request.json").read_bytes()
    state = harvest(state, action, observed)
    unit = state.units[key]
    assert unit.state == UnitState.READY and unit.claim.quiescent
    assert (unit.attempt, unit.failure_attempts) == (first_attempt, failure_debt)
    assert next(a for a in state.outbox if a.id == action.id).state == "confirmed"
    assert not any(p.kind == "launch" and p.unit == key for p in runtime.proposals(state))
    clock[0] = 201
    assert not any(p.kind == "launch" and p.unit == key for p in runtime.proposals(state))
    refresh = OutboxAction("fresh-capacity", "capacity", "", {}, state.generation)
    runtime.apply(state, refresh, runtime.perform(state, refresh))
    state = case.store.save(state, state.revision)
    proposal = next(p for p in runtime.proposals(state) if p.kind == "launch" and p.unit == key)
    state = supervisor._queue(state, (proposal,))
    unit = state.units[key]
    assert unit.state == UnitState.CLAIMED and not unit.claim.quiescent
    assert unit.claim.launch_id != action.id
    assert unit.attempt == first_attempt + 1 and unit.failure_attempts == failure_debt
    assert (old_dir / "request.json").read_bytes() == old_request
    assert not case.processes
