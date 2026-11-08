"""A bounded reducer failure partitions its frontier without re-extracting history."""

import dataclasses
import json
from pathlib import Path

import pytest
from test_campaign_handoff_flow import campaign as campaign
from test_campaign_handoff_retry import dispatch, finish, start, valid_result
from test_campaign_headless import prepared as prepared

from bmad_loop import campaign_handoff_flow as handoff
from bmad_loop import campaign_reasoning_flow as reasoning
from bmad_loop.campaign_model import CampaignError, OutboxAction
from bmad_loop.campaign_summary import SummaryJobSpec, load_input, quote_catalog
from bmad_loop.campaign_supervisor import Observation


def summary_result(row, size):
    spec = SummaryJobSpec(**row["request"]["coordinates"]["summary_spec"])
    facts = [
        {
            "kind": "progress",
            "text": "Historical detail " * size,
            "citations": [spec.event_ids[0]],
            "source_quote_id": "",
        }
    ]
    facts.extend(
        {
            "kind": "owner_request",
            "text": "Keep history and all acceptance criteria.",
            "source_quote_id": key,
            "citations": [quote["event_id"]],
        }
        for key, quote in quote_catalog(spec).items()
    )
    return {
        "schema_version": 1,
        "job_id": row["id"],
        "input_sha256": spec.input_sha256,
        "covered_ids": list(spec.primary_ids),
        "gaps": [],
        "facts": facts,
    }


def apply_completed(runtime, snapshot, action, size=1):
    row = snapshot.jobs[action.payload["job_id"]]
    result = (
        valid_result(row)
        if row["request"]["purpose"] == "handoff_audit"
        else summary_result(row, size)
    )
    finish(runtime, snapshot, action, "completed", result, usage_known=True)
    apply_action = dispatch(reasoning, runtime, snapshot, "reasoning-apply")
    reasoning.apply(runtime, snapshot, apply_action, Observation("confirmed", {}))
    assert row["state"] == "applied", row.get("reason")
    return row


def reducing(campaign):
    runtime, snapshot, _, receipt = campaign
    source = Path(receipt["sessions"][0]["transcript_path"])
    with source.open("a", encoding="utf-8") as stream:
        stream.write(
            json.dumps(
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "assistant",
                        "content": "Work context. " * 4000,
                    },
                }
            )
            + "\n"
        )
    snapshot.spec = dataclasses.replace(
        snapshot.spec,
        reasoning=dataclasses.replace(snapshot.spec.reasoning, packet_token_limit=12000),
    )
    snapshot.config_revision += 1
    runtime, snapshot, unit = start(campaign)
    while any(row["state"] != "applied" for row in snapshot.jobs.values()):
        apply_completed(
            runtime, snapshot, dispatch(reasoning, runtime, snapshot, "reasoning-launch"), 400
        )
    maps = list(unit.evidence["handoff"]["jobs"])
    assert len(maps) >= 3
    dispatch(handoff, runtime, snapshot, "handoff-audit-plan")
    reducers = unit.evidence["handoff"]["jobs"]
    assert len(reducers) >= 2
    assert any(len(snapshot.jobs[key]["request"]["dependencies"]) > 1 for key in reducers)
    return runtime, snapshot, unit, maps


def failed_frontier(campaign):
    runtime, snapshot, unit, maps = reducing(campaign)
    failed, kept = None, []
    for _ in list(unit.evidence["handoff"]["jobs"]):
        launch = dispatch(reasoning, runtime, snapshot, "reasoning-launch")
        row = snapshot.jobs[launch.payload["job_id"]]
        if failed is None and len(row["request"]["dependencies"]) > 1:
            failed = finish(runtime, snapshot, launch, "limit", limit_kind="native-output-tokens")
        else:
            kept.append(apply_completed(runtime, snapshot, launch, 30)["id"])
    assert failed and kept
    return runtime, snapshot, unit, maps, failed, kept


