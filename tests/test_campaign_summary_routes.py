"""Stage authority and final synthesis are enforced through real handoff effects."""

import dataclasses

import pytest
from test_campaign_handoff_flow import campaign as campaign
from test_campaign_handoff_flow import complete_audits, complete_maps, step
from test_campaign_headless import prepared as prepared

from bmad_loop import campaign_handoff_flow as flow
from bmad_loop import campaign_summary_routes as routes
from bmad_loop.campaign_jobs import request_from_dict
from bmad_loop.campaign_model import CampaignError, RoleCandidate, RoleSpec


def staged(snapshot):
    mapping = tuple(
        (stage, "auditors" if role == "handoff_audit" else "map")
        for stage, (role, _, _) in routes.STAGES.items()
    )
    snapshot.spec = dataclasses.replace(
        snapshot.spec,
        reasoning=dataclasses.replace(snapshot.spec.reasoning, summary_routes=mapping),
    )


@pytest.mark.parametrize("damaged", [False, True])
def test_single_leaf_requires_final_synthesis_before_owner_audit(campaign, damaged):
    from bmad_loop.campaign_reasoning_results import apply_result
    from bmad_loop.campaign_summary import FINAL_PROMPT_VERSION

    runtime, snapshot, unit, receipt = campaign
    staged(snapshot)
    flow.intercept_terminal(runtime, snapshot, unit, receipt)
    step(runtime, snapshot, unit, "handoff-capture")
    step(runtime, snapshot, unit, "handoff-plan")
    assert unit.evidence["handoff"]["audit_family"] == "anthropic"
    complete_maps(runtime, snapshot, unit)
    value = unit.evidence["handoff"]
    leaf = snapshot.jobs[value["jobs"][0]]
    assert leaf["request"]["coordinates"]["summary_stage"] == "leaf"
    step(runtime, snapshot, unit, "handoff-audit-plan")
    final = snapshot.jobs[value["jobs"][0]]
    assert final["id"] != leaf["id"] and value["phase"] == "summarize"
    assert final["request"]["coordinates"]["summary_stage"] == "final"
    assert final["request"]["coordinates"]["summary_spec"]["prompt_version"] == FINAL_PROMPT_VERSION
    assert "final successor synthesis" in final["request"]["prompt"]
    result = {
        **leaf["result"],
        "job_id": final["id"],
        "input_sha256": final["request"]["coordinates"]["summary_spec"]["input_sha256"],
        "covered_ids": [leaf["id"]],
    }
    if damaged:
        result["facts"] = []
    final.update(
        state="validated",
        validation="valid",
        result=result,
        receipt={"job_id": final["id"], "result": result, "provider_family": "openai"},
    )
    apply_result(runtime, snapshot, final)
    final["state"] = "applied"
    step(runtime, snapshot, unit, "handoff-audit-plan")
    assert value["phase"] == "audit"
    if damaged:
        import json
        from pathlib import Path

        row = snapshot.jobs[value["audits"][0]]
        content = json.loads(Path(row["request"]["inputs"][0]["path"]).read_text())
        coord = row["request"]["coordinates"]
        # Script only the model's comparison, consuming the actual emitted input.
        frontier = content.get("final_frontier", [{"summary": content["summary"]}])
        missing = not any(
            fact["kind"] == "owner_request"
            for child in frontier
            for fact in child["summary"]["facts"]
        )
        result = {
            "handoff_id": value["id"],
            "capture_digest": coord["capture_digest"],
            "covered_owner_ids": coord["owner_ids"],
            "covered_span_ids": [span["span_id"] for span in coord["owner_spans"]],
            "obligation_ids": coord["obligation_ids"],
            "ambiguities": ["Final synthesis omitted the leaf owner obligation"] if missing else [],
            "constraints": [
                {
                    "span_id": span["span_id"],
                    "interpretation": "Preserve requirement",
                    "relevance": "current",
                    "exclusion_evidence_id": "",
                }
                for span in coord["owner_spans"]
            ],
        }
        row.update(
            state="validated",
            validation="valid",
            result=result,
            receipt={"result": result, "provider_family": "anthropic"},
        )
        with pytest.raises(CampaignError, match="Final synthesis omitted"):
            flow.apply_audit(runtime, snapshot, row)
        assert value["phase"] == "audit"
        return
    complete_audits(runtime, snapshot, unit)
    step(runtime, snapshot, unit, "handoff-assemble")
    step(runtime, snapshot, unit, "handoff-release")
    assert value["phase"] == "ready"
    proposal = flow.resume_proposal(snapshot, unit, runtime)
    assert proposal is not None and proposal.payload["handoff_id"] == value["id"]


