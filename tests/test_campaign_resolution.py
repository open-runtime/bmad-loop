"""Bounded diagnosis cannot waive holds, reset budgets or bypass acceptance."""

from dataclasses import asdict, replace

import pytest

from bmad_loop.campaign_model import (
    CampaignError,
    CampaignSnapshot,
    CampaignUnit,
    EvidenceCoordinates,
    Hold,
    ResolutionSpec,
    UnitSpec,
    UnitState,
    WorkerClaim,
    spec_from_dict,
)
from bmad_loop.campaign_resolution import (
    CauseCode,
    FailureEvidence,
    ResolutionContext,
    ResolutionResult,
    apply_resolution,
    begin_resolution,
    diagnosis_unchanged,
    failure_from_receipt,
    record_proposal,
    resolution_fingerprint,
    resolution_packet,
    transient_retry,
    validate_proposal,
)


def context(code=CauseCode.VERIFICATION, **changes):
    cause = FailureEvidence(code, "engine.verify", "failure-1", "assert failed", "result-v1")
    values = dict(
        coordinates=EvidenceCoordinates("org/repo", "s:e:u", "head", "base", 1, 1),
        cause=cause,
        original_stage="verification",
        evidence=(("test-log", "command failed"),),
        allowed_paths=("src/cache", "tests/cache"),
        preserved_refs=("refs/bmad/recovery/1",),
        expected_verification=(("pytest", "tests/cache"),),
        quiescent=True,
        candidate_clean=True,
    )
    return ResolutionContext(**{**values, **changes})


def artifact(ctx, **changes):
    values = dict(
        schema_version=1,
        coordinates=asdict(ctx.coordinates),
        cause_id=ctx.cause.id,
        cause_code=str(ctx.cause.code),
        action="repair",
        rationale="Fix the accepted cache behavior",
        evidence_ids=[ctx.cause.id, "test-log"],
        affected_paths=["src/cache/db.py"],
        preserved_refs=list(ctx.preserved_refs),
        verification=[list(c) for c in ctx.expected_verification],
        unresolved_decisions=[],
        command=[],
    )
    return {**values, **changes}


def state(ctx):
    spec = spec_from_dict(
        {
            "schema_version": 1,
            "name": "repair",
            "sources": [{"kind": "stories", "path": "specs"}],
            "resolution": {"mode": "bounded-auto", "max_attempts_per_unit": 2, "max_sessions": 2},
        }
    )
    unit = CampaignUnit(
        UnitSpec("s:e:u", "s", "e", "cache"),
        UnitState.RUNNING,
        evidence={"verification": {"head": "head"}, "review": {"head": "head"}},
        bindings={"head": "head"},
    )
    return CampaignSnapshot("repairs", "org/repo", spec, {unit.id: unit})


def result(proposal, **changes):
    values = dict(
        proposal_id=proposal.id,
        coordinates=proposal.coordinates,
        parent_head="head",
        head="repaired-head",
        quiescent=True,
        preserves_history=True,
        source_identity="source-plan",
        verification=tuple((command, 0) for command in proposal.verification),
        recovery_push_confirmed=True,
    )
    return ResolutionResult(**{**values, **changes})


def test_cause_classification_uses_producer_codes_not_message_regexes():
    raw = {
        "code": "verification_failed",
        "producer": "engine.verify",
        "revision": "r1",
        "detail": "quota retry rate limit",
    }
    assert failure_from_receipt(raw).code == CauseCode.VERIFICATION
    assert failure_from_receipt({"detail": "quota retry"}).code == CauseCode.UNKNOWN
    assert failure_from_receipt({"code": "quota", "detail": "rate limit"}).code == CauseCode.UNKNOWN
    assert failure_from_receipt({**raw, "code": "new_unknown_cause"}).code == CauseCode.UNKNOWN


