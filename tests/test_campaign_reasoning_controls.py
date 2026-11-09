"""Operator controls fence native reasoning admission and retain retry ownership."""

import copy
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from test_campaign_handoff_flow import campaign as campaign
from test_campaign_handoff_retry import start
from test_campaign_headless import prepared as prepared
from test_campaign_reasoning_jobs import case

from bmad_loop import campaign_hold
from bmad_loop import campaign_reasoning_flow as flow
from bmad_loop import campaign_takeover
from bmad_loop.campaign_model import Hold, OutboxAction
from bmad_loop.campaign_supervisor import Proposal


def queued(tmp_path, prepared):
    snapshot, runtime, supervisor, clock = case(tmp_path, prepared, count=1)
    snapshot = supervisor._queue(snapshot, flow.proposals(runtime, snapshot))
    return snapshot, runtime, supervisor, clock, snapshot.outbox[0]


def finish_job(snapshot, runtime, supervisor, clock):
    with ThreadPoolExecutor(max_workers=2) as executor:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            snapshot = supervisor._harvest(snapshot)
            if snapshot.jobs["job-0"]["state"] == "validated":
                return snapshot
            proposals = tuple(
                p for p in flow.proposals(runtime, snapshot) if p.kind != "reasoning-apply"
            )
            snapshot = supervisor._queue(snapshot, proposals)
            snapshot = supervisor._dispatch_ready(snapshot, executor)
            clock[0] += 1
            time.sleep(0.02)
    pytest.fail(str(snapshot.jobs["job-0"]))


def release(runtime, snapshot, unit):
    runtime.control(snapshot, {"kind": "release", "unit": unit, "id": "release-control"})
    pending = snapshot.units[unit].bindings["operator_release"]
    action = OutboxAction("release-proof", "operator-release", unit, pending, snapshot.generation)
    campaign_takeover.apply(
        runtime, snapshot, action, campaign_takeover.observe(runtime, snapshot, action)
    )
    return runtime.store.save(snapshot, snapshot.revision)


@pytest.mark.parametrize("terminate", [False, True])
def test_stop_cancels_only_prepared_reasoning_and_resume_runs_same_job(
    tmp_path, prepared, terminate, monkeypatch
):
    snapshot, runtime, supervisor, clock, action = queued(tmp_path, prepared)
    prior = copy.deepcopy(snapshot.jobs["job-0"])
    runtime.control(snapshot, {"kind": "stop", "payload": {"terminate_workers": terminate}})
    snapshot = runtime.store.save(snapshot, snapshot.revision)
    with ThreadPoolExecutor(max_workers=1) as executor:
        snapshot = supervisor._dispatch_ready(snapshot, executor)
        assert not supervisor._active
    row = snapshot.jobs["job-0"]
    assert not prepared[0].mux.processes
    assert row["state"] == "proposed" and row["claim"]["quiescent"]
    assert row["claim"]["status"] == "not_launched"
    assert row["attempt"] == prior["attempt"] == 1
    assert row.get("failure_attempts", 0) == 0
    assert snapshot.outbox[0].state == "rejected"
    # Exercise ordinary drain/resume; unrelated unit development is outside this
    # reasoning fixture, while claims, generation adoption and dispatch are real.
    monkeypatch.setattr(runtime, "proposals", lambda _: ())
    monkeypatch.setattr(
        runtime, "observe_workers", lambda snapshot: flow.proposals(runtime, snapshot)
    )
    assert supervisor.run(snapshot.id, max_ticks=1).phase == "stopped"
    supervisor.run(snapshot.id, max_ticks=1)
    snapshot = runtime.store.load(snapshot.id)
    snapshot = finish_job(snapshot, runtime, supervisor, clock)
    row = snapshot.jobs["job-0"]
    assert row["request_digest"] == prior["request_digest"]
    assert row["attempt"] == 2 and row["claim"]["launch_id"] != action.id
    assert row.get("failure_attempts", 0) == 0
    assert len(prepared[0].mux.processes) == 1


def test_handoff_reasoning_waits_for_operator_hold_at_proposal(campaign):
    runtime, snapshot, unit = start(campaign)
    assert any(p.kind == "reasoning-launch" for p in flow.proposals(runtime, snapshot))
    unit.hold = Hold("operator", "Pause this unit", id="hold-control")
    assert not any(p.kind == "reasoning-launch" for p in flow.proposals(runtime, snapshot))


def test_hold_after_proposal_does_not_allocate_prepared_attempt(tmp_path, prepared):
    snapshot, runtime, supervisor, _ = case(tmp_path, prepared, count=1)
    proposals = flow.proposals(runtime, snapshot)
    runtime.control(snapshot, {"kind": "hold", "unit": proposals[0].unit, "id": "hold-control"})
    snapshot = supervisor._queue(snapshot, proposals)
    row = snapshot.jobs["job-0"]
    assert row["attempt"] == 0 and row["claim"] is None
    assert snapshot.outbox[0].state == "confirmed"
    assert snapshot.outbox[0].receipt["state"] == "deferred"