def test_output_limit_splits_then_combines_and_resumes_without_losing_siblings(campaign):
    runtime, snapshot, unit, maps, failed, kept = failed_frontier(campaign)
    old = SummaryJobSpec(**failed["request"]["coordinates"]["summary_spec"])
    originals = {key: snapshot.jobs[key]["request_digest"] for key in [*maps, *kept]}
    value = unit.evidence["handoff"]
    # An unconfirmed predecessor cannot be replaced even by an explicitly replayed command.
    failed["claim"]["quiescent"] = False
    premature = OutboxAction("premature", "handoff-replan", unit.id, {}, 0)
    assert handoff.perform(runtime, snapshot, premature).state == "unknown"
    assert not handoff.proposals(runtime, snapshot)
    failed["claim"]["quiescent"] = True
    dispatch(handoff, runtime, snapshot, "handoff-replan")
    runtime.store.save(snapshot, snapshot.revision)
    snapshot = runtime.store.load(snapshot.id)
    unit = snapshot.units[unit.id]
    value = unit.evidence["handoff"]
    runtime.clock = lambda: 160.0
    action = dispatch(handoff, runtime, snapshot, "handoff-replan")
    observation = runtime.last_handoff_observation
    split = [snapshot.jobs[key] for key in value["jobs"] if key not in kept]
    assert len(split) == 2 and value["map_jobs"] == maps
    assert set(kept) <= set(value["jobs"])
    assert value["reduction_level"] == 2
    assert {key for row in split for key in row["request"]["dependencies"]} == set(old.dependencies)
    assert {
        key for row in split for key in row["request"]["coordinates"]["summary_spec"]["event_ids"]
    } == set(old.event_ids)
    for row in split:
        spec = SummaryJobSpec(**row["request"]["coordinates"]["summary_spec"])
        assert row["failure_attempts"] == 1
        assert spec.artifact_bytes == old.artifact_bytes and spec.output_tokens == old.output_tokens
        assert load_input(spec)["source_input"] == load_input(old)["source_input"]
    # Lost acknowledgement cannot cancel a replacement after its lease starts.
    launch = dispatch(reasoning, runtime, snapshot, "reasoning-launch")
    handoff.apply(runtime, snapshot, action, observation)
    assert not snapshot.jobs[launch.payload["job_id"]]["claim"]["quiescent"]
    apply_completed(runtime, snapshot, launch, 85)
    apply_completed(
        runtime, snapshot, dispatch(reasoning, runtime, snapshot, "reasoning-launch"), 85
    )
    dispatch(handoff, runtime, snapshot, "handoff-audit-plan")
    combined = [snapshot.jobs[key] for key in value["jobs"]]
    assert all(row["request"]["purpose"] == "handoff_reduce" for row in combined)
    assert all(row["failure_attempts"] == 1 for row in combined)
    assert value["reduction_level"] == 3
    for _ in combined:
        apply_completed(
            runtime, snapshot, dispatch(reasoning, runtime, snapshot, "reasoning-launch")
        )
    dispatch(handoff, runtime, snapshot, "handoff-audit-plan")
    for _ in value["audits"]:
        apply_completed(
            runtime, snapshot, dispatch(reasoning, runtime, snapshot, "reasoning-launch")
        )
    dispatch(handoff, runtime, snapshot, "handoff-assemble")
    dispatch(handoff, runtime, snapshot, "handoff-release")
    assert handoff.resume_proposal(snapshot, unit) is not None
    assert originals == {
        key: handoff._job(runtime, snapshot, key)["request_digest"] for key in originals
    }
    assert all(handoff._job(runtime, snapshot, key)["state"] == "applied" for key in originals)


@pytest.mark.parametrize("kind", ["wall-time", "native-turns", "stdout-wire-bytes", ""])
def test_other_limits_retry_same_reducer_without_partition_or_reextract(campaign, kind):
    runtime, snapshot, unit, maps, failed, kept = failed_frontier(campaign)
    failed["receipt"]["limit_kind"] = kind
    original = failed["request"]
    dispatch(handoff, runtime, snapshot, "handoff-replan")
    runtime.clock = lambda: 160.0
    dispatch(handoff, runtime, snapshot, "handoff-replan")
    value = unit.evidence["handoff"]
    retried = [snapshot.jobs[key] for key in value["jobs"] if key not in kept]
    assert len(retried) == 1 and value["reduction_level"] == 1
    assert retried[0]["request"]["dependencies"] == original["dependencies"]
    assert retried[0]["request"]["inputs"][0]["sha256"] == original["inputs"][0]["sha256"]
    assert retried[0]["failure_attempts"] == 1 and value["map_jobs"] == maps


def test_reducer_split_cannot_reset_lifetime_attempts_or_round_bound(campaign):
    runtime, snapshot, unit, _, failed, _ = failed_frontier(campaign)
    value = unit.evidence["handoff"]
    value["reduction_level"] = 8
    with pytest.raises(CampaignError, match="eight qualified rounds"):
        handoff._summary_replan(runtime, snapshot, unit, {**value, "plan_epoch": 1})
    value["reduction_level"] = 1
    failed["failure_attempts"] = snapshot.spec.reasoning.max_attempts
    dispatch(handoff, runtime, snapshot, "handoff-failed")
    assert value["phase"] == "failed" and not reasoning.proposals(runtime, snapshot)
