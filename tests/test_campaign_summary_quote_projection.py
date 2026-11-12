"""Exact owner spans reach every synthesis stage before admission and partitioning."""

import copy
import hashlib
import json
from dataclasses import asdict, replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_campaign_handoff_context import budget, capture, native_row

from bmad_loop import campaign_handoff_flow as flow
from bmad_loop import campaign_summary_routes as routes
from bmad_loop.campaign_headless import render_input
from bmad_loop.campaign_history_trial import _plan_reducers, trial_request
from bmad_loop.campaign_jobs import request_from_dict
from bmad_loop.campaign_model import CampaignError, ReasoningSpec, RoleCandidate
from bmad_loop.campaign_summary import (
    REDUCE_PROMPT_VERSION,
    corrective_spec,
    load_input,
    owner_quote_references,
    plan_maps,
    plan_reduction,
    project_final_input,
    project_reduction_input,
    quote_catalog,
    split_reduction_input,
    validate_summary,
)
from bmad_loop.campaign_summary_quotes import resolve_owner_quotes
from bmad_loop.campaign_transcripts import blob, encoded, load_events


@pytest.fixture
def quoted_children(tmp_path):
    texts = (
        "Correction superseding my initial merge direction: HOLD PR #17. Preserve branch "
        "fix/cache, dirty src/cache.py and every accepted commit. Do not merge until I explicitly lift this hold.",
        "Before continuation, prove predecessor writers stopped and give the successor its own identity. "
        "Retain exact history retrieval and reconcile PR #17 before claiming delivery. Préserver 🧭.",
    )
    history = capture(
        tmp_path, [native_row("codex", text, f"m{i}") for i, text in enumerate(texts)]
    )
    events = load_events(history)
    children = []
    for index, event in enumerate(events):
        job = plan_maps((event,), budget(), tmp_path / f"maps-{index}")[0]
        quote_id = next(iter(quote_catalog(job)))
        result = {
            "schema_version": 1,
            "job_id": job.id,
            "input_sha256": job.input_sha256,
            "covered_ids": list(job.primary_ids),
            "gaps": [],
            "facts": [
                {
                    "kind": "owner_request",
                    "text": "Child interpretation; refer to source.",
                    "citations": [event.id],
                    "source_quote_id": quote_id,
                }
            ],
        }
        if index == 0:
            result["facts"].append(
                {**result["facts"][0], "text": "Another interpretation of the same span."}
            )
        children.append((job, validate_summary(job, result, events)))
    table = resolve_owner_quotes((history,), owner_quote_references([r for _, r in children]))
    return history, tuple(children), table


def capability():
    return {
        "profile": "grok",
        "model": "grok-test",
        "efforts": ["medium"],
        "input_tokens": 100000,
        "output_tokens": 2500,
        "context_tokens": 120000,
        "reserved_tokens": 1000,
        "input_mode": "prompt-file",
        "qualification": {"sha256": "scripted-geometry"},
    }


@pytest.mark.parametrize("projected", [False, True])
@pytest.mark.parametrize("stage", ["intermediate", "final"])
def test_synthesis_receives_full_exact_quotes_once_without_changing_leaf_results(
    tmp_path, quoted_children, projected, stage
):
    history, children, table = quoted_children
    before = [encoded(result) for _, result in children]
    job = plan_reduction(
        children, budget(), tmp_path / "reduce", owner_quotes=table, project_native_events=projected
    )[0]
    if stage == "final":
        job = project_final_input(job, tmp_path / "final")
    payload = load_input(job)
    assert payload["owner_quotes"] == table and len(table) == 2
    assert len(owner_quote_references([r for _, r in children])) == 3
    assert [encoded(result) for _, result in children] == before
    assert all("source_quote" not in fact for _, value in children for fact in value["facts"])
    for identity, quote in table.items():
        assert quote_catalog(job)[identity]["preview"] == quote["text"][:80]
        assert "Child interpretation" not in quote["text"]
    request = trial_request(job, capability(), (history,), 600)
    rendered, manifest = render_input(request, capability())
    # The actual source delivered by HeadlessRunner contains the full quote,
    # including the hold's branch/path and continuation versus delivery timing.
    delivered = next(
        json.loads(line)
        for line in rendered.decode().splitlines()
        if line.startswith('{"source_id":')
    )
    assert json.loads(delivered["content"]) == payload
    assert "dirty src/cache.py" in rendered.decode()
    assert "before claiming delivery" in rendered.decode()
    assert manifest["token_upper_bound"] == len(rendered)
    assert job.input_tokens == len(Path(job.input_path).read_bytes())
    with pytest.raises(CampaignError, match="Rendered prompt/source/schema"):
        render_input(request, {**capability(), "input_tokens": len(rendered) - 1})


