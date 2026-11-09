"""Stored evidence and the initial successor view have independent byte bounds."""

import copy
import dataclasses
import hashlib
import json
from pathlib import Path

import pytest
from test_campaign_handoff_context import budget, capture, native_row, result
from test_campaign_handoff_flow import campaign as campaign
from test_campaign_handoff_flow import (
    complete_audits,
    complete_maps,
    step,
)
from test_campaign_headless import prepared as prepared

from bmad_loop import campaign_handoff_context as context
from bmad_loop import campaign_handoff_flow as flow
from bmad_loop.campaign_model import CampaignError
from bmad_loop.campaign_reasoning_results import apply_result
from bmad_loop.campaign_summary import SummaryResultRejected, plan_maps, validate_summary
from bmad_loop.campaign_transcripts import blob, encoded, load_events


def audited(campaign):
    runtime, snapshot, unit, receipt = campaign
    snapshot.spec = dataclasses.replace(
        snapshot.spec,
        reasoning=dataclasses.replace(snapshot.spec.reasoning, packet_token_limit=12000),
    )
    snapshot.config_revision += 1
    assert flow.intercept_terminal(runtime, snapshot, unit, receipt)
    step(runtime, snapshot, unit, "handoff-capture")
    step(runtime, snapshot, unit, "handoff-plan")
    complete_maps(runtime, snapshot, unit)
    value = unit.evidence["handoff"]
    row = snapshot.jobs[value["jobs"][0]]
    event = row["result"]["facts"][0]["citations"][0]
    row["result"]["facts"].extend(
        [
            {"kind": "progress", "text": "Retained detail " * 1000, "citations": [event]},
            {
                "kind": "next_action",
                "text": "Finish PR 17 on the current branch.",
                "citations": [event],
            },
        ]
    )
    row["receipt"].update(
        outcome="completed",
        quiescent=True,
        input_complete=True,
        usage_known=True,
        usage={"output_tokens": 1000},
    )
    apply_result(runtime, snapshot, row)
    assert len(encoded(flow._applied(row))) > 12000
    before = dict(row["applied_result"])
    step(runtime, snapshot, unit, "handoff-audit-plan")
    assert value["phase"] == "audit"  # A complete retained root needs no repeated compaction.
    complete_audits(runtime, snapshot, unit)
    return runtime, snapshot, unit, row, before


def test_normal_audits_select_useful_history_and_continue_with_full_evidence(campaign):
    runtime, snapshot, unit, row, before = audited(campaign)
    step(runtime, snapshot, unit, "handoff-assemble")
    step(runtime, snapshot, unit, "handoff-release")
    value = unit.evidence["handoff"]
    payload = json.loads(Path(value["packet"]["path"]).read_text(encoding="utf-8"))
    assert value["packet"]["tokens"] <= 12000
    assert payload["obligations"] == list(value["capture"]["obligations"])
    assert (
        payload["resolved_constraints"][0]["source_quote"]
        == "Keep history and all acceptance criteria."
    )
    assert payload["history_view"][0]["kind"] == "next_action"
    assert payload["summaries"] == []
    assert payload["omitted_history_fact_index"]["count"] > 0
    assert context.retrieve_summary(payload, row["id"]) == flow._applied(row)
    omitted = json.loads(
        Path(payload["omitted_history_fact_index"]["path"]).read_text(encoding="utf-8")
    )
    ref = omitted["omitted"][0]
    assert context.retrieve_history_fact(payload, ref["summary_id"], ref["fact_id"])[
        "text"
    ].startswith("Retained detail")
    assert row["applied_result"] == before
    runtime.store.save(snapshot, snapshot.revision)
    snapshot = runtime.store.load(snapshot.id)
    unit = snapshot.units[unit.id]
    proposed = flow.resume_proposal(snapshot, unit)
    flow.decorate_launch(snapshot, unit, proposed.payload)
    assert proposed.payload["handoff_packet"]["read_only"] is False


