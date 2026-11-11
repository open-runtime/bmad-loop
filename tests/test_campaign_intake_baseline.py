"""Selected initial issues reuse story presentation without swallowing new intent."""

import dataclasses
import json

import pytest
from conftest import git
from test_campaign_headless import prepared as prepared
from test_campaign_reasoning_semantics import case
from test_campaign_runtime_intake import observed

from bmad_loop import campaign_intake_baseline as baseline
from bmad_loop import campaign_intake_flow as flow
from bmad_loop import campaign_reasoning_results as results
from bmad_loop import campaign_runtime_intake as intake
from bmad_loop.campaign_intake import IntakeBatch
from bmad_loop.campaign_jobs import current, request_from_dict, validate_result
from bmad_loop.campaign_model import CampaignError, Hold, UnitState
from bmad_loop.campaign_store import add_action


def setup(tmp_path, prepared, project):
    snapshot, runtime, candidate = case(tmp_path, prepared, ["triage"])
    unit = next(iter(snapshot.units.values()))
    unit.state = UnitState.READY
    path = project.project / "docs/baseline.md"
    path.parent.mkdir(exist_ok=True)
    path.write_text("# Accepted story\nPreserve quantities across restart.\n")
    git(project.project, "add", "docs/baseline.md")
    git(project.project, "commit", "-m", "fixture: selected product intent")
    head = git(project.project, "rev-parse", "HEAD")
    observation = observed(body="Preserve quantities across restart.")
    unit.bindings.update(
        workspace=str(project.project),
        spec_path="docs/baseline.md",
        head=head,
        issue={"number": observation.number, "database_id": int(observation.object_id)},
    )
    intake.apply_poll(
        snapshot,
        {
            "batch": dataclasses.asdict(
                IntakeBatch((observation,), observation.updated_at, True, 0)
            ),
            "observed_at": 100,
        },
    )
    snapshot = runtime.store.save(snapshot, snapshot.revision)
    unit = snapshot.units[unit.id]
    return snapshot, runtime, unit, observation, candidate


def launch_request(snapshot, runtime):
    proposal = baseline.proposals(snapshot)[0]
    action = add_action(snapshot, proposal.kind, unit=proposal.unit, payload=proposal.payload)
    seen = runtime.perform(snapshot, action)
    assert seen.state == "confirmed", seen
    runtime.apply(snapshot, action, seen)
    action.state = "confirmed"
    row = next(
        row
        for row in snapshot.jobs.values()
        if row["request"]["coordinates"].get("intake_baseline")
    )
    return row


def finish(snapshot, runtime, row, disposition="represented", missing=()):
    request = request_from_dict(row["request"])
    result = {
        "context_digest": request.coordinates["baseline_digest"],
        "disposition": disposition,
        "rationale": "Issue asks for the exact persisted quantity behavior in the selected specification.",
        "unrepresented_requirements": list(missing),
    }
    validate_result(result, request.output_schema)
    row.update(result=result, validation="valid", state="validated")
    results.apply_result(runtime, snapshot, row)
    row["state"] = "applied"


def test_initial_issue_is_compared_to_committed_source_before_control_allocation(
    tmp_path, prepared, project
):
    snapshot, runtime, unit, observation, candidate = setup(tmp_path, prepared, project)
    before = set(snapshot.units)
    assert baseline.waiting(snapshot, unit.id)
    assert flow.untriaged(snapshot) == ()
    assert not intake.proposals(snapshot, candidate)
    assert (
        intake.create_control(
            snapshot, {"input_versions": [observation.version_id], "packet_id": "old-allocation"}
        )
        == ""
    )
    row = launch_request(snapshot, runtime)
    request = request_from_dict(row["request"])
    packet = json.loads(open(request.inputs[0]["path"]).read())
    assert (
        packet["selected_specification"]
        == "# Accepted story\nPreserve quantities across restart.\n"
    )
    assert packet["observation"] == observation.to_dict()
    assert request.unit == unit.id and request.required
    finish(snapshot, runtime, row)
    assert snapshot.bindings[flow.INBOX]["records"][observation.version_id]["state"] == "baseline"
    assert not baseline.waiting(snapshot, unit.id)
    assert set(snapshot.units) == before and not flow.untriaged(snapshot)
    assert not intake.proposals(snapshot, candidate)


