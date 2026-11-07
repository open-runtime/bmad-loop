"""Qualification runs fixed native-protocol fixtures; never model inference."""

import json
from pathlib import Path

import pytest
from test_campaign_headless import fixture_capability
from test_campaign_headless import prepared as prepared

from bmad_loop.campaign_headless import file_digest, read_json
from bmad_loop.campaign_jobs import digest
from bmad_loop.campaign_model import CampaignError
from bmad_loop.campaign_qualification import qualify, validate_probe_envelope


def proposal(prepared, tmp_path):
    runner, _, _, candidate, cap = prepared
    script = tmp_path / "canary-native.py"
    script.write_text("""import json,re,sys
text=sys.stdin.read()
nonce=re.search(r"QUALIFICATION NONCE ([a-f0-9]+)",text).group(1)
markers=re.findall(r"CANARY [0-9]+: ([a-f0-9]+)",text)
print(json.dumps({"type":"system","model":"gpt-test","session_id":"native-canary"}))
print(json.dumps({"type":"result","structured_output":{"nonce":nonce,"markers":markers},"usage":{"input_tokens":1000,"output_tokens":512}}))
""")
    cap = {
        **cap,
        "argv": [cap["binary"], str(script)],
        "dependencies": [{"path": str(script), "sha256": file_digest(script)}],
    }
    cap.pop("qualification")
    path = tmp_path / "proposal.json"
    path.write_text(json.dumps(cap))
    return runner, candidate, cap, path


def test_probe_earns_only_observed_input_and_output_coverage(tmp_path, prepared):
    runner, _, _, path = proposal(prepared, tmp_path)
    result = qualify(
        path, tmp_path / "probe", input_bytes=4096, output_tokens=1024, timeout_s=20, runner=runner
    )
    assert result["ok"]
    qualified = read_json(Path(result["capability_path"]))
    assert 4096 < qualified["input_tokens"] < 10000
    assert qualified["output_tokens"] == 1024
    assert qualified["qualification"]["observed_output_tokens"] == 512
    assert qualified["qualification"]["native_output_limit_enforced"] is False
    assert qualified["max_turns"] == 1
    assert qualified["qualification"]["checks"]


def test_normal_launch_cannot_use_unqualified_probe_proposal(tmp_path, prepared):
    runner, candidate, cap, _ = proposal(prepared, tmp_path)
    request = prepared[2]
    with pytest.raises(CampaignError, match="qualification has not passed"):
        runner.prepare(
            tmp_path / "normal", request, candidate, cap, launch_id="normal", generation=1
        )
    path = runner.prepare_probe(tmp_path / "probe", candidate, cap, input_bytes=4096)
    assert read_json(path)["probe"]


def test_fixed_probe_cannot_be_repurposed_as_arbitrary_prompt(tmp_path, prepared):
    runner, candidate, cap, _ = proposal(prepared, tmp_path)
    path = runner.prepare_probe(tmp_path / "probe", candidate, cap, input_bytes=4096)
    envelope = read_json(path)
    validate_probe_envelope(envelope)
    envelope["request"]["prompt"] = "Implement unrelated work"
    envelope["probe"]["request_digest"] = digest(envelope["request"])
    envelope["probe_digest"] = digest(envelope["probe"])
    with pytest.raises(CampaignError, match="fixed canary"):
        validate_probe_envelope(envelope)


def test_fixed_probe_rejects_hidden_instructions_even_with_updated_hashes(tmp_path, prepared):
    runner, candidate, cap, _ = proposal(prepared, tmp_path)
    path = runner.prepare_probe(tmp_path / "probe", candidate, cap, input_bytes=4096)
    envelope = read_json(path)
    source = Path(envelope["request"]["inputs"][0]["path"])
    source.write_text(source.read_text().replace("inert-padding-", "perform-edits-", 1))
    source_hash = file_digest(source)
    envelope["request"]["inputs"][0]["sha256"] = source_hash
    envelope["probe"]["source_sha256"] = source_hash
    envelope["probe"]["request_digest"] = digest(envelope["request"])
    envelope["probe_digest"] = digest(envelope["probe"])
    with pytest.raises(CampaignError, match="fixed marker/padding grammar"):
        validate_probe_envelope(envelope)


