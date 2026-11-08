"""Semantic evidence cannot be relabeled across purposes or earned native routes."""

from types import SimpleNamespace

import pytest

from bmad_loop import campaign_handoff_flow as flow
from bmad_loop.campaign_fidelity import (
    HISTORY_AUDIT_PROMPT_VERSION,
    route_binding,
    semantic_contract,
)
from bmad_loop.campaign_headless import file_digest, read_json, write_json
from bmad_loop.campaign_jobs import digest
from bmad_loop.campaign_model import RoleCandidate
from bmad_loop.campaign_summary import (
    EXTRACT_PROMPT_VERSION,
    REDUCE_PROMPT_VERSION,
    summary_schema,
    summary_schema_template,
)
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
                "semantic_contract_digests": {
                    "handoff_map": digest(semantic_contract("handoff_map", flow.AUDIT_SCHEMA))
                },
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
            "summary_job_schema_digest": digest(summary_schema_template()),
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


@pytest.mark.parametrize("changed", ["artifact", "independent-result"])
def test_history_auditor_prompt_version_requires_independent_native_binding(proof, changed):
    from pathlib import Path

    cap, path = proof
    evidence = read_json(path)
    bindings = [route_binding(cap, "handoff_audit")]
    evidence.update(route_bindings=bindings, audit_prompt_version=HISTORY_AUDIT_PROMPT_VERSION)
    receipt_path = Path(evidence["independent_review"]["native_receipt_path"])
    receipt = read_json(receipt_path)
    receipt["result"].update(
        route_bindings_digest=digest(bindings),
        audit_prompt_version=HISTORY_AUDIT_PROMPT_VERSION,
        semantic_contract_digests={
            "handoff_audit": digest(semantic_contract("handoff_audit", flow.AUDIT_SCHEMA))
        },
    )

    def save():
        write_json(receipt_path, receipt)
        evidence["independent_review"]["sha256"] = file_digest(receipt_path)
        write_json(path, evidence)
        cap["semantic_qualification"]["proofs"][0]["sha256"] = file_digest(path)

    save()
    flow.validate_fidelity_qualification(cap, purpose="handoff_audit")
    if changed == "artifact":
        evidence["audit_prompt_version"] = "history-audit-v1"
    else:
        receipt["result"]["audit_prompt_version"] = "history-audit-v1"
    save()
    with pytest.raises(flow.HandoffRouteWait, match="does not match|audit prompt"):
        flow.validate_fidelity_qualification(cap, purpose="handoff_audit")


@pytest.mark.parametrize("contract", ["constraints-v1", "recovery-v1"])
def test_finite_proof_binds_its_own_schema_not_unused_history_schema(proof, contract, monkeypatch):
    from pathlib import Path

    from bmad_loop import campaign_handoff_constraints as constraints
    from bmad_loop import campaign_handoff_recovery as recovery

    cap, path = proof
    module = constraints if contract == "constraints-v1" else recovery
    source_schema = constraints.REDUCTION_SCHEMA if module is constraints else recovery.SCHEMA
    field = "constraint_schemas_digest" if module is constraints else "recovery_schemas_digest"
    purpose = "constraint_audit" if module is constraints else "recovery_audit"
    schema_digest = digest([source_schema, module.AUDIT_SCHEMA])
    evidence = read_json(path)
    bindings = [route_binding(cap, purpose)]
    evidence.update(route_bindings=bindings, **{field: schema_digest})
    receipt_path = Path(evidence["independent_review"]["native_receipt_path"])
    receipt = read_json(receipt_path)
    receipt["result"].update(
        route_bindings_digest=digest(bindings), contract_schema_digests={contract: schema_digest}
    )
    write_json(receipt_path, receipt)
    evidence["independent_review"]["sha256"] = file_digest(receipt_path)
    write_json(path, evidence)
    cap["semantic_qualification"]["proofs"][0]["sha256"] = file_digest(path)
    immutable = (path.read_bytes(), receipt_path.read_bytes())
    from bmad_loop import campaign_summary as summary
    from bmad_loop import campaign_transcripts as transcripts

    monkeypatch.setattr(summary, "summary_schema", lambda: {"unused-summary": "changed"})
    monkeypatch.setattr(summary, "EXTRACT_PROMPT_VERSION", "unused-prompt-changed")
    monkeypatch.setattr(transcripts, "READER_VERSIONS", {"unused-reader": 999})
    monkeypatch.setattr(flow, "AUDIT_SCHEMA", {"new_unused_history_schema": True})
    flow.validate_fidelity_qualification(cap, purpose=purpose, contract=contract)
    assert immutable == (path.read_bytes(), receipt_path.read_bytes())
    monkeypatch.setattr(module, "AUDIT_SCHEMA", {"changed_used_contract_schema": True})
    with pytest.raises(flow.HandoffRouteWait, match="does not match"):
        flow.validate_fidelity_qualification(cap, purpose=purpose, contract=contract)


