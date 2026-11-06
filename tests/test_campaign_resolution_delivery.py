"""Accepted diagnosis cannot substitute for observed same-PR repair and verification."""

from types import SimpleNamespace

import pytest
from conftest import git
from test_campaign_resolution_flow import candidate, diagnosis, state

from bmad_loop import verify
from bmad_loop.campaign_model import CampaignError, UnitState, WorkerClaim
from bmad_loop.campaign_resolution_delivery import (
    apply,
    finish,
    next_step,
    prepare,
    validate_repair,
)
from bmad_loop.campaign_resolution_flow import (
    apply_context,
    apply_diagnosis,
    collect_context,
)
from bmad_loop.campaign_resolution_flow import prepare as begin
from bmad_loop.campaign_resolution_flow import (
    proposals,
    validate_stage_result,
)


def accepted(project):
    snapshot = state(project)
    unit = snapshot.units["s:project:u"]
    unit.state, unit.resume_state = UnitState.REVIEWING, UnitState.SYNCING
    context = collect_context(project.project, snapshot, unit.id, "diagnosis")
    apply_context(snapshot, unit.id, context)
    launch = proposals(snapshot, project.project, candidate(), now=10)[0]
    begin(snapshot, unit.id, launch.payload)
    receipt = {
        "outcome": "stage_complete",
        "head": unit.bindings["head"],
        "artifact": diagnosis(context["packet"]),
    }
    receipt["validated_resolution"] = validate_stage_result(
        project.project, snapshot, unit.id, receipt
    )
    apply_diagnosis(snapshot, unit.id, receipt, candidate=candidate()["escalation"])
    return snapshot, unit


def repair(project, unit, path="src/cache/db.py"):
    file = project.project / path
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text("accepted correction", encoding="utf-8")
    git(project.project, "add", path)
    git(project.project, "commit", "-m", "repair accepted finding")
    return {
        "head": verify.rev_parse_head(project.project),
        "launch_id": "repair-actor",
        "protected_source_before": "same-source",
        "protected_source_after": "same-source",
    }


def test_repair_requires_separate_verification_and_confirmed_push(project):
    snapshot, unit = accepted(project)
    receipt = repair(project, unit)
    unit.evidence["repair_candidate"] = validate_repair(snapshot, unit, receipt)
    following = next_step(snapshot, unit)
    assert following.kind == "launch" and following.payload["stage"] == "resolution_verify"
    unit.claim = WorkerClaim(unit.id, 2, snapshot.generation, "verify-actor", quiescent=True)
    unit.evidence["resolution_verification"] = {
        "outcome": "verified",
        "resolution_id": unit.evidence["accepted_resolution"]["id"],
        "head": receipt["head"],
        "commands": [],
    }
    following = next_step(snapshot, unit)
    pushed = []
    runtime = SimpleNamespace(
        _pusher=lambda *args: SimpleNamespace(
            push=lambda action, head: pushed.append(head) or SimpleNamespace(state="confirmed")
        )
    )
    result = finish(
        runtime, snapshot, unit, SimpleNamespace(payload=following.payload, id="repair-push")
    )
    assert result.state == "confirmed" and pushed == [receipt["head"]]
    apply(snapshot, unit, result.data)
    assert unit.hold is None and unit.state == UnitState.SYNCING
    assert "review" not in unit.evidence and "verification" not in unit.evidence
    assert "accepted_resolution" not in unit.evidence


def test_repair_escape_is_refused_after_actor_exit(project):
    snapshot, unit = accepted(project)
    receipt = repair(project, unit, "outside.py")
    with pytest.raises(CampaignError, match="outside.*scope"):
        validate_repair(snapshot, unit, receipt)


def test_repair_cannot_rewrite_source_policy(project):
    snapshot, unit = accepted(project)
    receipt = repair(project, unit)
    receipt["protected_source_after"] = "different-source"
    with pytest.raises(CampaignError, match="frozen source"):
        validate_repair(snapshot, unit, receipt)


def test_failed_verification_never_authorizes_finish_or_delivery(project):
    snapshot, unit = accepted(project)
    unit.claim = WorkerClaim(unit.id, 2, snapshot.generation, "verify-actor", quiescent=True)
    unit.evidence["resolution_verification"] = {
        "resolution_id": unit.evidence["accepted_resolution"]["id"],
        "outcome": "verification_failed",
    }
    action = SimpleNamespace(
        payload={"proposal_id": unit.evidence["accepted_resolution"]["id"]}, id="push"
    )
    assert finish(None, snapshot, unit, action).state == "rejected"
    assert unit.hold is not None and unit.state != UnitState.DELIVERED


def test_repair_followups_consume_a_lifetime_budget(project):
    snapshot, unit = accepted(project)
    payload = {"resolution_proposal": unit.evidence["accepted_resolution"]}
    for _ in range(snapshot.spec.resolution.max_attempts_per_unit * 2):
        prepare(unit, payload)
    assert next_step(snapshot, unit).kind == "resolution-exhausted"
