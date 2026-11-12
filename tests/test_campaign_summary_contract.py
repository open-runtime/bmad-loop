"""Prompt/schema and persisted continuation versions change as one contract."""

import copy
import hashlib
import json
from dataclasses import replace

import pytest
from test_campaign_handoff_context import budget, capture, native_row, quotes, result
from test_campaign_handoff_flow import campaign as campaign
from test_campaign_handoff_flow import complete_audits, complete_maps
from test_campaign_handoff_retry import dispatch, finish, start, valid_result
from test_campaign_headless import prepared as prepared

from bmad_loop import campaign_handoff_flow as handoff
from bmad_loop import campaign_reasoning_flow as reasoning
from bmad_loop.campaign_jobs import digest
from bmad_loop.campaign_summary import (
    EXTRACT_PROMPT_VERSION,
    FINAL_PROMPT_VERSION,
    REDUCE_PROMPT_VERSION,
    corrective_spec,
    load_input,
    plan_maps,
    plan_reduction,
    plan_remaining_maps,
    project_final_input,
    summary_contract_current,
    summary_prompt,
    summary_schema,
    validate_summary,
)
from bmad_loop.campaign_supervisor import Observation
from bmad_loop.campaign_transcripts import load_events


@pytest.mark.parametrize("kind", ["map", "reduce", "correction"])
def test_prompt_embeds_the_exact_native_job_schema(tmp_path, kind):
    history = capture(
        tmp_path, [native_row("codex", "Keep every commit.\nMerge only after review.")]
    )
    events = load_events(history)
    job = plan_maps(events, budget(), tmp_path / "maps")[0]
    if kind == "reduce":
        value = result(job, events)
        value["facts"][0]["source_quote"] = "Keep every commit."
        validated = validate_summary(job, value, events)
        job = plan_reduction(
            ((job, validated),),
            budget(),
            tmp_path / "reduce",
            artifact_bytes=4000,
            owner_quotes=quotes(history, validated),
        )[0]
    elif kind == "correction":
        job = corrective_spec(job, attempt=1, receipt_sha256="receipt", reason="Missing obligation")
    prompt = summary_prompt(job)
    footer = json.loads(prompt[prompt.rindex('{"input_sha256"') :])
    assert footer["schema"] == summary_schema(job)
    facts = footer["schema"]["properties"]["facts"]["items"]
    assert "source_quote_id" in facts["required"]
    assert "source_quote" not in facts["properties"]
    assert footer["schema"]["properties"]["job_id"]["enum"] == [job.id]
    assert job.prompt_version == (
        REDUCE_PROMPT_VERSION if kind == "reduce" else EXTRACT_PROMPT_VERSION
    )


def test_old_map_artifact_is_retained_without_counting_as_current_coverage(tmp_path):
    history = capture(tmp_path, [native_row("codex")])
    events = load_events(history)
    job = plan_maps(events, budget(), tmp_path / "maps")[0]
    validated = validate_summary(job, result(job, events), events)
    old = replace(job, prompt_version="history-extract-v2")
    before = copy.deepcopy(validated)
    assert plan_remaining_maps(events, ((job, validated),), budget(), tmp_path / "current") == ()
    fresh = plan_remaining_maps(events, ((old, validated),), budget(), tmp_path / "renew")
    assert fresh and fresh[0].event_ids == job.event_ids
    assert validated == before


def prior_request(row):
    """Load a persisted prior-release request; never mutate it during recovery."""
    row["request"]["coordinates"]["summary_spec"]["prompt_version"] = "history-extract-v2"
    row["request_digest"] = digest(row["request"])
    return copy.deepcopy(row["request"])


