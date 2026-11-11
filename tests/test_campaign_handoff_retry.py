"""Deferred continuation plans retain failure identity through restart and expiry."""

import dataclasses
from pathlib import Path

import pytest
from test_campaign_handoff_flow import campaign as campaign
from test_campaign_handoff_flow import complete_audits, complete_maps
from test_campaign_headless import prepared as prepared

from bmad_loop import campaign_handoff_flow as handoff
from bmad_loop import campaign_reasoning_flow as reasoning
from bmad_loop.campaign_model import OutboxAction
from bmad_loop.campaign_supervisor import Observation


def dispatch(module, runtime, snapshot, kind):
    proposed = next(item for item in module.proposals(runtime, snapshot) if item.kind == kind)
    action = OutboxAction(
        f"{kind}-{len(snapshot.events)}", kind, proposed.unit, proposed.payload, snapshot.generation
    )
    if module is reasoning:
        reasoning.prepare_effect(runtime, snapshot, action)
    else:
        observed = handoff.perform(runtime, snapshot, action)
        assert observed.state == "confirmed", observed
        handoff.apply(runtime, snapshot, action, observed)
        runtime.last_handoff_observation = observed
    return action


def finish(runtime, snapshot, action, outcome, result=None, **telemetry):
    row = snapshot.jobs[action.payload["job_id"]]
    receipt = {
        "job_id": row["id"],
        "request_digest": row["request_digest"],
        "launch_id": row["claim"]["launch_id"],
        "generation": row["claim"]["generation"],
        "quiescent": True,
        "outcome": outcome,
        "input_complete": True,
        "result": result,
        "provider_family": "anthropic" if row["selection"]["profile"] == "claude" else "openai",
        "usage": {"output_tokens": 10, "cost_usd": 0},
        **telemetry,
    }
    observed = Observation("confirmed", {"state": "finished", "receipt": receipt})
    reasoning.apply(runtime, snapshot, action, observed)
    reasoning.apply(runtime, snapshot, action, observed)  # lost ACK replay is idempotent
    return row


def start(campaign):
    runtime, snapshot, unit, receipt = campaign
    runtime._role = lambda snapshot, role, **kwargs: kwargs["allowed_candidates"][0]
    handoff.intercept_terminal(runtime, snapshot, unit, receipt)
    dispatch(handoff, runtime, snapshot, "handoff-capture")
    dispatch(handoff, runtime, snapshot, "handoff-plan")
    return runtime, snapshot, unit


def valid_result(row):
    coordinates = row["request"]["coordinates"]
    if row["request"]["purpose"] == "handoff_map":
        from bmad_loop.campaign_summary import SummaryJobSpec, quote_catalog

        spec = SummaryJobSpec(**coordinates["summary_spec"])
        quote_id, quote = next(iter(quote_catalog(spec).items()))
        return {
            "schema_version": 1,
            "job_id": row["id"],
            "input_sha256": spec.input_sha256,
            "covered_ids": list(spec.primary_ids),
            "gaps": [],
            "facts": [
                {
                    "kind": "owner_request",
                    "text": "Keep history and acceptance",
                    "source_quote_id": quote_id,
                    "citations": [quote["event_id"]],
                }
            ],
        }
    return {
        "handoff_id": coordinates["handoff_id"],
        "capture_digest": coordinates["capture_digest"],
        "covered_owner_ids": coordinates["owner_ids"],
        "covered_span_ids": [span["span_id"] for span in coordinates["owner_spans"]],
        "obligation_ids": coordinates["obligation_ids"],
        "ambiguities": [],
        "constraints": [
            {
                "span_id": span["span_id"],
                "interpretation": "Preserve this requirement",
                "relevance": "current",
                "exclusion_evidence_id": "",
            }
            for span in coordinates["owner_spans"]
        ],
    }


