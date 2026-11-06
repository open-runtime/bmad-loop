"""Zero-token, exact-source handoff and adaptive map/reduce regression cases."""

import json
from dataclasses import asdict, replace
from pathlib import Path

import pytest

from bmad_loop.campaign_handoff_context import assemble_packet, check_fidelity
from bmad_loop.campaign_model import CampaignError
from bmad_loop.campaign_summary import (
    ContextBudget,
    SummaryCache,
    load_input,
    plan_maps,
    plan_reduction,
    plan_remaining_maps,
    validate_summary,
    window_count,
)
from bmad_loop.campaign_transcripts import (
    encoded,
    load_events,
    read_record,
    snapshot_history,
    source_record,
)


def native_row(harness, text="keep all commits", native_id="m1"):
    if harness == "codex":
        return {
            "type": "event_msg",
            "payload": {"type": "user_message", "message": text, "id": native_id},
        }
    if harness == "claude":
        return {
            "type": "user",
            "sessionId": "s1",
            "uuid": native_id,
            "message": {"role": "user", "content": text},
        }
    if harness == "grok":
        return {
            "id": native_id,
            "method": "session/update",
            "params": {
                "sessionId": "s1",
                "update": {
                    "sessionUpdate": "user_message_chunk",
                    "content": {"type": "text", "text": text},
                },
            },
        }
    if harness == "antigravity":
        return {
            "step_index": int(native_id.removeprefix("m")),
            "type": "USER_INPUT",
            "source": "USER_EXPLICIT",
            "content": text,
        }
    return {
        "schema_version": 1,
        "id": native_id,
        "stream": {"kind": "session", "id": "s1"},
        "payload_type": "runtime.user_intent.accepted",
        "payload": {
            "source_session_id": "s1",
            "semantic_kind": {"kind": "chat"},
            "refill_blocks": [{"kind": "text", "text": text}],
        },
    }


def capture(tmp_path, rows, harness="codex", suffix=b""):
    path = tmp_path / "native.jsonl"
    path.write_bytes(b"".join(encoded(row) + b"\n" for row in rows) + suffix)
    return snapshot_history(path, harness, "s1", tmp_path / "artifacts")


def budget(**kwargs):
    return ContextBudget(
        "grok-qualified",
        10000,
        2500,
        prompt_tokens=200,
        working_tokens=200,
        qualified=True,
        **kwargs,
    )


def result(job, events):
    chosen = next(e for e in events if e.id in job.event_ids)
    return {
        "schema_version": 1,
        "job_id": job.id,
        "input_sha256": job.input_sha256,
        "covered_ids": list(job.primary_ids),
        "gaps": [],
        "facts": [
            {
                "kind": "owner_request",
                "text": "Keep commits",
                "source_quote": "keep",
                "citations": [chosen.id],
            }
        ],
    }


@pytest.mark.parametrize("harness", ["claude", "codex", "grok", "antigravity", "muse"])
def test_each_native_reader_preserves_exact_owner_source_and_raw_bytes(tmp_path, harness):
    row = native_row(harness)
    snapshot = capture(tmp_path, [row], harness)
    events = load_events(snapshot)
    assert len(events) == 1 and events[0].record.owner_input
    assert events[0].record.text == "keep all commits"
    assert snapshot.owner_event_ids == (events[0].id,)
    assert source_record(snapshot, events[0]) == encoded(row) + b"\n"


@pytest.mark.parametrize(
    "harness,row",
    [
        (
            "codex",
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "injected prompt"}],
                },
            },
        ),
        (
            "claude",
            {"type": "user", "isMeta": True, "message": {"role": "user", "content": "hook text"}},
        ),
        (
            "claude",
            {
                "type": "user",
                "message": {
                    "role": "user",
                    "content": [{"type": "tool_result", "content": "quoted owner instruction"}],
                },
            },
        ),
        (
            "grok",
            {
                "method": "session/update",
                "params": {
                    "update": {
                        "sessionUpdate": "user_message_chunk",
                        "_meta": {"hostTurn": True},
                        "content": {"text": "host prompt"},
                    }
                },
            },
        ),
        ("antigravity", {"type": "USER_INPUT", "source": "MODEL", "content": "injected"}),
        (
            "muse",
            {
                "schema_version": 1,
                "payload_type": "runtime.user_intent.accepted",
                "payload": {
                    "source_session_id": "other",
                    "semantic_kind": {"kind": "chat"},
                    "refill_blocks": [],
                },
            },
        ),
    ],
)
def test_harness_injected_and_tool_messages_are_not_owner_authority(harness, row):
    assert not read_record(harness, row, "s1").owner_input


