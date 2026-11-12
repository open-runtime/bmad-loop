"""Bounded semantic repair retains native findings and requires a fresh full audit."""

import copy
import dataclasses
import hashlib
import json
from pathlib import Path

import pytest
from test_campaign_handoff_flow import campaign as campaign
from test_campaign_handoff_flow import complete_maps, step
from test_campaign_headless import prepared as prepared
from test_campaign_summary_routes import staged

from bmad_loop import campaign_handoff_flow as flow
from bmad_loop import campaign_reasoning_flow as reasoning
from bmad_loop.campaign_jobs import accept_receipt, prepare, request_from_dict
from bmad_loop.campaign_model import CampaignError, OutboxAction
from bmad_loop.campaign_summary import SummaryJobSpec, load_input, quote_catalog
from bmad_loop.campaign_supervisor import Observation


def finish(runtime, snapshot, row, result, family, *, harvest=False, output_tokens=None):
    row["attempt"] += 1
    row["claim"] = {
        "launch_id": "native-" + row["id"],
        "generation": snapshot.generation,
        "attempt": row["attempt"],
        "quiescent": False,
        "status": "running",
    }
    row["receipt"] = None
    receipt = {
        "job_id": row["id"],
        "launch_id": row["claim"]["launch_id"],
        "generation": snapshot.generation,
        "request_digest": row["request_digest"],
        "outcome": "completed",
        "quiescent": True,
        "input_complete": True,
        "processes_quiescent": True,
        "result": result,
        "provider_family": family,
        "usage": {},
        "turns": 1,
    }
    if output_tokens is not None:
        receipt.update(usage_known=True, usage={"output_tokens": output_tokens})
    accept_receipt(snapshot, row["id"], receipt, now=runtime.clock())
    assert row["state"] == "validated", row.get("reason")
    apply = OutboxAction(
        "apply-" + row["id"],
        "reasoning-apply",
        row["request"]["unit"],
        {"job_id": row["id"]},
        snapshot.generation,
    )
    reasoning.prepare_effect(runtime, snapshot, apply)
    if harvest:
        from concurrent.futures import Future

        from bmad_loop.campaign_runtime import CampaignRuntime
        from bmad_loop.campaign_supervisor import CampaignSupervisor

        # The real supervisor invokes runtime.apply before confirming this
        # pending outbox action. A read-only rejection must not self-block.
        apply.state = "sent_unknown"
        snapshot.outbox.append(apply)
        driver = CampaignRuntime(
            Path(snapshot.units[row["request"]["unit"]].bindings["workspace"]),
            runtime.store,
            clock=runtime.clock,
        )
        supervisor = CampaignSupervisor(runtime.store, driver, clock=runtime.clock)
        future = Future()
        future.set_result(Observation("confirmed", {}))
        supervisor._active[apply.id] = future
        snapshot = supervisor._harvest(snapshot)
        assert next(a for a in snapshot.outbox if a.id == apply.id).state == "confirmed"
        return snapshot
    reasoning.apply(runtime, snapshot, apply, Observation("confirmed", {}))
    return snapshot


def final_result(row, *, reverse=False):
    spec = SummaryJobSpec(**row["request"]["coordinates"]["summary_spec"])
    return {
        "schema_version": 4,
        "job_id": row["id"],
        "input_sha256": spec.input_sha256,
        "required_fact_ids": list(load_input(spec)["fact_catalog"]),
        "excluded_fact_ids": [],
        "covered_ids": list(spec.primary_ids),
        "ranked_fact_ids": (
            list(load_input(spec)["fact_catalog"])[::-1]
            if reverse
            else list(load_input(spec)["fact_catalog"])
        ),
    }


def audit_result(row, ambiguity=True):
    c = row["request"]["coordinates"]
    return {
        "handoff_id": c["handoff_id"],
        "capture_digest": c["capture_digest"],
        "covered_owner_ids": c["owner_ids"],
        "covered_span_ids": [s["span_id"] for s in c["owner_spans"]],
        "obligation_ids": c["obligation_ids"],
        "final_verdict": "passed",
        "final_findings": [],
        "constraints": [
            {
                "span_id": s["span_id"],
                "interpretation": "Keep this exact requirement",
                "relevance": "current",
                "exclusion_evidence_id": "",
            }
            for s in c["owner_spans"]
        ],
        "ambiguities": (
            ["The source claim remains ambiguous; do not infer approval"] if ambiguity else []
        ),
    }


@pytest.fixture
def audited_final(campaign, request):
    runtime, snapshot, unit, receipt = campaign
    staged(snapshot)
    snapshot.config_revision += 1
    flow.intercept_terminal(runtime, snapshot, unit, receipt)
    step(runtime, snapshot, unit, "handoff-capture")
    step(runtime, snapshot, unit, "handoff-plan")
    complete_maps(runtime, snapshot, unit)
    step(runtime, snapshot, unit, "handoff-audit-plan")
    value = unit.evidence["handoff"]
    final = snapshot.jobs[value["jobs"][0]]
    for attempt in range(getattr(request, "param", 0)):
        finish(
            runtime,
            snapshot,
            final,
            final_result(final),
            "openai",
            output_tokens=final["request"]["limits"]["output_tokens"] + 1,
        )
        assert final["state"] == "failed" and final["failure_attempts"] == attempt + 1
    finish(runtime, snapshot, final, final_result(final, reverse=True), "openai")
    assert final["state"] == "applied"
    step(runtime, snapshot, unit, "handoff-audit-plan")
    return runtime, snapshot, unit, final


def reject(runtime, snapshot, unit):
    row = snapshot.jobs[unit.evidence["handoff"]["audits"][0]]
    finish(runtime, snapshot, row, audit_result(row), "anthropic")
    assert row["state"] == "failed" and row["application"] == "rejected"
    assert unit.evidence["handoff"]["final_repair_pending"]
    return row


