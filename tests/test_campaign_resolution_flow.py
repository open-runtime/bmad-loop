"""Production recovery proposals preserve ownership, authority and stage budgets."""

import dataclasses
import json

import pytest
from conftest import git

from bmad_loop import verify
from bmad_loop.campaign_decisions import ObservedFact
from bmad_loop.campaign_model import (
    CampaignError,
    CampaignSnapshot,
    CampaignSpec,
    CampaignUnit,
    DecisionSpec,
    Hold,
    ResolutionSpec,
    RoleCandidate,
    UnitSpec,
    UnitState,
)
from bmad_loop.campaign_recovery import Quiescence, RecoveryReceipt
from bmad_loop.campaign_resolution import CauseCode
from bmad_loop.campaign_resolution_flow import (
    apply_backoff,
    apply_context,
    apply_diagnosis,
    collect_context,
    prepare,
    proposals,
    recover_append_only,
    resolution_context,
    typed_failure,
    validate_stage_result,
)


def state(project, *, hold="verification_failed"):
    repo = project.project
    spec = CampaignSpec("resolution", resolution=ResolutionSpec(mode="bounded-auto"))
    unit = CampaignUnit(
        UnitSpec("s:project:u", "s", "epic", "Cache", scopes=("src/cache",)),
        UnitState.RUNNING,
        hold=Hold(hold, "Fix the accepted cache behavior", id="hold-1"),
        bindings={
            "head": verify.rev_parse_head(repo),
            "base": verify.rev_parse_head(repo),
            "workspace": str(repo),
        },
    )
    unit.evidence["worker_history"] = [
        {"outcome": "verification_failed", "launch_id": "failed-worker"}
    ]
    return CampaignSnapshot("campaign", "org/repo", spec, {unit.id: unit})


def candidate():
    return {
        "escalation": RoleCandidate("codex", "astra"),
        "decision_review": RoleCandidate("claude", "fable"),
        "conflict_repair": RoleCandidate("codex", "astra"),
    }


def test_context_collection_is_a_separate_effect_before_model_launch(project):
    snapshot = state(project)
    first = proposals(snapshot, project.project, candidate(), now=10)
    assert first[0].kind == "resolution-context"
    context = collect_context(project.project, snapshot, "s:project:u", "diagnosis")
    apply_context(snapshot, "s:project:u", context)
    launch = proposals(snapshot, project.project, candidate(), now=10)[0]
    assert launch.kind == "launch"
    assert launch.payload["role"] == "escalation"
    assert launch.payload["result_contract"] == "resolution-v1"
    assert launch.payload["read_only"] is True
    assert snapshot.units["s:project:u"].resolution_attempts == 0
    prepare(snapshot, "s:project:u", launch.payload)
    assert snapshot.units["s:project:u"].resolution_attempts == 1
    assert proposals(snapshot, project.project, candidate(), now=10) == ()


def diagnosis(packet, **changes):
    raw = {
        "schema_version": 1,
        "coordinates": packet["coordinates"],
        "cause_id": packet["cause"]["id"],
        "cause_code": packet["cause"]["code"],
        "action": "repair",
        "rationale": "Repair the accepted cache behavior",
        "evidence_ids": [packet["cause"]["id"]],
        "affected_paths": ["src/cache/db.py"],
        "preserved_refs": packet["preserved_refs"],
        "verification": packet["expected_verification"],
        "unresolved_decisions": [],
        "command": [],
        "status": "done",
    }
    return {**raw, **changes}


def test_validated_diagnosis_requests_separate_owned_same_unit_repair(project):
    snapshot = state(project)
    context = collect_context(project.project, snapshot, "s:project:u", "diagnosis")
    apply_context(snapshot, "s:project:u", context)
    launch = proposals(snapshot, project.project, candidate(), now=10)[0]
    prepare(snapshot, "s:project:u", launch.payload)
    packet = context["packet"]
    receipt = {
        "outcome": "stage_complete",
        "head": packet["coordinates"]["head"],
        "artifact": diagnosis(packet),
    }
    receipt["validated_resolution"] = validate_stage_result(
        project.project, snapshot, "s:project:u", receipt
    )
    next_step = apply_diagnosis(
        snapshot, "s:project:u", receipt, candidate=candidate()["escalation"]
    )
    assert next_step.kind == "launch"
    assert next_step.unit == "s:project:u"
    assert next_step.payload["stage"] == "technical_repair"
    assert next_step.payload["read_only"] is False
    assert snapshot.units["s:project:u"].state != UnitState.DELIVERED
    assert snapshot.units["s:project:u"].resolution_attempts == 1
    assert snapshot.units["s:project:u"].evidence["accepted_resolution"]["automatic"] is True


