"""External landing transitions retain exact product acceptance debt on disk."""

import copy
from dataclasses import asdict, replace

import pytest

from bmad_loop.campaign_external_merge import ExternalLandingEvidence
from bmad_loop.campaign_model import (
    CampaignError,
    CampaignSnapshot,
    CampaignSpec,
    CampaignUnit,
    Hold,
    OutboxAction,
    UnitSpec,
    UnitState,
    WorkerClaim,
)
from bmad_loop.campaign_store import CampaignStore, reconcile_external_landing

HEAD = "a" * 40
BASE = "b" * 40
MERGE = "c" * 40
CANONICAL = "d" * 40
TREE = "e" * 40
OTHER = "f" * 40
UNIT = "source:project:owned"


@pytest.fixture(params=["json", "sqlite"])
def external_landing_case(tmp_path, request):
    spec = UnitSpec(
        UNIT,
        "source",
        "epic",
        "Owned story",
        acceptance=("Preserve the accepted product", "Retain published history"),
        scopes=("src/owned",),
    )
    coordinates = {
        "repository": "example/repo",
        "unit": UNIT,
        "head": HEAD,
        "base": BASE,
        "source_revision": "source-revision",
        "graph_revision": 1,
        "config_revision": 1,
    }
    unit = CampaignUnit(
        spec,
        UnitState.READY_TO_PUBLISH,
        attempt=8,
        failure_attempts=2,
        resolution_attempts=3,
        review_attempts=4,
        claim=WorkerClaim(UNIT, 8, 1, "review-launch", role="publication_review", quiescent=True),
        bindings={
            "head": HEAD,
            "base": BASE,
            "branch": "campaign-owned",
            "workspace": str(tmp_path / "owned-workspace"),
            "pr": {"number": 41, "html_url": "https://example.invalid/pull/41"},
            "source_revision": "source-revision",
        },
        evidence={
            "integrated": {"head": HEAD, "base": BASE},
            "verification": {
                "launch_id": "verify-launch",
                "outcome": "verified",
                "head": HEAD,
            },
            "review": {
                "launch_id": "review-launch",
                "head": HEAD,
                "provider_family": "claude",
                "validated": {"verdict": "approved", "coordinates": coordinates},
            },
            "developer_family": "codex",
            "developer_families": ["codex", "gemini"],
            "dev_attempts_spent": 5,
            "review_cycles_spent": 6,
            "unresolved_approval_requirements": ["retain-independent-review"],
        },
    )
    snapshot = CampaignSnapshot(
        "external-landing",
        "example/repo",
        CampaignSpec("External landing", units=(spec,)),
        {UNIT: unit},
        generation=1,
        outbox=[
            OutboxAction(
                "other-unknown-effect",
                "github",
                "",
                {"purpose": "unrelated"},
                1,
                state="sent_unknown",
            )
        ],
    )
    store = CampaignStore(tmp_path / "authority", storage=request.param)
    snapshot = store.create(snapshot)
    proof = ExternalLandingEvidence(
        repository=snapshot.repository,
        unit_id=UNIT,
        head=HEAD,
        base=BASE,
        target="main",
        merge_commit=MERGE,
        canonical_head=CANONICAL,
        parents=(BASE, HEAD),
        checkpoint_heads=(HEAD,),
        verification_launch="verify-launch",
        review_launch="review-launch",
        source_revision="source-revision",
        graph_revision=1,
        config_revision=1,
        covered_tree=False,
        quiescent=True,
        schema_version=2,
        merge_tree=TREE,
        pr_number=41,
    )
    return store, snapshot, proof


def land(case):
    store, snapshot, proof = case
    reconcile_external_landing(snapshot, UNIT, proof)
    return store.save(snapshot, snapshot.revision)


