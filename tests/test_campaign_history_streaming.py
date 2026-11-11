"""Large retained corpus mechanics; this does not qualify native model context."""

import json
from pathlib import Path

import pytest
from test_campaign_headless import finish
from test_campaign_headless import prepared as prepared
from test_campaign_managed_history import binding_for

from bmad_loop.campaign_managed_history import snapshot_managed_history
from bmad_loop.campaign_model import CampaignError
from bmad_loop.campaign_summary import (
    ContextBudget,
    plan_maps,
    plan_remaining_maps,
    validate_summary,
)
from bmad_loop.campaign_transcripts import load_events, seal_source, stream_hash


def test_large_managed_corpus_streams_then_maps_and_reuses_exact_coverage(
    prepared, tmp_path, monkeypatch
):
    runner, path, *_ = prepared
    runner.launch(path)
    receipt = finish(runner, path)
    # Extend retained raw fixture data after its scripted transport test. This
    # measures corpus storage/planning, never native output or semantic fidelity.
    stdout = path.parent / "stdout.jsonl"
    frame = (
        json.dumps({"type": "assistant", "text": "é" + "x" * 65536}, ensure_ascii=False).encode()
        + b"\n"
    )
    with stdout.open("ab") as output:
        for _ in range(1025):
            output.write(frame)
    assert stdout.stat().st_size > 64 * 1024 * 1024
    original_read = Path.read_bytes

    def bounded_read(target):
        assert target.stat().st_size < 8 * 1024 * 1024, "whole-corpus buffer used"
        return original_read(target)

    monkeypatch.setattr(Path, "read_bytes", bounded_read)
    binding = binding_for(path, receipt)
    with pytest.raises(CampaignError, match="capture bound"):
        snapshot_managed_history(path, binding, tmp_path / "too-small")
    source = snapshot_managed_history(
        path, binding, tmp_path / "large", max_bytes=128 * 1024 * 1024
    )
    events = load_events(source)
    index = next(event for event in events if event.record.kind == "managed-source-index")
    preserved = json.loads(index.record.text)["files"]["stdout.jsonl"]
    assert preserved["bytes"] == stdout.stat().st_size
    assert preserved["sha256"] == stream_hash(stdout)
    assert len([event for event in events if "é" in event.record.text]) == 1025
    budget = ContextBudget("scripted-storage-only", 3 * 1024 * 1024, 8192, qualified=True)
    jobs = plan_maps(events, budget, tmp_path / "maps", overlap_tokens=0)
    assert len(jobs) > 20
    completed = []
    for job in jobs:
        result = {
            "schema_version": 1,
            "job_id": job.id,
            "input_sha256": job.input_sha256,
            "covered_ids": list(job.primary_ids),
            "facts": [],
            "gaps": [],
        }
        validated = validate_summary(job, result, events)
        completed.append((job, validated))
    assert (
        plan_remaining_maps(events, tuple(completed), budget, tmp_path / "reused", overlap_tokens=0)
        == ()
    )


def test_stream_seal_rejects_a_record_over_its_independent_bound(tmp_path):
    from bmad_loop.campaign_transcripts import record_lines

    source = tmp_path / "source"
    source.write_bytes(b"x" * 33 + b"\n")
    sealed, _, _, _ = seal_source(source, tmp_path / "sealed", maximum=64)
    with pytest.raises(CampaignError, match="record exceeds"):
        list(record_lines(sealed, 32))