def test_transient_retry_is_durable_time_not_model_or_busy_worker_wait():
    ctx = context(CauseCode.RATE_LIMIT)
    cause = replace(ctx.cause, retry_after_s=120)
    assert transient_retry(cause, attempt=0, now=100) == 220
    assert transient_retry(cause, attempt=6, now=100) == 420
    assert transient_retry(context().cause, attempt=2, now=100) is None


def test_valid_bounded_repair_preserves_identity_and_returns_to_acceptance():
    ctx = context()
    snapshot = state(ctx)
    begin_resolution(snapshot, ctx.coordinates.unit, ctx)
    proposal = validate_proposal(artifact(ctx), ctx, snapshot.spec.resolution)
    assert proposal.automatic
    record_proposal(snapshot, ctx.coordinates.unit, proposal)
    apply_resolution(snapshot, ctx.coordinates.unit, proposal, result(proposal))
    unit = snapshot.units[ctx.coordinates.unit]
    assert unit.state == UnitState.READY
    assert unit.bindings["head"] == "repaired-head"
    assert unit.resolution_attempts == 1
    assert unit.attempt == 1
    assert "verification" not in unit.evidence
    assert "review" not in unit.evidence
    assert unit.evidence["resolution_history"][-1]["state"] == "verified"
    assert unit.state != UnitState.DELIVERED


@pytest.mark.parametrize(
    "cause",
    [
        CauseCode.OWNER,
        CauseCode.HUMAN_ONLY,
        CauseCode.CREDENTIALS,
        CauseCode.PERMISSIONS,
        CauseCode.SPEND,
        CauseCode.POLICY,
        CauseCode.OWNERSHIP,
        CauseCode.APPROVAL,
        CauseCode.UNKNOWN,
    ],
)
def test_resolver_cannot_waive_restricted_causes(cause):
    ctx = context(cause)
    proposal = validate_proposal(artifact(ctx), ctx, ResolutionSpec(mode="bounded-auto"))
    assert not proposal.automatic
    with pytest.raises(CampaignError, match="no automatic"):
        apply_resolution(state(ctx), ctx.coordinates.unit, proposal, result(proposal))


def test_diagnose_mode_never_automatically_repairs():
    ctx = context()
    proposal = validate_proposal(artifact(ctx), ctx, ResolutionSpec(mode="diagnose"))
    assert not proposal.automatic
    with pytest.raises(CampaignError, match="disabled"):
        validate_proposal(artifact(ctx), ctx, ResolutionSpec(mode="off"))


def test_technical_resolution_cannot_reclassify_ownership_as_transient():
    ctx = context(CauseCode.OWNERSHIP)
    with pytest.raises(CampaignError, match="downgrade or reclassify"):
        validate_proposal(
            artifact(ctx, cause_code="verification_failed"),
            ctx,
            ResolutionSpec(mode="bounded-auto"),
        )


@pytest.mark.parametrize(
    "changes,match",
    [
        ({"coordinates": {}}, "coordinates"),
        ({"cause_id": "another"}, "cause identity"),
        ({"evidence_ids": ["invented"]}, "known failure evidence"),
        ({"affected_paths": ["../../other"]}, "escapes accepted"),
        ({"affected_paths": ["unowned/file"]}, "escapes accepted"),
        ({"preserved_refs": []}, "preserved work"),
        ({"verification": []}, "weaken expected"),
        ({"verification": ["pytest tests"]}, "argv arrays"),
        ({"command": ["systemctl", "restart", "shared"]}, "not explicitly authorized"),
    ],
)
def test_proposal_must_preserve_contract_paths_and_commands(changes, match):
    ctx = context()
    with pytest.raises(CampaignError, match=match):
        validate_proposal(artifact(ctx, **changes), ctx, ResolutionSpec(mode="bounded-auto"))


