"""Bounded current-contract reconstruction never manufactures owner authority."""

import dataclasses
import json
from pathlib import Path

import pytest
from conftest import git
from test_campaign_handoff_flow import campaign as campaign
from test_campaign_handoff_flow import complete_audits, complete_maps, step
from test_campaign_headless import prepared as prepared

from bmad_loop import campaign_handoff_flow as flow
from bmad_loop.campaign_model import CampaignError
from bmad_loop.campaign_reasoning_results import apply_result


def complete(runtime, snapshot, unit, field, result_for):
    for key in unit.evidence["handoff"][field]:
        row = snapshot.jobs[key]
        inputs = json.loads(Path(row["request"]["inputs"][0]["path"]).read_text())
        result = result_for(inputs)
        row.update(
            state="validated",
            validation="valid",
            result=result,
            receipt={
                "result": result,
                "provider_family": "anthropic" if field.endswith("audits") else "openai",
            },
        )
        apply_result(runtime, snapshot, row)
        row["state"] = "applied"


def audit_identity(inputs):
    return {"handoff_id": inputs["handoff_id"], "capture_digest": inputs["capture_digest"]}


def complete_history(runtime, snapshot, unit):
    from bmad_loop.campaign_summary import SummaryJobSpec, load_input

    while unit.evidence["handoff"]["phase"] == "summarize":
        keys = unit.evidence["handoff"]["jobs"]
        if keys and snapshot.jobs[keys[0]]["request"]["purpose"] == "handoff_map":
            complete_maps(runtime, snapshot, unit)
        else:
            for key in keys:
                row = snapshot.jobs[key]
                spec = SummaryJobSpec(**row["request"]["coordinates"]["summary_spec"])
                source = load_input(spec)
                result = {
                    "schema_version": 1,
                    "job_id": key,
                    "input_sha256": spec.input_sha256,
                    "covered_ids": list(spec.primary_ids),
                    "facts": source["children"][0]["summary"]["facts"][:1],
                    "gaps": [],
                }
                row.update(
                    state="validated",
                    validation="valid",
                    result=result,
                    receipt={"result": result, "provider_family": "openai"},
                )
                apply_result(runtime, snapshot, row)
                row["state"] = "applied"
        step(runtime, snapshot, unit, "handoff-audit-plan")


def test_oversized_current_constraints_reduce_and_are_independently_audited(campaign):
    runtime, snapshot, unit, receipt = campaign
    source = Path(receipt["sessions"][0]["transcript_path"])
    with source.open("a") as stream:
        for index in range(90):
            stream.write(
                json.dumps(
                    {
                        "type": "event_msg",
                        "payload": {
                            "type": "user_message",
                            "message": f"Requirement {index}: "
                            + "Preserve the accepted invariant. " * 15,
                        },
                    }
                )
                + "\n"
            )
    flow.intercept_terminal(runtime, snapshot, unit, receipt)
    step(runtime, snapshot, unit, "handoff-capture")
    step(runtime, snapshot, unit, "handoff-plan")
    complete_history(runtime, snapshot, unit)
    complete_audits(runtime, snapshot, unit)
    step(runtime, snapshot, unit, "handoff-assemble")
    value = unit.evidence["handoff"]
    assert value["phase"] == "constraint_reduce" and len(value["constraint_jobs"]) > 1
    assert flow.resume_proposal(snapshot, unit) is None
    rounds = 0
    while unit.evidence["handoff"]["phase"] == "constraint_reduce":
        rounds += 1
        assert rounds <= 8, unit.evidence["handoff"]["phase"]
        complete(
            runtime,
            snapshot,
            unit,
            "constraint_jobs",
            lambda inputs: {
                **audit_identity(inputs),
                "covered_ids": [row["id"] for row in inputs["nodes"]],
                "groups": [
                    {
                        "text": "Preserve all accepted invariants and their recorded exceptions.",
                        "refs": [row["id"] for row in inputs["nodes"]],
                    }
                ],
                "ambiguities": [],
            },
        )
        # Persist/reload before independent review: completed reductions live in SQL.
        snapshot = runtime.store.save(snapshot, snapshot.revision)
        snapshot = runtime.store.load(snapshot.id)
        unit = snapshot.units[unit.id]
        step(runtime, snapshot, unit, "handoff-constraint-audit")
        complete(
            runtime,
            snapshot,
            unit,
            "constraint_audits",
            lambda inputs: {
                **audit_identity(inputs),
                "covered_ids": [row["id"] for row in inputs["nodes"]],
                "verdict": "faithful",
                "ambiguities": [],
            },
        )
        step(runtime, snapshot, unit, "handoff-assemble")
    step(runtime, snapshot, unit, "handoff-release")
    packet = json.loads(Path(unit.evidence["handoff"]["packet"]["path"]).read_text())
    assert packet["read_only"] is False
    exact = json.loads(Path(packet["exact_current_constraint_index"]["path"]).read_text())
    assert len(exact) == 91
    assert len(packet["resolved_constraints"]) < len(exact)
    assert packet["constraint_tree"]["sha256"]


