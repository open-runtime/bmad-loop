"""Native trial integration uses a scripted zero-token subprocess."""

import json
from dataclasses import asdict

import pytest
from test_campaign_handoff_context import capture, native_row
from test_campaign_headless import fixture_capability
from test_campaign_headless import prepared as prepared

from bmad_loop.campaign_headless import file_digest, read_json
from bmad_loop.campaign_history_trial import run_trial
from bmad_loop.campaign_model import CampaignError


def fixture_route(prepared, tmp_path):
    runner, _, _, _, cap = prepared
    script = tmp_path / "summary-native.py"
    script.write_text("""import json,sys
text=sys.stdin.read()
source=next(json.loads(line) for line in text.splitlines() if line.startswith('{"source_id":'))
payload=json.loads(source["content"])
result={"schema_version":1,"job_id":source["source_id"],"input_sha256":source["sha256"],"covered_ids":[x["id"] for x in payload["primary"]],"facts":[],"gaps":[]}
print(json.dumps({"type":"system","model":"gpt-test","session_id":"native-trial"}))
print(json.dumps({"type":"result","structured_output":result,"usage":{"input_tokens":1000,"output_tokens":512}}))
""")
    cap = {
        **cap,
        "argv": [cap["binary"], str(script)],
        "dependencies": [{"path": str(script), "sha256": file_digest(script)}],
    }
    cap = fixture_capability(tmp_path, cap)
    path = tmp_path / "capability.json"
    path.write_text(json.dumps(cap))
    snapshot = capture(tmp_path, [native_row("codex")])
    snapshots = tmp_path / "snapshots.json"
    snapshots.write_text(json.dumps({"schema_version": 1, "snapshots": [asdict(snapshot)]}))
    return runner, path, snapshots


def test_trial_runs_full_production_validator_and_resumes_without_new_actor(tmp_path, prepared):
    runner, capability, snapshots = fixture_route(prepared, tmp_path)
    directory = tmp_path / "trial"
    report = run_trial(snapshots, capability, directory, runner=runner, timeout_s=20)
    assert report["ok"] and report["state"] == "structurally-complete"
    assert report["completed_jobs"] == report["map_jobs"] == 1
    assert report["semantic_fidelity"] == "unqualified"
    request = next((directory / "jobs").glob("*/request.json"))
    before = request.read_bytes()
    again = run_trial(snapshots, capability, directory, runner=runner, timeout_s=20)
    assert again["ok"] and request.read_bytes() == before
    assert again["usage"]["output_tokens"] == 512


def test_prepared_trial_is_explicitly_incomplete_and_has_no_native_launch(tmp_path, prepared):
    runner, capability, snapshots = fixture_route(prepared, tmp_path)
    report = run_trial(snapshots, capability, tmp_path / "trial", runner=runner, execute=False)
    assert report["state"] == "prepared" and report["map_jobs"] == 1
    assert not report["ok"]
    assert not (tmp_path / "trial/jobs").exists()


def test_trial_resume_rejects_changed_route(tmp_path, prepared):
    runner, capability, snapshots = fixture_route(prepared, tmp_path)
    directory = tmp_path / "trial"
    run_trial(snapshots, capability, directory, runner=runner, execute=False)
    cap = read_json(capability)
    cap["reserved_tokens"] += 1
    cap = fixture_capability(tmp_path, cap)
    capability.write_text(json.dumps(cap))
    with pytest.raises(CampaignError, match="different frozen"):
        run_trial(snapshots, capability, directory, runner=runner, execute=False)


def test_capture_explicit_prefix_ignores_later_complete_records(tmp_path):
    from bmad_loop.campaign_transcripts import encoded, load_events, snapshot_history

    first = encoded(native_row("codex", native_id="m1")) + b"\n"
    path = tmp_path / "native.jsonl"
    path.write_bytes(first + encoded(native_row("codex", "later", native_id="m2")) + b"\n")
    snapshot = snapshot_history(path, "codex", "s1", tmp_path / "archive", prefix_bytes=len(first))
    assert snapshot.captured_bytes == len(first)
    assert len(load_events(snapshot)) == 1
    with pytest.raises(CampaignError, match="capture bound"):
        snapshot_history(
            path, "codex", "s1", tmp_path / "archive", prefix_bytes=path.stat().st_size + 1
        )