def test_failed_final_repairs_through_receipt_apply_reaudit_and_packet(audited_final):
    runtime, snapshot, unit, original = audited_final
    old_request, old_receipt = (
        copy.deepcopy(original["request"]),
        copy.deepcopy(original["receipt"]),
    )
    audit = reject(runtime, snapshot, unit)
    original_audit = copy.deepcopy(audit)
    assert not flow.job_launch_allowed(snapshot, audit)
    assert [p.kind for p in flow.proposals(runtime, snapshot)] == ["handoff-final-repair"]
    step(runtime, snapshot, unit, "handoff-final-repair")
    value = unit.evidence["handoff"]
    repair = snapshot.jobs[value["jobs"][0]]
    assert repair["id"] != original["id"] and value["phase"] == "summarize" and not value["audits"]
    assert original["request"] == old_request and original["receipt"] == old_receipt
    assert audit == original_audit
    assert repair["request"]["coordinates"]["summary_spec"]["correction_of"] == original["id"]
    assert original["id"] in repair["request"]["dependencies"]
    evidence = json.loads(Path(repair["request"]["inputs"][1]["path"]).read_text())
    assert evidence["rejections"][0]["finding"]["ambiguities"] == audit["result"]["ambiguities"]
    assert evidence["canonical_obligations"] == value["capture"]["obligations"]
    finish(runtime, snapshot, repair, final_result(repair), "openai")
    step(runtime, snapshot, unit, "handoff-audit-plan")
    assert set(value["audits"]).isdisjoint({audit["id"]})
    for identity in value["audits"]:
        fresh = snapshot.jobs[identity]
        assert fresh["request"]["coordinates"]["final_frontier"] == [repair["id"]]
        assert "required_fact_ids" in fresh["request"]["prompt"]
        assert "excluded_fact_ids" in fresh["request"]["prompt"]
        finish(runtime, snapshot, fresh, audit_result(fresh, False), "anthropic")
        assert fresh["state"] == "applied"
    step(runtime, snapshot, unit, "handoff-assemble")
    step(runtime, snapshot, unit, "handoff-release")
    assert value["phase"] == "ready" and flow.resume_proposal(snapshot, unit, runtime) is not None
    packet = json.loads(Path(value["packet"]["path"]).read_text())
    assert packet["history_view"] and all(row["required_context"] for row in packet["history_view"])
    assert len(unit.evidence["handoff_final_repairs"][unit.claim.launch_id]) == 1
    assert original["request"] == old_request and original["receipt"] == old_receipt


def test_resolved_source_still_rejects_bad_v6_required_claim_until_repair_and_reaudit(campaign):
    runtime, snapshot, unit, receipt = campaign
    owner_text = (
        "Preserve an explicit operator hold if one exists; this rule does not impose a hold."
    )
    pending = "The implementation still needs its pending review."
    unsupported = "The owner has imposed an active hold. Do not merge."
    source = Path(receipt["sessions"][0]["transcript_path"])
    source.write_text(
        "\n".join(
            json.dumps(record)
            for record in (
                {"type": "event_msg", "payload": {"type": "user_message", "message": owner_text}},
                {
                    "type": "response_item",
                    "payload": {"type": "message", "role": "assistant", "content": pending},
                },
            )
        )
        + "\n"
    )
    staged(snapshot)
    snapshot.config_revision += 1
    flow.intercept_terminal(runtime, snapshot, unit, receipt)
    step(runtime, snapshot, unit, "handoff-capture")
    step(runtime, snapshot, unit, "handoff-plan")
    value = unit.evidence["handoff"]
    for key in value["jobs"]:
        row = snapshot.jobs[key]
        spec = SummaryJobSpec(**row["request"]["coordinates"]["summary_spec"])
        quote_id = next(iter(quote_catalog(spec)))
        source_events = load_input(spec)["primary"]
        owner_id = next(e["event_id"] for e in source_events if e["owner_input"])
        pending_id = next(e["event_id"] for e in source_events if e["text"] == pending)
        result = {
            "schema_version": 1,
            "job_id": key,
            "input_sha256": spec.input_sha256,
            "covered_ids": list(spec.primary_ids),
            "gaps": [],
            "facts": [
                {
                    "kind": "owner_request",
                    "text": owner_text,
                    "source_quote_id": quote_id,
                    "citations": [owner_id],
                    "authority": "context",
                },
                {
                    "kind": "next_action",
                    "text": pending,
                    "source_quote_id": "",
                    "citations": [pending_id],
                    "authority": "context",
                },
                {
                    "kind": "constraint",
                    "text": unsupported,
                    "source_quote_id": "",
                    "citations": [owner_id],
                    "authority": "context",
                },
            ],
        }
        finish(runtime, snapshot, row, result, "openai")
        assert row["state"] == "applied"
    step(runtime, snapshot, unit, "handoff-audit-plan")
    original = snapshot.jobs[value["jobs"][0]]
    finish(runtime, snapshot, original, final_result(original), "openai")
    assert original["state"] == "applied"  # Closed v6 structural validity is not semantic approval.
    old_summary = flow._applied(original)
    bad_id = next(
        f["selected_from"]["catalog_id"] for f in old_summary["facts"] if unsupported in f["text"]
    )
    assert bad_id in old_summary["required_fact_ids"]
    old_final = copy.deepcopy(original)
    step(runtime, snapshot, unit, "handoff-audit-plan")
    old_audits = list(value["audits"])
    for key in old_audits:
        audit = snapshot.jobs[key]
        actual_input = json.loads(Path(audit["request"]["inputs"][0]["path"]).read_text())
        assert unsupported in json.dumps(actual_input) and pending in json.dumps(actual_input)
        assert "AS-IS" in audit["request"]["prompt"]
        result = audit_result(audit, False)
        result.update(
            final_verdict="repair_required",
            final_findings=["Required selected claim invents an active owner hold."],
        )
        for constraint in result["constraints"]:
            constraint["interpretation"] = (
                "Conditional rule; current evidence establishes no active owner hold."
            )
        finish(runtime, snapshot, audit, result, "anthropic")
        assert audit["state"] == "failed" and audit["application"] == "rejected"
    assert flow.resume_proposal(snapshot, unit, runtime) is None
    assert not value.get("packet")
    step(runtime, snapshot, unit, "handoff-final-repair")
    repair = snapshot.jobs[value["jobs"][0]]
    evidence = json.loads(Path(repair["request"]["inputs"][1]["path"]).read_text())
    assert evidence["rejections"][0]["finding"]["final_verdict"] == "repair_required"
    assert evidence["rejections"][0]["finding"]["ambiguities"] == []
    corrected = final_result(repair)
    assert bad_id in corrected["ranked_fact_ids"]  # Exact old catalog remains available.
    corrected["required_fact_ids"].remove(bad_id)
    corrected["excluded_fact_ids"] = [bad_id]
    finish(runtime, snapshot, repair, corrected, "openai")
    assert repair["state"] == "applied" and flow.resume_proposal(snapshot, unit, runtime) is None
    step(runtime, snapshot, unit, "handoff-audit-plan")
    assert set(value["audits"]).isdisjoint(old_audits)
    for key in value["audits"]:
        audit = snapshot.jobs[key]
        assert audit["request"]["coordinates"]["final_frontier"] == [repair["id"]]
        finish(runtime, snapshot, audit, audit_result(audit, False), "anthropic")
        assert audit["state"] == "applied"
    step(runtime, snapshot, unit, "handoff-assemble")
    step(runtime, snapshot, unit, "handoff-release")
    assert flow.resume_proposal(snapshot, unit, runtime) is not None
    packet = json.loads(Path(value["packet"]["path"]).read_text())
    assert pending in json.dumps(packet["history_view"])
    assert unsupported not in json.dumps(packet["history_view"])
    assert any(owner_text in row["source_quote"] for row in packet["resolved_constraints"])
    assert original == old_final
    assert len(unit.evidence["handoff_final_repairs"][unit.claim.launch_id]) == 1