@pytest.mark.parametrize("fault", ["audit-artifact", "resolved-constraints", "authority-index"])
def test_final_consumption_rechecks_exact_mandatory_authority(campaign, fault):
    runtime, snapshot, unit, _, _ = audited(campaign)
    step(runtime, snapshot, unit, "handoff-assemble")
    step(runtime, snapshot, unit, "handoff-release")
    value = unit.evidence["handoff"]
    payload = json.loads(Path(value["packet"]["path"]).read_text(encoding="utf-8"))
    if fault == "audit-artifact":
        Path(payload["current_constraint_audits"][0]["path"]).write_text("{}", encoding="utf-8")
        message = "authority evidence changed"
    elif fault == "authority-index":
        Path(payload["authority_provenance"]["path"]).write_text("{}", encoding="utf-8")
        message = "index or artifact changed"
    else:
        payload["resolved_constraints"] = []
        data = encoded(payload)
        path = blob(Path(value["packet"]["path"]).parent, data)
        value["packet"].update(
            path=str(path), sha256=hashlib.sha256(data).hexdigest(), tokens=len(data)
        )
        message = "mandatory context"
    with pytest.raises(CampaignError, match=message):
        flow.decorate_launch(snapshot, unit, flow.resume_proposal(snapshot, unit).payload)


def test_persisted_legacy_ready_packet_refreshes_and_resumes_without_new_debt(campaign):
    runtime, snapshot, unit, _, _ = audited(campaign)
    step(runtime, snapshot, unit, "handoff-assemble")
    step(runtime, snapshot, unit, "handoff-release")
    value = unit.evidence["handoff"]
    value["packet"].pop("mandatory_context_sha256")
    debt = copy.deepcopy(value.get("failure_ledger", {}))
    runtime.store.save(snapshot, snapshot.revision)
    snapshot = runtime.store.load(snapshot.id)
    unit = snapshot.units[unit.id]
    assert flow.resume_proposal(snapshot, unit, runtime) is None
    assert [proposal.kind for proposal in flow.proposals(runtime, snapshot)] == ["handoff-refresh"]
    step(runtime, snapshot, unit, "handoff-refresh")
    step(runtime, snapshot, unit, "handoff-plan")
    # Current-contract maps remain reusable; only fresh audited assembly earns
    # the absent authority binding, never the mutable old packet itself.
    if any(
        flow._job(runtime, snapshot, key)["state"] != "applied"
        for key in unit.evidence["handoff"]["jobs"]
    ):
        complete_maps(runtime, snapshot, unit)
    step(runtime, snapshot, unit, "handoff-audit-plan")
    complete_audits(runtime, snapshot, unit)
    step(runtime, snapshot, unit, "handoff-assemble")
    step(runtime, snapshot, unit, "handoff-release")
    assert unit.evidence["handoff"].get("failure_ledger", {}) == debt
    assert unit.evidence["handoff"]["packet"]["mandatory_context_sha256"]
    flow.decorate_launch(snapshot, unit, flow.resume_proposal(snapshot, unit).payload)


def test_legacy_continuing_writer_drains_naturally_before_a_fresh_handoff(campaign):
    runtime, snapshot, unit, _, _ = audited(campaign)
    step(runtime, snapshot, unit, "handoff-assemble")
    step(runtime, snapshot, unit, "handoff-release")
    old = unit.evidence["handoff"]
    request = flow.resume_proposal(snapshot, unit).payload
    claim = dataclasses.replace(unit.claim, launch_id="already-running", quiescent=False)
    flow.adopt_launch(snapshot, unit, request, claim)
    unit.claim = claim
    snapshot.outbox.append(
        dataclasses.replace(
            snapshot.outbox[0], id=claim.launch_id, payload=dict(request), state="confirmed"
        )
    )
    old["packet"].pop("mandatory_context_sha256")
    runtime.store.save(snapshot, snapshot.revision)
    snapshot = runtime.store.load(snapshot.id)
    unit = snapshot.units[unit.id]
    assert not flow.proposals(runtime, snapshot)
    assert not unit.claim.quiescent and unit.claim.launch_id == "already-running"
    unit.claim.quiescent = True  # Ordinary native observation, not forced retirement.
    assert flow.intercept_terminal(runtime, snapshot, unit, campaign[3])
    fresh = unit.evidence["handoff"]
    assert fresh["id"] != old["id"] and fresh["phase"] == "drain"
    assert fresh["predecessor"]["launch_id"] == "already-running"
    assert unit.evidence["handoff_history"][-1]["id"] == old["id"]