def recovery_result(inputs, *, ready):
    basis = next(
        (source for source in inputs["sources"] if source["authority"] == "adopted_policy"), None
    )
    return {
        **audit_identity(inputs),
        "covered_evidence_ids": [row["id"] for row in inputs["sources"]],
        "obligation_ids": inputs["obligation_ids"],
        "missing_history_ids": [row["id"] for row in inputs["missing_history"]],
        "disposition": "current_constraints_reconstructed" if ready else "needs_owner_evidence",
        "constraints": (
            [
                {
                    "source_id": basis["id"],
                    "source_quote": basis["content"],
                    "interpretation": "The adopted policy binds the complete current contract.",
                }
            ]
            if basis
            else []
        ),
        "authority_basis": [basis["id"]] if basis else [],
        "unresolved": (
            []
            if ready
            else ["No owner-authenticated current specification replaces the unavailable history."]
        ),
    }


@pytest.mark.parametrize("authority,large", [(False, False), (True, False), (True, True)])
def test_missing_history_attempts_readonly_recovery_before_escalation(campaign, authority, large):
    runtime, snapshot, unit, receipt = campaign
    runtime.project = Path(unit.bindings["workspace"])
    snapshot.spec = dataclasses.replace(
        snapshot.spec, github=dataclasses.replace(snapshot.spec.github, enabled=False)
    )
    if authority:
        policy = runtime.project / "CURRENT.md"
        policy.write_text(
            "This adopted policy is the complete current contract: retain every accepted feature and commit history. Prior conversation is historical context, not additional current requirements."
            + (" Retain every required test." * 100 if large else "")
        )
        git(runtime.project, "add", "CURRENT.md")
        git(runtime.project, "commit", "-qm", "adopt complete current contract")
        snapshot.spec = dataclasses.replace(
            snapshot.spec,
            decisions=dataclasses.replace(snapshot.spec.decisions, sources=("CURRENT.md",)),
        )
    if large:
        snapshot.spec = dataclasses.replace(
            snapshot.spec,
            reasoning=dataclasses.replace(snapshot.spec.reasoning, packet_token_limit=10000),
        )
    Path(receipt["sessions"][0]["transcript_path"]).unlink()
    flow.intercept_terminal(runtime, snapshot, unit, receipt)
    step(runtime, snapshot, unit, "handoff-capture")
    assert flow.proposals(runtime, snapshot)[0].kind == "handoff-recovery-plan"
    step(runtime, snapshot, unit, "handoff-recovery-plan")
    value = unit.evidence["handoff"]
    assert value["phase"] == "recovery_extract"
    complete(
        runtime,
        snapshot,
        unit,
        "recovery_jobs",
        lambda inputs: recovery_result(inputs, ready=authority),
    )
    step(runtime, snapshot, unit, "handoff-recovery-audit")
    if not authority:
        assert value["phase"] == "recovery_wait" and "owner-authenticated" in unit.hold.reason
        assert not flow.proposals(runtime, snapshot)
        return
    complete(
        runtime,
        snapshot,
        unit,
        "recovery_audits",
        lambda inputs: {
            **audit_identity(inputs),
            "covered_evidence_ids": [row["id"] for row in inputs["sources"]],
            "obligation_ids": inputs["obligation_ids"],
            "missing_history_ids": [row["id"] for row in inputs["missing_history"]],
            "verdict": "current_contract_established",
            "unresolved": [],
        },
    )
    step(runtime, snapshot, unit, "handoff-recovery-finish")
    step(runtime, snapshot, unit, "handoff-plan")
    step(runtime, snapshot, unit, "handoff-audit-plan")
    complete_audits(runtime, snapshot, unit)
    step(runtime, snapshot, unit, "handoff-assemble")
    if large:
        assert value["phase"] == "constraint_reduce"
        complete(
            runtime,
            snapshot,
            unit,
            "constraint_jobs",
            lambda inputs: {
                **audit_identity(inputs),
                "covered_ids": [row["id"] for row in inputs["nodes"]],
                "groups": [
                    {
                        "text": "Retain every accepted feature, published commit and required test. Adopted policy establishes the complete current contract; prior conversation is historical context only.",
                        "refs": [row["id"] for row in inputs["nodes"]],
                    }
                ],
                "ambiguities": [],
            },
        )
        step(runtime, snapshot, unit, "handoff-constraint-audit")
        complete(
            runtime,
            snapshot,
            unit,
            "constraint_audits",
            lambda inputs: {
                **audit_identity(inputs),
                "covered_ids": [row["id"] for row in inputs["nodes"]],
                "verdict": "faithful",
                "ambiguities": [],
            },
        )
        step(runtime, snapshot, unit, "handoff-assemble")
        assert value["phase"] == "assemble", (value["phase"], unit.hold.reason)
    step(runtime, snapshot, unit, "handoff-release")
    packet = json.loads(Path(value["packet"]["path"]).read_text())
    assert packet["missing_native_history"]
    assert packet["current_contract_recovery"]["missing_history_retained"]
    if large:
        index = json.loads(Path(packet["recovered_constraints_index"]["path"]).read_text())
        assert index[0]["source_quote"] == policy.read_text()
        assert packet["resolved_constraints"][0]["authority"] == "context-only"
        assert len(Path(value["packet"]["path"]).read_bytes()) <= 10000
    else:
        assert packet["recovered_constraints"]
    assert unit.hold is None and value["phase"] == "ready"