def test_model_capacity_wait_keeps_authorized_repair_resumable(project):
    snapshot = state(project)
    context = collect_context(project.project, snapshot, "s:project:u", "diagnosis")
    apply_context(snapshot, "s:project:u", context)
    launch = proposals(snapshot, project.project, candidate(), now=10)[0]
    prepare(snapshot, "s:project:u", launch.payload)
    packet = context["packet"]
    receipt = {
        "outcome": "stage_complete",
        "head": packet["coordinates"]["head"],
        "artifact": diagnosis(packet),
    }
    receipt["validated_resolution"] = validate_stage_result(
        project.project, snapshot, "s:project:u", receipt
    )
    assert apply_diagnosis(snapshot, "s:project:u", receipt) is None
    assert proposals(snapshot, project.project, {}, now=10) == ()
    resumed = proposals(snapshot, project.project, candidate(), now=10)
    assert resumed[0].payload["result_contract"] == "repair-v1"


def test_serialized_context_fingerprint_is_stable_across_restart(project):
    snapshot = state(project)
    original = collect_context(project.project, snapshot, "s:project:u", "diagnosis")
    roundtrip = json.loads(json.dumps(original, sort_keys=True))
    assert resolution_context(original["packet"]) == resolution_context(roundtrip["packet"])


def test_typed_transient_backoff_precedes_any_model_session(project):
    snapshot = state(project)
    unit = snapshot.units["s:project:u"]
    unit.evidence["failure"] = {
        "code": "quota",
        "producer": "adapter.quota",
        "revision": "quota-v1",
        "id": "quota-1",
    }
    first = proposals(snapshot, project.project, candidate(), now=100)
    assert first[0].kind == "resolution-backoff"
    apply_backoff(snapshot, unit.id, now=100)
    assert unit.retry_at == 105
    assert proposals(snapshot, project.project, candidate(), now=104) == ()
    retry = proposals(snapshot, project.project, candidate(), now=105)
    assert retry[0].kind == "resolution-reprobe"


def test_unknown_legacy_prose_never_becomes_automatic_transient_cause(project):
    snapshot = state(project, hold="failed")
    snapshot.units["s:project:u"].evidence["worker_history"] = [
        {"outcome": "failed", "error": "quota retry transport rate limit"}
    ]
    assert typed_failure(snapshot, "s:project:u").code == CauseCode.UNKNOWN
    assert proposals(snapshot, project.project, candidate(), now=10)[0].kind == "resolution-context"


def test_owner_decision_uses_evidence_review_before_escalation(project):
    snapshot = state(project, hold="product_choice")
    path = project.project / "owner-notes.md"
    path.write_text("Use SQLite for the cache.")
    git(project.project, "add", path.name)
    git(project.project, "commit", "-qm", "adopt owner decision")
    unit = snapshot.units["s:project:u"]
    unit.bindings["head"] = verify.rev_parse_head(project.project)
    snapshot.spec = dataclasses.replace(
        snapshot.spec, decisions=DecisionSpec(sources=("owner-notes.md",))
    )
    context = collect_context(project.project, snapshot, unit.id, "decision")
    apply_context(snapshot, unit.id, context)
    launch = proposals(snapshot, project.project, candidate(), now=10)[0]
    assert launch.payload["role"] == "decision_review"
    assert launch.payload["candidate"]["model"] == "fable"
    prepare(snapshot, unit.id, launch.payload)
    packet = context["packet"]
    source = packet["sources"][0]
    artifact = {
        "schema_version": 1,
        "coordinates": packet["coordinates"],
        "hold_id": unit.hold.id,
        "disposition": "prior_decision_applies",
        "action": "choose",
        "answer": "SQLite",
        "support": [
            {
                "source_id": source["id"],
                "revision": source["revision"],
                "quote": "Use SQLite for the cache.",
            }
        ],
    }
    receipt = {
        "outcome": "stage_complete",
        "head": packet["coordinates"]["head"],
        "artifact": artifact,
    }
    receipt["validated_resolution"] = validate_stage_result(
        project.project, snapshot, unit.id, receipt
    )
    assert apply_diagnosis(snapshot, unit.id, receipt) is None
    assert unit.hold is None
    assert snapshot.bindings["decision_receipts"]
    assert unit.resolution_attempts == 0