@pytest.mark.parametrize(
    "fault",
    [
        "fact",
        "obligation",
        "omission",
        "version",
        "bool-version",
        "float-version",
        "legacy-downgrade",
        "owner-direction",
    ],
)
def test_final_successor_consumption_rejects_invented_view_or_missing_authority(campaign, fault):
    runtime, snapshot, unit, _, _ = audited(campaign)
    step(runtime, snapshot, unit, "handoff-assemble")
    step(runtime, snapshot, unit, "handoff-release")
    value = unit.evidence["handoff"]
    payload = json.loads(Path(value["packet"]["path"]).read_text(encoding="utf-8"))
    if fault == "fact":
        payload["history_view"][0]["text"] = "The owner waived all acceptance."
        message = "immutable parent fact"
    elif fault == "obligation":
        payload["obligations"] = []
        message = "canonical obligations"
    elif fault == "omission":
        ref = payload["omitted_history_fact_index"]
        wrong = encoded({"schema_version": 1, "omitted": []})
        path = blob(Path(ref["path"]).parent, wrong)
        payload["omitted_history_fact_index"] = {
            "path": str(path),
            "sha256": hashlib.sha256(wrong).hexdigest(),
            "count": 0,
        }
        message = "omission coverage"
    else:
        if fault == "owner-direction":
            payload["latest_owner_direction"] = [{"quote": "New unaudited instruction"}]
        elif fault == "legacy-downgrade":
            for field in ("history_view_version", "history_view", "omitted_history_fact_index"):
                payload.pop(field)
        elif fault == "version":
            payload.pop("history_view_version")
        else:
            payload["history_view_version"] = True if fault == "bool-version" else 1.0
        message = "view version|Partial history"
    data = encoded(payload)
    path = blob(Path(value["packet"]["path"]).parent, data)
    # The outer packet hash is internally consistent: only the specific final
    # fact/authority validation can reject this corrupted persisted projection.
    value["packet"].update(
        path=str(path), sha256=hashlib.sha256(data).hexdigest(), tokens=len(data)
    )
    proposed = flow.resume_proposal(snapshot, unit)
    with pytest.raises(CampaignError, match=message):
        flow.decorate_launch(snapshot, unit, proposed.payload)


def test_canonical_obligation_overflow_cannot_become_owner_constraint_compression(campaign):
    runtime, snapshot, unit, _, _ = audited(campaign)
    value = unit.evidence["handoff"]
    value["capture"]["obligations"][0]["criterion"] = "Mandatory canonical requirement. " * 1000
    with pytest.raises(CampaignError, match="Canonical obligations"):
        step(runtime, snapshot, unit, "handoff-assemble")
    assert value["phase"] == "audit" and unit.hold is not None


def history_packet(tmp_path, fact_count=1):
    history = capture(tmp_path, [native_row("codex")])
    events = load_events(history)
    spec = plan_maps(events, budget(), tmp_path / "maps")[0]
    raw = result(spec, events)
    raw["facts"] = [
        {"kind": "next_action", "text": f"Finish step {i}.", "citations": [events[0].id]}
        for i in range(fact_count)
    ]
    full = validate_summary(spec, raw, events, observed_output_tokens=1000)
    packet = context.assemble_packet(
        (history,), (), {"pr": 17}, (full,), budget(), tmp_path / "packet"
    )
    return json.loads(Path(packet.path).read_text(encoding="utf-8")), full