@pytest.mark.parametrize("timing", ["before-prepare", "during-prepare"])
@pytest.mark.parametrize("control", ["store", "channel", "stop"])
def test_fresh_hold_defers_without_failure_then_release_runs(
    tmp_path, prepared, monkeypatch, timing, control
):
    snapshot, runtime, supervisor, clock, action = queued(tmp_path, prepared)
    frozen = snapshot.clone()
    row = snapshot.jobs["job-0"]
    unit = row["request"]["unit"]
    actual_prepare = prepared[0].prepare
    calls = []

    def hold():
        latest = runtime.store.load(snapshot.id)
        if control == "stop":
            # The executor is already submitted; stop must leave its claim for
            # the authoritative launch guard to reconcile.
            latest.outbox[0].state = "sent_unknown"
            runtime.control(latest, {"kind": "stop"})
            runtime.store.save(latest, latest.revision)
            return
        runtime.control(latest, {"kind": "hold", "unit": unit, "id": "hold-control"})
        if control == "store":
            runtime.store.save(latest, latest.revision)
            # Independently prove the authoritative store path, not the channel.
            campaign_hold.write(
                campaign_hold.channel(runtime.store.directory(snapshot.id), unit),
                campaign=snapshot.id,
                unit=unit,
                hold="",
            )

    def prepare(*args, **kwargs):
        calls.append(True)
        path = actual_prepare(*args, **kwargs)
        if timing == "during-prepare":
            hold()
        return path

    monkeypatch.setattr(prepared[0], "prepare", prepare)
    if timing == "before-prepare":
        hold()
    observation = flow.perform(runtime, frozen, action)
    assert observation.state == "confirmed" and observation.data["state"] == "deferred"
    assert observation.data["effect_binding"] == action.payload["job_binding"]
    assert len(calls) == (timing == "during-prepare")
    assert not prepared[0].mux.processes
    latest = runtime.store.load(snapshot.id)
    flow.apply(runtime, latest, action, observation)
    latest.outbox[0].state = "confirmed"
    assert latest.jobs["job-0"]["claim"]["quiescent"]
    assert latest.jobs["job-0"].get("failure_attempts", 0) == 0
    latest = runtime.store.save(latest, latest.revision)
    if control == "channel":
        runtime.control(latest, {"kind": "hold", "unit": unit, "id": "hold-control"})
        latest = runtime.store.save(latest, latest.revision)
    if control == "stop":
        latest.phase = "active"
        latest = runtime.store.save(latest, latest.revision)
    else:
        latest = release(runtime, latest, unit)
    monkeypatch.setattr(prepared[0], "prepare", actual_prepare)
    latest = finish_job(latest, runtime, supervisor, clock)
    assert latest.jobs["job-0"]["attempt"] == 2
    assert len(prepared[0].mux.processes) == 1


@pytest.mark.parametrize("reconcile", [False, True])
@pytest.mark.parametrize("state", ["prepared", "sent_unknown"])
def test_sent_unknown_native_intent_is_observed_during_hold_and_stop(
    tmp_path, prepared, monkeypatch, reconcile, state
):
    snapshot, runtime, supervisor, _, action = queued(tmp_path, prepared)
    action.state = state
    snapshot = runtime.store.save(snapshot, snapshot.revision)
    launched = flow.perform(runtime, snapshot, action)
    assert launched.state == "confirmed"
    assert len(prepared[0].mux.processes) == 1
    runtime.control(snapshot, {"kind": "hold", "unit": action.unit, "id": "hold-control"})
    runtime.control(snapshot, {"kind": "stop"})
    snapshot = runtime.store.save(snapshot, snapshot.revision)
    assert snapshot.outbox[0].state == state
    assert not snapshot.jobs["job-0"]["claim"]["quiescent"]
    for child in prepared[0].mux.processes.values():
        assert child.wait(timeout=20) == 0
    monkeypatch.setattr(
        prepared[0], "launch", lambda _: pytest.fail("intent must reconcile, never launch")
    )
    observation = flow.perform(runtime, snapshot, action, reconcile=reconcile)
    assert observation.data["state"] == "finished"
    flow.apply(runtime, snapshot, action, observation)
    assert snapshot.jobs["job-0"]["state"] == "validated"
    assert snapshot.jobs["job-0"]["receipt"] == observation.data["receipt"]
    assert snapshot.jobs["job-0"]["claim"]["quiescent"]


def test_intent_discovered_before_deferred_apply_cannot_synthesize_quiescence(tmp_path, prepared):
    snapshot, runtime, _, clock, action = queued(tmp_path, prepared)
    runtime.control(snapshot, {"kind": "hold", "unit": action.unit, "id": "hold-control"})
    snapshot = runtime.store.save(snapshot, snapshot.revision)
    observed = flow.perform(runtime, snapshot, action)
    row = snapshot.jobs["job-0"]
    path = Path(row["claim"]["run_dir"]) / "launch-intent.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}")
    flow.apply(runtime, snapshot, action, observed)
    assert row["state"] == "uncertain" and not row["claim"]["quiescent"]
    assert row["receipt"] is None
    clock[0] += 2
    assert flow.proposals(runtime, snapshot)[0].kind == "reasoning-observe"


def test_prepared_observer_settles_after_stop_without_request(tmp_path, prepared):
    snapshot, runtime, supervisor, _, action = queued(tmp_path, prepared)
    snapshot = supervisor._queue(
        snapshot, (Proposal("reasoning-observe", action.unit, {"job_id": "job-0"}),)
    )
    runtime.control(snapshot, {"kind": "stop"})
    snapshot = runtime.store.save(snapshot, snapshot.revision)
    observe = next(item for item in snapshot.outbox if item.kind == "reasoning-observe")
    result = flow.perform(runtime, snapshot, observe)
    assert result.state == "confirmed" and result.data["state"] == "settled"
    assert not prepared[0].mux.processes
