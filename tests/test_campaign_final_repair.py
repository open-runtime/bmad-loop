"""Bounded semantic repair retains native findings and requires a fresh full audit."""

import copy
import dataclasses
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
from bmad_loop.campaign_summary import SummaryJobSpec, quote_catalog
from bmad_loop.campaign_supervisor import Observation


def finish(runtime, snapshot, row, result, family, *, harvest=False):
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


def final_result(row, *, missing=False):
    spec = SummaryJobSpec(**row["request"]["coordinates"]["summary_spec"])
    catalog = quote_catalog(spec)
    identity, quote = next(iter(catalog.items()))
    return {
        "schema_version": 1,
        "job_id": row["id"],
        "input_sha256": spec.input_sha256,
        "covered_ids": list(spec.primary_ids),
        "gaps": [],
        "facts": (
            []
            if missing
            else [
                {
                    "kind": "owner_request",
                    "text": "Keep history and all acceptance criteria.",
                    "source_quote_id": identity,
                    "citations": [quote["event_id"]],
                }
            ]
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
            ["The final omitted the owner requirement; do not infer approval"] if ambiguity else []
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
        invalid = final_result(final)
        invalid["facts"][0]["text"] = "x" * (final["request"]["limits"]["output_tokens"] + 1)
        finish(runtime, snapshot, final, invalid, "openai")
        assert final["state"] == "failed" and final["failure_attempts"] == attempt + 1
    finish(runtime, snapshot, final, final_result(final, missing=True), "openai")
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
    old_request, old_receipt = copy.deepcopy(original["request"]), copy.deepcopy(
        original["receipt"]
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
        finish(runtime, snapshot, fresh, audit_result(fresh, False), "anthropic")
        assert fresh["state"] == "applied"
    step(runtime, snapshot, unit, "handoff-assemble")
    step(runtime, snapshot, unit, "handoff-release")
    assert value["phase"] == "ready" and flow.resume_proposal(snapshot, unit, runtime) is not None
    assert len(unit.evidence["handoff_final_repairs"][unit.claim.launch_id]) == 1
    assert original["request"] == old_request and original["receipt"] == old_receipt


def test_two_repairs_exhaust_without_reset_after_persistence_and_config_refresh(audited_final):
    runtime, snapshot, unit, _ = audited_final
    for attempt in (1, 2):
        reject(runtime, snapshot, unit)
        step(runtime, snapshot, unit, "handoff-final-repair")
        value = unit.evidence["handoff"]
        repair = snapshot.jobs[value["jobs"][0]]
        assert repair["request"]["coordinates"]["final_repair"]["attempt"] == attempt
        finish(runtime, snapshot, repair, final_result(repair, missing=True), "openai")
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
            finish(runtime, snapshot, row, final_result(row), "openai")
            assert row["state"] == "applied"
    step(runtime, snapshot, unit, "handoff-audit-plan")
    final = snapshot.jobs[value["jobs"][0]]
    finish(runtime, snapshot, final, final_result(final, missing=True), "openai")
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
    # Real bounded immutable input bytes, not inflated token bookkeeping.
    content["retained_finding_detail"] = "Required exact contextual detail. " * 400
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
