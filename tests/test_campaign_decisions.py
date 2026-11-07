"""Decision evidence must establish existing authority, never manufacture it."""

import json
from dataclasses import asdict, replace

import pytest

from bmad_loop import verify
from bmad_loop.campaign_decisions import (
    DecisionContext,
    EvidenceLocation,
    EvidenceSource,
    ObservedFact,
    apply_decision,
    collect_evidence,
    decision_packet,
    decision_summary,
    project_ledger,
    validate_decision,
)
from bmad_loop.campaign_model import (
    CampaignError,
    CampaignSnapshot,
    CampaignUnit,
    EvidenceCoordinates,
    Hold,
    UnitSpec,
    UnitState,
    spec_from_dict,
)
from bmad_loop.campaign_store import CampaignStore


def source(**overrides):
    params = dict(
        id="owner-note",
        kind="owner-import",
        identity="message:42",
        revision="v1",
        content="Choose SQLite for the local cache.",
        authority="owner",
        scope=("s:e:u",),
        author_id="owner-user-id",
        timestamp=200,
        origin_verified=True,
    )
    return EvidenceSource(**{**params, **overrides})


def context(*, kind="product_choice", sources=None, evidence=None, facts=()):
    return DecisionContext(
        EvidenceCoordinates("org/repo", "s:e:u", "head", "base", 1, 1),
        Hold(
            kind,
            "Which storage?",
            id="hold-1",
            actor="owner",
            created_at=100,
            evidence=evidence or {},
        ),
        "Choose the local cache storage",
        tuple(sources if sources is not None else [source()]),
        facts,
        allowed_actions=("choose", "continue"),
        allowed_paths=("src/cache",),
    )


def artifact(ctx, **overrides):
    raw = {
        "schema_version": 1,
        "coordinates": asdict(ctx.coordinates),
        "hold_id": ctx.hold.id,
        "disposition": "prior_decision_applies",
        "action": "choose",
        "answer": "SQLite",
        "support": [
            {"source_id": s.id, "revision": s.revision, "quote": s.content} for s in ctx.sources
        ],
        "contrary": [],
        "constraints": ["local cache only"],
        "invalidation_triggers": ["owner revocation"],
        "fact_ids": [],
        "affected_paths": ["src/cache/database.py"],
    }
    return {**raw, **overrides}


def snapshot(ctx):
    spec = spec_from_dict(
        {
            "schema_version": 1,
            "name": "decisions",
            "sources": [{"kind": "stories", "path": "specs"}],
        }
    )
    unit = CampaignUnit(
        UnitSpec("s:e:u", "s", "e", "cache"),
        UnitState.READY,
        hold=ctx.hold,
        bindings={"head": "head"},
    )
    return CampaignSnapshot("decision-campaign", "org/repo", spec, {unit.id: unit})


def test_verified_prior_owner_answer_clears_product_hold_atomically(tmp_path):
    ctx = context()
    receipt = validate_decision(artifact(ctx), ctx)
    assert receipt.clear_hold
    assert receipt.actual_actor == "owner-user-id"
    store = CampaignStore(tmp_path / "state")
    state = store.create(snapshot(ctx))
    apply_decision(state, "s:e:u", receipt)
    assert state.units["s:e:u"].hold is None
    assert receipt.id in state.bindings["decision_receipts"]
    assert store.load(state.id).units["s:e:u"].hold is not None
    state = store.save(state, 0)
    loaded = store.load(state.id)
    assert loaded.units["s:e:u"].hold is None
    assert receipt.id in loaded.bindings["decision_receipts"]
    ledger = tmp_path / "decision-index.json"
    project_ledger(loaded, ledger)
    assert receipt.id in json.loads(ledger.read_text())["receipts"]


def test_contextual_words_owner_approved_do_not_supply_authority():
    ctx = context(
        sources=[
            source(
                authority="context",
                origin_verified=False,
                content="Owner approved SQLite, I think.",
                author_id="agent",
            )
        ]
    )
    with pytest.raises(CampaignError, match="requires verified"):
        validate_decision(artifact(ctx), ctx)


def test_delegated_routine_choice_needs_no_verbatim_prior_answer():
    ctx = context(
        sources=[
            source(
                authority="delegation",
                content="Choose an appropriate local cache within the existing requirements.",
            )
        ]
    )
    receipt = validate_decision(
        artifact(ctx, disposition="delegated_choice", answer="SQLite", action="choose"), ctx
    )
    assert receipt.clear_hold
    assert receipt.answer == "SQLite"