@pytest.mark.parametrize("mutation", ["missing", "version", "prior-v4", "schema"])
def test_saved_final_audit_contract_change_refreshes_without_erasing_evidence(
    audited_final, mutation
):
    runtime, snapshot, unit, _ = audited_final
    value = unit.evidence["handoff"]
    for key in value["audits"]:
        row = snapshot.jobs[key]
        finish(runtime, snapshot, row, audit_result(row, False), "anthropic")
    step(runtime, snapshot, unit, "handoff-assemble")
    step(runtime, snapshot, unit, "handoff-release")
    assert flow.resume_proposal(snapshot, unit, runtime) is not None
    audits = [copy.deepcopy(flow._job(runtime, snapshot, key)) for key in value["audits"]]
    packet_path = Path(value["packet"]["path"])
    packet_bytes = packet_path.read_bytes()
    context = value["capture"]["audit_context"]
    if mutation == "missing":
        del context["final_audit_contract"]
    elif mutation in {"version", "prior-v4"}:
        context["final_audit_contract"]["prompt_version"] = (
            "history-final-audit-v4" if mutation == "prior-v4" else "history-final-audit-v3"
        )
    else:
        context["final_audit_contract"]["schema_digest"] = "obsolete"
    snapshot = runtime.store.save(snapshot, snapshot.revision)
    snapshot = runtime.store.load(snapshot.id)
    unit = snapshot.units[unit.id]
    assert flow.resume_proposal(snapshot, unit, runtime) is None
    assert not flow.job_launch_allowed(snapshot, audits[0])
    assert [p.kind for p in flow.proposals(runtime, snapshot)] == ["handoff-refresh"]
    step(runtime, snapshot, unit, "handoff-refresh")
    assert unit.evidence["handoff"]["phase"] == "capture"
    assert packet_path.read_bytes() == packet_bytes
    assert [flow._job(runtime, snapshot, row["id"]) for row in audits] == audits


@pytest.mark.parametrize(
    "mutation", ["result-fields", "frontier", "version", "prior-v4", "capture-context"]
)
def test_current_final_application_refuses_obsolete_or_stripped_metadata(audited_final, mutation):
    runtime, snapshot, unit, _ = audited_final
    value = unit.evidence["handoff"]
    row = snapshot.jobs[value["audits"][0]]
    finish(runtime, snapshot, row, audit_result(row, False), "anthropic")
    assert row["state"] == "applied"
    # The current application boundary validates even a structurally accepted
    # retained receipt; it does not rely solely on the request JSON schema.
    coordinates = row["request"]["coordinates"]
    if mutation == "result-fields":
        del row["result"]["final_verdict"]
        del row["result"]["final_findings"]
        match = "missing or inconsistent"
    elif mutation == "frontier":
        del coordinates["final_frontier"]
        match = "lacks its frontier"
    elif mutation in {"version", "prior-v4"}:
        coordinates["audit_prompt_version"] = (
            "history-final-audit-v4" if mutation == "prior-v4" else "history-final-audit-v3"
        )
        match = "Obsolete final audit"
    else:
        del value["capture"]["audit_context"]["final_audit_contract"]
        match = "contract or frontier changed"
    with pytest.raises(CampaignError, match=match):
        flow.apply_audit(runtime, snapshot, row)
    assert not value.get(
        "final_repair_pending"
    )  # Invalid authority is not repairable model feedback.