def test_no_tools_canary_rejects_execution_grants(tmp_path, prepared):
    runner, candidate, cap, _ = proposal(prepared, tmp_path)
    path = runner.prepare_probe(tmp_path / "probe", candidate, cap, input_bytes=4096)
    envelope = read_json(path)
    envelope["grants"] = {"workspace": "elsewhere"}
    with pytest.raises(CampaignError, match="cannot grant"):
        validate_probe_envelope(envelope)


def test_native_grok_selected_alias_preserves_actual_served_identity():
    from bmad_loop.campaign_headless_protocol import parse_output

    frame = {
        "sessionId": "native",
        "structuredOutput": {"done": True},
        "num_turns": 1,
        "stopReason": "end_turn",
        "modelUsage": {"grok-4.7-build": {"modelCalls": 1}},
        "usage": {"input_tokens": 100, "output_tokens": 30, "reasoning_tokens": 10},
        "total_cost_usd": 0.01,
    }
    with pytest.raises(CampaignError, match="primary model"):
        parse_output(json.dumps(frame), protocol="grok-json", selected_model="grok-4.7")
    result = parse_output(
        json.dumps(frame),
        protocol="grok-json",
        selected_model="grok-4.7",
        served_model_aliases=("grok-4.7-build",),
    )
    assert result["selected_model"] == "grok-4.7"
    assert result["observed_model"] == "grok-4.7-build"
    assert result["result"] == {"done": True}
    assert result["usage"] == {
        "input_tokens": 100,
        "output_tokens": 30,
        "reasoning_tokens": 10,
        "cost_usd": 0.01,
    }


def test_cli_qualifier_uses_real_fixed_protocol_without_campaign_store(
    tmp_path, prepared, monkeypatch, capsys
):
    from bmad_loop import cli

    runner, _, _, path = proposal(prepared, tmp_path)
    monkeypatch.setattr("bmad_loop.campaign_headless.HeadlessRunner", lambda: runner)
    assert (
        cli.main(
            [
                "campaign",
                "qualify-headless",
                str(path),
                "--directory",
                str(tmp_path / "cli-probe"),
                "--input-bytes",
                "4096",
                "--output-tokens",
                "1024",
                "--timeout",
                "20",
                "--json",
            ]
        )
        == 0
    )
    result = json.loads(capsys.readouterr().out)
    assert result["ok"] and result["coverage"]["output_tokens"] == 512


def test_claude_native_assistant_error_survives_generic_final_failure():
    from bmad_loop.campaign_headless_protocol import parse_output

    frames = [
        {"type": "system", "model": "claude-opus-5-5", "session_id": "native"},
        {"type": "assistant", "error": "rate_limit", "message": {"content": []}},
        {"type": "result", "is_error": True, "subtype": "success", "result": "provider prose"},
    ]
    parsed = parse_output(
        json.dumps(frames), protocol="claude-stream", selected_model="claude-opus-5-5"
    )
    assert parsed["outcome"] == "quota"
    assert parsed["reason"] == "native assistant failure: rate_limit"


def test_muse_msp_receipt_uses_native_model_usage_and_bound_session():
    from bmad_loop.campaign_headless_protocol import parse_output

    def frame(kind, payload, stream="session"):
        return {
            "schema_version": 1,
            "payload_schema_version": 1,
            "payload_type": kind,
            "stream": {"kind": stream, "id": "native-session"},
            "payload": {"run_stream": {"kind": "run", "id": "native-run"}, **payload},
        }

    frames = [
        frame("session.run.linked", {}),
        frame(
            "run.usage",
            {
                "models": [
                    {
                        "model": "muse-spark-1.3",
                        "input_tokens": 10,
                        "output_tokens": 4,
                        "reasoning_tokens": 2,
                    }
                ],
                "calls": 1,
                "failed_calls": 0,
            },
        ),
        frame("run.terminal.completed", {"terminal": "completed", "text": '{"ok":true}'}),
    ]
    parsed = parse_output(json.dumps(frames), protocol="muse-json", selected_model="muse-spark-1.3")
    assert parsed["result"] == {"ok": True}
    assert parsed["session_id"] == "native-session" and parsed["observed_model"] == "muse-spark-1.3"
    assert parsed["usage"]["reasoning_tokens"] == 2
    frames[-1]["payload"]["run_stream"]["id"] = "other-run"
    with pytest.raises(CampaignError, match="another run"):
        parse_output(json.dumps(frames), protocol="muse-json", selected_model="muse-spark-1.3")


