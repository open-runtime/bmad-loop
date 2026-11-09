"""Incremental extraction has no write lease and final reuse requires exact bytes."""

import dataclasses
import json
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_campaign_handoff_flow import campaign as campaign
from test_campaign_headless import prepared as prepared

from bmad_loop import campaign_history_warmup as warmup
from bmad_loop import campaign_reasoning_flow as reasoning
from bmad_loop import runs
from bmad_loop.adapters.base import SessionHandle, SessionSpec
from bmad_loop.campaign_jobs import current, request_from_dict
from bmad_loop.campaign_model import OutboxAction
from bmad_loop.campaign_reflection_flow import observe_adapter
from bmad_loop.campaign_transcripts import snapshot_history


@pytest.fixture
def active(campaign, monkeypatch):
    runtime, snapshot, unit, receipt = campaign
    monkeypatch.setattr(warmup, "MIN_BYTES", 1)
    unit.claim.quiescent = False
    unit.claim.run_id = "native-run"
    unit.claim.branch = unit.bindings["branch"]
    directory = runtime.store.directory(snapshot.id) / "workers" / unit.claim.launch_id
    run_dir = runs.run_dir_for(Path(unit.claim.workspace), unit.claim.run_id)
    adapter = SimpleNamespace(
        start_session=lambda spec: SessionHandle(spec.task_id, "native", 1, "native-before")
    )
    observe_adapter(
        adapter,
        directory=directory,
        run_dir=run_dir,
        claim=unit.claim,
        campaign_id=snapshot.id,
        environment={},
    )
    adapter.start_session(SessionSpec("dev-task", "dev", "test", Path(unit.claim.workspace)))
    events = runs.events_dir_for(Path(unit.claim.workspace), unit.claim.run_id)
    events.mkdir(parents=True, exist_ok=True)
    source = Path(receipt["sessions"][0]["transcript_path"])
    (events / "start.json").write_text(
        json.dumps(
            {
                "event": "SessionStart",
                "task_id": "dev-task",
                "session_id": "native-before",
                "transcript_path": str(source),
                "ts": 10,
            }
        )
    )
    return runtime, snapshot, unit, source


def observe(active):
    runtime, snapshot, unit, _ = active
    (proposal,) = warmup.proposals(runtime, snapshot)
    action = OutboxAction("warm", proposal.kind, unit.id, proposal.payload, snapshot.generation)
    observation = warmup.perform(runtime, snapshot, action)
    assert observation.state == "confirmed", observation
    warmup.apply(runtime, snapshot, action, observation)
    return observation


def finish(runtime, snapshot, unit):
    from bmad_loop.campaign_reasoning_results import apply_result
    from bmad_loop.campaign_summary import SummaryJobSpec, load_input, quote_catalog

    for key in unit.evidence["history_warmup"]["pending_jobs"]:
        row = snapshot.jobs[key]
        spec = SummaryJobSpec(**row["request"]["coordinates"]["summary_spec"])
        source = load_input(spec)
        quotes = quote_catalog(spec)
        result = {
            "schema_version": 1,
            "job_id": key,
            "input_sha256": spec.input_sha256,
            "covered_ids": list(spec.primary_ids),
            "gaps": [],
            "facts": [
                {
                    "kind": "owner_request",
                    "text": part["text"],
                    "source_quote_id": next(
                        q for q, value in quotes.items() if value["event_id"] == part["event_id"]
                    ),
                    "citations": [part["event_id"]],
                }
                for part in source["primary"]
                if part["owner_input"]
            ],
        }
        row.update(
            state="validated",
            validation="valid",
            result=result,
            receipt={"job_id": key, "result": result, "provider_family": "openai"},
        )
        apply_result(runtime, snapshot, row)
        row["state"] = "applied"


def final_sources(active, tmp_path):
    _, _, _, source = active
    return [
        dataclasses.asdict(snapshot_history(source, "codex", "native-before", tmp_path / "final"))
    ]


