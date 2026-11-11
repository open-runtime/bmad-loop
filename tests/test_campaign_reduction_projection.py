"""Admission counts the exact native view while retaining complete source artifacts."""

import hashlib
import json
from dataclasses import asdict, replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_campaign_handoff_context import budget, capture, native_row

from bmad_loop.campaign_model import CampaignError
from bmad_loop.campaign_summary import (
    ContextBudget,
    load_input,
    plan_maps,
    plan_reduction,
    project_reduction_input,
    validate_summary,
)
from bmad_loop.campaign_transcripts import encoded


def recorded_size_children(tmp_path):
    history = capture(tmp_path, [native_row("codex")])
    from bmad_loop.campaign_transcripts import load_events

    events = load_events(history)
    source = plan_maps(events, budget(), tmp_path / "maps")[0]
    fragment = load_input(source)["primary"][0]["id"]
    children = []
    for index, size in enumerate((33912, 30050)):
        child = replace(source, id=f"child-{index}", output_tokens=32768)
        result = {
            "schema_version": 1,
            "job_id": child.id,
            "input_sha256": child.input_sha256,
            "covered_ids": list(child.primary_ids),
            "gaps": [],
            "facts": [
                {"kind": "progress", "text": f"Detail {n}", "citations": [fragment]}
                for n in range(20)
            ],
        }
        normalized = validate_summary(child, result, events, observed_output_tokens=10)
        # Reproduce the exact full-child sizes from the finite native corpus,
        # using synthetic text and real legacy fragment provenance, not private chat.
        result["facts"][-1]["text"] += "x" * (size - len(encoded(normalized)))
        normalized = validate_summary(child, result, events, observed_output_tokens=10)
        assert len(encoded(normalized)) == size
        children.append((child, normalized))
    return tuple(children)


def fable_budget():
    # Actual earned Fable/high geometry after independent input/context charging.
    return ContextBudget(
        "qualified-fable-high",
        82599,
        32768,
        prompt_tokens=8192,
        working_tokens=8192,
        qualified=True,
    )


def test_project_before_partition_preserves_full_children_and_native_citations(tmp_path):
    children = recorded_size_children(tmp_path)
    original = [encoded(result) for _, result in children]
    with pytest.raises(CampaignError, match="child summary exceeds"):
        plan_reduction(children, fable_budget(), tmp_path / "legacy")
    jobs = plan_reduction(
        children,
        fable_budget(),
        tmp_path / "projected",
        artifact_bytes=16565,
        project_native_events=True,
    )
    assert len(jobs) == 2
    assert set(key for job in jobs for key in job.dependencies) == {
        child.id for child, _ in children
    }
    for job, (child, full), before in zip(jobs, children, original, strict=True):
        payload = load_input(job)
        assert payload["citation_presentation"] == "native-events-v1"
        assert (
            job.input_tokens
            == len(Path(job.input_path).read_bytes())
            <= fable_budget().payload_tokens
        )
        reference = payload["source_input"]
        source = Path(reference["path"]).read_bytes()
        assert hashlib.sha256(source).hexdigest() == reference["sha256"]
        assert encoded(json.loads(source)["children"][0]["summary"]) == before
        assert encoded(full) == before
        assert job.primary_ids == job.dependencies == (child.id,)
        assert job.event_ids == tuple(
            dict.fromkeys(ref for fact in full["facts"] for ref in fact["citations"])
        )
        for fact, expected in zip(
            payload["children"][0]["summary"]["facts"], full["facts"], strict=True
        ):
            assert fact["text"] == expected["text"] and fact["citations"] == expected["citations"]
            assert "fragment_provenance" not in fact and "id" not in fact
        assert project_reduction_input(job, tmp_path / "unused") == job
        assert not (tmp_path / "unused").exists()


