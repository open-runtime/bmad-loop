"""Semantic evidence cannot be relabeled across purposes or earned native routes."""

from types import SimpleNamespace

import pytest

from bmad_loop import campaign_handoff_flow as flow
from bmad_loop.campaign_fidelity import route_binding
from bmad_loop.campaign_headless import file_digest, read_json, write_json
from bmad_loop.campaign_jobs import digest
from bmad_loop.campaign_model import RoleCandidate
from bmad_loop.campaign_summary import EXTRACT_PROMPT_VERSION, REDUCE_PROMPT_VERSION, summary_schema
from bmad_loop.campaign_transcripts import READER_VERSIONS


@pytest.fixture
def proof(tmp_path):
    source = tmp_path / "source.txt"
    source.write_text("Keep every acceptance criterion and owner hold.", encoding="utf-8")
    corpus = [
        {
            "source_path": str(source),
            "source_sha256": file_digest(source),
            "critical_ids": ["owner-1"],
        }
    ]
    cap = {
        "profile": "codex",
        "model": "gpt-test",
        "cli_version": "fixture-v1",
        "binary_sha256": "1" * 64,
        "efforts": ["high"],
        "qualification": {"sha256": "2" * 64},
    }
    bindings = [route_binding(cap, "handoff_map")]
    receipt = tmp_path / "independent.json"
    write_json(
        receipt,
        {
            "outcome": "completed",
            "quiescent": True,
            "input_complete": True,
            "session_id": "independent-native",
            "observed_model": "claude-independent",
            "result": {
                "verdict": "passed",
                "corpus_digest": digest(corpus),
                "route_bindings_digest": digest(bindings),
                "missing_critical_ids": [],
                "invented_authority": [],
                "unresolved_conflicts": [],
            },
        },
    )
    path = tmp_path / "fidelity.json"
    write_json(
        path,
        {
            "schema_version": 2,
            "route_bindings": bindings,
            "reader_versions": READER_VERSIONS,
            "summary_schema_digest": digest(summary_schema()),
            "audit_schema_digest": digest(flow.AUDIT_SCHEMA),
            "prompt_versions": [EXTRACT_PROMPT_VERSION, REDUCE_PROMPT_VERSION],
            "structural_pass": True,
            "corpus": corpus,
            "independent_review": {
                "native_receipt_path": str(receipt),
                "sha256": file_digest(receipt),
            },
        },
    )
    cap["semantic_qualification"] = {
        "schema_version": 2,
        "proofs": [{"path": str(path), "sha256": file_digest(path)}],
    }
    return cap, path


def test_exact_purpose_effort_and_transport_proof_is_accepted(proof):
    cap, _ = proof
    flow.validate_fidelity_qualification(cap, purpose="handoff_map")
    with pytest.raises(flow.HandoffRouteWait, match="purpose"):
        flow.validate_fidelity_qualification(cap, purpose="handoff_audit")


@pytest.mark.parametrize("change", ["effort", "transport", "cli"])
def test_native_route_changes_require_new_semantic_evidence(proof, change):
    cap, _ = proof
    if change == "effort":
        cap["efforts"] = ["medium"]
    elif change == "transport":
        cap["qualification"]["sha256"] = "3" * 64
    else:
        cap["cli_version"] = "new-version"
    with pytest.raises(flow.HandoffRouteWait, match="does not match"):
        flow.validate_fidelity_qualification(cap, purpose="handoff_map")


def test_editing_manifest_cannot_expand_independently_reviewed_operations(proof):
    cap, path = proof
    evidence = read_json(path)
    evidence["route_bindings"].append(route_binding(cap, "handoff_reduce"))
    write_json(path, evidence)
    cap["semantic_qualification"]["proofs"][0]["sha256"] = file_digest(path)
    with pytest.raises(flow.HandoffRouteWait, match="did not review these purposes"):
        flow.validate_fidelity_qualification(cap, purpose="handoff_reduce")


def test_legacy_generic_semantic_proof_does_not_grant_new_exact_route(proof):
    cap, _ = proof
    cap["semantic_qualification"]["schema_version"] = 1
    with pytest.raises(flow.HandoffRouteWait, match="schema-2"):
        flow.validate_fidelity_qualification(cap, purpose="handoff_map")


def test_automatic_summary_route_requires_its_own_purpose_proof(proof, monkeypatch):
    cap, _ = proof
    candidate = RoleCandidate("codex", "gpt-test", "high", "openai")
    snapshot = SimpleNamespace(spec=SimpleNamespace(candidates=lambda _: (candidate,)))
    runtime = SimpleNamespace(clock=lambda: 100)
    monkeypatch.setattr(flow, "_cap", lambda *args: cap)
    assert flow._summary_route(runtime, snapshot, "summarization")[0] == candidate
    with pytest.raises(flow.HandoffRouteWait, match="purpose"):
        flow._summary_route(runtime, snapshot, "summarization", purpose="handoff_reduce")