def test_hook_bound_live_capture_is_optional_and_survives_graph_change(active):
    runtime, snapshot, unit, _ = active
    observed = observe(active)
    assert observed.data["requests"]
    assert unit.claim and not unit.claim.quiescent and unit.hold is None
    request = request_from_dict(observed.data["requests"][0])
    assert not request.required and "handoff_id" not in request.coordinates
    snapshot.graph_revision += 1
    assert current(snapshot, request)
    unit.claim.quiescent = True
    assert current(snapshot, request)
    assert not warmup.launch_allowed(snapshot, request)
    unit.claim.launch_id = "replacement"
    assert not current(snapshot, request)


@pytest.fixture
def prepared_warmup(active):
    runtime, snapshot, unit, _ = active
    snapshot.generation = 1
    runtime._role = lambda snapshot, role, **kwargs: kwargs["allowed_candidates"][0]
    observe(active)
    proposal = next(
        row for row in reasoning.proposals(runtime, snapshot) if row.kind == "reasoning-launch"
    )
    action = OutboxAction(
        "warmup-launch", proposal.kind, unit.id, proposal.payload, snapshot.generation
    )
    reasoning.prepare_effect(runtime, snapshot, action)
    snapshot.outbox.append(action)
    snapshot = runtime.store.save(snapshot, snapshot.revision)
    row = snapshot.jobs[action.payload["job_id"]]
    return (
        runtime,
        snapshot,
        snapshot.units[unit.id],
        row,
        action,
        Path(row["claim"]["run_dir"]) / "request.json",
    )


@pytest.mark.parametrize("boundary", ["quiescence", "handoff"])
def test_final_admission_defers_unstarted_warmup_after_preparation(prepared_warmup, boundary):
    runtime, snapshot, unit, row, action, path = prepared_warmup
    request = request_from_dict(row["request"])
    assert warmup.launch_allowed(snapshot, request)
    if boundary == "quiescence":
        unit.claim.quiescent = True
    else:
        unit.evidence["handoff"] = {"phase": "drain"}
    runtime.store.save(snapshot, snapshot.revision)
    assert current(snapshot, request)
    assert not warmup.launch_allowed(snapshot, request)
    guarded = reasoning._launch_guard(runtime, snapshot, row, action, path)
    assert guarded is not None
    assert guarded.data["state"] == "deferred"
    reasoning.apply(runtime, snapshot, action, guarded)
    assert row["claim"]["quiescent"] and row["claim"]["status"] == "not_launched"
    assert row["state"] == "proposed" and row.get("failure_attempts", 0) == 0
    assert not (path.parent / "launch-intent.json").exists()


def test_issued_warmup_completes_and_reuses_after_writer_quiescence(
    active, prepared_warmup, tmp_path, monkeypatch
):
    from test_campaign_handoff_runtime import ScriptedReasoning

    from bmad_loop.campaign_handoff_flow import _cap
    from bmad_loop.campaign_headless import write_json
    from bmad_loop.campaign_supervisor import Observation

    runtime, snapshot, unit, row, action, path = prepared_warmup
    request = request_from_dict(row["request"])
    candidate = request.candidates[0]
    runner = ScriptedReasoning()
    runtime.reasoning_runner = runner
    runner.prepare(
        path.parent,
        request,
        candidate,
        _cap(snapshot, candidate, "reasoning"),
        launch_id=action.id,
        generation=action.generation,
    )

    @contextmanager
    def admitted():
        with reasoning._admission(runtime, snapshot, row, action, path):
            write_json(
                path.parent / "launch-intent.json",
                {
                    "launch_id": action.id,
                    "generation": action.generation,
                    "phase": "window-pending",
                },
            )
            yield

    completed = runner.launch(path, admission=admitted)
    unit.claim.quiescent = True
    runtime.store.save(snapshot, snapshot.revision)
    assert not warmup.launch_allowed(snapshot, request)
    monkeypatch.setattr(runner, "launch", lambda *a, **kw: pytest.fail("replacement launch"))
    assert reasoning._launch_guard(runtime, snapshot, row, action, path) is None
    observed = reasoning.perform(runtime, snapshot, action, reconcile=True)
    assert observed.data["receipt"] == completed["receipt"]
    reasoning.apply(runtime, snapshot, action, observed)
    assert row["state"] == "validated" and row["claim"]["quiescent"]
    apply_action = OutboxAction(
        "warmup-apply", "reasoning-apply", unit.id, {"job_id": row["id"]}, snapshot.generation
    )
    reasoning.prepare_effect(runtime, snapshot, apply_action)
    reasoning.apply(runtime, snapshot, apply_action, Observation("confirmed", {}))
    assert row["state"] == "applied"
    completed, rejected = warmup.reusable(runtime, snapshot, unit, final_sources(active, tmp_path))
    assert [spec.id for spec, _ in completed] == [row["id"]] and not rejected


