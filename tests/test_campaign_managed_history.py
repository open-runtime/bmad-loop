"""Actual headless launch capture retains injected context and native output."""

import json
import time
from pathlib import Path

import pytest
from test_campaign_handoff_flow import campaign as campaign
from test_campaign_handoff_flow import step
from test_campaign_headless import finish
from test_campaign_headless import prepared as prepared

from bmad_loop import campaign_handoff_flow as flow
from bmad_loop.campaign_headless import file_digest
from bmad_loop.campaign_headless_protocol import parse_output
from bmad_loop.campaign_managed_history import snapshot_managed_history
from bmad_loop.campaign_model import CampaignError
from bmad_loop.campaign_transcripts import HistorySnapshot, load_events


def binding_for(path, receipt):
    return {
        "headless_request": str(path),
        "headless_request_sha256": file_digest(path),
        "headless_receipt_sha256": file_digest(path.parent / "receipt.json"),
        "quiescent": receipt["quiescent"],
        "session_id": receipt["session_id"],
        "profile": "custom-headless-profile",
    }


def test_real_headless_input_output_reach_handoff_map_without_owner_authority(
    campaign, prepared, tmp_path
):
    from bmad_loop.adapters.base import SessionSpec
    from bmad_loop.adapters.headless import HeadlessAdapter
    from bmad_loop.adapters.profile import get_profile
    from bmad_loop.policy import Policy

    runner, _, request, candidate, cap = prepared
    adapter = HeadlessAdapter(
        run_dir=tmp_path / "adapter", profile=get_profile("codex"), policy=Policy(), mux=runner.mux
    )
    adapter.set_capability(cap, None, request.coordinates, request.inputs[0])
    spec = SessionSpec(
        "managed-capture",
        "dev",
        request.prompt,
        tmp_path,
        model=candidate.model,
        effort=candidate.effort,
        timeout_s=15,
    )
    result = adapter.run(spec)
    path = adapter.requests[spec.task_id]
    native = adapter.receipts[spec.task_id]
    runtime, snapshot, unit, receipt = campaign
    receipt["sessions"] = [
        {
            "role": "dev",
            "status": "crashed",
            "ended": "usage",
            "models": ["gpt-test"],
            "adapter": "custom-headless-profile",
            "session_id": native["session_id"],
            "transcript_path": str(path.parent / "stdout.jsonl"),
            "native_binding": result.native_binding,
        }
    ]
    unit.evidence["worker_history"] = [receipt]
    assert flow.intercept_terminal(runtime, snapshot, unit, receipt)
    step(runtime, snapshot, unit, "handoff-capture")
    capture = unit.evidence["handoff"]["capture"]
    assert capture["gaps"] == []
    assert len(capture["sources"]) == 1
    history = HistorySnapshot(**capture["sources"][0])
    assert history.harness == "managed-headless" and not history.owner_event_ids
    events = load_events(history)
    by_kind = {event.record.kind: event for event in events}
    assert request.prompt in by_kind["managed-delivered-input"].record.text
    assert "owner obligation" in by_kind["managed-delivered-input"].record.text
    assert "native-test" in by_kind["managed-native:system"].record.text
    assert native["result"]["digest"] in by_kind["managed-native:result"].record.text
    files = json.loads(by_kind["managed-source-index"].record.text)["files"]
    for name, row in files.items():
        assert Path(row["path"]).read_bytes() == (path.parent / name).read_bytes()
    step(runtime, snapshot, unit, "handoff-plan")
    assert snapshot.jobs
    covered = {
        event
        for job in snapshot.jobs.values()
        for event in job["request"]["coordinates"]["summary_spec"]["event_ids"]
    }
    assert covered == {event.id for event in events}


@pytest.mark.parametrize("changed", ["request", "receipt", "input", "ownership"])
def test_capture_refuses_changed_launch_bindings(prepared, tmp_path, changed):
    runner, path, *_ = prepared
    runner.launch(path)
    receipt = finish(runner, path)
    binding = binding_for(path, receipt)
    if changed == "request":
        path.write_text(path.read_text() + " ")
    elif changed == "receipt":
        target = path.parent / "receipt.json"
        target.write_text(target.read_text() + " ")
    elif changed == "input":
        (path.parent / "input.txt").write_text("different context")
    else:
        binding["quiescent"] = False
    with pytest.raises(CampaignError):
        snapshot_managed_history(path, binding, tmp_path / "archive", session_id="native-test")