def test_recovery_cannot_claim_canonical_acceptance_is_owner_authority(campaign):
    runtime, snapshot, unit, receipt = campaign
    runtime.project = Path(unit.bindings["workspace"])
    snapshot.spec = dataclasses.replace(
        snapshot.spec, github=dataclasses.replace(snapshot.spec.github, enabled=False)
    )
    Path(receipt["sessions"][0]["transcript_path"]).unlink()
    flow.intercept_terminal(runtime, snapshot, unit, receipt)
    step(runtime, snapshot, unit, "handoff-capture")
    step(runtime, snapshot, unit, "handoff-recovery-plan")
    with pytest.raises(CampaignError, match="authoritative"):
        complete(
            runtime,
            snapshot,
            unit,
            "recovery_jobs",
            lambda inputs: recovery_result(inputs, ready=True),
        )


@pytest.mark.parametrize("input_limit", [100000, 50000])
def test_ready_fallback_reuses_or_replans_packet_for_fresh_destination_capacity(
    campaign, input_limit
):
    runtime, snapshot, unit, receipt = campaign
    flow.intercept_terminal(runtime, snapshot, unit, receipt)
    step(runtime, snapshot, unit, "handoff-capture")
    step(runtime, snapshot, unit, "handoff-plan")
    complete_maps(runtime, snapshot, unit)
    step(runtime, snapshot, unit, "handoff-audit-plan")
    complete_audits(runtime, snapshot, unit)
    step(runtime, snapshot, unit, "handoff-assemble")
    step(runtime, snapshot, unit, "handoff-release")
    value = unit.evidence["handoff"]
    packet = dict(value["packet"])
    first = snapshot.spec.candidates("execution")[0]
    alternative = dataclasses.replace(first, model="gpt-available")
    snapshot.spec = dataclasses.replace(
        snapshot.spec,
        role_pools=tuple(
            (name, (*pool, alternative) if name == "writers" else pool)
            for name, pool in snapshot.spec.role_pools
        ),
    )
    cap = {
        **snapshot.config_snapshot["reasoning_capabilities"]["codex:gpt-test"]["execution"],
        "model": alternative.model,
        "input_tokens": input_limit,
    }
    snapshot.config_snapshot["reasoning_capabilities"]["codex:gpt-available"] = {"execution": cap}
    runtime._role = lambda snapshot, role, *, allowed_candidates: next(
        (candidate for candidate in allowed_candidates if candidate == alternative), None
    )
    assert flow.resume_proposal(snapshot, unit, runtime=runtime) is None
    proposed = flow.proposals(runtime, snapshot)[0]
    assert proposed.kind == "handoff-route"
    from bmad_loop.campaign_model import OutboxAction

    action = OutboxAction("reroute", proposed.kind, unit.id, proposed.payload, snapshot.generation)
    flow.apply(runtime, snapshot, action, flow.perform(runtime, snapshot, action))
    assert unit.claim.launch_id == "original" and unit.claim.quiescent
    if input_limit == 100000:
        assert value["packet"] == packet and value["phase"] == "ready"
        assert (
            flow.resume_proposal(snapshot, unit, runtime=runtime).payload["candidate"]["model"]
            == alternative.model
        )
    else:
        assert value["phase"] == "audit" and "packet" not in value
        assert flow.resume_proposal(snapshot, unit, runtime=runtime) is None