def test_appended_tail_reuses_complete_prefix_but_rewrite_drops_old_facts(active, tmp_path):
    runtime, snapshot, unit, source = active
    # A partial JSONL tail is not a completed owner record, but it still belongs
    # to the sealed raw prefix that must match before any cache reuse.
    source.write_text(source.read_text() + '{"type":"event_msg","payload":')
    observe(active)
    finish(runtime, snapshot, unit)
    keys = unit.evidence["history_warmup"]["pending_jobs"][:]
    with source.open("a") as stream:
        stream.write('{"type":"user_message","message":"Later correction"}}\n')
    completed, rejected = warmup.reusable(runtime, snapshot, unit, final_sources(active, tmp_path))
    assert [spec.id for spec, _ in completed] == keys and not rejected
    source.write_text(source.read_text().replace("Keep history", "Drop history"))
    completed, rejected = warmup.reusable(runtime, snapshot, unit, final_sources(active, tmp_path))
    assert not completed and rejected == keys


def test_cached_old_route_survives_capacity_fallback(active, tmp_path):
    runtime, snapshot, unit, _ = active
    observe(active)
    finish(runtime, snapshot, unit)
    # No admission call is needed to reuse a qualified old route. An exhausted
    # provider is unavailable for new work, not evidence that old work vanished.
    runtime._role = lambda *args, **kwargs: None
    completed, rejected = warmup.reusable(runtime, snapshot, unit, final_sources(active, tmp_path))
    assert completed and not rejected


def test_current_requires_durable_pending_membership(active):
    _, snapshot, unit, _ = active
    observed = observe(active)
    request = request_from_dict(observed.data["requests"][0])
    assert current(snapshot, request)
    unit.evidence["history_warmup"]["pending_jobs"] = []
    assert not current(snapshot, request)


def test_observation_rearms_after_missing_hook_and_archived_completion(active, monkeypatch):
    runtime, snapshot, unit, _ = active
    events = runs.events_dir_for(Path(unit.claim.workspace), unit.claim.run_id)
    event = events / "start.json"
    content = event.read_text()
    event.unlink()
    first = observe(active)
    assert not first.data["requests"] and unit.hold is None
    event.write_text(content)
    runtime.clock = lambda: 200.0
    second = observe(active)
    assert second.data["requests"]
    finish(runtime, snapshot, unit)
    snapshot = runtime.store.save(snapshot, snapshot.revision)
    active = runtime, snapshot, snapshot.units[unit.id], active[3]
    runtime.clock = lambda: 300.0
    monkeypatch.setattr(
        warmup,
        "snapshot_history",
        lambda *args, **kwargs: pytest.fail("unchanged history was resealed"),
    )
    last = observe(active)
    assert not last.data["requests"] and last.data["state"]["state"] == "available"