def test_external_landing_atomically_preserves_work_and_adds_pending_debt(external_landing_case):
    store, snapshot, proof = external_landing_case
    before = copy.deepcopy(asdict(snapshot.units[UNIT]))
    actions = copy.deepcopy(snapshot.outbox)
    reconcile_external_landing(snapshot, UNIT, proof)
    unit = snapshot.units[UNIT]
    assert unit.state == UnitState.MERGED_PENDING_SYNC
    assert unit.evidence["external_landing"] == asdict(proof)
    debt = unit.evidence["landing_acceptance"]
    assert debt["state"] == "pending"
    assert debt["merge_commit"] == MERGE
    assert debt["merge_tree"] == TREE
    assert tuple(debt["merge_parents"]) == proof.parents
    assert debt["canonical_head"] == CANONICAL
    assert debt["candidate"] == {
        "repository": snapshot.repository,
        "unit_id": UNIT,
        "pr_number": 41,
        "head": HEAD,
        "base": BASE,
        "source_revision": "source-revision",
        "graph_revision": 1,
        "config_revision": "1",
        "target": "main",
    }
    after = asdict(unit)
    after["state"] = before["state"]
    after["evidence"] = {key: after["evidence"][key] for key in before["evidence"]}
    assert after == before
    assert snapshot.outbox == actions
    assert not any(event.get("target") == "merge_requested" for event in snapshot.events)
    saved = store.save(snapshot, snapshot.revision)
    restored = store.load(saved.id)
    assert restored.units[UNIT].evidence["landing_acceptance"] == debt
    assert restored.units[UNIT].bindings == before["bindings"]
    assert restored.outbox == actions


@pytest.mark.parametrize(
    "damage",
    ["missing", "accepted", "tree", "head", "source", "config", "graph", "pr"],
)
def test_initial_external_landing_save_requires_exact_pending_debt(external_landing_case, damage):
    store, snapshot, proof = external_landing_case
    reconcile_external_landing(snapshot, UNIT, proof)
    debt = snapshot.units[UNIT].evidence["landing_acceptance"]
    assert debt["state"] == "pending" and debt["merge_tree"] == TREE
    if damage == "missing":
        snapshot.units[UNIT].evidence.pop("landing_acceptance")
    elif damage == "accepted":
        debt["state"] = "accepted"
    elif damage == "tree":
        debt["merge_tree"] = OTHER
    else:
        key = {
            "source": "source_revision",
            "config": "config_revision",
            "graph": "graph_revision",
            "pr": "pr_number",
        }.get(damage, damage)
        debt["candidate"][key] = {
            "head": OTHER,
            "source": "different-source",
            "config": "2",
            "graph": 2,
            "pr": 42,
        }[damage]
    with pytest.raises(CampaignError):
        store.save(snapshot, snapshot.revision)
    restored = store.load(snapshot.id)
    assert restored.units[UNIT].state == UnitState.READY_TO_PUBLISH
    assert "external_landing" not in restored.units[UNIT].evidence
    assert "landing_acceptance" not in restored.units[UNIT].evidence


@pytest.mark.parametrize(
    "damage",
    [
        "proof_missing",
        "debt_missing",
        "proof_commit",
        "debt_tree",
        "debt_candidate",
        "both_rebound",
    ],
)
def test_later_save_cannot_remove_or_rebind_external_landing(external_landing_case, damage):
    store, _, _ = external_landing_case
    original = land(external_landing_case)
    snapshot = original.clone()
    unit = snapshot.units[UNIT]
    assert unit.state == UnitState.MERGED_PENDING_SYNC
    assert unit.evidence["landing_acceptance"]["state"] == "pending"
    if damage == "proof_missing":
        unit.evidence.pop("external_landing")
    elif damage == "debt_missing":
        unit.evidence.pop("landing_acceptance")
    elif damage == "proof_commit":
        unit.evidence["external_landing"]["merge_commit"] = OTHER
    elif damage == "debt_tree":
        unit.evidence["landing_acceptance"]["merge_tree"] = OTHER
    elif damage == "debt_candidate":
        unit.evidence["landing_acceptance"]["candidate"]["pr_number"] = 42
    else:
        unit.evidence["external_landing"]["merge_commit"] = OTHER
        unit.evidence["landing_acceptance"]["merge_commit"] = OTHER
    with pytest.raises(CampaignError):
        store.save(snapshot, snapshot.revision)
    restored = store.load(original.id)
    assert restored.units[UNIT] == original.units[UNIT]


def test_landing_debt_survives_later_graph_revision_and_operator_hold(external_landing_case):
    store, _, _ = external_landing_case
    snapshot = land(external_landing_case)
    unit = snapshot.units[UNIT]
    frozen_candidate = copy.deepcopy(unit.evidence["landing_acceptance"]["candidate"])
    snapshot.graph_revision += 1
    unit.hold = Hold("operator", "Inspect the actual merged product", id="owner-hold")
    unit.evidence["landing_acceptance"].update(state="verifying", workspace="/synthetic/landing")
    saved = store.save(snapshot, snapshot.revision)
    restored = store.load(saved.id)
    assert restored.units[UNIT].hold == unit.hold
    assert restored.units[UNIT].evidence["landing_acceptance"]["candidate"] == frozen_candidate
    assert restored.units[UNIT].evidence["landing_acceptance"]["state"] == "verifying"