def test_final_audit_separates_controller_only_hold_from_historical_catalog(audited_final):
    from bmad_loop.campaign_model import Hold
    from bmad_loop.campaign_transcripts import digest

    runtime, snapshot, unit, final = audited_final
    value = unit.evidence["handoff"]
    original = copy.deepcopy(final)
    spec = SummaryJobSpec(**final["request"]["coordinates"]["summary_spec"])
    source = load_input(spec)
    hold = Hold("operator", "Wait for explicit resolution", id="controller-only-hold")
    unit.hold = hold
    state = flow._controller_controls(snapshot, unit)
    controls = {
        "observed_revision": snapshot.revision,
        "state": state,
        "state_digest": digest(state),
    }
    # Exercise the real planner with explicit current controller evidence. This
    # is not a native semantic outcome or permission to dispatch through a hold.
    value["capture"]["current"]["controller_controls"] = controls
    requests = flow._audit_plan(runtime, snapshot, unit, value)["requests"]
    assert requests
    assert hold.id not in json.dumps(source["children"])
    assert hold.id not in source["fact_catalog"]
    assert set(final["result"]["required_fact_ids"]) <= set(source["fact_catalog"])
    for request in requests:
        actual = json.loads(Path(request["inputs"][0]["path"]).read_text())
        assert actual["current_state"]["controller_controls"] == controls
        assert actual["final_frontier"] == [
            {"job_id": final["id"], "summary": flow._applied(final)}
        ]
        assert request["coordinates"]["audit_prompt_version"] == "history-final-audit-v5"
        prompt = request["prompt"]
        assert (
            "Current controller controls and canonical obligations are separate mandatory context"
            in prompt
        )
        assert "Absence of that duplicate historical fact is not an omission" in prompt
        assert (
            "For historical leaf-source content, every source-supported material unfinished obligation"
            in prompt
        )
        assert "contradicts, clears or weakens those controls or obligations" in prompt
    assert final == original  # No synthetic hold fact, reranking, or source rewrite.


def test_two_repairs_exhaust_without_reset_after_persistence_and_config_refresh(audited_final):
    runtime, snapshot, unit, _ = audited_final
    for attempt in (1, 2):
        reject(runtime, snapshot, unit)
        step(runtime, snapshot, unit, "handoff-final-repair")
        value = unit.evidence["handoff"]
        repair = snapshot.jobs[value["jobs"][0]]
        assert repair["request"]["coordinates"]["final_repair"]["attempt"] == attempt
        finish(runtime, snapshot, repair, final_result(repair, reverse=True), "openai")
        step(runtime, snapshot, unit, "handoff-audit-plan")
        # Ordinary SQLite save/load, retaining the actual immutable attempts.
        snapshot = runtime.store.save(snapshot, snapshot.revision)
        snapshot = runtime.store.load(snapshot.id)
        unit = snapshot.units[unit.id]
    reject(runtime, snapshot, unit)
    before = copy.deepcopy(unit.evidence["handoff_final_repairs"])
    # Actual refresh through the current canonical context must not refund credits.
    snapshot.graph_revision += 1
    snapshot.config_revision += 1
    step(runtime, snapshot, unit, "handoff-refresh")
    assert unit.evidence["handoff_final_repairs"] == before
    assert unit.evidence["handoff"]["invalidated_final_rejections"]
    # The pure repair planner must reject the retained budget independently of
    # graph/config freshness and ordinary native job attempts.
    value = unit.evidence["handoff"]
    assert len(next(iter(before.values()))) == 2
    # Re-establish the completed current frontier via normal planning/audit.
    step(runtime, snapshot, unit, "handoff-plan")
    for key in value["jobs"]:
        row = flow._job(runtime, snapshot, key)
        if row["state"] != "applied":
            # A changed configuration may require a fresh exact extraction;
            # already applied cold records are reused without reinsertion.
            assert row is snapshot.jobs[key]
            spec = SummaryJobSpec(**row["request"]["coordinates"]["summary_spec"])
            identity, quote = next(iter(quote_catalog(spec).items()))
            result = {
                "schema_version": 1,
                "job_id": row["id"],
                "input_sha256": spec.input_sha256,
                "covered_ids": list(spec.primary_ids),
                "gaps": [],
                "facts": [
                    {
                        "kind": "owner_request",
                        "text": "Keep history and all acceptance criteria.",
                        "source_quote_id": identity,
                        "citations": [quote["event_id"]],
                    }
                ],
            }
            finish(runtime, snapshot, row, result, "openai")
            assert row["state"] == "applied"
    step(runtime, snapshot, unit, "handoff-audit-plan")
    final = snapshot.jobs[value["jobs"][0]]
    finish(runtime, snapshot, final, final_result(final, reverse=True), "openai")
    step(runtime, snapshot, unit, "handoff-audit-plan")
    reject(runtime, snapshot, unit)
    jobs_before = set(snapshot.jobs)
    step(runtime, snapshot, unit, "handoff-final-repair")
    assert value["phase"] == "failed" and "two lifetime" in unit.hold.reason
    assert set(snapshot.jobs) == jobs_before and unit.evidence["handoff_final_repairs"] == before
    assert flow.resume_proposal(snapshot, unit, runtime) is None


