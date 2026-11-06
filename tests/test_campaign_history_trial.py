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
    assert read_json(directory / "binding.json")["planning"] == "adaptive-render-v1"


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