def test_trial_planning_accounts_for_rendered_json_escaping(tmp_path, prepared):
    runner, capability, snapshots = fixture_route(prepared, tmp_path)
    snapshot = capture(tmp_path, [native_row("codex", text="\\" * 60000)])
    snapshots.write_text(json.dumps({"schema_version": 1, "snapshots": [asdict(snapshot)]}))
    directory = tmp_path / "escaped-trial"
    report = run_trial(snapshots, capability, directory, runner=runner, execute=False)
    assert report["map_jobs"] > 1
    bounds = read_json(directory / "binding.json")["budget"]
    assert (
        bounds["context_tokens"] - bounds["output_tokens"]
        == read_json(capability)["input_tokens"] * 3 // 4 - 8192
    )
    assert read_json(directory / "binding.json")["planning"] == "adaptive-render-v2"


def test_trial_corrects_rejected_completed_result_as_distinct_bounded_job(tmp_path, prepared):
    runner, capability, snapshots = fixture_route(prepared, tmp_path)
    cap = read_json(capability)
    script = tmp_path / "summary-native.py"
    text = script.read_text().replace(
        "import json,sys", "import json,sys\nfrom pathlib import Path"
    )
    text = text.replace(
        'print(json.dumps({"type":"system"',
        'marker=Path(__file__).with_suffix(".attempt")\nif not marker.exists():\n result["covered_ids"]=["invalid-id"]\n marker.write_text("attempted")\nprint(json.dumps({"type":"system"',
    )
    script.write_text(text)
    cap["dependencies"] = [{"path": str(script), "sha256": file_digest(script)}]
    cap = fixture_capability(tmp_path, cap)
    capability.write_text(json.dumps(cap))
    directory = tmp_path / "corrected"
    report = run_trial(snapshots, capability, directory, runner=runner, timeout_s=20)
    assert report["ok"]
    assert len(list((directory / "jobs").glob("*/receipt.json"))) == 2
    assert len(list((directory / "jobs").glob("*/rejected.json"))) == 1
    validated = next((directory / "jobs").glob("*/validated.json"))
    request = read_json(validated.parent / "request.json")["request"]
    assert request["coordinates"]["summary_spec"]["correction_of"]
    assert "prior completed attempt was rejected" in request["prompt"]


def test_complete_native_output_limit_consumes_existing_correction_budget(tmp_path, prepared):
    runner, capability, snapshots = fixture_route(prepared, tmp_path)
    cap = read_json(capability)
    script = tmp_path / "summary-native.py"
    source = script.read_text().replace(
        "import json,sys", "import json,sys\nfrom pathlib import Path"
    )
    source = source.replace(
        'print(json.dumps({"type":"system"',
        'marker=Path(__file__).with_suffix(".attempt")\nfirst=not marker.exists()\nmarker.write_text("attempted")\nprint(json.dumps({"type":"system"',
    )
    source = source.replace('"output_tokens":512', '"output_tokens":5000 if first else 512')
    script.write_text(source)
    cap["dependencies"] = [{"path": str(script), "sha256": file_digest(script)}]
    capability.write_text(json.dumps(fixture_capability(tmp_path, cap)))
    directory = tmp_path / "output-limit"
    report = run_trial(snapshots, capability, directory, runner=runner, timeout_s=20)
    assert report["ok"] and report["usage"]["output_tokens"] == 5512
    receipts = [read_json(path) for path in (directory / "jobs").glob("*/receipt.json")]
    assert len(receipts) == 2
    rejected = next(row for row in receipts if row["outcome"] == "limit")
    assert (
        rejected["limit_kind"] == "native-output-tokens"
        and rejected["native_outcome"] == "completed"
    )
    assert len(list((directory / "jobs").glob("*/rejected.json"))) == 1