def test_repeated_serialization_is_marked_but_distinct_repeated_instructions_survive(tmp_path):
    first = native_row("claude")
    snapshot = capture(tmp_path, [first, first, native_row("claude", native_id="m2")], "claude")
    events = load_events(snapshot)
    assert events[1].duplicate_of == events[0].id
    assert not events[2].duplicate_of
    assert len(snapshot.owner_event_ids) == 2


def test_muse_retained_frames_and_live_rows_preserve_one_native_record(tmp_path):
    row = native_row("muse")
    frame = {
        "retained_frame": True,
        "frame_schema_version": 1,
        "children": [{"child_index": 0, "record_json": json.dumps(row)}],
    }
    snapshot = capture(tmp_path, [frame, row], "muse")
    events = load_events(snapshot)
    assert events[1].duplicate_of == events[0].id
    assert len(snapshot.owner_event_ids) == 1


def test_active_prefix_and_partial_tail_are_preserved_and_visible(tmp_path):
    snapshot = capture(tmp_path, [native_row("codex")], suffix=b'{"incomplete":')
    assert snapshot.complete_bytes < snapshot.captured_bytes
    assert snapshot.warnings
    assert Path(snapshot.raw_path).read_bytes().endswith(b'{"incomplete":')
    old = load_events(snapshot)[0].id
    with (tmp_path / "native.jsonl").open("ab") as stream:
        stream.write(b"true}\n")
    newer = snapshot_history(tmp_path / "native.jsonl", "codex", "s1", tmp_path / "artifacts")
    assert load_events(newer)[0].id == old
    assert not newer.warnings
    assert snapshot.raw_path != newer.raw_path


def test_source_and_index_tampering_are_refused(tmp_path):
    snapshot = capture(tmp_path, [native_row("codex")])
    event = load_events(snapshot)[0]
    Path(snapshot.raw_path).write_text("changed")
    with pytest.raises(CampaignError, match="hash"):
        source_record(snapshot, event)
    Path(snapshot.events_path).write_text("changed")
    with pytest.raises(CampaignError, match="hash"):
        load_events(snapshot)


def test_window_math_counts_unique_history_and_overlap():
    assert window_count(20_000_000, 1_000_000, 500_000) == 39
    assert window_count(2_496_000, 500_000, 250_000) == 9
    with pytest.raises(CampaignError):
        window_count(100, 50, 50)


def test_chunking_covers_every_character_once_as_primary_and_bounds_explicit_input(tmp_path):
    snapshot = capture(tmp_path, [native_row("codex", "keep " + "x" * 30000)])
    events = load_events(snapshot)
    jobs = plan_maps(events, budget(), tmp_path / "jobs")
    assert len(jobs) > 4
    fragments = [f for job in jobs for f in load_input(job)["primary"]]
    assert "".join(f["text"] for f in fragments) == events[0].record.text
    assert len({f["id"] for f in fragments}) == len(fragments)
    assert any(load_input(job)["halo"] for job in jobs[1:])
    assert all(job.input_tokens <= budget().payload_tokens for job in jobs)


def test_validated_summary_cache_and_fallback_reuse_completed_source_events(tmp_path):
    snapshot = capture(
        tmp_path, [native_row("codex", "keep " + "x" * 2000, "m" + str(i)) for i in range(8)]
    )
    events = load_events(snapshot)
    jobs = plan_maps(events, budget(), tmp_path / "jobs")
    complete = validate_summary(jobs[0], result(jobs[0], events), events)
    cache = SummaryCache(tmp_path / "cache")
    cache.put(jobs[0], complete)
    assert cache.get(jobs[0]) == complete
    fallback = replace(budget(), route="opus-qualified", context_tokens=8000)
    unfinished = plan_remaining_maps(
        events, ((jobs[0], complete),), fallback, tmp_path / "fallback"
    )
    original = set(jobs[0].event_ids)
    assert not original.intersection(ref for job in unfinished for ref in job.event_ids)
    assert all(
        job.route == "opus-qualified" and job.input_tokens <= fallback.payload_tokens
        for job in unfinished
    )