@pytest.mark.parametrize("phase", ["summarize", "audit", "assemble", "ready"])
def test_persisted_old_summary_refreshes_and_completes_before_resume(campaign, phase):
    runtime, snapshot, unit = start(campaign)
    original = snapshot.jobs[unit.evidence["handoff"]["jobs"][0]]
    if phase != "summarize":
        complete_maps(runtime, snapshot, unit)
        dispatch(handoff, runtime, snapshot, "handoff-audit-plan")
    if phase in {"assemble", "ready"}:
        complete_audits(runtime, snapshot, unit)
        dispatch(handoff, runtime, snapshot, "handoff-assemble")
    if phase == "ready":
        dispatch(handoff, runtime, snapshot, "handoff-release")
        assert handoff.resume_proposal(snapshot, unit, runtime) is not None
    immutable = prior_request(original)
    original["failure_attempts"] = 1
    old_artifact = copy.deepcopy(original.get("applied_result"))
    runtime.store.save(snapshot, snapshot.revision)
    snapshot = runtime.store.load(snapshot.id)
    unit = snapshot.units[unit.id]
    old = handoff._job(runtime, snapshot, original["id"])
    assert handoff.resume_proposal(snapshot, unit, runtime) is None
    if phase == "summarize":
        stale = dispatch(reasoning, runtime, snapshot, "reasoning-stale")
        reasoning.apply(runtime, snapshot, stale, Observation("confirmed", {}))
        assert old["state"] == "stale"
    refresh = dispatch(handoff, runtime, snapshot, "handoff-refresh")
    assert refresh.kind == "handoff-refresh"
    dispatch(handoff, runtime, snapshot, "handoff-plan")
    replacement_ids = unit.evidence["handoff"]["jobs"]
    assert original["id"] not in replacement_ids
    replacement = snapshot.jobs[replacement_ids[0]]
    assert (
        replacement["request"]["coordinates"]["summary_spec"]["prompt_version"]
        == EXTRACT_PROMPT_VERSION
    )
    assert replacement["failure_attempts"] == 1
    assert old["request"] == immutable and old.get("applied_result") == old_artifact
    launch = dispatch(reasoning, runtime, snapshot, "reasoning-launch")
    assert launch.payload["job_id"] == replacement["id"]
    finish(runtime, snapshot, launch, "completed", valid_result(replacement))
    apply = dispatch(reasoning, runtime, snapshot, "reasoning-apply")
    reasoning.apply(runtime, snapshot, apply, Observation("confirmed", {}))
    dispatch(handoff, runtime, snapshot, "handoff-audit-plan")
    while any(
        snapshot.jobs[key]["state"] != "applied" for key in unit.evidence["handoff"]["audits"]
    ):
        audit_launch = dispatch(reasoning, runtime, snapshot, "reasoning-launch")
        audit_row = snapshot.jobs[audit_launch.payload["job_id"]]
        finish(runtime, snapshot, audit_launch, "completed", valid_result(audit_row))
        audit_apply = dispatch(reasoning, runtime, snapshot, "reasoning-apply")
        reasoning.apply(runtime, snapshot, audit_apply, Observation("confirmed", {}))
    dispatch(handoff, runtime, snapshot, "handoff-assemble")
    dispatch(handoff, runtime, snapshot, "handoff-release")
    assert handoff.resume_proposal(snapshot, unit, runtime) is not None
    runtime.store.save(snapshot, snapshot.revision)
    snapshot = runtime.store.load(snapshot.id)
    unit = snapshot.units[unit.id]
    proposed = handoff.resume_proposal(snapshot, unit)
    assert proposed is not None
    request = {**proposed.payload, "role": "execution"}
    handoff.decorate_launch(snapshot, unit, request)
    assert request["handoff_packet"]["path"] == unit.evidence["handoff"]["packet"]["path"]


def test_old_live_request_is_observed_until_quiescent_before_refresh(campaign):
    runtime, snapshot, unit = start(campaign)
    launch = dispatch(reasoning, runtime, snapshot, "reasoning-launch")
    row = snapshot.jobs[launch.payload["job_id"]]
    immutable = prior_request(row)
    launch.payload["job_binding"]["request_digest"] = row["request_digest"]
    row["observed_at"] = 0
    kinds = [proposal.kind for proposal in reasoning.proposals(runtime, snapshot)]
    assert "reasoning-observe" in kinds and "reasoning-launch" not in kinds
    assert not handoff.proposals(runtime, snapshot)
    finish(runtime, snapshot, launch, "completed", valid_result(row))
    assert row["claim"]["quiescent"] and row["state"] == "stale"
    dispatch(handoff, runtime, snapshot, "handoff-refresh")
    dispatch(handoff, runtime, snapshot, "handoff-plan")
    assert row["request"] == immutable
    assert unit.evidence["handoff"]["jobs"] != [row["id"]]