@pytest.mark.parametrize(
    "change",
    [
        {"limit_kind": "wall-time"},
        {"limit_kind": "stdout-wire-bytes"},
        {"limit_kind": "stderr-bytes"},
        {"limit_kind": "native-turns"},
        {"turns": 2},
        {"native_outcome": "quota"},
        {"quiescent": False},
        {"input_complete": False},
        {"truncated": True},
        {"result": None},
        {"usage_known": False},
        {"usage": {"output_tokens": 4096}},
    ],
)
def test_other_native_failure_evidence_cannot_claim_output_correction_budget(change):
    from types import SimpleNamespace

    from bmad_loop.campaign_history_trial import _output_rejected

    request = SimpleNamespace(limits=SimpleNamespace(output_tokens=4096, max_turns=1))
    receipt = {
        "outcome": "limit",
        "limit_kind": "native-output-tokens",
        "native_outcome": "completed",
        "failure_kind": "",
        "input_complete": True,
        "quiescent": True,
        "processes_quiescent": True,
        "truncated": False,
        "result": {"fact": "retained"},
        "usage_known": True,
        "usage": {"output_tokens": 5000},
        "turns": 1,
    }
    assert _output_rejected(receipt, request)
    assert not _output_rejected({**receipt, **change}, request)


@pytest.mark.parametrize(
    "change",
    [
        {"limit_kind": "native-turns"},
        {"turns": 2},
        {"limit_kind": "wall-time"},
        {"limit_kind": "stdout-wire-bytes"},
        {"limit_kind": "stderr-bytes"},
        {"native_outcome": "quota"},
        {"turns": None},
    ],
)
def test_schema_rejection_cannot_hide_another_native_limit(change):
    from types import SimpleNamespace

    from bmad_loop.campaign_history_trial import _schema_rejected

    request = SimpleNamespace(limits=SimpleNamespace(max_turns=1))
    receipt = {
        "failure_kind": "schema",
        "native_terminal_completed": True,
        "outcome": "limit",
        "limit_kind": "native-output-tokens",
        "native_outcome": "failed",
        "turns": 1,
        "input_complete": True,
        "quiescent": True,
        "processes_quiescent": True,
        "truncated": False,
    }
    assert _schema_rejected(receipt, request)
    assert not _schema_rejected({**receipt, **change}, request)


@pytest.mark.parametrize("exhaust", [False, True])
def test_trial_corrects_native_schema_rejection_with_preserved_usage(tmp_path, prepared, exhaust):
    runner, capability, snapshots = fixture_route(prepared, tmp_path)
    cap = read_json(capability)
    script = tmp_path / "grok-schema-native.py"
    script.write_text("""import json,sys
from pathlib import Path
source=next(json.loads(line) for line in sys.stdin.read().splitlines() if line.startswith('{"source_id":'))
payload=json.loads(source["content"])
result={"schema_version":1,"job_id":source["source_id"],"input_sha256":source["sha256"],"covered_ids":[x["id"] for x in payload["primary"]],"facts":[],"gaps":[]}
marker=Path(__file__).with_suffix('.attempt')
reject=not marker.exists() or EXHAUST
marker.write_text('attempted')
print(json.dumps({"sessionId":"native-schema","stopReason":"end_turn","modelUsage":{"gpt-test":{"modelCalls":1}},"num_turns":1,"usage":{"input_tokens":1000,"output_tokens":512},"structuredOutput":None if reject else result,"structuredOutputError":"citation is not in the enum" if reject else None}))
""".replace("EXHAUST", repr(exhaust)))
    cap.update(
        protocol="grok-json",
        argv=[
            cap["binary"],
            str(script),
            "--tools",
            "",
            "--deny",
            "*",
            "--no-subagents",
            "--disable-web-search",
        ],
        dependencies=[{"path": str(script), "sha256": file_digest(script)}],
    )
    capability.write_text(json.dumps(fixture_capability(tmp_path, cap)))
    directory = tmp_path / "native-schema"
    report = run_trial(snapshots, capability, directory, runner=runner, timeout_s=20)
    assert report["ok"] is not exhaust
    receipts = [read_json(p) for p in (directory / "jobs").glob("*/receipt.json")]
    assert len(receipts) == (3 if exhaust else 2)
    assert report["usage"]["output_tokens"] == 512 * len(receipts)
    assert report["usage_unknown_jobs"] == 0
    assert all(r["input_complete"] and r["processes_quiescent"] for r in receipts)
    if exhaust:
        assert "budget exhausted" in report["problems"][0]["problem"]
        assert not list((directory / "jobs").glob("*/validated.json"))