def test_partial_native_telemetry_survives_unfinished_tail_without_completion():
    text = "\n".join(
        [
            json.dumps({"type": "system", "model": "gpt-test", "session_id": "same-session"}),
            json.dumps(
                {"type": "turn.completed", "usage": {"input_tokens": 27, "output_tokens": 9}}
            ),
            '{"type": "item.completed",',
        ]
    )
    result = parse_output(text, protocol="codex-jsonl", selected_model="gpt-test", partial=True)
    assert result["usage"] == {"input_tokens": 27, "output_tokens": 9}
    assert result["session_id"] == "same-session" and result["observed_model"] == "gpt-test"
    assert result["result"] is None and result["outcome"] != "completed"


def test_native_completion_cannot_borrow_identity_from_another_session():
    frames = [
        {"type": "system", "model": "gpt-test", "session_id": "first"},
        {"type": "result", "session_id": "second", "structured_output": {"done": True}},
    ]
    with pytest.raises(CampaignError, match="different session identities"):
        parse_output(json.dumps(frames), protocol="bmad-test-v1", selected_model="gpt-test")


def test_canceled_child_preserves_emitted_native_usage(prepared, tmp_path):
    runner, _, request, candidate, cap = prepared
    script = tmp_path / "cancel-native.py"
    script.write_text("""import json,time
print(json.dumps({"type":"system","model":"gpt-test","session_id":"cancel-session"}),flush=True)
print(json.dumps({"type":"turn.completed","usage":{"input_tokens":27,"output_tokens":9}}),flush=True)
time.sleep(20)
""")
    cap = {
        **cap,
        "argv": [cap["binary"], str(script)],
        "dependencies": [{"path": str(script), "sha256": file_digest(script)}],
    }
    path = runner.prepare(
        tmp_path / "canceled", request, candidate, cap, launch_id="cancel", generation=1
    )
    runner.launch(path)
    deadline = time.monotonic() + 15
    stdout = path.parent / "stdout.jsonl"
    while time.monotonic() < deadline:
        if stdout.exists() and "turn.completed" in stdout.read_text():
            break
        time.sleep(0.03)
    else:
        pytest.fail("scripted native process did not emit telemetry")
    runner.terminate(path)
    receipt = finish(runner, path)
    assert receipt["outcome"] == "canceled" and receipt["quiescent"]
    assert receipt["usage"] == {"input_tokens": 27, "output_tokens": 9}
    assert receipt["session_id"] == "cancel-session" and receipt["observed_model"] == "gpt-test"


def test_distinct_quota_attempts_without_session_ids_keep_their_own_usage(prepared, tmp_path):
    from bmad_loop.adapters.base import SessionSpec
    from bmad_loop.adapters.headless import HeadlessAdapter
    from bmad_loop.adapters.profile import get_profile
    from bmad_loop.policy import Policy

    runner, _, request, candidate, cap = prepared
    script = tmp_path / "quota-native.py"
    script.write_text("""import json,sys
value=29 if 'second' in sys.stdin.read() else 17
print(json.dumps({"type":"result","is_error":True,"error":{"code":"quota_exceeded"},"usage":{"input_tokens":value}}))
""")
    cap = {
        **cap,
        "argv": [cap["binary"], str(script)],
        "dependencies": [{"path": str(script), "sha256": file_digest(script)}],
    }
    adapter = HeadlessAdapter(
        run_dir=tmp_path / "quota-adapter",
        profile=get_profile("codex"),
        policy=Policy(),
        mux=runner.mux,
    )
    adapter.set_capability(cap, None, request.coordinates)
    results = [
        adapter.run(
            SessionSpec(
                name,
                "dev",
                name,
                tmp_path,
                model=candidate.model,
                effort=candidate.effort,
                timeout_s=15,
            )
        )
        for name in ("first", "second")
    ]
    assert not any(result.session_id for result in results)
    assert [adapter.read_usage(result).input_tokens for result in results] == [17, 29]
