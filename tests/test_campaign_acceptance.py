"""Proof comes from actual verifier records, not an approved review verdict."""

from dataclasses import asdict, replace
from types import SimpleNamespace

import pytest

from bmad_loop.campaign_acceptance import (
    authorize_amendment,
    context,
    digest,
    ensure,
    evaluate,
    provenance,
    record,
)
from bmad_loop.campaign_model import CampaignError, UnitSpec
from bmad_loop.campaign_publication import Candidate


@pytest.fixture
def proof_case(tmp_path):
    unit = SimpleNamespace(
        id="story",
        spec=UnitSpec(
            "story",
            "source",
            "epic",
            "Story",
            acceptance=("Text becomes searchable", "Deleted records stay deleted after restart"),
        ),
        evidence={},
    )
    candidate = Candidate("a/b", unit.id, 1, "head", "base", "source", 3, "2")
    commands = [
        {"argv": ["pytest", "search"], "returncode": 0},
        {"argv": ["pytest", "restart"], "returncode": 0},
    ]
    unit.evidence["verification"] = {
        "outcome": "verified",
        "launch_id": "verify-1",
        "head": candidate.head,
        "commands": commands,
        "proof": provenance(
            tmp_path,
            candidate.head,
            [row["argv"] for row in commands],
            config_revision=candidate.config_revision,
        ),
    }
    unit.evidence["review"] = {
        "head": candidate.head,
        "launch_id": "review-1",
        "validated": {"verdict": "approved", "coordinates": asdict(candidate)},
        "artifact": {
            "acceptance_evidence": [
                {
                    "criterion_id": row["id"],
                    "status": "passed",
                    "command_indexes": [index],
                    "verification_launch_id": "verify-1",
                    "reason": "Exact test covers this behavior",
                }
                for index, row in enumerate(context(unit)["criteria"])
            ]
        },
    }
    return unit, candidate


def test_exact_proof_retains_environment_recipe_and_review(proof_case):
    unit, candidate = proof_case
    result = record(unit, candidate)
    assert result["complete"]
    assert [row["status"] for row in result["criteria"]] == ["passed", "passed"]
    assert result["criteria"][1]["evidence"][0]["command_index"] == 1
    assert result["criteria"][1]["evidence"][0]["head"] == candidate.head
    assert result["criteria"][1]["evidence"][0]["environment_digest"]


@pytest.mark.parametrize("missing", ["acceptance_evidence", "second", "empty-commands", "prose"])
def test_approved_prose_or_followup_never_substitutes_missing_proof(proof_case, missing):
    unit, candidate = proof_case
    artifact = unit.evidence["review"]["artifact"]
    if missing == "acceptance_evidence":
        artifact.clear()
    elif missing == "second":
        artifact["acceptance_evidence"].pop()
    elif missing == "empty-commands":
        artifact["acceptance_evidence"][1]["command_indexes"] = []
    else:
        artifact["acceptance_evidence"][1].update(
            status="not_run", reason="Tracked as follow-up #42"
        )
    result = evaluate(unit, candidate)
    assert not result["complete"]
    assert result["criteria"][1]["status"] == "not_run"


@pytest.mark.parametrize("change", ["head", "config", "recipe", "environment", "review"])
def test_stale_proof_is_preserved_but_cannot_pass(proof_case, change):
    unit, candidate = proof_case
    record(unit, candidate)
    if change == "head":
        candidate = replace(candidate, head="new-head")
    elif change == "config":
        candidate = replace(candidate, config_revision="3")
    elif change == "review":
        unit.evidence["review"]["head"] = "old-head"
    else:
        unit.evidence["verification"]["proof"][change + "_digest"] = "wrong"
    result = record(unit, candidate)
    assert not result["complete"]
    assert result["criteria"][0]["status"] == "stale"
    assert unit.evidence["acceptance"]["history"][0]["complete"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("returncode", 1),
        ("timed_out", True),
        ("spawn_error", "cannot start"),
        ("interrupted", True),
    ],
)
def test_failing_executable_result_cannot_be_reviewed_away(proof_case, field, value):
    unit, candidate = proof_case
    unit.evidence["verification"]["commands"][1][field] = value
    result = evaluate(unit, candidate)
    assert not result["complete"]
    assert result["criteria"][1]["status"] == "failed"


@pytest.mark.parametrize("change", ["unknown", "duplicate", "command", "launch"])
def test_invalid_mapping_is_not_accepted(proof_case, change):
    unit, candidate = proof_case
    claims = unit.evidence["review"]["artifact"]["acceptance_evidence"]
    if change == "unknown":
        claims[0]["criterion_id"] = "invented"
    elif change == "duplicate":
        claims.append(claims[0])
    elif change == "command":
        claims[0]["command_indexes"] = [99]
    else:
        claims[0]["verification_launch_id"] = "another-run"
    with pytest.raises(CampaignError):
        evaluate(unit, candidate)


def test_removed_required_criterion_survives_plan_edit(proof_case):
    unit, candidate = proof_case
    original = context(unit)["criteria"]
    unit.spec = replace(unit.spec, acceptance=unit.spec.acceptance[:1])
    unit.evidence["review"]["artifact"]["acceptance_evidence"].pop()
    assert context(unit)["criteria"] == original
    assert not evaluate(unit, candidate)["complete"]


def test_authorized_scope_change_records_exact_obligation_and_source(proof_case):
    unit, candidate = proof_case
    before = unit.spec.acceptance
    revision = ensure(unit)["revision"]
    unit.spec = replace(unit.spec, acceptance=before[:1])
    authority = {
        "kind": "owner-decision",
        "id": "decision-1",
        "source": "github:comment:42",
        "sha256": digest({"owner": "explicit scope decision"}),
    }
    authorize_amendment(
        unit, expected_revision=revision, before=before, after=before[:1], authority=authority
    )
    unit.evidence["review"]["artifact"]["acceptance_evidence"].pop()
    result = evaluate(unit, candidate)
    assert result["complete"]
    assert result["criteria"][1]["required"] is False
    assert unit.evidence["acceptance"]["amendments"][0]["authority"] == authority


def test_model_asserted_scope_waiver_is_not_authority(proof_case):
    unit, _ = proof_case
    with pytest.raises(CampaignError, match="authority"):
        authorize_amendment(
            unit,
            expected_revision=ensure(unit)["revision"],
            before=unit.spec.acceptance,
            after=unit.spec.acceptance,
            authority={"kind": "model", "id": "approved"},
        )
