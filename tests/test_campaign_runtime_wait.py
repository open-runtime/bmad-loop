"""Completion waits observe terminal campaign state, independently of loop speed."""

import threading
from types import SimpleNamespace

import pytest
from test_campaign_runtime import run_case
from test_campaign_supervisor import PipelineDriver
from test_campaign_supervisor import campaign_store as campaign_store

from bmad_loop.campaign_model import UnitState
from bmad_loop.campaign_supervisor import Observation, Proposal


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