def test_muse_wire_echo_budget_is_distinct_from_native_output_tokens(tmp_path, prepared):
    from dataclasses import replace

    from test_campaign_headless import finish

    from bmad_loop.campaign_jobs import JobLimits

    runner, _, request, candidate, cap = prepared
    script = tmp_path / "msp-wire-fixture.py"
    script.write_text("""import json,sys,time
sys.stdin.read()
print(json.dumps({"type":"system","model":"gpt-test","session_id":"native","input_echo":"x"*70000}),flush=True)
time.sleep(.5)
print(json.dumps({"type":"result","structured_output":{"ok":True},"usage":{"input_tokens":1000,"output_tokens":32}}),flush=True)
""")
    cap = {
        **cap,
        "protocol": "muse-json",
        "argv": [cap["binary"], str(script)],
        "dependencies": [{"path": str(script), "sha256": file_digest(script)}],
    }
    cap["argv"].extend(
        [
            "--preset",
            "empty",
            "--disable-web-tools",
            "--disable-reminders",
            "--disable-shell",
            "--disable-write",
        ]
    )
    cap = fixture_capability(tmp_path, cap)
    request = replace(request, limits=JobLimits(100000, 64, max_turns=1, timeout_s=20))
    path = runner.prepare(
        tmp_path / "wire", request, candidate, cap, launch_id="wire", generation=1
    )
    runner.launch(path)
    receipt = finish(runner, path)
    assert receipt["outcome"] == "completed", receipt
    assert receipt["usage"]["output_tokens"] == 32
    assert receipt["wire_output_limit_bytes"] > 70000


def test_prepared_headless_request_reconciles_json_normalized_candidate(prepared):
    runner, path, request, candidate, cap = prepared
    assert (
        runner.prepare(path.parent, request, candidate, cap, launch_id="launch-1", generation=1)
        == path
    )


def test_parallel_native_jobs_create_shared_mux_session_once(tmp_path, prepared):
    import threading
    import time
    from concurrent.futures import ThreadPoolExecutor
    from dataclasses import replace

    from test_campaign_headless import ChildMux, finish

    from bmad_loop.adapters.multiplexer import MultiplexerError
    from bmad_loop.campaign_headless import HeadlessRunner

    class RacingMux(ChildMux):
        def __init__(self):
            super().__init__()
            self.created = False
            self.creations = 0
            self.window_lock = threading.Lock()

        def has_session(self, session):
            if not self.created:
                time.sleep(0.05)
                return False
            return True

        def new_session(self, session, cwd):
            if self.created:
                raise MultiplexerError("duplicate session")
            self.created = True
            self.creations += 1

        def new_window(self, *args):
            with self.window_lock:
                return super().new_window(*args)

    _, _, request, candidate, cap = prepared
    mux = RacingMux()
    runner = HeadlessRunner(mux)
    paths = [
        runner.prepare(
            tmp_path / "parallel" / str(i),
            replace(request, id=f"job-{i}"),
            candidate,
            cap,
            launch_id=f"launch-{i}",
            generation=1,
        )
        for i in range(4)
    ]
    try:
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(runner.launch, paths))
        assert all(row["state"] == "running" for row in results)
        assert mux.creations == 1
        assert all(finish(runner, path)["outcome"] == "completed" for path in paths)
    finally:
        for process in mux.processes.values():
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=10)