def test_live_sibling_blocks_repair_until_exact_claim_is_quiescent(audited_final):
    runtime, snapshot, unit, _ = audited_final
    value = unit.evidence["handoff"]
    row = snapshot.jobs[value["audits"][0]]
    sibling = prepare(
        snapshot, dataclasses.replace(request_from_dict(row["request"]), id="sibling-audit")
    )
    sibling["claim"] = {
        "launch_id": "live-sibling",
        "generation": snapshot.generation,
        "attempt": 1,
        "quiescent": False,
    }
    sibling["state"] = "running"
    value["audits"].append(sibling["id"])
    reject(runtime, snapshot, unit)
    assert not flow.job_launch_allowed(snapshot, row)
    assert not any(p.kind == "handoff-final-repair" for p in flow.proposals(runtime, snapshot))
    action = OutboxAction(
        "repair", "handoff-final-repair", unit.id, {"handoff_id": value["id"]}, snapshot.generation
    )
    with pytest.raises(CampaignError, match="awaits all prior"):
        flow.perform(runtime, snapshot, action)
    sibling["claim"]["quiescent"] = True
    sibling["state"] = "failed"
    step(runtime, snapshot, unit, "handoff-final-repair")
    assert value["phase"] == "summarize" and sibling["state"] == "failed"


@pytest.mark.parametrize("damage", ["raw", "spans", "uncertain", "stale_frontier"])
def test_bad_evidence_never_becomes_semantic_repair(audited_final, damage):
    runtime, snapshot, unit, _ = audited_final
    value = unit.evidence["handoff"]
    row = snapshot.jobs[value["audits"][0]]
    result = audit_result(row)
    if damage == "raw":
        path = Path(value["capture"]["sources"][0]["raw_path"])
        path.write_bytes(path.read_bytes() + b"corrupt")
    elif damage == "spans":
        row["request"]["coordinates"]["owner_spans"][0]["text"] = "Invented owner text"
    elif damage == "uncertain":
        unit.claim.quiescent = False
    else:
        row["request"]["coordinates"]["final_frontier"] = ["foreign-final"]
    finish(runtime, snapshot, row, result, "anthropic")
    assert row["state"] == "failed"
    if damage == "raw":
        assert value["final_repair_pending"]  # retained finding is not permission
        before = set(snapshot.jobs)
        action = OutboxAction(
            "repair-corrupt",
            "handoff-final-repair",
            unit.id,
            {"handoff_id": value["id"]},
            snapshot.generation,
        )
        with pytest.raises(CampaignError, match="original native history changed"):
            flow.perform(runtime, snapshot, action)
        assert set(snapshot.jobs) == before
    else:
        assert "final_repair_pending" not in value
    assert not unit.evidence.get("handoff_final_repairs")


def test_technical_fallback_retains_complete_repair_input(audited_final):
    runtime, snapshot, unit, _ = audited_final
    reject(runtime, snapshot, unit)
    step(runtime, snapshot, unit, "handoff-final-repair")
    value = unit.evidence["handoff"]
    repair = snapshot.jobs[value["jobs"][0]]
    repair["claim"] = {
        "launch_id": "limited",
        "generation": snapshot.generation,
        "attempt": 1,
        "quiescent": True,
    }
    repair.update(
        state="failed", receipt={"outcome": "limit", "quiescent": True}, failure_attempts=1
    )
    prior_inputs = copy.deepcopy(repair["request"]["inputs"][1:])
    prior_budget = copy.deepcopy(unit.evidence["handoff_final_repairs"])
    # No route selection mutation or capacity override; this fixture has its
    # normal qualified summary route and an expired deterministic cooldown.
    runtime.clock = lambda: 1000.0
    value["plan_epoch"] = value.get("plan_epoch", 0) + 1
    response = flow._summary_replan(runtime, snapshot, unit, value)
    assert response["requests"][0]["inputs"][1:] == tuple(prior_inputs)
    assert (
        response["requests"][0]["coordinates"]["final_repair"]
        == repair["request"]["coordinates"]["final_repair"]
    )
    assert unit.evidence["handoff_final_repairs"] == prior_budget


def test_supervisor_receipt_application_records_rejection_before_own_effect_confirmation(
    audited_final,
):
    runtime, snapshot, unit, _ = audited_final
    row = snapshot.jobs[unit.evidence["handoff"]["audits"][0]]
    saved = finish(runtime, snapshot, row, audit_result(row), "anthropic", harvest=True)
    unit = saved.units[unit.id]
    assert unit.evidence["handoff"]["final_repair_pending"]
    assert saved.jobs[row["id"]]["application"] == "rejected"
    assert [p.kind for p in flow.proposals(runtime, saved)] == ["handoff-final-repair"]


def test_prepared_old_audit_defers_at_immediate_dispatch_when_repair_pending(
    audited_final, tmp_path
):
    runtime, snapshot, unit, _ = audited_final
    audit = reject(runtime, snapshot, unit)
    candidate = audit["request"]["candidates"][0]
    action = OutboxAction(
        "pending-audit-launch",
        "reasoning-launch",
        unit.id,
        {"job_id": audit["id"], "candidate": candidate},
        snapshot.generation,
    )
    snapshot.outbox.append(action)
    reasoning.prepare_effect(runtime, snapshot, action)
    snapshot = runtime.store.save(snapshot, snapshot.revision)
    row = snapshot.jobs[audit["id"]]
    path = tmp_path / "unprepared-request.json"
    observed = reasoning._launch_guard(runtime, snapshot, row, action, path)
    assert observed is not None and observed.state == "confirmed"
    assert observed.data["state"] == "deferred"
    assert "handoff" in observed.data["reason"]
    assert not path.exists()


def test_completed_receipt_without_process_quiescence_cannot_seed_repair(audited_final):
    runtime, snapshot, unit, _ = audited_final
    row = reject(runtime, snapshot, unit)
    unit.evidence["handoff"].pop("final_repair_pending")
    row["receipt"]["processes_quiescent"] = False
    with pytest.raises(CampaignError, match="exact quiescent"):
        flow.apply_audit(runtime, snapshot, row)
    assert "final_repair_pending" not in unit.evidence["handoff"]