@pytest.mark.parametrize("phase", ["summarize", "audit"])
@pytest.mark.parametrize("legacy_timestamp", [False, True])
def test_single_route_failure_cools_once_and_continues_after_restart(
    campaign, phase, legacy_timestamp
):
    runtime, snapshot, unit = start(campaign)
    if phase == "audit":
        complete_maps(runtime, snapshot, unit)
        dispatch(handoff, runtime, snapshot, "handoff-audit-plan")
    launch = dispatch(reasoning, runtime, snapshot, "reasoning-launch")
    failed = finish(runtime, snapshot, launch, "limit")
    if legacy_timestamp:
        failed.pop("updated_at")  # persisted attempts predating controller timestamps
    retry_kind = "handoff-replan" if phase == "summarize" else "handoff-audit-replan"
    retried = dispatch(handoff, runtime, snapshot, retry_kind)
    observation = runtime.last_handoff_observation
    epoch = unit.evidence["handoff"].get("plan_epoch", 0)
    handoff.apply(runtime, snapshot, retried, observation)
    assert unit.evidence["handoff"].get("plan_epoch", 0) == epoch
    value = unit.evidence["handoff"]
    route = failed["selection"]["profile"] + ":" + failed["selection"]["model"]
    assert value["route_cooldowns"][route] == 160
    assert set(value["failure_ledger"].values()) == {1}
    events = dict(value["route_cooldown_events"])
    assert not handoff.proposals(runtime, snapshot)
    assert not reasoning.proposals(runtime, snapshot)

    runtime.store.save(snapshot, snapshot.revision)
    snapshot = runtime.store.load(snapshot.id)
    unit = snapshot.units[unit.id]
    runtime.clock = lambda: 130.0
    dispatch(handoff, runtime, snapshot, retry_kind)
    assert unit.evidence["handoff"]["route_cooldowns"][route] == 160
    assert unit.evidence["handoff"]["route_cooldown_events"] == events
    runtime.clock = lambda: 160.0
    retried = dispatch(handoff, runtime, snapshot, retry_kind)
    observation = runtime.last_handoff_observation
    launch = dispatch(reasoning, runtime, snapshot, "reasoning-launch")
    replacement = snapshot.jobs[launch.payload["job_id"]]
    assert replacement["id"] != failed["id"]
    assert replacement["failure_attempts"] == 1
    handoff.apply(runtime, snapshot, retried, observation)
    assert replacement["state"] == "prepared" and not replacement["claim"]["quiescent"]

    finish(runtime, snapshot, launch, "completed", valid_result(replacement))
    apply_action = dispatch(reasoning, runtime, snapshot, "reasoning-apply")
    reasoning.apply(runtime, snapshot, apply_action, Observation("confirmed", {}))
    assert replacement["state"] == "applied", replacement.get("reason")
    if phase == "summarize":
        dispatch(handoff, runtime, snapshot, "handoff-audit-plan")
        complete_audits(runtime, snapshot, unit)
    dispatch(handoff, runtime, snapshot, "handoff-assemble")
    dispatch(handoff, runtime, snapshot, "handoff-release")
    assert handoff.resume_proposal(snapshot, unit) is not None
    assert unit.evidence["handoff"]["failure_ledger"]


def test_distinct_failed_attempts_keep_lifetime_budget_and_unknown_claim(campaign):
    runtime, snapshot, unit = start(campaign)
    for count in range(1, snapshot.spec.reasoning.max_attempts + 1):
        launch = dispatch(reasoning, runtime, snapshot, "reasoning-launch")
        # A launch with unknown completion cannot be superseded by a replan.
        assert not handoff.proposals(runtime, snapshot)
        premature = OutboxAction("premature", "handoff-replan", unit.id, {}, 0)
        assert handoff.perform(runtime, snapshot, premature).state == "unknown"
        row = finish(runtime, snapshot, launch, "limit")
        assert row["failure_attempts"] == count
        if count == snapshot.spec.reasoning.max_attempts:
            dispatch(handoff, runtime, snapshot, "handoff-failed")
            break
        now = runtime.clock()
        dispatch(handoff, runtime, snapshot, "handoff-replan")
        value = unit.evidence["handoff"]
        assert len(value["route_cooldown_events"]) == count
        assert max(value["route_cooldowns"].values()) == now + 60
        runtime.clock = lambda now=now: now + 60
        dispatch(handoff, runtime, snapshot, "handoff-replan")
    assert unit.evidence["handoff"]["phase"] == "failed"
    assert not reasoning.proposals(runtime, snapshot)
    assert not handoff.proposals(runtime, snapshot)