@pytest.mark.parametrize("change", ["foreign", "missing"])
def test_stage_request_cannot_change_quality_pool(campaign, change):
    runtime, snapshot, unit, receipt = campaign
    staged(snapshot)
    flow.intercept_terminal(runtime, snapshot, unit, receipt)
    step(runtime, snapshot, unit, "handoff-capture")
    step(runtime, snapshot, unit, "handoff-plan")
    row = snapshot.jobs[unit.evidence["handoff"]["jobs"][0]]
    request = request_from_dict(row["request"])
    assert routes.request_stage(snapshot.spec, request) == "leaf"
    request = (
        dataclasses.replace(request, candidates=snapshot.spec.candidates("handoff_audit"))
        if change == "foreign"
        else dataclasses.replace(
            request,
            coordinates={
                key: value for key, value in request.coordinates.items() if key != "summary_stage"
            },
        )
    )
    with pytest.raises(CampaignError, match="stage authority"):
        routes.request_stage(snapshot.spec, request)


def test_author_cannot_consume_the_only_independent_auditor(campaign):
    runtime, snapshot, _, _ = campaign
    staged(snapshot)
    first = snapshot.spec.candidates("summarization")[0]
    auditor = snapshot.spec.candidates("handoff_audit")[0]
    assert routes.reserve_auditor(runtime, snapshot, {"openai"}) == "anthropic"
    with pytest.raises(CampaignError, match="independent"):
        routes.reserve_auditor(runtime, snapshot, {"openai", "anthropic"})
    snapshot.spec = dataclasses.replace(
        snapshot.spec,
        role_pools=tuple(
            (name, (auditor, first) if name == "map" else items)
            for name, items in snapshot.spec.role_pools
        ),
    )
    assert (
        flow._summary_route(runtime, snapshot, "summarization", {"audit_family": "anthropic"})[0]
        == first
    )


def test_ordered_stage_selection_does_not_follow_fairness_or_model_advice(project, tmp_path):
    from test_campaign_runtime_admission import configured

    runtime, snapshot, first, second = configured(project, tmp_path)
    mapping = tuple((stage, "planners") for stage in routes.STAGES)
    snapshot.spec = dataclasses.replace(
        snapshot.spec,
        reasoning=dataclasses.replace(snapshot.spec.reasoning, summary_routes=mapping),
        roles=(*snapshot.spec.roles, ("summarization", RoleSpec(pool="planners", max_sessions=4))),
    )
    snapshot.bindings["role_selections"] = {first.profile + "\0" + first.model: 100}
    assert runtime._role(snapshot, "summarization") == second
    assert runtime._role(snapshot, "summarization", summary_stage="leaf") == first
    snapshot.bindings["capacity"]["observations"][0]["capacity"]["available"] = False
    assert runtime._role(snapshot, "summarization", summary_stage="leaf") == second


def test_legacy_reducer_proof_does_not_qualify_final_synthesis(campaign):
    runtime, snapshot, _, _ = campaign
    staged(snapshot)
    candidate = snapshot.spec.candidates("summarization")[0]
    cap = flow._cap(snapshot, candidate, "reasoning")
    from pathlib import Path

    from bmad_loop.campaign_fidelity import validate
    from bmad_loop.campaign_headless import file_digest, read_json, write_json

    proof = Path(cap["semantic_qualification"]["path"])
    value = read_json(proof)
    receipt = Path(value["independent_review"]["native_receipt_path"])
    native = read_json(receipt)
    del native["result"]["semantic_contract_digests"]["handoff_final"]
    write_json(receipt, native)
    value["independent_review"]["sha256"] = file_digest(receipt)
    write_json(proof, value)
    cap["semantic_qualification"]["sha256"] = file_digest(proof)
    validate(cap, flow.AUDIT_SCHEMA, candidate=candidate, purpose="handoff_reduce")
    with pytest.raises(CampaignError, match="semantic contract"):
        validate(cap, flow.AUDIT_SCHEMA, candidate=candidate, purpose="handoff_final")


def test_audit_reservation_checks_all_production_stages(campaign):
    _, snapshot, _, _ = campaign
    first = snapshot.spec.candidates("summarization")
    second = snapshot.spec.candidates("handoff_audit")
    options = {
        stage: second if role == "handoff_audit" else first
        for stage, (role, _, _) in routes.STAGES.items()
    }
    assert routes.independent_families(options) == ("anthropic",)
    options["final"] = second
    assert routes.independent_families(options) == ()


def test_agy_default_capacity_is_unknown_without_inference(tmp_path, monkeypatch):
    from bmad_loop import campaign_capacity

    monkeypatch.setattr(
        campaign_capacity, "run_argv", lambda *a, **k: pytest.fail("AGY must not call TeamRouter")
    )
    result = campaign_capacity.observe(
        RoleCandidate("antigravity", "gemini-3.8-flash-high", "high"), tmp_path
    )
    assert not result.available and not result.known
    assert "explicit" in result.reason