def test_small_fallback_cannot_omit_extra_repair_evidence_from_its_budget(audited_final):
    from bmad_loop.campaign_model import RoleCandidate
    from bmad_loop.campaign_transcripts import blob, digest, encoded

    runtime, snapshot, unit, _ = audited_final
    reject(runtime, snapshot, unit)
    step(runtime, snapshot, unit, "handoff-final-repair")
    value = unit.evidence["handoff"]
    repair = snapshot.jobs[value["jobs"][0]]
    old = SummaryJobSpec(**repair["request"]["coordinates"]["summary_spec"])
    main = flow._candidate(repair["request"]["candidates"][0])
    small = RoleCandidate(main.profile, "gpt-small-fallback", main.effort, "openai")
    small_cap = runtime.fixture_cap(small, input_tokens=2 * (old.input_tokens + 16384 + 4096))
    assert old.input_tokens <= flow._budget(small, small_cap).payload_tokens
    extra = repair["request"]["inputs"][1]
    content = json.loads(Path(extra["path"]).read_text())
    # Include real immutable bytes between the two routes' payload ceilings.
    # Normalized final metadata is part of the retained evidence and may grow.
    small_budget = flow._budget(small, small_cap).payload_tokens
    main_budget = flow._budget(main, flow._cap(snapshot, main, "reasoning")).payload_tokens
    assert small_budget < main_budget
    content["retained_finding_detail"] = ""
    target = max(small_budget + 1, old.input_tokens + len(encoded(content)))
    assert target < main_budget
    content["retained_finding_detail"] = "x" * (target - old.input_tokens - len(encoded(content)))
    raw = encoded(content)
    path = blob(runtime.store.directory(snapshot.id) / "large-repair-finding", raw)
    extra.update(path=str(path), sha256=digest(content), tokens=len(raw))
    assert old.input_tokens + len(raw) > flow._budget(small, small_cap).payload_tokens
    snapshot.spec = dataclasses.replace(
        snapshot.spec,
        role_pools=tuple(
            (name, (small, *pool) if name == "map" else pool)
            for name, pool in snapshot.spec.role_pools
        ),
    )
    snapshot.config_snapshot["reasoning_capabilities"][small.profile + ":" + small.model] = {
        "reasoning": small_cap
    }
    repair.update(
        state="failed",
        receipt={"outcome": "limit", "quiescent": True},
        claim={"launch_id": "limited", "generation": snapshot.generation, "quiescent": True},
        failure_attempts=1,
    )
    value["plan_epoch"] = value.get("plan_epoch", 0) + 1
    response = flow._summary_replan(runtime, snapshot, unit, value)
    chosen = response["requests"][0]
    assert chosen["candidates"][0]["model"] == main.model
    assert chosen["inputs"][1] == extra


def test_late_sibling_findings_stale_a_planned_repair_without_spending_credit(audited_final):
    runtime, snapshot, unit, _ = audited_final
    first = reject(runtime, snapshot, unit)
    value = unit.evidence["handoff"]
    action = OutboxAction(
        "repair-before-sibling",
        "handoff-final-repair",
        unit.id,
        {"handoff_id": value["id"]},
        snapshot.generation,
    )
    observed = flow.perform(runtime, snapshot, action)
    assert observed.state == "confirmed" and observed.data["final_repair"]
    sibling = prepare(
        snapshot, dataclasses.replace(request_from_dict(first["request"]), id="later-sibling-audit")
    )
    value["audits"].append(sibling["id"])
    result = audit_result(sibling)
    result["ambiguities"] = ["A second exact unresolved finding must also survive"]
    finish(runtime, snapshot, sibling, result, "anthropic")
    original_frontier = list(value["jobs"])
    flow.apply(runtime, snapshot, action, observed)
    assert value["jobs"] == original_frontier and value["phase"] == "audit"
    assert not unit.evidence.get("handoff_final_repairs")
    assert len(value["final_repair_pending"]["findings"]) == 2
    step(runtime, snapshot, unit, "handoff-final-repair")
    row = snapshot.jobs[value["jobs"][0]]
    evidence = json.loads(Path(row["request"]["inputs"][1]["path"]).read_text())
    assert len(evidence["rejections"]) == 2
    # Two real audit jobs can report on the same exact source. Keep both
    # findings/identities while inlining the source only once.
    assert len(evidence["audited_inputs_by_sha256"]) == 1
    assert (
        evidence["rejections"][0]["audited_inputs"] == evidence["rejections"][1]["audited_inputs"]
    )
    assert len(unit.evidence["handoff_final_repairs"][unit.claim.launch_id]) == 1


@pytest.mark.parametrize("audited_final", [1, 2], indirect=True)
@pytest.mark.parametrize("cold", [False, True])
def test_applied_final_native_failure_debt_survives_semantic_repair(audited_final, cold):
    runtime, snapshot, unit, original = audited_final
    failures = original["failure_attempts"]
    assert failures in {1, 2}
    if cold:
        snapshot = runtime.store.save(snapshot, snapshot.revision)
        snapshot = runtime.store.load(snapshot.id)
        unit = snapshot.units[unit.id]
        getattr(runtime, "_handoff_job_cache", {}).clear()
        assert original["id"] not in snapshot.jobs
        assert flow._job(runtime, snapshot, original["id"])["failure_attempts"] == failures
    reject(runtime, snapshot, unit)
    step(runtime, snapshot, unit, "handoff-final-repair")
    repair = snapshot.jobs[unit.evidence["handoff"]["jobs"][0]]
    assert repair["failure_attempts"] == failures
    assert len(unit.evidence["handoff_final_repairs"][unit.claim.launch_id]) == 1