def test_historical_schema_failure_reparse_is_bound_and_never_successful(tmp_path):
    from bmad_loop.campaign_headless import write_json
    from bmad_loop.campaign_history_trial import _observed_receipt

    (tmp_path / "input.txt").write_text("immutable delivered input")
    envelope = {
        "request_digest": "bound-request",
        "launch_id": "bound-launch",
        "selection": {"model": "grok-test"},
        "capability": {"protocol": "grok-json"},
        "input": {"sha256": file_digest(tmp_path / "input.txt")},
        "request": {"limits": {"max_turns": 1, "output_tokens": 500}},
    }
    request_path = tmp_path / "request.json"
    write_json(request_path, envelope)
    receipt = {
        "request_digest": "bound-request",
        "launch_id": "bound-launch",
        "outcome": "failed",
        "quiescent": True,
        "processes_quiescent": True,
        "reason": "CampaignError: Native session identity or structured result is missing",
    }
    for name in ("receipt.json", "observed-receipt.json"):
        write_json(tmp_path / name, receipt)
    write_json(
        tmp_path / "observed-receipt-binding.json",
        {
            "native_receipt_sha256": file_digest(tmp_path / "receipt.json"),
            "observed_sha256": file_digest(tmp_path / "observed-receipt.json"),
        },
    )
    write_json(
        tmp_path / "stdout.jsonl",
        {
            "sessionId": "native",
            "stopReason": "end_turn",
            "modelUsage": {"grok-test": {"modelCalls": 1}},
            "num_turns": 1,
            "usage": {"input_tokens": 1000, "output_tokens": 501},
            "structuredOutputError": "unknown citation",
        },
    )
    before = (tmp_path / "receipt.json").read_bytes()
    result = _observed_receipt(request_path, None)
    assert result["outcome"] == "limit" and result["failure_kind"] == "schema"
    assert result["result"] is None and result["usage"]["output_tokens"] == 501
    assert (tmp_path / "receipt.json").read_bytes() == before
    assert (tmp_path / "schema-rejection-reparse-v2.json").is_file()
    assert result["limit_kind"] == "native-output-tokens"
    (tmp_path / "input.txt").write_text("tampered input")
    with pytest.raises(CampaignError, match="complete native schema-rejection proof"):
        _observed_receipt(request_path, None)


@pytest.mark.parametrize("revision_passes", [False, True])
def test_legacy_reducer_has_one_presentation_revision_without_resetting_attempts(
    tmp_path, prepared, revision_passes
):
    from test_campaign_handoff_context import budget, quotes, result

    from bmad_loop.campaign_history_trial import _one, trial_request
    from bmad_loop.campaign_summary import plan_maps, plan_reduction, validate_summary
    from bmad_loop.campaign_transcripts import load_events

    runner, _, _, _, cap = prepared
    snapshot = capture(tmp_path, [native_row("codex")])
    events = load_events(snapshot)
    mapped = plan_maps(events, budget(), tmp_path / "map-inputs")[0]
    child = validate_summary(mapped, result(mapped, events), events)
    job = plan_reduction(
        ((mapped, child),),
        budget(),
        tmp_path / "reduce-inputs",
        owner_quotes=quotes(snapshot, child),
    )[0]
    script = tmp_path / "reducer-native.py"
    script.write_text("""import json,sys
source=next(json.loads(line) for line in sys.stdin.read().splitlines() if line.startswith('{"source_id":'))
payload=json.loads(source['content'])
revised=payload.get('citation_presentation')=='native-events-v1' and REVISION_PASSES
result={'schema_version':1,'job_id':source['source_id'],'input_sha256':source['sha256'],'covered_ids':[x['job_id'] for x in payload['children']],'facts':[],'gaps':[]}
print(json.dumps({'sessionId':'native-reducer','stopReason':'end_turn','modelUsage':{'gpt-test':{'modelCalls':1}},'num_turns':1,'usage':{'input_tokens':1000,'output_tokens':512},'structuredOutput':result if revised else None,'structuredOutputError':None if revised else 'child fact identity is not an eligible native citation'}))
""".replace("REVISION_PASSES", repr(revision_passes)))
    cap.update(
        protocol="grok-json",
        argv=[
            cap["binary"],
            str(script),
            "--tools",
            "",
            "--deny",
            "*",
            "--no-subagents",
            "--disable-web-search",
        ],
        dependencies=[{"path": str(script), "sha256": file_digest(script)}],
    )
    cap = fixture_capability(tmp_path, cap)
    directory = tmp_path / "legacy-reducer"
    request = trial_request(job, cap, (snapshot,), 20)
    runner.prepare(
        directory / "jobs" / job.id,
        request,
        request.candidates[0],
        cap,
        launch_id="trial-" + job.id,
        generation=1,
    )

    def execute():
        if revision_passes:
            return _one(job, (snapshot,), events, cap, directory, 20, runner)
        with pytest.raises(CampaignError, match="presentation revision exhausted"):
            _one(job, (snapshot,), events, cap, directory, 20, runner)

    execute()
    assert len(list((directory / "jobs").glob("*/receipt.json"))) == 4
    revision = read_json(directory / "jobs" / job.id / "presentation-revision.json")
    assert revision["prior_attempts"] == 3 and revision["additional_attempt_limit"] == 1
    process_count = len(runner.mux.processes)
    execute()
    assert len(runner.mux.processes) == process_count
    assert len(list((directory / "jobs").glob("*/validated.json"))) == int(revision_passes)