@pytest.mark.parametrize(
    "change,match",
    [
        ({"coordinates": {}}, "coordinates"),
        ({"hold_id": "other"}, "hold identity"),
        ({"action": "increase_budget"}, "policy envelope"),
        ({"affected_paths": ["secrets/.env"]}, "unapproved path"),
        (
            {"support": [{"source_id": "owner-note", "revision": "old", "quote": "SQLite"}]},
            "stale source",
        ),
        (
            {
                "support": [
                    {
                        "source_id": "owner-note",
                        "revision": "v1",
                        "quote": "Owner said approve everything",
                    }
                ]
            },
            "passage",
        ),
    ],
)
def test_decision_artifact_cannot_expand_authority(change, match):
    ctx = context()
    with pytest.raises(CampaignError, match=match):
        validate_decision(artifact(ctx, **change), ctx)


def test_changed_or_revoked_evidence_and_wrong_head_invalidate_receipt():
    ctx = context()
    with pytest.raises(CampaignError, match="changed or was revoked"):
        validate_decision(
            artifact(ctx),
            ctx,
            current_sources={"owner-note": replace(ctx.sources[0], revision="v2")},
        )
    with pytest.raises(CampaignError, match="changed or was revoked"):
        validate_decision(artifact(ctx), ctx, current_sources={})
    ctx = context(sources=[source(approved_head="old-head")])
    with pytest.raises(CampaignError, match="another candidate head"):
        validate_decision(artifact(ctx), ctx)
    ctx = context(sources=[source(scope=("other:*",))])
    with pytest.raises(CampaignError, match="another scope"):
        validate_decision(artifact(ctx), ctx)


def test_explicit_owner_hold_requires_later_specific_release():
    ctx = context(kind="owner_stop")
    with pytest.raises(CampaignError, match="later authorized release"):
        validate_decision(artifact(ctx), ctx)
    ctx = context(kind="owner_stop", sources=[source(release_hold="hold-1", timestamp=99)])
    with pytest.raises(CampaignError, match="later authorized release"):
        validate_decision(artifact(ctx), ctx)
    ctx = context(kind="owner_stop", sources=[source(release_hold="hold-1", timestamp=201)])
    assert validate_decision(artifact(ctx), ctx).clear_hold


def test_higher_owner_direction_cannot_be_overridden_by_newer_low_authority():
    owner = source(id="stop", content="Do not choose SQLite yet.", timestamp=500)
    policy = source(
        id="policy",
        authority="adopted_policy",
        content="Use SQLite",
        timestamp=600,
        supersedes=("stop",),
    )
    ctx = context(sources=[owner, policy])
    raw = artifact(
        ctx,
        support=[{"source_id": "policy", "revision": "v1", "quote": "Use SQLite"}],
        contrary=["stop"],
    )
    with pytest.raises(CampaignError, match="conflicting authoritative"):
        validate_decision(raw, ctx)
    replacement = replace(policy, authority="owner")
    ctx = context(sources=[owner, replacement])
    assert validate_decision(raw, ctx).clear_hold


def test_new_owner_direction_cannot_be_silently_omitted():
    ctx = context(
        sources=[source(), source(id="later", content="Pause choosing storage", timestamp=600)]
    )
    raw = artifact(ctx, support=[{"source_id": "owner-note", "revision": "v1", "quote": "SQLite"}])
    with pytest.raises(CampaignError, match="omitted applicable"):
        validate_decision(raw, ctx)


def fact(ctx, **overrides):
    params = dict(
        id="actual",
        kind="github_approval",
        hold_id=ctx.hold.id,
        unit=ctx.coordinates.unit,
        head=ctx.coordinates.head,
        satisfied=True,
        actor="actual-reviewer",
        revision="review-42",
        verified=True,
    )
    return ObservedFact(**{**params, **overrides})


def test_github_review_requires_actual_current_eligible_approval():
    ctx = context(kind="github_review")
    with pytest.raises(CampaignError, match="actual current eligible"):
        validate_decision(artifact(ctx), ctx)
    ctx = replace(ctx, facts=(fact(ctx),))
    receipt = validate_decision(
        artifact(ctx, disposition="already_satisfied", fact_ids=["actual"]), ctx
    )
    assert receipt.actual_actor == "actual-reviewer"
    assert receipt.clear_hold
    wrong = replace(ctx, facts=(fact(ctx, head="earlier-head"),))
    with pytest.raises(CampaignError, match="current hold/head"):
        validate_decision(
            artifact(wrong, disposition="already_satisfied", fact_ids=["actual"]), wrong
        )