def test_current_contract_replan_obeys_same_durable_cooldown(campaign):
    from test_campaign_handoff_contracts import recovery_result

    from bmad_loop.campaign_headless import read_json

    runtime, snapshot, unit, receipt = campaign
    runtime.project = Path(unit.bindings["workspace"])
    runtime._role = lambda snapshot, role, **kwargs: kwargs["allowed_candidates"][0]
    snapshot.spec = dataclasses.replace(
        snapshot.spec, github=dataclasses.replace(snapshot.spec.github, enabled=False)
    )
    Path(receipt["sessions"][0]["transcript_path"]).unlink()
    handoff.intercept_terminal(runtime, snapshot, unit, receipt)
    dispatch(handoff, runtime, snapshot, "handoff-capture")
    dispatch(handoff, runtime, snapshot, "handoff-recovery-plan")
    launch = dispatch(reasoning, runtime, snapshot, "reasoning-launch")
    failed = finish(runtime, snapshot, launch, "transport")
    dispatch(handoff, runtime, snapshot, "handoff-contract-replan")
    value = unit.evidence["handoff"]
    assert value["phase"] == "recovery_extract" and value["failure_ledger"]
    runtime.clock = lambda: 160.0
    dispatch(handoff, runtime, snapshot, "handoff-contract-replan")
    launch = dispatch(reasoning, runtime, snapshot, "reasoning-launch")
    row = snapshot.jobs[launch.payload["job_id"]]
    assert row["id"] != failed["id"] and row["failure_attempts"] == 1
    inputs = read_json(Path(row["request"]["inputs"][0]["path"]))
    finish(runtime, snapshot, launch, "completed", recovery_result(inputs, ready=False))
    action = dispatch(reasoning, runtime, snapshot, "reasoning-apply")
    reasoning.apply(runtime, snapshot, action, Observation("confirmed", {}))
    assert row["state"] == "applied", row.get("reason")


def test_persisted_summary_without_artifact_field_keeps_native_request_identity(campaign):
    runtime, snapshot, unit, receipt = campaign
    runtime._role = lambda snapshot, role, **kwargs: kwargs["allowed_candidates"][0]
    handoff.intercept_terminal(runtime, snapshot, unit, receipt)
    dispatch(handoff, runtime, snapshot, "handoff-capture")
    proposed = next(iter(handoff.proposals(runtime, snapshot)))
    action = OutboxAction("legacy-plan", proposed.kind, unit.id, proposed.payload, 0)
    observed = handoff.perform(runtime, snapshot, action)
    # Characterize the existing producer's persisted shape, before the optional
    # reducer field was introduced. The current consumer must not rewrite it.
    for request in observed.data["requests"]:
        assert request["coordinates"]["summary_spec"].pop("artifact_bytes") == 0
    handoff.apply(runtime, snapshot, action, observed)
    launch = dispatch(reasoning, runtime, snapshot, "reasoning-launch")
    original = snapshot.jobs[launch.payload["job_id"]]
    original_digest = original["request_digest"]
    runtime.store.save(snapshot, snapshot.revision)
    snapshot = runtime.store.load(snapshot.id)
    row = snapshot.jobs[original["id"]]
    assert "artifact_bytes" not in row["request"]["coordinates"]["summary_spec"]
    assert row["request_digest"] == original_digest
    finish(runtime, snapshot, launch, "completed", valid_result(row))
    action = dispatch(reasoning, runtime, snapshot, "reasoning-apply")
    reasoning.apply(runtime, snapshot, action, Observation("confirmed", {}))
    assert row["state"] == "applied", row.get("reason")
    assert row["request_digest"] == original_digest


def test_summary_purpose_selects_exact_qualified_effort(campaign):
    from bmad_loop.campaign_headless import file_digest, read_json, write_json
    from bmad_loop.campaign_jobs import digest

    runtime, snapshot, _, _ = campaign
    base = snapshot.spec.candidates("summarization")[0]
    high = dataclasses.replace(base, effort="xhigh")
    medium = dataclasses.replace(base, effort="medium")
    variants = {}
    for candidate, purpose in [(high, "handoff_map"), (medium, "handoff_reduce")]:
        cap = runtime.fixture_cap(candidate)
        path = Path(cap["semantic_qualification"]["path"])
        evidence = read_json(path)
        bindings = [row for row in evidence["route_bindings"] if row["purpose"] == purpose]
        evidence["route_bindings"] = bindings
        receipt_path = Path(evidence["independent_review"]["native_receipt_path"])
        receipt = read_json(receipt_path)
        receipt["result"]["route_bindings_digest"] = digest(bindings)
        write_json(receipt_path, receipt)
        evidence["independent_review"]["sha256"] = file_digest(receipt_path)
        write_json(path, evidence)
        cap["semantic_qualification"]["sha256"] = file_digest(path)
        cap["semantic_qualification"]["proofs"][0]["sha256"] = file_digest(path)
        variants[candidate.effort] = {"reasoning": cap}
    snapshot.config_snapshot["reasoning_capabilities"][base.profile + ":" + base.model] = {
        "by_effort": variants
    }
    snapshot.spec = dataclasses.replace(
        snapshot.spec,
        role_pools=tuple(
            (name, (high, medium) if name == "map" else pool)
            for name, pool in snapshot.spec.role_pools
        ),
    )
    for purpose, expected in [("handoff_map", high), ("handoff_reduce", medium)]:
        selected, cap = handoff._summary_route(runtime, snapshot, "summarization", purpose=purpose)
        assert selected == expected and cap["efforts"] == [expected.effort]