def test_diagnosis_requires_quiescent_worker_and_unchanged_candidate():
    ctx = context(quiescent=False)
    with pytest.raises(CampaignError, match="quiescent"):
        validate_proposal(artifact(ctx), ctx, ResolutionSpec(mode="bounded-auto"))
    before = {"head": "h", "index": "i", "worktree": "w", "untracked": "u", "source": "s"}
    diagnosis_unchanged(before, dict(before))
    for key in before:
        with pytest.raises(CampaignError, match="changed candidate"):
            diagnosis_unchanged(before, {**before, key: "changed"})
    with pytest.raises(CampaignError, match="incomplete"):
        diagnosis_unchanged({"head": "h"}, {"head": "h"})


def test_same_failure_stops_earlier_and_lifetime_budgets_survive_rearm():
    ctx = context()
    snapshot = state(ctx)
    begin_resolution(snapshot, ctx.coordinates.unit, ctx)
    with pytest.raises(CampaignError, match="same failure repeated"):
        begin_resolution(snapshot, ctx.coordinates.unit, ctx)
    prior = replace(ctx, prior_fingerprints=(resolution_fingerprint(ctx),))
    assert not validate_proposal(artifact(prior), prior, snapshot.spec.resolution).automatic
    unit = snapshot.units[ctx.coordinates.unit]
    unit.resolution_attempts = 2
    with pytest.raises(CampaignError, match="lifetime"):
        begin_resolution(
            snapshot, ctx.coordinates.unit, replace(ctx, cause=replace(ctx.cause, revision="new"))
        )
    exhausted = replace(ctx, attempts=2)
    assert not validate_proposal(artifact(exhausted), exhausted, snapshot.spec.resolution).automatic


def test_unresolved_product_decision_is_not_permission_to_weaken_acceptance():
    ctx = context()
    proposal = validate_proposal(
        artifact(ctx, unresolved_decisions=["Can we remove the cache requirement?"]),
        ctx,
        ResolutionSpec(mode="bounded-auto"),
    )
    assert not proposal.automatic
    snapshot = state(ctx)
    begin_resolution(snapshot, ctx.coordinates.unit, ctx)
    record_proposal(snapshot, ctx.coordinates.unit, proposal)
    assert snapshot.units[ctx.coordinates.unit].hold.kind == "resolution"
    assert snapshot.units[ctx.coordinates.unit].state == UnitState.RUNNING


def test_environment_repair_requires_exact_authorized_command_and_reprobe():
    ctx = context(CauseCode.ENVIRONMENT, repair_commands=(("project-tool", "repair-local"),))
    raw = artifact(ctx, command=["project-tool", "repair-local"])
    proposal = validate_proposal(raw, ctx, ResolutionSpec(mode="bounded-auto"))
    assert proposal.automatic
    snapshot = state(ctx)
    begin_resolution(snapshot, ctx.coordinates.unit, ctx)
    record_proposal(snapshot, ctx.coordinates.unit, proposal)
    with pytest.raises(CampaignError, match="reprobe"):
        apply_resolution(snapshot, ctx.coordinates.unit, proposal, result(proposal))
    apply_resolution(
        snapshot, ctx.coordinates.unit, proposal, result(proposal, reprobe_passed=True)
    )
    assert snapshot.units[ctx.coordinates.unit].state == UnitState.READY


@pytest.mark.parametrize(
    "changes,match",
    [
        ({"parent_head": "lost-parent"}, "preserve candidate/source"),
        ({"preserves_history": False}, "preserve candidate/source"),
        ({"source_identity": ""}, "preserve candidate/source"),
        ({"recovery_push_confirmed": False}, "push is not confirmed"),
        ({"quiescent": False}, "writer has not exited"),
        ({"verification": ()}, "verification is missing or failed"),
    ],
)
def test_repair_result_needs_independent_evidence_before_any_mutation(changes, match):
    ctx = context()
    snapshot = state(ctx)
    begin_resolution(snapshot, ctx.coordinates.unit, ctx)
    proposal = validate_proposal(artifact(ctx), ctx, snapshot.spec.resolution)
    record_proposal(snapshot, ctx.coordinates.unit, proposal)
    before = snapshot.clone()
    with pytest.raises(CampaignError, match=match):
        apply_resolution(snapshot, ctx.coordinates.unit, proposal, result(proposal, **changes))
    assert snapshot == before