def test_changed_candidate_or_authoritative_document_cannot_clear_hold(project):
    snapshot = state(project, hold="product_choice")
    context = collect_context(project.project, snapshot, "s:project:u", "decision")
    apply_context(snapshot, "s:project:u", context)
    launch = proposals(snapshot, project.project, candidate(), now=10)[0]
    prepare(snapshot, "s:project:u", launch.payload)
    snapshot.units["s:project:u"].bindings["head"] = "changed-head"
    with pytest.raises(CampaignError, match="no longer matches"):
        validate_stage_result(
            project.project,
            snapshot,
            "s:project:u",
            {"outcome": "stage_complete", "head": context["packet"]["coordinates"]["head"]},
        )


def test_recovery_wrapper_keeps_fence_through_confirmed_publication(project):
    snapshot = state(project)
    packet = collect_context(project.project, snapshot, "s:project:u", "diagnosis")["packet"]
    ctx = resolution_context(packet)
    calls = []
    head = "a" * 40
    corrected = "b" * 40
    ctx = dataclasses.replace(ctx, coordinates=dataclasses.replace(ctx.coordinates, head=head))

    class Recovery:
        def begin(self, action, **kwargs):
            calls.append(("begin", action, kwargs))

        def recover(self, action, **kwargs):
            calls.append(("recover", action, kwargs))
            return RecoveryReceipt(action, head, corrected, "tree", "ref", None, (), "committed")

        def finish(self, action, **kwargs):
            calls.append(("finish", action, kwargs))
            return RecoveryReceipt(action, head, corrected, "tree", "ref", None, (), "complete")

    result = recover_append_only(
        Recovery(),
        "recovery-1",
        context=ctx,
        baseline="c" * 40,
        owned_paths=("src/cache/db.py",),
        quiescence=Quiescence(head, 2, True, True, "observed-quiescence"),
    )
    assert [row[0] for row in calls] == ["begin", "recover", "finish"]
    assert calls[-1][2]["quiescence"].head == corrected
    assert result["state"] == "complete"
    assert result["head"] == corrected


def test_approval_revocation_after_context_collection_invalidates_satisfaction(project):
    snapshot = state(project, hold="github_review")
    unit = snapshot.units["s:project:u"]
    fact = ObservedFact(
        "review-1",
        "github_approval",
        unit.hold.id,
        unit.id,
        unit.bindings["head"],
        True,
        "eligible-reviewer",
        "review-v1",
        verified=True,
    )
    result = collect_context(project.project, snapshot, unit.id, "decision", facts=(fact,))
    apply_context(snapshot, unit.id, result)
    launch = proposals(snapshot, project.project, candidate(), now=10)[0]
    prepare(snapshot, unit.id, launch.payload)
    packet = result["packet"]
    raw = {
        "schema_version": 1,
        "coordinates": packet["coordinates"],
        "hold_id": unit.hold.id,
        "disposition": "already_satisfied",
        "action": "continue",
        "answer": "Current review approved",
        "fact_ids": ["review-1"],
    }
    receipt = {"outcome": "stage_complete", "head": unit.bindings["head"], "artifact": raw}
    with pytest.raises(CampaignError, match="independently refreshed"):
        validate_stage_result(project.project, snapshot, unit.id, receipt)
    with pytest.raises(CampaignError, match="facts changed"):
        validate_stage_result(
            project.project,
            snapshot,
            unit.id,
            receipt,
            facts=(dataclasses.replace(fact, satisfied=False),),
        )
    receipt["validated_resolution"] = validate_stage_result(
        project.project, snapshot, unit.id, receipt, facts=(fact,)
    )
    apply_diagnosis(snapshot, unit.id, receipt)
    assert unit.hold is None