@pytest.mark.parametrize("purpose", ["handoff_map", "handoff_reduce", "handoff_audit"])
@pytest.mark.parametrize("changed", ["reader", "summary-schema", "prompt", "missing-native-digest"])
def test_editable_manifest_cannot_relabel_used_native_semantic_contract(
    proof, monkeypatch, purpose, changed
):
    from pathlib import Path

    from bmad_loop import campaign_fidelity as fidelity
    from bmad_loop import campaign_summary as summary
    from bmad_loop import campaign_transcripts as transcripts

    cap, path = proof
    evidence = read_json(path)
    bindings = [route_binding(cap, purpose)]
    evidence.update(route_bindings=bindings, audit_prompt_version=HISTORY_AUDIT_PROMPT_VERSION)
    receipt_path = Path(evidence["independent_review"]["native_receipt_path"])
    receipt = read_json(receipt_path)
    receipt["result"].update(
        route_bindings_digest=digest(bindings),
        semantic_contract_digests={purpose: digest(semantic_contract(purpose, flow.AUDIT_SCHEMA))},
        audit_prompt_version=HISTORY_AUDIT_PROMPT_VERSION,
    )
    write_json(receipt_path, receipt)
    evidence["independent_review"]["sha256"] = file_digest(receipt_path)
    write_json(path, evidence)
    cap["semantic_qualification"]["proofs"][0]["sha256"] = file_digest(path)
    flow.validate_fidelity_qualification(cap, purpose=purpose)
    native_bytes = receipt_path.read_bytes()
    if changed == "reader":
        monkeypatch.setattr(transcripts, "READER_VERSIONS", {"changed": 999})
        evidence["reader_versions"] = transcripts.READER_VERSIONS
    elif changed == "summary-schema":
        monkeypatch.setattr(
            summary, "summary_schema", lambda *args, **kwargs: {"changed": "schema"}
        )
        evidence["summary_schema_digest"] = digest(summary.summary_schema())
        evidence["summary_job_schema_digest"] = digest(summary.summary_schema_template())
    elif changed == "prompt":
        if purpose == "handoff_audit":
            monkeypatch.setattr(fidelity, "HISTORY_AUDIT_PROMPT_VERSION", "changed-audit-prompt")
            evidence["audit_prompt_version"] = fidelity.HISTORY_AUDIT_PROMPT_VERSION
        else:
            monkeypatch.setattr(
                summary,
                "EXTRACT_PROMPT_VERSION" if purpose == "handoff_map" else "REDUCE_PROMPT_VERSION",
                "changed-prompt",
            )
        evidence["prompt_versions"] = [
            summary.EXTRACT_PROMPT_VERSION,
            summary.REDUCE_PROMPT_VERSION,
        ]
    else:
        receipt["result"].pop("semantic_contract_digests")
        write_json(receipt_path, receipt)
        evidence["independent_review"]["sha256"] = file_digest(receipt_path)
    write_json(path, evidence)
    cap["semantic_qualification"]["proofs"][0]["sha256"] = file_digest(path)
    with pytest.raises(flow.HandoffRouteWait, match="semantic contract|audit prompt"):
        flow.validate_fidelity_qualification(cap, purpose=purpose)
    if changed != "missing-native-digest":
        assert receipt_path.read_bytes() == native_bytes


def test_editing_audit_schema_manifest_cannot_relabel_independent_native_receipt(
    proof, monkeypatch
):
    from pathlib import Path

    cap, path = proof
    evidence = read_json(path)
    bindings = [route_binding(cap, "handoff_audit")]
    evidence.update(route_bindings=bindings, audit_prompt_version=HISTORY_AUDIT_PROMPT_VERSION)
    receipt_path = Path(evidence["independent_review"]["native_receipt_path"])
    receipt = read_json(receipt_path)
    receipt["result"].update(
        route_bindings_digest=digest(bindings),
        audit_prompt_version=HISTORY_AUDIT_PROMPT_VERSION,
        semantic_contract_digests={
            "handoff_audit": digest(semantic_contract("handoff_audit", flow.AUDIT_SCHEMA))
        },
    )
    write_json(receipt_path, receipt)
    evidence["independent_review"]["sha256"] = file_digest(receipt_path)
    write_json(path, evidence)
    cap["semantic_qualification"]["proofs"][0]["sha256"] = file_digest(path)
    flow.validate_fidelity_qualification(cap, purpose="handoff_audit")
    native_bytes = receipt_path.read_bytes()
    monkeypatch.setattr(flow, "AUDIT_SCHEMA", {"changed": "used-audit-schema"})
    evidence["audit_schema_digest"] = digest(flow.AUDIT_SCHEMA)
    write_json(path, evidence)
    cap["semantic_qualification"]["proofs"][0]["sha256"] = file_digest(path)
    with pytest.raises(flow.HandoffRouteWait, match="semantic contract"):
        flow.validate_fidelity_qualification(cap, purpose="handoff_audit")
    assert receipt_path.read_bytes() == native_bytes


def test_map_proof_ignores_unused_audit_schema_and_reducer_prompt(proof, monkeypatch):
    from bmad_loop import campaign_summary as summary

    cap, path = proof
    manifest = path.read_bytes()
    monkeypatch.setattr(flow, "AUDIT_SCHEMA", {"changed-unused": True})
    monkeypatch.setattr(summary, "REDUCE_PROMPT_VERSION", "changed-unused-prompt")
    flow.validate_fidelity_qualification(cap, purpose="handoff_map")
    assert path.read_bytes() == manifest


@pytest.mark.parametrize("change", ["additionalProperties", "required"])
def test_specialized_native_schema_change_cannot_borrow_generic_semantic_proof(
    proof, monkeypatch, change
):
    from bmad_loop import campaign_summary as summary

    cap, path = proof
    flow.validate_fidelity_qualification(cap, purpose="handoff_map")
    before = path.read_bytes()
    original = summary.summary_schema

    def changed(job=None, **kwargs):
        schema = original(job, **kwargs)
        if job is not None:
            facts = schema["properties"]["facts"]["items"]
            if change == "additionalProperties":
                facts["additionalProperties"] = True
            else:
                facts["required"].remove("source_quote_id")
        return schema

    monkeypatch.setattr(summary, "summary_schema", changed)
    assert digest(summary.summary_schema()) == read_json(path)["summary_schema_digest"]
    with pytest.raises(flow.HandoffRouteWait, match="does not match"):
        flow.validate_fidelity_qualification(cap, purpose="handoff_map")
    assert path.read_bytes() == before