def test_pending_external_landing_cannot_be_accepted_by_state_alone(external_landing_case):
    store, _, _ = external_landing_case
    original = land(external_landing_case)
    snapshot = original.clone()
    debt = snapshot.units[UNIT].evidence["landing_acceptance"]
    assert debt["state"] == "pending"
    assert debt["merge_commit"] == MERGE
    assert "verification" not in debt and "review" not in debt
    debt["state"] = "accepted"
    with pytest.raises(CampaignError):
        store.save(snapshot, snapshot.revision)
    restored = store.load(snapshot.id)
    assert restored.units[UNIT] == original.units[UNIT]


@pytest.mark.parametrize("revision", ["graph_revision", "config_revision"])
def test_initial_external_landing_save_cannot_change_revision_at_once(
    external_landing_case, revision
):
    store, snapshot, proof = external_landing_case
    reconcile_external_landing(snapshot, UNIT, proof)
    assert snapshot.units[UNIT].evidence["landing_acceptance"]["state"] == "pending"
    assert getattr(snapshot, revision) == getattr(proof, revision)
    setattr(snapshot, revision, getattr(snapshot, revision) + 1)
    with pytest.raises(CampaignError):
        store.save(snapshot, snapshot.revision)
    restored = store.load(snapshot.id)
    assert restored.units[UNIT].state == UnitState.READY_TO_PUBLISH
    assert getattr(restored, revision) == getattr(proof, revision)


@pytest.mark.parametrize("damage", ["head", "source", "config", "graph", "pr", "live", "hold"])
def test_external_landing_rejects_changed_authority_without_erasing_it(
    external_landing_case, damage
):
    _, snapshot, proof = external_landing_case
    unit = snapshot.units[UNIT]
    if damage == "head":
        unit.evidence["integrated"]["head"] = OTHER
    elif damage == "source":
        unit.bindings["source_revision"] = "changed-source"
    elif damage == "config":
        snapshot.config_revision += 1
    elif damage == "graph":
        snapshot.graph_revision += 1
    elif damage == "pr":
        unit.bindings["pr"]["number"] = 42
    elif damage == "live":
        unit.claim.quiescent = False
    else:
        unit.hold = Hold("required_review", "Human objection remains", id="review-hold")
    before = copy.deepcopy(asdict(unit))
    with pytest.raises(CampaignError):
        reconcile_external_landing(snapshot, UNIT, proof)
    assert asdict(unit) == before


@pytest.mark.parametrize("prior", ["protected_hold", "live_claim"])
def test_external_landing_save_cannot_replace_prior_ownership_authority(
    external_landing_case, prior
):
    store, snapshot, proof = external_landing_case
    if prior == "protected_hold":
        snapshot.units[UNIT].hold = Hold("owner", "Explicit owner hold", id="owner-hold")
    else:
        snapshot.units[UNIT].claim.quiescent = False
    snapshot = store.save(snapshot, snapshot.revision)
    original = snapshot.clone()
    snapshot.units[UNIT].hold = None
    snapshot.units[UNIT].claim.quiescent = True
    reconcile_external_landing(snapshot, UNIT, proof)
    with pytest.raises(CampaignError):
        store.save(snapshot, snapshot.revision)
    assert store.load(snapshot.id).units[UNIT] == original.units[UNIT]


