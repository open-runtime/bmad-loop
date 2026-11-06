"""Native trial integration uses a scripted zero-token subprocess."""

import json
from dataclasses import asdict

import pytest
from test_campaign_handoff_context import capture, native_row
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
    assert bounds["context_tokens"] - bounds["output_tokens"] < 100000 * 3 // 4 - 8192