@pytest.mark.parametrize(
    "contract,field",
    [("constraints-v1", "constraint_schemas_digest"), ("recovery-v1", "recovery_schemas_digest")],
)
def test_each_additional_contract_requires_its_own_earned_fidelity_binding(
    campaign, tmp_path, contract, field
):
    from bmad_loop.campaign_headless import file_digest

    _, snapshot, _, _ = campaign
    cap = dict(
        snapshot.config_snapshot["reasoning_capabilities"]["claude:claude-test"]["reasoning"]
    )
    flow.validate_fidelity_qualification(cap, contract=contract)
    evidence = json.loads(Path(cap["semantic_qualification"]["path"]).read_text())
    evidence.pop(field)
    path = tmp_path / "history-only-fidelity.json"
    path.write_text(json.dumps(evidence))
    cap["semantic_qualification"] = {
        "schema_version": 1,
        "path": str(path),
        "sha256": file_digest(path),
    }
    with pytest.raises(flow.HandoffRouteWait, match="does not match"):
        flow.validate_fidelity_qualification(cap, contract=contract)


def test_constraint_groups_must_retain_every_input_reference():
    from bmad_loop.campaign_handoff_constraints import validate

    inputs = {
        "handoff_id": "handoff",
        "capture_digest": "capture",
        "nodes": [{"id": "first"}, {"id": "second"}],
    }
    result = {
        "handoff_id": "handoff",
        "capture_digest": "capture",
        "covered_ids": ["first", "second"],
        "ambiguities": [],
        "groups": [{"text": "Both obligations remain", "refs": ["first", "second"]}],
    }
    assert validate(result, inputs)["validated"]
    result["groups"][0]["refs"] = ["first"]
    with pytest.raises(CampaignError, match="dropped"):
        validate(result, inputs)