def test_trial_reuses_verified_prior_coverage_without_native_relaunch(tmp_path, prepared):
    runner, capability, snapshots = fixture_route(prepared, tmp_path)
    first = tmp_path / "first"
    assert run_trial(snapshots, capability, first, runner=runner, timeout_s=20)["ok"]
    count = len(runner.mux.processes)
    report = run_trial(
        snapshots,
        capability,
        tmp_path / "second",
        runner=runner,
        timeout_s=20,
        reuse_directory=first,
    )
    assert report["ok"] and report["reused_jobs"] == 1 and report["new_map_jobs"] == 0
    assert len(runner.mux.processes) == count
    third = run_trial(
        snapshots,
        capability,
        tmp_path / "third",
        runner=runner,
        timeout_s=20,
        reuse_directory=tmp_path / "second",
    )
    assert third["ok"] and third["reused_jobs"] == 1 and third["new_map_jobs"] == 0
    assert len(runner.mux.processes) == count


def test_new_trial_retains_old_maps_without_reusing_obsolete_prompt_coverage(
    tmp_path, prepared, monkeypatch
):
    from bmad_loop import campaign_history_trial as trial
    from bmad_loop import campaign_summary as summary

    runner, capability, snapshots = fixture_route(prepared, tmp_path)
    first = tmp_path / "old"
    with monkeypatch.context() as prior:
        prior.setattr(summary, "EXTRACT_PROMPT_VERSION", "history-extract-v2")
        prior.setattr(trial, "EXTRACT_PROMPT_VERSION", "history-extract-v2")
        existing_defaults = summary.SummaryJobSpec.__init__.__defaults__
        assert existing_defaults is not None
        defaults = list(existing_defaults)
        defaults[2] = "history-extract-v2"
        prior.setattr(summary.SummaryJobSpec.__init__, "__defaults__", tuple(defaults))
        assert run_trial(snapshots, capability, first, runner=runner, timeout_s=20)["ok"]
    old_requests = {
        str(path): path.read_bytes() for path in (first / "jobs").glob("*/request.json")
    }
    count = len(runner.mux.processes)
    report = run_trial(
        snapshots,
        capability,
        tmp_path / "fresh",
        runner=runner,
        timeout_s=20,
        reuse_directory=first,
    )
    assert report["ok"] and report["reused_jobs"] == 0 and report["new_map_jobs"] == 1
    assert len(runner.mux.processes) == count + 1
    from pathlib import Path

    assert all(Path(path).read_bytes() == content for path, content in old_requests.items())
    with pytest.raises(CampaignError, match="obsolete prompt contract"):
        run_trial(snapshots, capability, first, runner=runner, timeout_s=20)


def test_fresh_trial_projects_whole_children_before_production_admission(tmp_path):
    from test_campaign_reduction_projection import recorded_size_children

    from bmad_loop.campaign_history_trial import _plan_reducers
    from bmad_loop.campaign_summary import load_input

    children = recorded_size_children(tmp_path)
    cap = {
        "profile": "claude",
        "model": "claude-fable-test",
        "efforts": ["high"],
        "input_tokens": 99662,
        "output_tokens": 32768,
        "context_tokens": 200000,
        "reserved_tokens": 65536,
        "qualification": {"sha256": "proof"},
    }
    jobs = _plan_reducers(children, cap, tmp_path / "v2", "adaptive-render-v2", ())
    assert len(jobs) == 2
    assert {key for job in jobs for key in job.primary_ids} == {job.id for job, _ in children}
    assert all(job.input_tokens <= 33447 for job in jobs)
    assert all(load_input(job)["citation_presentation"] == "native-events-v1" for job in jobs)
    legacy = _plan_reducers(children, cap, tmp_path / "v1", "adaptive-render-v1", ())
    assert len(legacy) == 1 and "citation_presentation" not in load_input(legacy[0])