@pytest.mark.parametrize("change", ["claim", "run", "hook"])
def test_unrelated_handle_or_hook_cannot_supply_history(active, change):
    runtime, snapshot, unit, _ = active
    handle_path = (
        runtime.store.directory(snapshot.id)
        / "workers"
        / unit.claim.launch_id
        / "native-sessions"
        / "dev-task.json"
    )
    if change == "hook":
        event = runs.events_dir_for(Path(unit.claim.workspace), unit.claim.run_id) / "start.json"
        row = json.loads(event.read_text())
        row["session_id"] = "unrelated"
        event.write_text(json.dumps(row))
    else:
        row = json.loads(handle_path.read_text())
        row["launch_id" if change == "claim" else "run_dir"] = (
            "unrelated"
            if change == "claim"
            else str(Path(unit.claim.workspace) / "foreign" / unit.claim.run_id)
        )
        handle_path.write_text(json.dumps(row))
    observation = observe(active)
    assert observation.data["state"]["state"] == "unavailable"
    assert not observation.data["requests"] and unit.hold is None


def test_optional_warmup_stops_new_admission_on_drain(active):
    runtime, snapshot, unit, _ = active
    assert warmup.proposals(runtime, snapshot)
    snapshot.phase = "draining"
    assert not warmup.proposals(runtime, snapshot)


def test_corrupt_optional_cache_does_not_strand_required_handoff(active, tmp_path):
    runtime, snapshot, unit, _ = active
    observe(active)
    finish(runtime, snapshot, unit)
    index = Path(unit.evidence["history_warmup"]["index_path"])
    index.write_text("{}")
    completed, rejected = warmup.reusable(runtime, snapshot, unit, final_sources(active, tmp_path))
    assert not completed and rejected


@pytest.mark.parametrize("damage", ["result", "prompt", "schema", "route"])
def test_optional_result_or_extraction_contract_damage_is_reextracted(active, tmp_path, damage):
    runtime, snapshot, unit, _ = active
    observe(active)
    finish(runtime, snapshot, unit)
    key = unit.evidence["history_warmup"]["pending_jobs"][0]
    row = snapshot.jobs[key]
    if damage == "result":
        Path(row["applied_result"]["artifact_path"]).write_text("{}")
    elif damage == "route":
        row["request"]["coordinates"]["summary_spec"]["route"] = "obsolete-qualified-route"
    else:
        row["request"]["prompt" if damage == "prompt" else "output_schema"] = (
            "old prompt" if damage == "prompt" else {"type": "object"}
        )
    completed, rejected = warmup.reusable(runtime, snapshot, unit, final_sources(active, tmp_path))
    assert not completed and key in rejected


def test_exhausted_optional_failure_does_not_block_later_observation(active):
    runtime, snapshot, unit, _ = active
    observe(active)
    key = unit.evidence["history_warmup"]["pending_jobs"][0]
    snapshot.jobs[key].update(state="failed", failure_attempts=snapshot.spec.reasoning.max_attempts)
    runtime.clock = lambda: 200.0
    assert warmup.proposals(runtime, snapshot)


def test_rewritten_unparsed_tail_invalidates_cache_even_when_old_events_match(active, tmp_path):
    runtime, snapshot, unit, source = active
    source.write_text(source.read_text() + '{"type":')
    observe(active)
    finish(runtime, snapshot, unit)
    keys = unit.evidence["history_warmup"]["pending_jobs"][:]
    # All previously normalized event IDs and texts still match. Only the sealed
    # incomplete raw tail changed, so fragment matching alone cannot detect this.
    text = source.read_text()
    source.write_text(
        text[: -len('{"type":')]
        + '{ "type":"event_msg","payload":{"type":"user_message","message":"Tail"}}\n'
    )
    completed, rejected = warmup.reusable(runtime, snapshot, unit, final_sources(active, tmp_path))
    assert not completed and rejected == keys


