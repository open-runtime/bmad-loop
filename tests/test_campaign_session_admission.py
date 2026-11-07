"""Reasoning and coding workers share one durable global admission budget."""

from dataclasses import asdict, replace

from test_campaign_headless import prepared as prepared
from test_campaign_reasoning_jobs import case

from bmad_loop.campaign_model import RoleSpec, UnitState
from bmad_loop.campaign_scheduler import active_sessions
from bmad_loop.campaign_supervisor import Proposal


def limited_case(tmp_path, prepared, monkeypatch, count=1):
    from bmad_loop import campaign_boundary_sync

    candidate = prepared[3]

    def configure(snapshot):
        snapshot.bindings["inventory"] = {"complete": True, "observed_at": 100}
        for unit in snapshot.units.values():
            unit.state = UnitState.DELIVERED
        unit = next(iter(snapshot.units.values()))
        unit.state = UnitState.REVIEWING
        unit.bindings.update(workspace=str(tmp_path), branch="unit", head="head", base="base")

    snapshot, driver, supervisor, clock = case(
        tmp_path,
        prepared,
        count=count,
        max_sessions=1,
        configure=configure,
        job_unit="",
        extra_roles=(
            (
                "publication_review",
                RoleSpec(
                    profile=candidate.profile,
                    model=candidate.model,
                    effort=candidate.effort,
                ),
            ),
        ),
    )
    unit = next(iter(snapshot.units.values()))
    monkeypatch.setattr(campaign_boundary_sync, "before_proposal", lambda _r, _s, p: p)
    proposal = Proposal(
        "launch",
        unit.id,
        {
            "kind": "stage",
            "role": "publication_review",
            "stage": "publication_review",
            "candidate": asdict(candidate),
        },
    )
    return snapshot, driver, supervisor, clock, proposal


def test_live_reasoning_job_blocks_review_and_releases_capacity(tmp_path, prepared, monkeypatch):
    snapshot, driver, supervisor, _, _ = limited_case(tmp_path, prepared, monkeypatch)
    snapshot = supervisor._queue(snapshot, driver.observe_workers(snapshot))
    assert active_sessions(snapshot) == 1
    assert not any(p.kind == "launch" for p in driver.proposals(snapshot))
    snapshot.jobs["job-0"]["claim"]["quiescent"] = True
    for action in snapshot.outbox:
        action.state = "confirmed"
    assert any(p.kind == "launch" for p in driver.proposals(snapshot))


def test_never_sent_admission_defers_without_claim_attempt_or_hold(tmp_path, prepared, monkeypatch):
    snapshot, driver, supervisor, _, proposal = limited_case(tmp_path, prepared, monkeypatch)
    snapshot = supervisor._queue(snapshot, driver.observe_workers(snapshot))
    snapshot = supervisor._queue(snapshot, (proposal,))
    unit = snapshot.units[proposal.unit]
    assert unit.claim is None and unit.attempt == 0 and unit.hold is None
    deferred = [a for a in snapshot.outbox if a.kind == "launch"]
    assert len(deferred) == 1 and deferred[0].state == "confirmed"
    assert deferred[0].receipt["admission_deferred"]
    snapshot.jobs["job-0"]["claim"]["quiescent"] = True
    snapshot = supervisor._queue(snapshot, (proposal,))
    assert snapshot.units[proposal.unit].claim.launch_id != deferred[0].id
    assert snapshot.units[proposal.unit].attempt == 1
    assert active_sessions(snapshot) == 1


def test_reasoning_prepare_guard_does_not_spend_attempt_when_last_slot_was_taken(
    tmp_path, prepared, monkeypatch
):
    snapshot, driver, supervisor, _, _ = limited_case(tmp_path, prepared, monkeypatch, count=2)
    first = driver.observe_workers(snapshot)[0]
    second = replace(first, payload={**first.payload, "job_id": "job-1"})
    snapshot = supervisor._queue(snapshot, (first, second))
    assert snapshot.jobs["job-0"]["claim"] and not snapshot.jobs["job-0"]["claim"]["quiescent"]
    assert snapshot.jobs["job-1"]["claim"] is None
    assert snapshot.jobs["job-1"]["attempt"] == 0
    assert active_sessions(snapshot) == 1


def test_supervisor_reserves_reasoning_before_unit_proposals(tmp_path, prepared, monkeypatch):
    snapshot, driver, supervisor, _, _ = limited_case(tmp_path, prepared, monkeypatch)
    original = driver.proposals
    seen = []

    def proposals(current):
        seen.append(active_sessions(current))
        result = original(current)
        assert not any(p.kind == "launch" for p in result)
        return ()  # This fixture has no external GitHub service.

    monkeypatch.setattr(driver, "proposals", proposals)
    supervisor.run(snapshot.id, max_ticks=1)
    assert seen == [1]
    assert len(prepared[0].mux.processes) == 1
    for process in prepared[0].mux.processes.values():
        assert process.wait(timeout=30) == 0