def test_exact_quote_bytes_participate_in_whole_child_partitioning(tmp_path, quoted_children):
    _, children, table = quoted_children
    root = tmp_path / "reduce"
    whole = plan_reduction(
        children, budget(), root, owner_quotes=table, project_native_events=True
    )[0]
    payload = load_input(whole)
    without_quotes = len(encoded({**payload, "owner_quotes": {}}))
    ceiling = whole.input_tokens - 1
    assert without_quotes < ceiling
    tight = replace(budget(), context_tokens=ceiling + budget().output_tokens + 400)
    jobs = plan_reduction(children, tight, root, owner_quotes=table, project_native_events=True)
    assert len(jobs) == 2  # A table added after planning would incorrectly keep one group.
    assert {key for job in jobs for key in load_input(job)["owner_quotes"]} == set(table)
    for job in jobs:
        assert len(job.primary_ids) == 1 and job.input_tokens <= ceiling
        assert len(load_input(job)["owner_quotes"]) == 1


def test_quotes_survive_projection_split_correction_and_later_fold(tmp_path, quoted_children):
    history, children, table = quoted_children
    job = plan_reduction(children, budget(), tmp_path / "reduce", owner_quotes=table)[0]
    projected = project_reduction_input(job, tmp_path / "projected")
    assert load_input(projected)["owner_quotes"] == table
    split = split_reduction_input(projected, tmp_path / "split")
    assert len(split) == 2
    folded = []
    events = load_events(history)
    for part in split:
        payload = load_input(part)
        assert len(payload["owner_quotes"]) == 1
        corrected = corrective_spec(part, attempt=1, receipt_sha256="rejection", reason="coverage")
        assert corrected.input_path == part.input_path and load_input(corrected) == payload
        value = {
            "schema_version": 1,
            "job_id": part.id,
            "input_sha256": part.input_sha256,
            "covered_ids": list(part.primary_ids),
            "gaps": [],
            "facts": payload["children"][0]["summary"]["facts"],
        }
        folded.append((part, validate_summary(part, value, events)))
    again = resolve_owner_quotes((history,), owner_quote_references([r for _, r in folded]))
    final = plan_reduction(tuple(folded), budget(), tmp_path / "final", owner_quotes=again)[0]
    assert load_input(final)["owner_quotes"] == table
    assert final.dependencies == tuple(part.id for part in split)


@pytest.mark.parametrize("fault", ["missing", "paraphrase", "range", "reference-conflict"])
def test_reducer_refuses_missing_or_different_exact_quote_table(tmp_path, quoted_children, fault):
    _, children, original = quoted_children
    table = copy.deepcopy(original)
    identity = next(iter(table))
    if fault == "missing":
        table.pop(identity)
    elif fault == "paraphrase":
        table[identity]["text"] = "Child interpretation; refer to source."
    elif fault == "range":
        table[identity]["end"] += 1
    else:
        children = copy.deepcopy(children)
        children[0][1]["facts"][1]["source_quote_ref"]["sha256"] = "0" * 64
    with pytest.raises(CampaignError, match="owner quote"):
        plan_reduction(children, budget(), tmp_path / "reduce", owner_quotes=table)