def test_projection_budget_includes_complete_source_wrapper_at_exact_boundary(tmp_path):
    children = recorded_size_children(tmp_path)[:1]
    directory = tmp_path / "évidence"
    job = plan_reduction(children, fable_budget(), directory, project_native_events=True)[0]
    payload = load_input(job)
    without_wrapper = len(
        encoded({key: value for key, value in payload.items() if key != "source_input"})
    )
    assert without_wrapper < job.input_tokens
    exact = replace(fable_budget(), context_tokens=job.input_tokens + 32768 + 16384)
    assert (
        plan_reduction(children, exact, directory, project_native_events=True)[0].input_tokens
        == job.input_tokens
    )
    with pytest.raises(CampaignError, match="child summary exceeds"):
        plan_reduction(
            children,
            replace(exact, context_tokens=exact.context_tokens - 1),
            directory,
            project_native_events=True,
        )


def test_handoff_reduce_constructs_projected_requests_from_current_route_geometry(
    tmp_path, monkeypatch
):
    from bmad_loop import campaign_handoff_flow as flow
    from bmad_loop.campaign_headless import render_input
    from bmad_loop.campaign_jobs import request_from_dict
    from bmad_loop.campaign_model import ReasoningSpec, RoleCandidate
    from bmad_loop.campaign_transcripts import blob

    children = recorded_size_children(tmp_path)
    rows = {}
    for child, result in children:
        data = encoded(result)
        path = blob(tmp_path / "applied", data)
        rows[child.id] = {
            "state": "applied",
            "request": {"purpose": "handoff_map", "coordinates": {"summary_spec": asdict(child)}},
            "applied_result": {
                "artifact_path": str(path),
                "artifact_sha256": hashlib.sha256(data).hexdigest(),
            },
        }
    candidate = RoleCandidate("claude", "claude-test", "high")
    reader = {
        "input_tokens": 99662,
        "output_tokens": 32768,
        "context_tokens": 200000,
        "reserved_tokens": 65536,
        "qualification": {"sha256": "scripted-reader"},
        "input_mode": "claude-stream-stdin",
    }
    writer = {
        "input_tokens": 132163,
        "output_tokens": 16384,
        "context_tokens": 262144,
        "reserved_tokens": 65536,
        "qualification": {"sha256": "scripted-writer"},
    }
    # I/O/route selection is injected here; the real producer owns partitioning,
    # complete-source bindings, stored-artifact mode and complete prompt admission.
    monkeypatch.setattr(flow, "_job", lambda runtime, snapshot, key: rows[key])
    monkeypatch.setattr(flow, "_summary_route", lambda *args, **kwargs: (candidate, reader))
    monkeypatch.setattr(flow, "_target_cap", lambda *args, **kwargs: writer)
    monkeypatch.setattr(flow, "_root", lambda *args: tmp_path / "handoff")
    monkeypatch.setattr(
        flow,
        "_coordinates",
        lambda *args: {"head": "", "base": "", "graph_revision": 1, "config_revision": 1},
    )
    snapshot = SimpleNamespace(
        config_revision=1,
        spec=SimpleNamespace(reasoning=ReasoningSpec(packet_token_limit=160000, timeout_s=600)),
    )
    runtime, unit = SimpleNamespace(clock=lambda: 1.0), SimpleNamespace(id="story")
    value = {
        "id": "handoff",
        "jobs": list(rows),
        "candidate": asdict(candidate),
        "role": "execution",
        "capture": {"sources": []},
    }
    planned = flow._reduce(runtime, snapshot, unit, value)
    assert isinstance(planned, dict) and planned["reduction"]
    raw_requests = planned["requests"]
    assert isinstance(raw_requests, list) and len(raw_requests) == 2
    assert all(isinstance(raw, dict) for raw in raw_requests)
    requests = [request_from_dict(raw) for raw in raw_requests]
    assert {key for request in requests for key in request.dependencies} == set(rows)
    for request in requests:
        spec = request.coordinates["summary_spec"]
        assert spec["artifact_bytes"] == 0
        assert request.limits.output_tokens == reader["output_tokens"]
        assert request.inputs[0]["tokens"] <= flow._budget(candidate, reader).payload_tokens
        _, manifest = render_input(request, reader)
        assert manifest["token_upper_bound"] <= 99662