def test_resolution_never_clears_unrelated_owner_or_github_hold():
    ctx = context()
    for kind in ["operator", "owner_stop", "github_review", "human_only"]:
        snapshot = state(ctx)
        begin_resolution(snapshot, ctx.coordinates.unit, ctx)
        proposal = validate_proposal(artifact(ctx), ctx, snapshot.spec.resolution)
        record_proposal(snapshot, ctx.coordinates.unit, proposal)
        snapshot.units[ctx.coordinates.unit].hold = Hold(kind, "Hold still applies", id="real-hold")
        with pytest.raises(CampaignError, match="unrelated hold"):
            apply_resolution(snapshot, ctx.coordinates.unit, proposal, result(proposal))
        assert snapshot.units[ctx.coordinates.unit].hold.id == "real-hold"


def test_missing_persisted_proposal_does_not_mutate_candidate():
    ctx = context()
    snapshot = state(ctx)
    proposal = validate_proposal(artifact(ctx), ctx, snapshot.spec.resolution)
    before = snapshot.clone()
    with pytest.raises(CampaignError, match="persisted accepted proposal"):
        apply_resolution(snapshot, ctx.coordinates.unit, proposal, result(proposal))
    assert snapshot == before


def test_live_or_uncertain_claim_cannot_be_replaced_by_resolver():
    ctx = context()
    snapshot = state(ctx)
    unit = snapshot.units[ctx.coordinates.unit]
    unit.claim = WorkerClaim(unit.id, 1, 1, "uncertain-launch")
    with pytest.raises(CampaignError, match="quiescent"):
        begin_resolution(snapshot, unit.id, ctx)
    assert unit.resolution_attempts == 0
    assert "resolution_history" not in unit.evidence


def test_transient_technical_repair_defers_to_deterministic_reconciliation():
    ctx = context(CauseCode.QUOTA)
    proposal = validate_proposal(artifact(ctx), ctx, ResolutionSpec(mode="bounded-auto"))
    assert not proposal.automatic
    assert "read-only" in resolution_packet(ctx)["instructions"]


@pytest.mark.parametrize("phase", [UnitState.REVIEWING, UnitState.VERIFYING, UnitState.SYNCING])
def test_post_local_repair_returns_to_sync_without_reexecuting_done_source(phase):
    ctx = context()
    snapshot = state(ctx)
    unit = snapshot.units[ctx.coordinates.unit]
    unit.state = phase
    unit.resume_state = UnitState.SYNCING
    unit.evidence["integrated"] = {"head": "stale-head", "base": "stale-base"}
    unit.evidence["pushed"] = "stale-head"
    begin_resolution(snapshot, unit.id, ctx)
    proposal = validate_proposal(artifact(ctx), ctx, snapshot.spec.resolution)
    record_proposal(snapshot, unit.id, proposal)
    apply_resolution(snapshot, unit.id, proposal, result(proposal))
    assert unit.state == UnitState.SYNCING
    assert unit.resume_state is None
    for key in ("integrated", "pushed", "review", "verification"):
        assert key not in unit.evidence
    assert unit.resolution_attempts == 1


@pytest.mark.parametrize(
    "target", [UnitState.DELIVERED, UnitState.MERGE_REQUESTED, UnitState.READY_TO_PUBLISH]
)
def test_repair_resume_cannot_jump_past_acceptance(target):
    ctx = context()
    snapshot = state(ctx)
    unit = snapshot.units[ctx.coordinates.unit]
    unit.resume_state = target
    begin_resolution(snapshot, unit.id, ctx)
    proposal = validate_proposal(artifact(ctx), ctx, snapshot.spec.resolution)
    record_proposal(snapshot, unit.id, proposal)
    before = snapshot.clone()
    with pytest.raises(CampaignError, match="cannot bypass"):
        apply_resolution(snapshot, unit.id, proposal, result(proposal))
    assert snapshot == before