def test_all_original_raw_sources_authenticate_even_without_owner_quotes(tmp_path):
    from bmad_loop.campaign_transcripts import snapshot_history

    raw = tmp_path / "assistant.jsonl"
    raw.write_text(
        json.dumps(
            {
                "type": "response_item",
                "payload": {"type": "message", "role": "assistant", "content": "Context only"},
            }
        )
        + "\n"
    )
    history = snapshot_history(raw, "codex", "source-no-owner", tmp_path / "sealed")
    value = {"capture": {"gaps": [], "sources": [dataclasses.asdict(history)]}}
    assert flow._repair_sources(value) == (history,)
    Path(history.raw_path).write_bytes(Path(history.raw_path).read_bytes() + b"altered")
    with pytest.raises(CampaignError, match="original native history changed"):
        flow._repair_sources(value)


@pytest.mark.parametrize("damage", ["missing", "wrong_bytes"])
def test_repair_requires_exact_rejected_audit_input_before_spending_credit(audited_final, damage):
    runtime, snapshot, unit, _ = audited_final
    row = reject(runtime, snapshot, unit)
    path = Path(row["request"]["inputs"][0]["path"])
    if damage == "missing":
        path.unlink()
    else:
        content = json.loads(path.read_text())
        content["history"]["primary"][0]["text"] = "A different source, not an owner instruction"
        path.write_text(json.dumps(content))
    jobs = set(snapshot.jobs)
    before = copy.deepcopy(unit.evidence.get("handoff_final_repairs"))
    action = OutboxAction(
        "repair-input-damage",
        "handoff-final-repair",
        unit.id,
        {"handoff_id": unit.evidence["handoff"]["id"]},
        snapshot.generation,
    )
    with pytest.raises(CampaignError, match="rejected audit input"):
        flow.perform(runtime, snapshot, action)
    assert set(snapshot.jobs) == jobs
    assert unit.evidence.get("handoff_final_repairs") == before
    assert not unit.bindings.get("handoff_ready")


def test_audited_projection_retains_unknown_fields_and_refuses_stale_shared_context(audited_final):
    from bmad_loop.campaign_jobs import digest
    from bmad_loop.campaign_transcripts import blob, encoded

    runtime, snapshot, unit, _ = audited_final
    row = reject(runtime, snapshot, unit)
    descriptor = copy.deepcopy(unit.evidence["handoff"]["final_repair_pending"]["findings"][0])
    saved = flow._applied({"applied_result": descriptor})
    content = json.loads(Path(row["request"]["inputs"][0]["path"]).read_text())
    content["future_exact_context"] = {"unresolved_proposal": "Reread before replaying old writes"}
    raw = encoded(content)
    path = blob(runtime.store.directory(snapshot.id) / "future-audit-context", raw)
    content_digest = hashlib.sha256(raw).hexdigest()
    saved["request"]["inputs"][0].update(path=str(path), sha256=content_digest, tokens=len(raw))
    descriptor["request_digest"] = digest(saved["request"])
    shared = {
        key: content[key] for key in ("canonical_obligations", "current_state", "final_frontier")
    }
    projected = {}
    flow._final_repair_audit_inputs(saved, descriptor, shared, projected)
    output = projected[content_digest]
    assert {**output["content"], **{key: shared[key] for key in output["shared_fields"]}} == content
    assert output["content"]["future_exact_context"] == content["future_exact_context"]
    with pytest.raises(CampaignError, match="rejected audit request changed"):
        flow._final_repair_audit_inputs(
            saved, {**descriptor, "request_digest": "0" * 64}, shared, {}
        )
    with pytest.raises(CampaignError, match="context is stale: current_state"):
        flow._final_repair_audit_inputs(saved, descriptor, {**shared, "current_state": {}}, {})


def test_initial_repair_route_pays_for_complete_rejected_audit_context(audited_final):
    from bmad_loop.campaign_model import RoleCandidate
    from bmad_loop.campaign_summary import load_input
    from bmad_loop.campaign_transcripts import encoded

    runtime, snapshot, unit, original = audited_final
    reject(runtime, snapshot, unit)
    value = unit.evidence["handoff"]
    response = flow._final_repair(runtime, snapshot, unit, value)
    repair = response["final_repair"]
    evidence = json.loads(Path(repair["inputs"][1]["path"]).read_text())
    old = SummaryJobSpec(**original["request"]["coordinates"]["summary_spec"])
    assert load_input(old)
    full = old.input_tokens + len(encoded(evidence))
    without_context = old.input_tokens + len(
        encoded(
            {
                **evidence,
                "audited_inputs_by_sha256": {},
            }
        )
    )
    assert without_context < full
    main = flow._candidate(repair["candidates"][0])
    small = RoleCandidate(main.profile, "gpt-small-audit-context", main.effort, "openai")
    # The synthetic canary reserves4096 proposal bytes before constructing its
    # actual input. The earned bound below, not the proposal, selects the route.
    small_cap = runtime.fixture_cap(small, input_tokens=2 * (without_context + 16384) + 4096)
    assert without_context <= flow._budget(small, small_cap).payload_tokens < full
    snapshot.spec = dataclasses.replace(
        snapshot.spec,
        role_pools=tuple(
            (name, (small, *pool) if name == "map" else pool)
            for name, pool in snapshot.spec.role_pools
        ),
    )
    snapshot.config_snapshot["reasoning_capabilities"][small.profile + ":" + small.model] = {
        "reasoning": small_cap,
    }
    response = flow._final_repair(runtime, snapshot, unit, value)
    assert response["final_repair"]["candidates"][0]["model"] == main.model
    assert not unit.evidence.get("handoff_final_repairs")  # planning spends no credit