def test_unrepresented_initial_requirement_returns_to_ordinary_triage(tmp_path, prepared, project):
    snapshot, runtime, unit, observation, candidate = setup(tmp_path, prepared, project)
    row = launch_request(snapshot, runtime)
    finish(snapshot, runtime, row, "needs_triage", ["New export format"])
    assert flow.untriaged(snapshot) == (observation.version_id,)
    proposal = intake.proposals(snapshot, candidate)[0]
    control = intake.create_control(snapshot, proposal.payload)
    assert snapshot.units[control].bindings["intake_control"]["input_versions"] == [
        observation.version_id
    ]
    assert not baseline.waiting(snapshot, unit.id)


@pytest.mark.parametrize("change", ["head", "source", "graph", "config", "issue"])
def test_stale_baseline_cannot_consume_intent(tmp_path, prepared, project, change):
    snapshot, runtime, unit, observation, _ = setup(tmp_path, prepared, project)
    row = launch_request(snapshot, runtime)
    request = request_from_dict(row["request"])
    assert current(snapshot, request)
    if change == "head":
        unit.bindings["head"] = "changed-head"
    elif change == "source":
        unit.spec = dataclasses.replace(unit.spec, acceptance=("Changed acceptance",))
    elif change == "graph":
        snapshot.graph_revision += 1
    elif change == "config":
        snapshot.config_revision += 1
    else:
        edited = observed(body="Owner added a new export requirement")
        intake.apply_poll(
            snapshot,
            {
                "batch": dataclasses.asdict(IntakeBatch((edited,), edited.updated_at, True, 0)),
                "observed_at": 101,
            },
        )
    assert not current(snapshot, request)
    with pytest.raises(CampaignError, match="current validated result"):
        finish(snapshot, runtime, row)
    assert snapshot.bindings[flow.INBOX]["records"][observation.version_id]["state"] != "baseline"


@pytest.mark.parametrize("state", ["capacity_wait", "failed", "stale"])
def test_unavailable_baseline_falls_back_without_spawning_another_job(
    tmp_path, prepared, project, state
):
    snapshot, runtime, unit, observation, candidate = setup(tmp_path, prepared, project)
    row = launch_request(snapshot, runtime)
    row.update(state=state, failure_attempts=snapshot.spec.reasoning.max_attempts)
    proposal = baseline.proposals(snapshot)[0]
    assert proposal.payload["fallback"]
    action = add_action(snapshot, proposal.kind, unit=proposal.unit, payload=proposal.payload)
    runtime.apply(snapshot, action, runtime.perform(snapshot, action))
    assert flow.untriaged(snapshot) == (observation.version_id,)
    assert intake.proposals(snapshot, candidate)[0].kind == "intake-control"
    assert len(snapshot.jobs) == 1
    assert not baseline.waiting(snapshot, unit.id)


def test_comments_and_later_edits_are_not_initial_baselines(tmp_path, prepared, project):
    snapshot, runtime, unit, observation, candidate = setup(tmp_path, prepared, project)
    finish(snapshot, runtime, launch_request(snapshot, runtime))
    comment = observed(body="Owner requires CSV export", kind="comment")
    edited = observed(body="New issue acceptance")
    intake.apply_poll(
        snapshot,
        {
            "batch": dataclasses.asdict(IntakeBatch((comment, edited), edited.updated_at, True, 0)),
            "observed_at": 102,
        },
    )
    assert set(flow.untriaged(snapshot)) == {comment.version_id, edited.version_id}
    assert not baseline.selected(snapshot)
    assert intake.proposals(snapshot, candidate)[0].kind == "intake-control"


def test_operator_hold_blocks_baseline_preparation(tmp_path, prepared, project):
    snapshot, runtime, unit, observation, _ = setup(tmp_path, prepared, project)
    unit.hold = Hold("operator", "Owner paused work")
    assert not baseline.proposals(snapshot)
    assert snapshot.bindings[flow.INBOX]["records"][observation.version_id]["state"] == "pending"


def test_unavailable_launcher_falls_back_after_bounded_admission_wait(
    tmp_path, prepared, project, monkeypatch
):
    from bmad_loop import campaign_reasoning_flow

    snapshot, runtime, unit, observation, candidate = setup(tmp_path, prepared, project)
    row = launch_request(snapshot, runtime)
    monkeypatch.setattr(runtime, "_role", lambda *_args, **_kwargs: None)
    assert campaign_reasoning_flow.proposals(runtime, snapshot) == ()
    assert row["state"] == "proposed" and row["attempt"] == 0
    assert baseline.proposals(snapshot, now=129) == ()
    proposal = baseline.proposals(snapshot, now=131)[0]
    assert proposal.payload["fallback"]
    action = add_action(snapshot, proposal.kind, unit=proposal.unit, payload=proposal.payload)
    runtime.apply(snapshot, action, runtime.perform(snapshot, action))
    assert not baseline.waiting(snapshot, unit.id)
    assert flow.untriaged(snapshot) == (observation.version_id,)
    assert intake.proposals(snapshot, candidate)[0].kind == "intake-control"