def test_authoritative_document_edit_after_context_cannot_apply_old_decision(project):
    snapshot = state(project, hold="product_choice")
    path = project.project / "owner-notes.md"
    path.write_text("Use SQLite for the cache.")
    git(project.project, "add", path.name)
    git(project.project, "commit", "-qm", "adopt owner decision")
    unit = snapshot.units["s:project:u"]
    unit.bindings["head"] = verify.rev_parse_head(project.project)
    snapshot.spec = dataclasses.replace(
        snapshot.spec, decisions=DecisionSpec(sources=("owner-notes.md",))
    )
    result = collect_context(project.project, snapshot, unit.id, "decision")
    apply_context(snapshot, unit.id, result)
    launch = proposals(snapshot, project.project, candidate(), now=10)[0]
    prepare(snapshot, unit.id, launch.payload)
    packet = result["packet"]
    source = packet["sources"][0]
    artifact = {
        "schema_version": 1,
        "coordinates": packet["coordinates"],
        "hold_id": unit.hold.id,
        "disposition": "prior_decision_applies",
        "action": "choose",
        "answer": "SQLite",
        "support": [
            {
                "source_id": source["id"],
                "revision": source["revision"],
                "quote": "Use SQLite for the cache.",
            }
        ],
    }
    path.write_text("Wait for a new owner decision before choosing storage.")
    with pytest.raises(CampaignError, match="changed or was revoked"):
        validate_stage_result(
            project.project,
            snapshot,
            unit.id,
            {"outcome": "stage_complete", "head": unit.bindings["head"], "artifact": artifact},
        )
    assert unit.hold is not None


def test_diagnosis_packet_redacts_credentials_but_preserves_evidence_coordinates(project):
    snapshot = state(project, hold="failed")
    secret = "ghp_scripted_test_credential_never_real"
    unit = snapshot.units["s:project:u"]
    unit.evidence["worker_history"] = [
        {
            "outcome": "failed",
            "error": f"request refused with {secret}",
            "authorization": "Bearer test-credential",
        }
    ]
    packet = collect_context(project.project, snapshot, unit.id, "diagnosis")["packet"]
    rendered = json.dumps(packet)
    assert secret not in rendered
    assert "Bearer test-credential" not in rendered
    assert unit.bindings["head"] in rendered
    assert "<redacted:credential>" in rendered


def test_accepted_technical_diagnosis_can_finish_without_waiving_original_hold(project):
    from bmad_loop.campaign_resolution import ResolutionResult
    from bmad_loop.campaign_resolution_flow import finish_repair

    snapshot = state(project)
    unit = snapshot.units["s:project:u"]
    unit.state = UnitState.REVIEWING
    unit.resume_state = UnitState.SYNCING
    context = collect_context(project.project, snapshot, unit.id, "diagnosis")
    apply_context(snapshot, unit.id, context)
    launch = proposals(snapshot, project.project, candidate(), now=10)[0]
    prepare(snapshot, unit.id, launch.payload)
    packet = context["packet"]
    receipt = {
        "outcome": "stage_complete",
        "head": packet["coordinates"]["head"],
        "artifact": diagnosis(packet),
    }
    receipt["validated_resolution"] = validate_stage_result(
        project.project, snapshot, unit.id, receipt
    )
    next_step = apply_diagnosis(snapshot, unit.id, receipt, candidate=candidate()["escalation"])
    assert unit.hold.kind == "resolution"
    assert unit.hold.evidence["original_hold"]["kind"] == "verification_failed"
    accepted = unit.evidence["accepted_resolution"]
    from bmad_loop.campaign_model import EvidenceCoordinates

    result = ResolutionResult(
        accepted["id"],
        EvidenceCoordinates(**accepted["coordinates"]),
        unit.bindings["head"],
        unit.bindings["head"],
        True,
        True,
        "source",
        recovery_push_confirmed=True,
    )
    finish_repair(snapshot, unit.id, result)
    assert unit.state == UnitState.SYNCING
    assert unit.hold is None
    assert next_step.unit == unit.id


def test_owner_holds_cannot_enter_technical_diagnosis_to_bypass_decision_review(project):
    snapshot = state(project, hold="owner_stop")
    with pytest.raises(CampaignError, match="decision evidence review first"):
        collect_context(project.project, snapshot, "s:project:u", "diagnosis")