def test_adaptive_partition_keeps_fitting_jobs_and_every_source_character(tmp_path, prepared):
    from bmad_loop.campaign_headless import render_input
    from bmad_loop.campaign_history_trial import _fit_maps, trial_request
    from bmad_loop.campaign_summary import ContextBudget, load_input, plan_maps
    from bmad_loop.campaign_transcripts import load_events

    _, _, _, _, capability = prepared
    snapshot = capture(
        tmp_path,
        [
            native_row("codex", "\\" * 30000, "m1"),
            native_row("codex", "ordinary context " * 2000, "m2"),
        ],
    )
    events = load_events(snapshot)
    budget = ContextBudget(
        "codex:gpt-test", 65000, 4096, prompt_tokens=0, working_tokens=0, qualified=True
    )
    original = plan_maps(events, budget, tmp_path / "inputs", overlap_tokens=0)
    fitting = []
    for job in original:
        try:
            render_input(trial_request(job, capability, (snapshot,), 20), capability)
            fitting.append(job.id)
        except CampaignError:
            pass
    assert fitting and len(fitting) < len(original)
    fitted = _fit_maps(original, capability, (snapshot,), 20, tmp_path / "inputs")
    assert set(fitting) <= {job.id for job in fitted}
    for job in fitted:
        render_input(trial_request(job, capability, (snapshot,), 20), capability)
    for event in events:
        spans = sorted(
            (f["start"], f["end"], f["text"])
            for job in fitted
            for f in load_input(job)["primary"]
            if f["event_id"] == event.id
        )
        assert spans[0][0] == 0 and spans[-1][1] == len(event.record.text)
        assert all(a[1] == b[0] for a, b in zip(spans, spans[1:]))
        assert "".join(row[2] for row in spans) == event.record.text


def test_trial_drain_finishes_admitted_actor_and_resume_keeps_its_receipt(
    tmp_path, prepared, monkeypatch
):
    runner, capability, snapshots = fixture_route(prepared, tmp_path)
    snapshot = capture(tmp_path, [native_row("codex", "context " * 14000)])
    snapshots.write_text(json.dumps({"schema_version": 1, "snapshots": [asdict(snapshot)]}))
    directory = tmp_path / "drainable"
    original = runner.launch

    def launch(path):
        value = original(path)
        requested = run_trial(snapshots, capability, directory, drain=True)
        assert requested["state"] == "drain-requested"
        return value

    monkeypatch.setattr(runner, "launch", launch)
    report = run_trial(snapshots, capability, directory, parallel=1, runner=runner, timeout_s=20)
    assert report["state"] == "drained" and not report["ok"]
    assert len(runner.mux.processes) == 1
    receipt = next((directory / "jobs").glob("*/observed-receipt.json"))
    assert read_json(receipt)["quiescent"] and read_json(receipt)["outcome"] == "completed"
    before = receipt.read_bytes()
    monkeypatch.setattr(runner, "launch", original)
    resumed = run_trial(snapshots, capability, directory, parallel=1, runner=runner, timeout_s=20)
    assert resumed["ok"] and receipt.read_bytes() == before
    assert len(runner.mux.processes) == resumed["map_jobs"]
    assert list((directory / "control-history").glob("*.json"))


def test_trial_parallel_ceiling_is_explicit_and_preparation_remains_zero_token(tmp_path, prepared):
    runner, capability, snapshots = fixture_route(prepared, tmp_path)
    assert (
        run_trial(
            snapshots, capability, tmp_path / "wide", parallel=64, execute=False, runner=runner
        )["state"]
        == "prepared"
    )
    with pytest.raises(CampaignError, match="bounds"):
        run_trial(
            snapshots, capability, tmp_path / "too-wide", parallel=65, execute=False, runner=runner
        )
    assert not runner.mux.processes