def test_missing_history_wait_rechecks_new_committed_evidence_and_resumes(campaign, tmp_path):
    runtime, snapshot, unit, receipt = campaign
    # Canonical policy advances independently of the preserved worker checkout.
    runtime.project = tmp_path / "canonical-policy"
    git(Path(unit.bindings["workspace"]), "worktree", "add", "-b", "policy", str(runtime.project))
    snapshot.spec = dataclasses.replace(
        snapshot.spec,
        github=dataclasses.replace(snapshot.spec.github, enabled=False),
        decisions=dataclasses.replace(snapshot.spec.decisions, sources=("CURRENT.md",)),
    )
    Path(receipt["sessions"][0]["transcript_path"]).unlink()
    flow.intercept_terminal(runtime, snapshot, unit, receipt)
    step(runtime, snapshot, unit, "handoff-capture")
    step(runtime, snapshot, unit, "handoff-recovery-plan")
    complete(
        runtime,
        snapshot,
        unit,
        "recovery_jobs",
        lambda inputs: recovery_result(inputs, ready=False),
    )
    step(runtime, snapshot, unit, "handoff-recovery-audit")
    assert not flow.proposals(runtime, snapshot)
    runtime.clock = lambda: 131.0
    assert flow.proposals(runtime, snapshot)[0].kind == "handoff-recovery-observe"
    step(runtime, snapshot, unit, "handoff-recovery-observe")
    assert not flow.proposals(runtime, snapshot)
    assert unit.evidence["handoff"]["phase"] == "recovery_wait"
    (runtime.project / "CURRENT.md").write_text(
        "The complete current contract is to retain accepted features and commit history. "
        "Prior conversation adds no further current requirements."
    )
    git(runtime.project, "add", "CURRENT.md")
    git(runtime.project, "commit", "-qm", "adopt current contract")
    runtime.clock = lambda: 162.0
    assert flow.proposals(runtime, snapshot)[0].kind == "handoff-recovery-observe"
    step(runtime, snapshot, unit, "handoff-recovery-observe")
    assert unit.evidence["handoff"]["phase"] == "capture"
    step(runtime, snapshot, unit, "handoff-recovery-plan")
    complete(
        runtime, snapshot, unit, "recovery_jobs", lambda inputs: recovery_result(inputs, ready=True)
    )
    step(runtime, snapshot, unit, "handoff-recovery-audit")
    complete(
        runtime,
        snapshot,
        unit,
        "recovery_audits",
        lambda inputs: {
            **audit_identity(inputs),
            "covered_evidence_ids": [row["id"] for row in inputs["sources"]],
            "obligation_ids": inputs["obligation_ids"],
            "missing_history_ids": [row["id"] for row in inputs["missing_history"]],
            "verdict": "current_contract_established",
            "unresolved": [],
        },
    )
    step(runtime, snapshot, unit, "handoff-recovery-finish")
    step(runtime, snapshot, unit, "handoff-plan")
    step(runtime, snapshot, unit, "handoff-audit-plan")
    complete_audits(runtime, snapshot, unit)
    step(runtime, snapshot, unit, "handoff-assemble")
    step(runtime, snapshot, unit, "handoff-release")
    assert unit.evidence["handoff"]["phase"] == "ready" and unit.hold is None