def test_bounded_coverage_remains_visible_when_unchanged(active, monkeypatch):
    from bmad_loop.campaign_operator import diagnostics

    runtime, _, unit, _ = active
    monkeypatch.setattr(warmup, "PLAN_BYTES", 1)
    observe(active)
    first = diagnostics(unit)["history_warmup"]["deferred_large_events"]
    assert first > 0
    runtime.clock = lambda: 200.0
    observe(active)
    assert diagnostics(unit)["history_warmup"]["deferred_large_events"] == first


@pytest.mark.parametrize("terminal", ["failed", "stale"])
def test_exhausted_first_batch_advances_later_maps_within_index_bound(
    active, monkeypatch, terminal
):
    from bmad_loop import campaign_handoff_flow as handoff

    runtime, snapshot, unit, source = active
    original_budget = handoff._budget
    monkeypatch.setattr(
        handoff,
        "_budget",
        lambda candidate, cap: dataclasses.replace(
            original_budget(candidate, cap),
            context_tokens=12000,
            output_tokens=1000,
            prompt_tokens=1000,
            working_tokens=1000,
        ),
    )
    monkeypatch.setattr(warmup, "MAX_INDEX_JOBS", 5)
    # One event spans many maps, so the first four attempts cannot make its
    # entire event disappear from the next planning page.
    source.write_text(
        json.dumps(
            {
                "type": "event_msg",
                "payload": {"type": "user_message", "message": "Keep the acceptance. " * 3000},
            }
        )
        + "\n"
    )
    first = observe(active)
    first_ids = {row["id"] for row in first.data["requests"]}
    assert len(first_ids) == warmup.BATCH_SIZE == 4
    for key in first_ids:
        snapshot.jobs[key].update(
            state=terminal, failure_attempts=snapshot.spec.reasoning.max_attempts
        )
    runtime.clock = lambda: 200.0
    second = observe(active)
    later_ids = {row["id"] for row in second.data["requests"]}
    assert len(later_ids) == 1 and later_ids.isdisjoint(first_ids)
    assert all(snapshot.jobs[key]["state"] == terminal for key in first_ids)
    assert len(warmup._index(runtime, snapshot, unit)["jobs"]) == 5


def test_exhausted_full_page_advances_but_is_not_final_handoff_coverage(
    active, monkeypatch, tmp_path
):
    from bmad_loop import campaign_handoff_flow as handoff
    from bmad_loop.campaign_summary import SummaryJobSpec, load_input

    runtime, snapshot, unit, source = active
    monkeypatch.setattr(warmup, "PLAN_BYTES", 3000)
    with source.open("a") as stream:
        for index in range(12):
            stream.write(
                json.dumps(
                    {
                        "type": "event_msg",
                        "payload": {
                            "type": "user_message",
                            "message": str(index) + " Preserve this requirement. " * 20,
                        },
                    }
                )
                + "\n"
            )
    first = observe(active)
    first_events = {
        part["event_id"]
        for row in first.data["requests"]
        for part in load_input(SummaryJobSpec(**row["coordinates"]["summary_spec"]))["primary"]
    }
    assert first_events and first.data["state"]["backlog"]
    for row in first.data["requests"]:
        snapshot.jobs[row["id"]].update(
            state="failed", failure_attempts=snapshot.spec.reasoning.max_attempts
        )
    runtime.clock = lambda: 200.0
    second = observe(active)
    later_events = {
        part["event_id"]
        for row in second.data["requests"]
        for part in load_input(SummaryJobSpec(**row["coordinates"]["summary_spec"]))["primary"]
    }
    assert later_events and later_events.isdisjoint(first_events)
    value = {
        "id": "mandatory-final",
        "capture": {"sources": final_sources(active, tmp_path), "capture_digest": "final"},
    }
    final = handoff._maps(runtime, snapshot, unit, value)
    assert not final["reuse_jobs"]
    final_events = {
        event
        for row in final["requests"]
        for event in row["coordinates"]["summary_spec"]["event_ids"]
    }
    assert first_events <= final_events