def test_baseline_result_cannot_claim_coverage_with_missing_requirements(
    tmp_path, prepared, project
):
    snapshot, runtime, _, observation, _ = setup(tmp_path, prepared, project)
    row = launch_request(snapshot, runtime)
    with pytest.raises(CampaignError, match="cannot omit"):
        finish(snapshot, runtime, row, missing=["New owner requirement"])
    assert snapshot.bindings[flow.INBOX]["records"][observation.version_id]["state"] == "pending"


def test_deferred_never_launched_attempt_can_fall_back_after_resume(
    tmp_path, prepared, project, monkeypatch
):
    from bmad_loop import campaign_reasoning_flow as reasoning

    snapshot, runtime, unit, observation, candidate = setup(tmp_path, prepared, project)
    row = launch_request(snapshot, runtime)
    action = add_action(
        snapshot,
        "reasoning-launch",
        unit=unit.id,
        payload={"job_id": row["id"], "candidate": dataclasses.asdict(candidate)},
    )
    reasoning.prepare_effect(runtime, snapshot, action)
    assert row["attempt"] == 1 and not row["claim"]["quiescent"]
    unit.hold = Hold("operator", "Pause before native launch")
    reasoning.apply(runtime, snapshot, action, reasoning._deferred(row, "operator hold"))
    assert row["state"] == "proposed" and row["claim"]["status"] == "not_launched"
    unit.hold = None
    monkeypatch.setattr(runtime, "_role", lambda *_args, **_kwargs: None)
    assert reasoning.proposals(runtime, snapshot) == ()
    proposal = baseline.proposals(snapshot, now=131)[0]
    assert proposal.payload["fallback"]
    fallback = add_action(snapshot, proposal.kind, unit=proposal.unit, payload=proposal.payload)
    runtime.apply(snapshot, fallback, runtime.perform(snapshot, fallback))
    assert flow.untriaged(snapshot) == (observation.version_id,)


def test_missing_archived_job_returns_issue_to_ordinary_triage(tmp_path, prepared, project):
    snapshot, runtime, unit, observation, _ = setup(tmp_path, prepared, project)
    row = launch_request(snapshot, runtime)
    del snapshot.jobs[row["id"]]
    proposal = baseline.proposals(snapshot, now=101)[0]
    assert proposal.payload["fallback"]
    action = add_action(snapshot, proposal.kind, unit=proposal.unit, payload=proposal.payload)
    runtime.apply(snapshot, action, runtime.perform(snapshot, action))
    assert flow.untriaged(snapshot) == (observation.version_id,)


def test_baseline_wait_does_not_reserve_an_execution_slot_and_is_visible(
    tmp_path, prepared, project, monkeypatch
):
    from bmad_loop.campaign_metrics import observe_admission
    from bmad_loop.campaign_scheduler import admit

    snapshot, runtime, unit, _, candidate = setup(tmp_path, prepared, project)
    snapshot.spec = dataclasses.replace(snapshot.spec, max_workers=1)
    ordinary = admit(snapshot, now=100)
    assert ordinary.units == (unit.id,)
    blocked = baseline.admission_waits(snapshot)
    admission = admit(snapshot, now=100, blocked=blocked)
    assert len(admission.units) == 1 and admission.units[0] != unit.id
    assert admission.waits[unit.id] == "initial issue comparison pending"
    observe_admission(snapshot, now=100)
    assert unit.evidence["admission_wait"]["reason"] == admission.waits[unit.id]
    monkeypatch.setattr(runtime, "_role", lambda *_args, **_kwargs: candidate)
    for row in snapshot.units.values():
        row.evidence["admission"] = {"ready": True}
    launches = [proposal for proposal in runtime.proposals(snapshot) if proposal.kind == "launch"]
    assert len(launches) == 1
    assert launches[0].unit == admission.units[0]
    assert launches[0].payload["kind"] == "execution"