def test_exact_serialized_boundary_and_no_useful_fact_are_not_empty_success(tmp_path):
    original, _ = history_packet(tmp_path)
    fitted = copy.deepcopy(original)
    context.fit_audited_history(fitted, tmp_path / "packet", ceiling=10000)
    exact = len(encoded(fitted))
    again = copy.deepcopy(original)
    context.fit_audited_history(again, tmp_path / "packet", ceiling=exact)
    assert len(encoded(again)) == exact and again["history_view"]
    with pytest.raises(context.HistoryContextRecoveryNeeded, match="useful complete"):
        context.fit_audited_history(copy.deepcopy(original), tmp_path / "packet", ceiling=exact - 1)


def test_large_frontier_hashes_omission_index_once_and_keeps_a_bounded_view(tmp_path, monkeypatch):
    payload, full = history_packet(tmp_path, 10000)
    original = context._fact_omissions
    counts = []

    def observe(facts, selected):
        counts.append(len(facts))
        return original(facts, selected)

    monkeypatch.setattr(context, "_fact_omissions", observe)
    context.fit_audited_history(payload, tmp_path / "packet", ceiling=8000)
    assert len(encoded(payload)) <= 8000 and payload["history_view"]
    assert payload["omitted_history_fact_index"]["count"] > 9900
    assert counts == [10000, 10000]  # Construction, then independent validation.
    assert context.retrieve_summary(payload, full["job_id"]) == full


def test_failed_old_artifact_limit_is_not_reinterpreted_as_selected_view(tmp_path):
    history = capture(tmp_path, [native_row("codex")])
    events = load_events(history)
    spec = dataclasses.replace(
        plan_maps(events, budget(), tmp_path / "maps")[0], artifact_bytes=400
    )
    raw = result(spec, events)
    before = encoded(raw)
    with pytest.raises(SummaryResultRejected, match="artifact exceeds"):
        validate_summary(spec, raw, events, observed_output_tokens=1000)
    assert encoded(raw) == before and spec.artifact_bytes == 400


def test_view_metadata_overflow_reaches_existing_owner_constraint_recovery(tmp_path):
    payload, _ = history_packet(tmp_path)
    payload["summaries"] = []
    payload["latest_owner_direction"] = []
    payload["resolved_constraints"] = [{"span_id": "exact-owner", "source_quote": "x" * 3000}]
    old_size = len(encoded(payload))
    with pytest.raises(context.AuditedConstraintsTooLarge):
        context.fit_audited_history(payload, tmp_path / "packet", ceiling=old_size)


@pytest.mark.parametrize("reason", ["no-observed-shrink", "eight-round-limit"])
def test_bounded_compaction_fallback_still_schedules_real_audits(campaign, reason):
    from test_campaign_handoff_reducer_retry import apply_completed, reducing
    from test_campaign_handoff_retry import dispatch

    from bmad_loop import campaign_reasoning_flow as reasoning

    runtime, snapshot, unit, maps = reducing(campaign)
    value = unit.evidence["handoff"]
    assert value["reduction_frontier"]["nodes"] == len(maps)
    for _ in list(value["jobs"]):
        apply_completed(
            runtime, snapshot, dispatch(reasoning, runtime, snapshot, "reasoning-launch"), 800
        )
    if reason == "eight-round-limit":
        value["reduction_level"] = 8
    else:
        value["reduction_frontier"] = {"bytes": 1, "nodes": 1}
    before = list(value["all_jobs"])
    dispatch(flow, runtime, snapshot, "handoff-audit-plan")
    assert value["phase"] == "audit" and value["audits"]
    assert value["history_compaction"]["reason"] == reason
    assert value["all_jobs"] == before
    assert all(
        snapshot.jobs[key]["request"]["purpose"] == "handoff_audit" for key in value["audits"]
    )


def test_compaction_fallback_cannot_skip_unapplied_children(campaign):
    runtime, snapshot, unit, _, _ = audited(campaign)
    value = unit.evidence["handoff"]
    snapshot.jobs[value["jobs"][0]]["state"] = "failed"
    value["reduction_level"] = 8
    with pytest.raises(CampaignError, match="complete applied frontier"):
        flow._reduce(runtime, snapshot, unit, value)