@pytest.mark.parametrize("damage", ["family", "primary", "incomplete_false", "incomplete_missing"])
def test_later_landing_save_retains_writer_union_and_unknown_attribution(
    external_landing_case, damage
):
    store, _, _ = external_landing_case
    snapshot = land(external_landing_case)
    unit = snapshot.units[UNIT]
    if damage == "primary":
        # The primary may be represented separately from accumulated families.
        # Moving that representation is valid while the complete union survives.
        unit.evidence["developer_families"] = ["gemini"]
    elif damage.startswith("incomplete_"):
        unit.evidence["developer_attribution_incomplete"] = True
    snapshot = store.save(snapshot, snapshot.revision)
    original = snapshot.clone()
    evidence = snapshot.units[UNIT].evidence
    assert snapshot.units[UNIT].state == UnitState.MERGED_PENDING_SYNC
    assert evidence["landing_acceptance"]["state"] == "pending"
    if damage == "family":
        assert "gemini" in evidence["developer_families"]
        evidence["developer_families"].remove("gemini")
    elif damage == "primary":
        assert evidence["developer_family"] == "codex"
        assert "codex" not in evidence["developer_families"]
        evidence["developer_family"] = "gemini"
    elif damage == "incomplete_false":
        assert evidence["developer_attribution_incomplete"] is True
        evidence["developer_attribution_incomplete"] = False
    else:
        assert evidence.pop("developer_attribution_incomplete") is True
    with pytest.raises(CampaignError, match="retain every contributing family"):
        store.save(snapshot, snapshot.revision)
    assert store.load(snapshot.id).units[UNIT] == original.units[UNIT]


def test_later_landing_save_can_accumulate_writers_without_losing_previous_families(
    external_landing_case,
):
    store, _, _ = external_landing_case
    snapshot = land(external_landing_case)
    unit = snapshot.units[UNIT]
    original_families = set(unit.evidence["developer_families"])
    assert unit.evidence["developer_family"] in original_families
    unit.evidence.pop("developer_family")
    unit.evidence["developer_families"].append("claude")
    unit.evidence["developer_attribution_incomplete"] = True
    saved = store.save(snapshot, snapshot.revision)
    restored = store.load(saved.id).units[UNIT]
    assert set(restored.evidence["developer_families"]) == original_families | {"claude"}
    assert restored.evidence["developer_attribution_incomplete"] is True
    assert restored.evidence["landing_acceptance"]["state"] == "pending"


@pytest.mark.parametrize("damage", ["landing_parent", "landing_original_merge"])
def test_accepted_landing_repair_remains_bound_to_its_original_product(
    external_landing_case, damage
):
    from bmad_loop.campaign_acceptance import ensure

    store, _, _ = external_landing_case
    snapshot = land(external_landing_case)
    original = snapshot.units[UNIT]
    repair_id = "source:project:landing-repair"
    repair = CampaignUnit(
        replace(original.spec, id=repair_id, kind="landing_repair", parent=UNIT),
        UnitState.DELIVERED,
        bindings={"landing_parent": UNIT},
        evidence={
            "landing_original_merge": MERGE,
            "merge": {
                "state": "merged_pending_sync",
                "merge_commit": OTHER,
                "canonical_head": OTHER,
            },
        },
    )
    repair_ledger = ensure(repair)
    repair_acceptance = {
        "complete": True,
        "head": OTHER,
        "criteria": [{**row, "status": "passed"} for row in repair_ledger["criteria"]],
    }
    repair_ledger["current"] = repair_acceptance
    snapshot.units[repair_id] = repair
    snapshot.graph_revision += 1
    original.evidence["landing_acceptance"].update(
        state="accepted",
        repair_unit=repair_id,
        repaired={
            "repair_unit": repair_id,
            "repair_merge": copy.deepcopy(repair.evidence["merge"]),
            "acceptance": copy.deepcopy(repair_acceptance),
        },
    )
    # Establish that the complete linked-repair store contract actually passes
    # before changing only the parent or original merged-product binding.
    snapshot = store.save(snapshot, snapshot.revision)
    accepted = store.load(snapshot.id)
    assert accepted.units[UNIT].evidence["landing_acceptance"]["state"] == "accepted"
    assert accepted.units[repair_id].state == UnitState.DELIVERED
    assert accepted.units[repair_id].spec.parent == UNIT
    assert accepted.units[repair_id].bindings["landing_parent"] == UNIT
    assert accepted.units[repair_id].evidence["landing_original_merge"] == MERGE
    if damage == "landing_parent":
        snapshot.units[repair_id].bindings["landing_parent"] = "another-owned-parent"
    else:
        snapshot.units[repair_id].evidence["landing_original_merge"] = OTHER
    with pytest.raises(CampaignError, match="completed linked repair proof"):
        store.save(snapshot, snapshot.revision)
    restored = store.load(snapshot.id)
    assert restored.units[UNIT] == accepted.units[UNIT]
    assert restored.units[repair_id] == accepted.units[repair_id]
