"""Completion waits observe terminal campaign state, independently of loop speed."""

import threading
from types import SimpleNamespace

import pytest
from test_campaign_runtime import run_case
from test_campaign_supervisor import PipelineDriver
from test_campaign_supervisor import campaign_store as campaign_store

from bmad_loop.campaign_model import UnitState
from bmad_loop.campaign_supervisor import Observation, Proposal


def test_progress_watchdog_waits_for_new_milestones_beyond_old_absolute_deadline(campaign_store):
    from campaign_runtime_watchdog import ProgressWatchdog

    store, campaign_id = campaign_store
    snapshot = store.load(campaign_id)
    watch = ProgressWatchdog(snapshot, now=0, idle_s=10)
    unit = next(iter(snapshot.units.values()))
    unit.state = UnitState.RUNNING
    assert watch.check(snapshot, now=9) == ""
    unit.state = UnitState.LOCALLY_COMPLETE
    assert watch.check(snapshot, now=18) == ""
    unit.state = UnitState.DELIVERED
    assert watch.check(snapshot, now=27) == ""


def test_progress_watchdog_poll_retry_and_repeated_states_do_not_reset_guard(campaign_store):
    from campaign_runtime_watchdog import ProgressWatchdog

    store, campaign_id = campaign_store
    snapshot = store.load(campaign_id)
    watch = ProgressWatchdog(snapshot, now=0, idle_s=10)
    unit = next(iter(snapshot.units.values()))
    unit.state = UnitState.REVIEWING
    assert watch.check(snapshot, now=1) == ""
    unit.state = UnitState.READY  # Already observed at initialization.
    assert watch.check(snapshot, now=2) == ""
    for now in range(3, 11):
        unit.state = UnitState.REVIEWING
        unit.bindings["head"] = f"new-head-{now}"
        unit.failure_attempts = now
        snapshot.revision += 1
        snapshot.events.append({"kind": "poll-completed", "observed_at": now})
        assert watch.check(snapshot, now=now) == ""
    assert watch.check(snapshot, now=11) == "new durable milestone within 10s"


def test_progress_watchdog_validated_stage_counts_once(campaign_store):
    from campaign_runtime_watchdog import ProgressWatchdog

    store, campaign_id = campaign_store
    snapshot = store.load(campaign_id)
    watch = ProgressWatchdog(snapshot, now=0, idle_s=10)
    stages = {}
    next(iter(snapshot.units.values())).evidence["planning_flow"] = {"stages": stages}
    stages["requirements"] = {"validated": {"proof": "first"}}
    assert watch.check(snapshot, now=9) == ""
    stages["requirements"] = {"validated": {"proof": "second"}}
    stages["arbitrary-attempt-id"] = {"validated": {"proof": "not a defined stage"}}
    assert watch.check(snapshot, now=18) == ""
    assert watch.check(snapshot, now=19) == "new durable milestone within 10s"


def test_progress_watchdog_absolute_ceiling_wins_even_with_new_progress(campaign_store):
    from campaign_runtime_watchdog import ProgressWatchdog

    store, campaign_id = campaign_store
    snapshot = store.load(campaign_id)
    watch = ProgressWatchdog(snapshot, now=0, idle_s=10)
    unit = next(iter(snapshot.units.values()))
    for now, state in (
        (9, UnitState.RUNNING),
        (18, UnitState.LOCALLY_COMPLETE),
        (27, UnitState.REVIEWING),
    ):
        unit.state = state
        assert watch.check(snapshot, now=now) == ""
    unit.state = UnitState.DELIVERED
    assert watch.check(snapshot, now=30) == "terminal state within 30s absolute ceiling"


def test_progress_watchdog_explicit_deadline_does_not_extend(campaign_store):
    from campaign_runtime_watchdog import ProgressWatchdog

    store, campaign_id = campaign_store
    snapshot = store.load(campaign_id)
    watch = ProgressWatchdog(snapshot, now=0, timeout_s=10)
    next(iter(snapshot.units.values())).state = UnitState.DELIVERED
    assert watch.check(snapshot, now=9) == ""
    assert watch.check(snapshot, now=10) == "terminal state within 10s absolute ceiling"


def test_progress_watchdog_unbounded_intake_does_not_extend_initial_budget(campaign_store):
    from dataclasses import replace

    from campaign_runtime_watchdog import ProgressWatchdog

    from bmad_loop.campaign_model import CampaignUnit

    store, campaign_id = campaign_store
    snapshot = store.load(campaign_id)
    watch = ProgressWatchdog(snapshot, now=0, idle_s=10)
    initial = set(snapshot.units)
    spec = next(iter(snapshot.units.values())).spec
    for now in range(1, 11):
        key = f"unexpected-{now}"
        snapshot.units[key] = CampaignUnit(replace(spec, id=key), UnitState.DELIVERED)
        reason = watch.check(snapshot, now=now)
        assert watch.units == initial
    assert reason == "new durable milestone within 10s"


