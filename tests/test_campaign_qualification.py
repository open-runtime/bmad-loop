"""Qualification runs fixed native-protocol fixtures; never model inference."""

import json
from pathlib import Path

import pytest
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
