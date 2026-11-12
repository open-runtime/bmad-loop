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
    from bmad_loop.campaign_summary import FINAL_PROMPT_VERSION, SummaryJobSpec, load_input

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
    if damaged:
        # A model-authored leaf can contain a wrong progress claim. The closed
        # final preserves attribution but still needs independent source review.
        leaf["result"]["facts"][0]["text"] = "All work is complete."
        apply_result(runtime, snapshot, leaf)
    step(runtime, snapshot, unit, "handoff-audit-plan")
    final = snapshot.jobs[value["jobs"][0]]
    assert final["id"] != leaf["id"] and value["phase"] == "summarize"
    assert final["request"]["coordinates"]["summary_stage"] == "final"
    assert final["request"]["coordinates"]["summary_spec"]["prompt_version"] == FINAL_PROMPT_VERSION
    assert "ranked_fact_ids" in final["request"]["prompt"]
    spec = SummaryJobSpec(**final["request"]["coordinates"]["summary_spec"])
    result = {
        "schema_version": 4,
        "job_id": final["id"],
        "input_sha256": spec.input_sha256,
        "covered_ids": [leaf["id"]],
        "ranked_fact_ids": list(load_input(spec)["fact_catalog"]),
        "required_fact_ids": [],
        "excluded_fact_ids": [],
    }
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
        incorrect = any(
            "All work is complete." in fact["text"]
            for child in frontier
            for fact in child["summary"]["facts"]
        )
        result = {
            "handoff_id": value["id"],
            "capture_digest": coord["capture_digest"],
            "covered_owner_ids": coord["owner_ids"],
            "covered_span_ids": [span["span_id"] for span in coord["owner_spans"]],
            "obligation_ids": coord["obligation_ids"],
            "ambiguities": ["Final source claim contradicts unfinished work"] if incorrect else [],
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
        from bmad_loop.campaign_jobs import accept_receipt

        row["claim"] = {
            "launch_id": "final-audit-native",
            "generation": snapshot.generation,
            "quiescent": False,
        }
        accept_receipt(
            snapshot,
            row["id"],
            {
                "job_id": row["id"],
                "launch_id": row["claim"]["launch_id"],
                "generation": snapshot.generation,
                "request_digest": row["request_digest"],
                "outcome": "completed",
                "quiescent": True,
                "processes_quiescent": True,
                "input_complete": True,
                "result": result,
                "provider_family": "anthropic",
            },
        )
        assert row["state"] == "validated"
        with pytest.raises(CampaignError, match="Final source claim contradicts"):
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
    cap["semantic_qualification"]["proofs"] = [{"path": str(proof), "sha256": file_digest(proof)}]
    validate(cap, flow.AUDIT_SCHEMA, candidate=candidate, purpose="handoff_reduce")
    with pytest.raises(CampaignError, match="does not match|semantic contract"):
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


def _fill_summary(runtime, snapshot, row, *, near_limit=False):
    """Script only summary output; use the actual native-input/validation path."""
    from bmad_loop.campaign_reasoning_results import apply_result
    from bmad_loop.campaign_summary import (
        FINAL_PROMPT_VERSION,
        SummaryJobSpec,
        load_input,
    )
    from bmad_loop.campaign_transcripts import encoded

    spec = SummaryJobSpec(**row["request"]["coordinates"]["summary_spec"])
    source = load_input(spec)
    primary = source.get("primary", [])
    citations = [primary[0]["event_id"]] if primary else list(spec.event_ids[:1])
    result = {
        "schema_version": 1,
        "job_id": spec.id,
        "input_sha256": spec.input_sha256,
        "covered_ids": list(spec.primary_ids),
        "gaps": [],
        "facts": [
            {"kind": "progress", "text": "Retain the source obligations", "citations": citations}
        ],
    }
    row.update(
        state="validated",
        validation="valid",
        result=result,
        receipt={
            "job_id": row["id"],
            "result": result,
            "provider_family": "openai",
            "observed_models": ["gpt-test", "grok-4.7"],
        },
    )
    if spec.prompt_version == FINAL_PROMPT_VERSION:
        result = {
            "schema_version": 4,
            "job_id": spec.id,
            "input_sha256": spec.input_sha256,
            "covered_ids": list(spec.primary_ids),
            "ranked_fact_ids": list(source["fact_catalog"]),
            "required_fact_ids": [],
            "excluded_fact_ids": [],
        }
        row["result"] = row["receipt"]["result"] = result
        # The model ranks every fact; production Python packs the bounded view.
        apply_result(runtime, snapshot, row)
        assert flow._applied(row)["facts"]
        row["state"] = "applied"
        return spec
    apply_result(runtime, snapshot, row)
    if near_limit:
        current = len(encoded(flow._applied(row)))
        # Validator provenance is already measured; fill only the fact's text.
        result["facts"][0]["text"] += "x" * (spec.artifact_bytes - current - 32)
        apply_result(runtime, snapshot, row)
        assert spec.artifact_bytes - 64 < len(encoded(flow._applied(row))) <= spec.artifact_bytes
    row["state"] = "applied"
    return spec


def test_reserved_audit_envelope_renders_near_limit_leaves_and_bounded_final(campaign):
    import json
    from pathlib import Path

    from bmad_loop.campaign_headless import render_input
    from bmad_loop.campaign_summary import SummaryJobSpec
    from bmad_loop.campaign_transcripts import encoded

    runtime, snapshot, unit, receipt = campaign
    staged(snapshot)
    mapper = snapshot.spec.candidates("summarization")[0]
    snapshot.config_snapshot["reasoning_capabilities"][mapper.profile + ":" + mapper.model][
        "reasoning"
    ] = runtime.fixture_cap(mapper, input_tokens=131072, context_tokens=262144, output_tokens=32768)
    auditor = snapshot.spec.candidates("handoff_audit")[0]
    cap = runtime.fixture_cap(
        auditor, input_tokens=65536, context_tokens=131072, output_tokens=16384
    )
    snapshot.config_snapshot["reasoning_capabilities"][auditor.profile + ":" + auditor.model][
        "reasoning"
    ] = cap
    source = Path(receipt["sessions"][0]["transcript_path"])
    with source.open("a") as stream:
        stream.write(
            json.dumps(
                {
                    "type": "response_item",
                    "payload": {"type": "message", "role": "assistant", "content": "x" * 24000},
                }
            )
            + "\n"
        )
    flow.intercept_terminal(runtime, snapshot, unit, receipt)
    step(runtime, snapshot, unit, "handoff-capture")
    step(runtime, snapshot, unit, "handoff-plan")
    value = unit.evidence["handoff"]
    leaves = list(value["jobs"])
    assert len(leaves) > 1
    source_limit, leaf_limit, final_limit = flow._audit_limits(snapshot, "anthropic")
    assert (
        max(
            snapshot.jobs[key]["request"]["coordinates"]["summary_spec"]["input_tokens"]
            for key in leaves
        )
        > source_limit * 0.8
    )
    for key in leaves:
        row = snapshot.jobs[key]
        spec = _fill_summary(runtime, snapshot, row, near_limit=True)
        assert spec.input_tokens <= source_limit and spec.artifact_bytes == leaf_limit
        assert spec.output_tokens > spec.artifact_bytes  # Native allowance is unchanged.
    for _ in range(8):
        step(runtime, snapshot, unit, "handoff-audit-plan")
        final = snapshot.jobs[value["jobs"][0]]
        if final["request"]["coordinates"]["summary_stage"] == "final":
            break
        for key in value["jobs"]:
            _fill_summary(runtime, snapshot, snapshot.jobs[key], near_limit=True)
    else:
        pytest.fail("Bounded reduction did not produce the final stage")
    assert len(value["jobs"]) == 1
    assert _fill_summary(runtime, snapshot, final, near_limit=True).artifact_bytes == final_limit
    step(runtime, snapshot, unit, "handoff-audit-plan")
    assert len(value["audits"]) >= len(leaves)
    seen = set()
    for key in value["audits"]:
        request = request_from_dict(snapshot.jobs[key]["request"])
        raw, measured = render_input(request, cap)
        assert (
            len(raw) <= cap["input_tokens"] and measured["token_upper_bound"] <= cap["input_tokens"]
        )
        content = json.loads(Path(request.inputs[0]["path"]).read_text())
        seen.update(part["id"] for part in content["history"]["primary"])
        final_summary = content["final_frontier"][0]["summary"]
        assert final_summary["facts"] and len(encoded(final_summary)) <= final_limit
        assert all(fact["selected_from"] for fact in final_summary["facts"])
    assert seen == {
        part
        for key in leaves
        for part in SummaryJobSpec(
            **snapshot.jobs[key]["request"]["coordinates"]["summary_spec"]
        ).primary_ids
    }


@pytest.mark.parametrize("state", ["applied", "failed"])
def test_all_observed_summary_contributors_reserve_independence(campaign, state):
    runtime, snapshot, unit, receipt = campaign
    staged(snapshot)
    flow.intercept_terminal(runtime, snapshot, unit, receipt)
    step(runtime, snapshot, unit, "handoff-capture")
    step(runtime, snapshot, unit, "handoff-plan")
    complete_maps(runtime, snapshot, unit)
    value = unit.evidence["handoff"]
    row = snapshot.jobs[value["jobs"][0]]
    row["state"] = state  # Retained applied artifact still contributes after interruption.
    row["receipt"]["observed_models"] = ["gpt-test", "grok-4.7"]
    assert flow._summary_authors(runtime, snapshot, value) == {"openai", "xai"}
    assert flow._summary_route(runtime, snapshot, "summarization", value)[0].profile == "codex"
    row["receipt"]["observed_models"].append("claude-test")
    with pytest.raises(flow.HandoffRouteWait, match="independent"):
        flow._summary_route(runtime, snapshot, "summarization", value)


@pytest.mark.parametrize(
    "attribution", [{}, {"observed_models": ["unknown"]}, {"observed_models": "gpt-test"}]
)
def test_missing_or_unknown_observation_does_not_guess_selected_author(campaign, attribution):
    runtime, snapshot, unit, receipt = campaign
    staged(snapshot)
    flow.intercept_terminal(runtime, snapshot, unit, receipt)
    step(runtime, snapshot, unit, "handoff-capture")
    step(runtime, snapshot, unit, "handoff-plan")
    complete_maps(runtime, snapshot, unit)
    value = unit.evidence["handoff"]
    snapshot.jobs[value["jobs"][0]]["receipt"] = attribution
    with pytest.raises(flow.HandoffRouteWait, match="unknown|malformed"):
        flow._summary_authors(runtime, snapshot, value)


def test_audit_auxiliary_cannot_share_a_summary_author(campaign):
    runtime, snapshot, unit, receipt = campaign
    flow.intercept_terminal(runtime, snapshot, unit, receipt)
    step(runtime, snapshot, unit, "handoff-capture")
    step(runtime, snapshot, unit, "handoff-plan")
    complete_maps(runtime, snapshot, unit)
    step(runtime, snapshot, unit, "handoff-audit-plan")
    complete_audits(runtime, snapshot, unit)
    row = snapshot.jobs[unit.evidence["handoff"]["audits"][0]]
    row["receipt"]["observed_models"] = ["claude-test", "gpt-test"]
    with pytest.raises(CampaignError, match="not independent"):
        flow.apply_audit(runtime, snapshot, row)


def test_reused_artifact_keeps_prior_native_contributors_after_later_attempt(campaign):
    runtime, snapshot, unit, receipt = campaign
    staged(snapshot)
    flow.intercept_terminal(runtime, snapshot, unit, receipt)
    step(runtime, snapshot, unit, "handoff-capture")
    step(runtime, snapshot, unit, "handoff-plan")
    complete_maps(runtime, snapshot, unit)
    value = unit.evidence["handoff"]
    row = snapshot.jobs[value["jobs"][0]]
    row["applied_result"]["receipt"]["observed_models"] = ["gpt-test", "claude-test"]
    row.update(
        state="failed", receipt={"provider_family": "openai", "observed_models": ["gpt-test"]}
    )
    assert flow._summary_authors(runtime, snapshot, value) == {"openai", "anthropic"}
    with pytest.raises(flow.HandoffRouteWait, match="independent"):
        flow._summary_route(runtime, snapshot, "summarization", value)


def test_complete_audit_request_is_render_checked_before_native_admission(campaign, monkeypatch):
    from bmad_loop import campaign_headless

    runtime, snapshot, unit, receipt = campaign
    flow.intercept_terminal(runtime, snapshot, unit, receipt)
    step(runtime, snapshot, unit, "handoff-capture")
    step(runtime, snapshot, unit, "handoff-plan")
    complete_maps(runtime, snapshot, unit)
    calls = []

    def reject(request, cap):
        calls.append(request)
        raise CampaignError("Rendered prompt/source/schema exceeds its input token bound")

    monkeypatch.setattr(campaign_headless, "render_input", reject)
    with pytest.raises(flow.HandoffRouteWait, match="Complete history audit envelope"):
        flow._audit_plan(runtime, snapshot, unit, unit.evidence["handoff"])
    assert calls and calls[0].purpose == "handoff_audit"