def test_nonowner_caution_is_inlined_for_repair_then_fresh_audit_and_release(campaign):
    from bmad_loop.campaign_headless import render_input
    from bmad_loop.campaign_summary import load_input

    runtime, snapshot, unit, receipt = campaign
    # Synthetic equivalent of a historical partial receipt and an agent's
    # caution; neither is a new owner hold on the current successor.
    exact_context = "Historical tool receipt: phase partial; the predecessor preserved staged and untracked work."
    historical_proposal = "Agent proposal: reread the old receipt before replaying its writes; no new owner duty is established."
    source = Path(receipt["sessions"][0]["transcript_path"])
    source.write_text(
        "\n".join(
            json.dumps(
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "assistant",
                        "content": text,
                    },
                }
            )
            for text in (exact_context, historical_proposal)
        )
        + "\n"
    )
    staged(snapshot)
    snapshot.config_revision += 1
    flow.intercept_terminal(runtime, snapshot, unit, receipt)
    step(runtime, snapshot, unit, "handoff-capture")
    step(runtime, snapshot, unit, "handoff-plan")
    value = unit.evidence["handoff"]

    def contextual_result(row, text):
        spec = SummaryJobSpec(**row["request"]["coordinates"]["summary_spec"])
        assert quote_catalog(spec) == {}
        return {
            "schema_version": 1,
            "job_id": row["id"],
            "input_sha256": spec.input_sha256,
            "covered_ids": list(spec.primary_ids),
            "gaps": [],
            "facts": [
                {
                    "kind": "next_action",
                    "text": text,
                    "source_quote_id": "",
                    "citations": list(spec.event_ids),
                    "authority": "context",
                }
            ],
        }

    for key in value["jobs"]:
        row = snapshot.jobs[key]
        finish(runtime, snapshot, row, contextual_result(row, historical_proposal), "openai")
        assert row["state"] == "applied"
    step(runtime, snapshot, unit, "handoff-audit-plan")
    original = snapshot.jobs[value["jobs"][0]]
    # Full ranking cannot restore contextual nuance absent from the leaf claim.
    # The scripted independent verdict below exercises bounded repair and source retention.
    finish(runtime, snapshot, original, final_result(original, reverse=True), "openai")
    assert original["state"] == "applied"  # Selection needs an independent semantic audit.
    old_input = load_input(SummaryJobSpec(**original["request"]["coordinates"]["summary_spec"]))
    assert exact_context not in json.dumps(old_input)
    step(runtime, snapshot, unit, "handoff-audit-plan")
    old_audits = list(value["audits"])
    for key in old_audits:
        audit = snapshot.jobs[key]
        result = audit_result(audit)
        assert result["covered_owner_ids"] == result["covered_span_ids"] == []
        result["ambiguities"] = [
            "The final omitted the historical caution needed to interpret the partial receipt."
        ]
        finish(runtime, snapshot, audit, result, "anthropic")
        assert audit["state"] == "failed"
    assert not unit.bindings.get("handoff_ready") and not value.get("packet")
    step(runtime, snapshot, unit, "handoff-final-repair")
    repair = snapshot.jobs[value["jobs"][0]]
    evidence = json.loads(Path(repair["request"]["inputs"][1]["path"]).read_text())
    inline = evidence["audited_inputs_by_sha256"]
    texts = [
        fragment["text"]
        for row in inline.values()
        for fragment in row["content"]["history"]["primary"]
    ]
    assert exact_context in texts and historical_proposal in texts
    rendered, _ = render_input(
        request_from_dict(repair["request"]),
        flow._cap(snapshot, flow._candidate(repair["request"]["candidates"][0]), "reasoning"),
    )
    assert exact_context.encode() in rendered and historical_proposal.encode() in rendered
    for key in old_audits:
        source = snapshot.jobs[key]["request"]["inputs"][0]
        original_audit_input = json.loads(Path(source["path"]).read_text())
        projected = inline[source["sha256"]]
        assert projected["content"]["summary"] == original_audit_input["summary"]
        assert projected["content"]["owner_spans"] == []
        # Reconstruct every original field: deduplication loses no semantics.
        restored = dict(projected["content"])
        for name in projected["shared_fields"]:
            reference = evidence["audited_input_shared_fields"][name]
            restored[name] = (
                [
                    {"job_id": item["job_id"], "summary": evidence[item["summary_evidence_field"]]}
                    for item in reference
                ]
                if name == "final_frontier"
                else evidence[reference["evidence_field"]]
            )
        assert restored == original_audit_input
    assert evidence["audited_input_shared_fields"]["final_frontier"] == [
        {"job_id": original["id"], "summary_evidence_field": "previous_final"}
    ]
    corrected = final_result(repair)
    finish(runtime, snapshot, repair, corrected, "openai")
    assert repair["state"] == "applied"
    restored_summary = flow._applied(repair)
    assert (
        restored_summary["facts"][0]["selected_from"]["presentation"] == "historical-summary-claim"
    )
    assert json.dumps(historical_proposal) in restored_summary["facts"][0]["text"]
    step(runtime, snapshot, unit, "handoff-audit-plan")
    assert set(value["audits"]).isdisjoint(old_audits)
    for key in value["audits"]:
        audit = snapshot.jobs[key]
        assert audit["request"]["coordinates"]["final_frontier"] == [repair["id"]]
        finish(runtime, snapshot, audit, audit_result(audit, False), "anthropic")
        assert audit["state"] == "applied"
    step(runtime, snapshot, unit, "handoff-assemble")
    step(runtime, snapshot, unit, "handoff-release")
    assert value["phase"] == "ready" and flow.resume_proposal(snapshot, unit, runtime) is not None
    assert len(unit.evidence["handoff_final_repairs"][unit.claim.launch_id]) == 1