@pytest.mark.parametrize("stage", ["intermediate", "final"])
@pytest.mark.parametrize("artifact_bytes", [0, 4000])
def test_synthesis_preserves_obligation_timing_and_qualifies_child_inferences(
    tmp_path, stage, artifact_bytes
):
    history = capture(tmp_path, [native_row("codex", "keep commits; reconcile PR before delivery")])
    events = load_events(history)
    leaf = plan_maps(events, budget(), tmp_path / "maps")[0]
    child = validate_summary(leaf, result(leaf, events), events)
    job = plan_reduction(
        ((leaf, child),),
        budget(),
        tmp_path / "reduce",
        artifact_bytes=artifact_bytes,
        owner_quotes=quotes(history, child),
    )[0]
    if stage == "final":
        job = project_final_input(job, tmp_path / "final")
    prompt = summary_prompt(job)
    if stage == "final":
        assert "Return a complete ranked_fact_ids permutation" in prompt
        assert "required whole historically attributed facts" in prompt
        assert (
            "Exact owner spans, canonical obligations and current controller controls remain separate mandatory context"
            in prompt
        )
        footer = json.loads(prompt[prompt.rindex('{"input_sha256"') :])
        assert footer["schema"] == summary_schema(job)
        assert footer["primary_ids"] == [leaf.id]
        assert set(footer["schema"]["properties"]) == {
            "schema_version",
            "job_id",
            "input_sha256",
            "covered_ids",
            "ranked_fact_ids",
            "required_fact_ids",
            "excluded_fact_ids",
        }
        assert load_input(job)["owner_quotes"]
        return
    # These are rendered model instructions, not a claim that a scripted model
    # established semantic fidelity. The native independent audit remains required.
    for instruction in (
        "Preserve each obligation's action, scope, condition and timing.",
        "Do not turn delivery postconditions into prerequisites for editing, continuation or merge.",
        "Do not invent owner approval requirements or review waivers.",
        "Pending review or verification describes unfinished work, not an instruction to leave it undone.",
        "Child summaries are fallible paraphrases, not new authority.",
        "The owner_quotes table supplies authenticated exact source spans;",
        "Use exact source text when supplied. If it is not supplied, attribute the child claim",
        "retrieve retained originals before acting on a disputed requirement.",
        "Keep contradictions visible rather than choosing an unsupported resolution.",
        "Describe a missing worktree or PR identity as a historical omission",
        "If the canonical packet supplies it, use that current identity",
        "Do not infer canonical values that are absent from this request.",
    ):
        assert instruction in prompt
    if artifact_bytes:
        assert "Index only detail categories actually present in the retained sources." in prompt
        assert (
            "Do not claim commands, absolute roots, hashes, grants, lifecycle receipts or debug chronology exist merely because this prompt lists them."
            in prompt
        )
    footer = json.loads(prompt[prompt.rindex('{"input_sha256"') :])
    assert footer["schema"] == summary_schema(job)
    assert footer["primary_ids"] == [leaf.id]
    assert footer["quote_catalog"]  # The real source coordinate remains available.


@pytest.mark.parametrize(
    ("artifact_bytes", "expected_prefix_sha"),
    [
        (0, "829c1816fceba18cafbdd1f650babecdac050674813d340254a1a1317e9c90f2"),
        (4000, "688564515973b3102888bb8376977f0ebf05175462d243054330dc725b88c1be"),
    ],
)
def test_leaf_instructions_remain_byte_identical_for_existing_extract_contract(
    tmp_path, artifact_bytes, expected_prefix_sha
):
    history = capture(tmp_path, [native_row("codex")])
    job = replace(
        plan_maps(load_events(history), budget(), tmp_path / "maps")[0],
        artifact_bytes=artifact_bytes,
    )
    prompt = summary_prompt(job)
    instructions = prompt[: prompt.rindex('{"input_sha256"')]
    assert hashlib.sha256(instructions.encode()).hexdigest() == expected_prefix_sha
    assert job.prompt_version == EXTRACT_PROMPT_VERSION == "history-extract-v3"


@pytest.mark.parametrize(
    ("stage", "prior", "current"),
    [
        ("intermediate", "history-reduce-v6", REDUCE_PROMPT_VERSION),
        ("final", "history-final-v2", FINAL_PROMPT_VERSION),
    ],
)
def test_saved_synthesis_contract_requires_fresh_request_after_fidelity_repair(
    stage, prior, current
):
    request = {
        "purpose": "handoff_reduce",
        "coordinates": {"summary_stage": stage, "summary_spec": {"prompt_version": prior}},
    }
    retained = copy.deepcopy(request)
    assert not summary_contract_current(request)
    assert request == retained
    request["coordinates"]["summary_spec"]["prompt_version"] = current
    assert summary_contract_current(request)