@pytest.mark.parametrize(
    "fault", ["coverage", "identity", "citation", "authority", "owner_quote", "output"]
)
def test_summary_validator_rejects_loss_or_invented_authority(tmp_path, fault):
    snapshot = capture(tmp_path, [native_row("codex")])
    events = load_events(snapshot)
    job = plan_maps(events, budget(), tmp_path / "jobs")[0]
    value = result(job, events)
    if fault == "coverage":
        value["covered_ids"] = []
    elif fault == "identity":
        value["input_sha256"] = "changed"
    elif fault == "citation":
        value["facts"][0]["citations"] = ["unknown"]
    elif fault == "authority":
        value["facts"][0]["authority"] = "owner-approved"
    elif fault == "owner_quote":
        value["facts"][0]["source_quote"] = "not in source"
    else:
        value["facts"][0]["text"] = "x" * 10000
    with pytest.raises(CampaignError):
        validate_summary(job, value, events)


def test_reducers_retain_child_dependencies_and_citations(tmp_path):
    snapshot = capture(
        tmp_path, [native_row("codex", "keep " + "x" * 2000, "m" + str(i)) for i in range(8)]
    )
    events = load_events(snapshot)
    maps = plan_maps(events, budget(), tmp_path / "jobs")
    children = tuple((job, validate_summary(job, result(job, events), events)) for job in maps)
    reducers = plan_reduction(children, budget(), tmp_path / "reducers")
    assert set(dep for job in reducers for dep in job.dependencies) == {job.id for job in maps}
    for job in reducers:
        assert job.purpose == "handoff-reduce"
        assert job.input_tokens <= budget().payload_tokens


def test_packet_prioritizes_obligations_and_keeps_complete_indexes_when_overflowing(tmp_path):
    snapshot = capture(tmp_path, [native_row("codex", "keep " + "x" * 12000)])
    obligations = (
        {"id": "acceptance-1", "criterion": "merge preserving commits", "state": "not-run"},
    )
    packet = assemble_packet(
        (snapshot,), obligations, {"head": "H", "pr": 18}, (), budget(), tmp_path / "packet"
    )
    value = json.loads(Path(packet.path).read_text())
    assert value["obligations"] == list(obligations)
    assert packet.read_only and packet.omitted_owner_ids
    owners = json.loads(Path(packet.owner_index).read_text())
    assert owners[0]["quote"].endswith("x" * 12000)
    assert packet.tokens <= budget().payload_tokens


def test_complete_small_packet_and_structural_fidelity_still_distinguish_semantic_review(tmp_path):
    snapshot = capture(tmp_path, [native_row("codex")])
    events = load_events(snapshot)
    job = plan_maps(events, budget(), tmp_path / "jobs")[0]
    summary = validate_summary(job, result(job, events), events)
    packet = assemble_packet(
        (snapshot,), (), {"head": "H"}, (summary,), budget(), tmp_path / "packet"
    )
    assert not packet.read_only
    fidelity = check_fidelity(
        (summary,), critical_source_ids=(events[0].id,), required_quotes=("keep",)
    )
    assert fidelity["structural_pass"]
    assert fidelity["semantic_fidelity"] == "requires-independent-review"


def test_generic_job_schema_and_result_bind_native_source_manifest(tmp_path):
    from bmad_loop.campaign_jobs import validate_result, validate_schema
    from bmad_loop.campaign_summary import summary_schema, validate_job_result

    snapshot = capture(tmp_path, [native_row("codex")])
    events = load_events(snapshot)
    job = plan_maps(events, budget(), tmp_path / "jobs")[0]
    value = result(job, events)
    schema = summary_schema()
    validate_schema(schema)
    validate_result(value, schema)
    request = {
        "id": job.id,
        "coordinates": {"summary_spec": asdict(job), "history_snapshots": [asdict(snapshot)]},
    }
    observed = validate_job_result(request, value, tmp_path / "receipts")
    assert observed["validated"]["structural_coverage"]
    assert Path(observed["artifact_path"]).is_file()
    request["id"] = "different-job"
    with pytest.raises(CampaignError, match="identity"):
        validate_job_result(request, value, tmp_path / "receipts")