def test_trial_resolves_owner_spans_before_production_planning(tmp_path, quoted_children):
    history, children, table = quoted_children
    jobs = _plan_reducers(
        children, capability(), tmp_path / "trial", "adaptive-render-v2", (history,)
    )
    assert len(jobs) == 1 and load_input(jobs[0])["owner_quotes"] == table
    assert jobs[0].prompt_version == REDUCE_PROMPT_VERSION


def test_normal_handoff_counts_quotes_before_final_fit_and_resolves_once(
    tmp_path, quoted_children, monkeypatch
):
    history, children, table = quoted_children
    # Choose a final window which fits the children, but not their exact owner spans.
    root = tmp_path / "handoff"
    full = plan_reduction(
        children, budget(), root / "reductions", owner_quotes=table, project_native_events=True
    )[0]
    small = replace(budget(), context_tokens=full.input_tokens - 1 + 2500 + 400)
    rows = {}
    for job, result in children:
        content = encoded(result)
        path = blob(tmp_path / "applied", content)
        rows[job.id] = {
            "state": "applied",
            "request": {"purpose": "handoff_map", "coordinates": {"summary_spec": asdict(job)}},
            "applied_result": {
                "artifact_path": str(path),
                "artifact_sha256": hashlib.sha256(content).hexdigest(),
            },
        }
    candidates = {
        stage: RoleCandidate("grok", "grok-" + stage, "medium")
        for stage in ("final", "intermediate")
    }
    monkeypatch.setattr(routes, "enabled", lambda spec: True)
    monkeypatch.setattr(flow, "_job", lambda runtime, snapshot, key: rows[key])
    monkeypatch.setattr(flow, "_root", lambda *args: root)
    monkeypatch.setattr(flow, "_target_cap", lambda *args, **kwargs: capability())
    monkeypatch.setattr(
        flow,
        "_budget",
        lambda candidate, cap: small if candidate == candidates["final"] else budget(),
    )
    monkeypatch.setattr(
        flow,
        "_summary_route",
        lambda *args, **kwargs: (candidates[kwargs.get("stage", "intermediate")], capability()),
    )
    monkeypatch.setattr(flow, "_summary_budget", lambda *args: (budget(), 0))
    monkeypatch.setattr(
        flow,
        "_coordinates",
        lambda *args: {"graph_revision": 1, "config_revision": 1, "head": "", "base": ""},
    )
    monkeypatch.setattr(flow, "_stage_coordinates", lambda *args: {"summary_stage": args[-1]})
    from bmad_loop import campaign_summary_quotes

    resolutions = []

    def counted(snapshots, refs):
        resolutions.append((snapshots, refs))
        return resolve_owner_quotes(snapshots, refs)

    monkeypatch.setattr(campaign_summary_quotes, "resolve_owner_quotes", counted)
    runtime = SimpleNamespace(clock=lambda: 1.0)
    snapshot = SimpleNamespace(config_revision=1, spec=SimpleNamespace(reasoning=ReasoningSpec()))
    value = {
        "id": "handoff",
        "jobs": list(rows),
        "candidate": asdict(candidates["final"]),
        "role": "execution",
        "capture": {"sources": [asdict(history)]},
    }
    result = flow._reduce(runtime, snapshot, SimpleNamespace(id="story"), value)
    assert result is not None
    assert len(resolutions) == 1
    raw_requests = result["requests"]
    assert isinstance(raw_requests, list) and all(isinstance(row, dict) for row in raw_requests)
    requests = [request_from_dict(row) for row in raw_requests]
    assert requests and all(r.coordinates["summary_stage"] == "intermediate" for r in requests)
    for request in requests:
        from bmad_loop.campaign_summary import SummaryJobSpec

        assert (
            load_input(SummaryJobSpec(**request.coordinates["summary_spec"]))["owner_quotes"]
            == table
        )
        render_input(request, capability())