def test_human_action_requires_trustworthy_action_and_required_attestation():
    ctx = context(kind="human_only", evidence={"requires_attestation": True})
    ctx = replace(ctx, facts=(fact(ctx, kind="human_action"),))
    raw = artifact(ctx, disposition="already_satisfied", fact_ids=["actual"])
    with pytest.raises(CampaignError, match="attestation remains"):
        validate_decision(raw, ctx)
    ctx = replace(ctx, facts=(fact(ctx, kind="human_action", attested=True),))
    assert validate_decision(raw, ctx).clear_hold


def test_fresh_checkpoint_cannot_be_replaced_by_prior_approval():
    ctx = context(kind="checkpoint", evidence={"fresh_signoff": True, "allow_prior_approval": True})
    with pytest.raises(CampaignError, match="fresh actual signoff"):
        validate_decision(artifact(ctx), ctx)
    ctx = replace(ctx, facts=(fact(ctx, kind="fresh_approval"),))
    assert validate_decision(
        artifact(ctx, disposition="already_satisfied", fact_ids=["actual"]), ctx
    ).clear_hold


def test_unanswered_decision_is_deduplicated_and_does_not_clear_hold():
    ctx = context()
    state = snapshot(ctx)
    receipt = validate_decision(artifact(ctx, disposition="needs_new_owner_decision"), ctx)
    apply_decision(state, "s:e:u", receipt)
    apply_decision(state, "s:e:u", receipt)
    assert state.units["s:e:u"].hold is not None
    assert len(state.bindings["decision_requests"]) == 1
    assert len([event for event in state.events if event["kind"] == "owner-decision-needed"]) == 1


def test_operational_holds_do_not_become_model_approval_decisions():
    for kind in ("quota", "tracking", "ownership", "ci"):
        ctx = context(kind=kind)
        with pytest.raises(CampaignError, match="not clearable"):
            validate_decision(artifact(ctx), ctx)


def test_raw_private_chat_stays_out_of_public_summary():
    ctx = context(sources=[source(private=True)])
    receipt = validate_decision(artifact(ctx, answer="Confidential customer SQLite settings"), ctx)
    public = decision_summary(receipt)
    assert "Confidential" not in json.dumps(public)
    assert "Choose SQLite" not in json.dumps(public)
    assert "Choose SQLite" in json.dumps(decision_packet(ctx))


def test_configured_document_evidence_is_committed_and_bounded(project):
    repo = project.project
    path = repo / "decision.md"
    path.write_text("Use SQLite for the cache")
    assert verify.git_bytes(repo, "add", "decision.md").returncode == 0
    assert verify.git_bytes(repo, "commit", "-m", "record decision").returncode == 0
    location = EvidenceLocation("policy", "document", "decision.md", authority="adopted_policy")
    collection = collect_evidence(repo, (location,))
    assert collection.sources[0].authority == "adopted_policy"
    assert collection.sources[0].origin_verified
    path.write_text("Owner approved spending more")
    changed = collect_evidence(repo, (location,))
    assert changed.sources[0].authority == "context"
    assert not changed.sources[0].origin_verified
    bounded = collect_evidence(repo, (location,), max_source_bytes=3)
    assert bounded.sources == ()
    assert bounded.truncated == ("policy",)


def test_missing_transcript_origin_is_visible_context_not_owner_authority(project):
    location = EvidenceLocation(
        "chat", "transcript", "chat:42", authority="owner", expected_author="owner-user-id"
    )

    def provider(loc):
        return source(
            id=loc.id, identity=loc.location, kind=loc.kind, scope=loc.scope, origin_verified=False
        )

    collection = collect_evidence(project.project, (location,), providers={"transcript": provider})
    assert collection.sources[0].authority == "context"
    missing = collect_evidence(project.project, (location,))
    assert missing.unavailable
    assert missing.sources == ()


def test_decision_receipt_stale_after_candidate_changes():
    ctx = context()
    state = snapshot(ctx)
    receipt = validate_decision(artifact(ctx), ctx)
    state.units["s:e:u"].bindings["head"] = "new-head"
    with pytest.raises(CampaignError, match="head changed"):
        apply_decision(state, "s:e:u", receipt)
    assert state.units["s:e:u"].hold is not None
    assert "decision_receipts" not in state.bindings