@pytest.mark.parametrize("contract", ["constraints-v1", "recovery-v1"])
def test_history_native_receipt_cannot_be_relabelled_as_contract_proof(
    campaign, tmp_path, contract
):
    from bmad_loop.campaign_headless import file_digest

    _, snapshot, _, _ = campaign
    cap = dict(
        snapshot.config_snapshot["reasoning_capabilities"]["claude:claude-test"]["reasoning"]
    )
    flow.validate_fidelity_qualification(cap, contract=contract)
    path = Path(cap["semantic_qualification"]["path"])
    evidence = json.loads(path.read_text())
    receipt = json.loads(Path(evidence["independent_review"]["native_receipt_path"]).read_text())
    receipt["result"].pop("contract_schema_digests")
    receipt_path = tmp_path / "history-receipt.json"
    receipt_path.write_text(json.dumps(receipt))
    evidence["independent_review"] = {
        "native_receipt_path": str(receipt_path),
        "sha256": file_digest(receipt_path),
    }
    path.write_text(json.dumps(evidence))
    cap["semantic_qualification"]["sha256"] = file_digest(path)
    with pytest.raises(flow.HandoffRouteWait, match="did not review this contract"):
        flow.validate_fidelity_qualification(cap, contract=contract)


def test_quota_replan_preserves_sibling_semantic_failures_and_bounds_limits(campaign):
    from bmad_loop.campaign_jobs import prepare, request_from_dict
    from bmad_loop.campaign_model import OutboxAction

    runtime, snapshot, unit, receipt = campaign
    flow.intercept_terminal(runtime, snapshot, unit, receipt)
    step(runtime, snapshot, unit, "handoff-capture")
    step(runtime, snapshot, unit, "handoff-plan")
    value = unit.evidence["handoff"]
    original = snapshot.jobs[value["jobs"][0]]
    original.update(state="failed", failure_attempts=1, receipt={"outcome": "completed"})
    sibling = prepare(
        snapshot, dataclasses.replace(request_from_dict(original["request"]), id="quota-sibling")
    )
    sibling.update(state="capacity_wait", receipt={"outcome": "quota"})
    value["jobs"].append(sibling["id"])
    assert flow.proposals(runtime, snapshot)[0].kind == "handoff-replan"
    step(runtime, snapshot, unit, "handoff-replan")
    replacement = snapshot.jobs[value["jobs"][0]]
    assert replacement["id"] != original["id"]
    assert replacement["failure_attempts"] == 1
    for failures in range(2, snapshot.spec.reasoning.max_attempts):
        replacement.update(state="failed", failure_attempts=failures, receipt={"outcome": "limit"})
        step(runtime, snapshot, unit, "handoff-replan")
        replacement = snapshot.jobs[value["jobs"][0]]
        assert replacement["failure_attempts"] == failures
    replacement.update(
        state="failed",
        failure_attempts=snapshot.spec.reasoning.max_attempts,
        receipt={"outcome": "limit"},
    )
    proposed = flow.proposals(runtime, snapshot)[0]
    assert proposed.kind == "handoff-failed"
    action = OutboxAction("exhausted", proposed.kind, unit.id, proposed.payload, 0)
    flow.apply(runtime, snapshot, action, flow.perform(runtime, snapshot, action))
    assert value["phase"] == "failed" and "lifetime" in unit.hold.reason
    assert not flow.proposals(runtime, snapshot)


def test_bound_contract_schemas_use_exact_ids_without_empty_enums():
    from bmad_loop.campaign_handoff_constraints import bound_schema
    from bmad_loop.campaign_handoff_recovery import SCHEMA
    from bmad_loop.campaign_jobs import validate_result, validate_schema

    inputs = {
        "handoff_id": "h",
        "capture_digest": "c",
        "sources": [],
        "obligation_ids": [],
        "missing_history": [{"id": "missing"}],
    }
    schema = bound_schema(SCHEMA, inputs)
    validate_schema(schema)
    result = {
        "handoff_id": "h",
        "capture_digest": "c",
        "covered_evidence_ids": [],
        "obligation_ids": [],
        "missing_history_ids": ["missing"],
        "disposition": "needs_owner_evidence",
        "constraints": [],
        "authority_basis": [],
        "unresolved": ["No authenticated current contract is available"],
    }
    validate_result(result, schema)
    assert '"enum": []' not in json.dumps(schema)
    result["covered_evidence_ids"] = ["outer-transport-source"]
    with pytest.raises(CampaignError):
        validate_result(result, schema)