def test_failure_details_preserves_interrupted_worker_evidence(campaign_store, tmp_path):
    from campaign_runtime_watchdog import failure_details

    from bmad_loop.campaign_model import Hold, WorkerClaim

    store, campaign_id = campaign_store
    snapshot = store.load(campaign_id)
    unit = next(iter(snapshot.units.values()))
    directory = tmp_path / "failed-worker"
    directory.mkdir()
    (directory / "engine.pid").write_text("123 456.0")
    (directory / "startup.json").write_text('{"launch_id": "old-actor"}')
    (directory / "script.log").write_text("x" * 10000 + "failure tail")
    unit.claim = WorkerClaim(unit.id, 1, 1, "old-actor", run_dir=str(directory), quiescent=True)
    unit.hold = Hold("technical_failure", "interrupted without receipt")
    unit.bindings["head"] = "bound-head"
    process = SimpleNamespace(pid=123, poll=lambda: 17)
    details = failure_details(SimpleNamespace(processes={directory: process}), snapshot, {})
    worker = details["workers"][unit.id]
    assert worker["bound_head"] == "bound-head"
    assert worker["owned_returncode"] == 17
    assert worker["files"]["engine.pid"]["tail"] == "123 456.0"
    assert worker["files"]["script.log"]["tail"].endswith("failure tail")
    assert len(worker["files"]["script.log"]["tail"]) == 4096
    assert "error" in worker["files"]["receipt.json"]


def test_effect_timings_preserve_results_exceptions_and_active_call_evidence():
    from campaign_runtime_watchdog import TimedDriver

    now = [0.0]
    action = SimpleNamespace(id="action", kind="launch", unit="unit")
    expected = Observation("confirmed", {"observed": True})

    def perform(snapshot, received):
        assert snapshot == "snapshot" and received is action
        now[0] = 3.0
        assert timed.details()["active_effect_timings"][0]["elapsed_s"] == 3.0
        now[0] = 5.0
        return expected

    def reconcile(snapshot, received):
        now[0] += 7.0
        raise ValueError("original failure")

    timed = TimedDriver(
        SimpleNamespace(perform=perform, reconcile_action=reconcile), clock=lambda: now[0]
    )
    assert timed.perform("snapshot", action) is expected
    with pytest.raises(ValueError, match="original failure"):
        timed.reconcile_action("snapshot", action)
    details = timed.details()
    assert details["active_effect_timings"] == []
    assert [row["duration_s"] for row in details["slowest_effects"]] == [7.0, 5.0]


def test_completion_wait_includes_delayed_hierarchy_close_after_600_ticks(campaign_store):
    store, campaign_id = campaign_store
    started, release, past_old_limit, finished = (threading.Event() for _ in range(4))

    class ClosingDriver(PipelineDriver):
        pending_ticks = 0
        closed = False
        clock = staticmethod(lambda: 100.0)

        def proposals(self, snapshot):
            proposals = super().proposals(snapshot)
            if all(unit.state == UnitState.DELIVERED for unit in snapshot.units.values()):
                if not any(row.kind == "hierarchy-close" for row in snapshot.outbox):
                    return proposals + (Proposal("hierarchy-close", "s:e:first"),)
            return proposals

        def perform(self, snapshot, action):
            if action.kind == "hierarchy-close":
                started.set()
                if not release.wait(60):
                    raise AssertionError("test did not release its closure effect")
                return Observation("confirmed")
            return super().perform(snapshot, action)

        def observe_workers(self, snapshot):
            if started.is_set() and not release.is_set():
                self.pending_ticks += 1
                if self.pending_ticks > 650:
                    past_old_limit.set()
            return super().observe_workers(snapshot)

        def apply(self, snapshot, action, observation):
            if action.kind == "hierarchy-close":
                self.closed = True
            else:
                super().apply(snapshot, action, observation)

        def completion_ready(self, snapshot):
            return self.closed

    driver = ClosingDriver(store)
    case = SimpleNamespace(store=store, runtime=driver, snapshot=store.load(campaign_id))
    outcomes, errors = [], []

    def run():
        try:
            outcomes.append(run_case(case))
        except BaseException as exc:
            errors.append(exc)
        finally:
            finished.set()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    try:
        assert started.wait(30), "closure effect was never launched"
        assert past_old_limit.wait(30), "coordinator stopped at an iteration budget"
        assert not finished.is_set(), "pending hierarchy closure was treated as completion"
    finally:
        release.set()
        thread.join(30)
    assert not thread.is_alive(), "coordinator did not stop after confirmed closure"
    if errors:
        raise errors[0]
    assert outcomes[0].phase == "delivered"
    assert any(
        row.kind == "hierarchy-close" and row.state == "confirmed" for row in outcomes[0].outbox
    )


def test_completion_wait_deadline_has_durable_state_and_effect_diagnostics(campaign_store):
    store, campaign_id = campaign_store
    driver = PipelineDriver(store)
    driver.clock = lambda: 100.0
    case = SimpleNamespace(store=store, runtime=driver, snapshot=store.load(campaign_id))
    with pytest.raises(AssertionError, match="terminal state within 0s") as caught:
        run_case(case, timeout_s=0)
    assert all(
        name in str(caught.value) for name in ("phase", "units", "unsettled", "active_effects")
    )


def test_explicit_partial_run_retains_tick_bound(campaign_store):
    store, campaign_id = campaign_store
    driver = PipelineDriver(store)
    driver.clock = lambda: 100.0
    case = SimpleNamespace(store=store, runtime=driver, snapshot=store.load(campaign_id))
    assert run_case(case, ticks=1).phase == "active"
