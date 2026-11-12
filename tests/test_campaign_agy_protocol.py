"""AGY native envelopes and hook recipes, exclusively scripted/zero-token."""

import json
import os
import subprocess
import sys
from pathlib import Path
from types import MappingProxyType

import pytest
from test_campaign_headless import prepared as prepared

from bmad_loop import campaign_agy_launcher as launcher
from bmad_loop.campaign_headless import file_digest, read_json, validate_capability
from bmad_loop.campaign_headless_protocol import parse_output
from bmad_loop.campaign_model import CampaignError, RoleCandidate
from bmad_loop.campaign_qualification import _no_tools, qualify


@pytest.mark.parametrize("inherited", [None, "1", "false", "true"])
def test_agy_child_environment_forces_literal_true_without_mutating_source(inherited):
    source = {"HOME": "/existing-account", "HCOM_DIR": "/owned-room"}
    if inherited is not None:
        source["AGY_CLI_DISABLE_AUTO_UPDATE"] = inherited
    before = dict(source)
    result = launcher.child_environment(MappingProxyType(source))
    assert result == {**before, "AGY_CLI_DISABLE_AUTO_UPDATE": "true"}
    assert source == before


def test_no_tools_main_passes_forced_update_setting_to_native(tmp_path, monkeypatch):
    native, source, schema = (tmp_path / name for name in ("native", "input", "schema"))
    native.write_text("not executed")
    source.write_text("fixed input")
    schema.write_text(json.dumps({"type": "object"}))
    monkeypatch.setenv("AGY_CLI_DISABLE_AUTO_UPDATE", "1")
    monkeypatch.setenv("HCOM_DIR", str(tmp_path / "owned-room"))
    before = dict(os.environ)
    calls = []

    def native_run(argv, **kwargs):
        calls.append((argv, kwargs))
        assert kwargs["env"] == {**before, "AGY_CLI_DISABLE_AUTO_UPDATE": "true"}
        assert json.loads(kwargs["input"])["message"]["content"] == "fixed input"
        return subprocess.CompletedProcess(argv, 17)

    monkeypatch.setattr(launcher.subprocess, "run", native_run)
    result = launcher.main(
        [
            "--native",
            str(native),
            "--input",
            str(source),
            "--schema",
            str(schema),
            "--model",
            "gemini-test",
            "--effort",
            "high",
            "--max-invocations",
            "1",
        ]
    )
    assert result == 17 and len(calls) == 1
    assert calls[0][0][0] == str(native)
    assert dict(os.environ) == before


def frames(**changes):
    return [
        {"event": "init", "conversation_id": "native", "init": {"model": "gemini-test"}},
        {
            "event": "step_update",
            "step_update": {
                "conversation_id": "native",
                "usage": {"input_tokens": 100, "output_tokens": 20},
            },
        },
        {
            "event": "result",
            "result": {
                "conversation_id": "native",
                "status": "SUCCESS",
                "num_turns": 2,
                "structured_output": {"ok": True},
                "usage": {
                    "input_tokens": 150,
                    "output_tokens": 30,
                    "thinking_tokens": 10,
                    "cache_read_tokens": 80,
                },
                **changes,
            },
        },
    ]


def parse(rows, **kwargs):
    return parse_output(
        json.dumps(rows), protocol="agy-json", selected_model="gemini-test", **kwargs
    )


def test_agy_cumulative_native_usage_is_not_double_counted():
    result = parse(frames())
    assert result["result"] == {"ok": True}
    assert result["turns"] == 2
    assert result["observed_model"] == "gemini-test"
    assert result["usage"] == {
        "input_tokens": 150,
        "output_tokens": 30,
        "reasoning_tokens": 10,
        "cache_read_tokens": 80,
    }


@pytest.mark.parametrize("status", ["CANCELED", "INTERRUPTED"])
def test_agy_native_cancellation_is_typed(status):
    assert parse(frames(status=status, num_turns=0))["outcome"] == "canceled"


def test_agy_native_failure_preserves_error_without_classifying_prose():
    result = parse(frames(status="ERROR", error="quota was exhausted"))
    assert result["outcome"] == "failed"
    assert "quota was exhausted" in result["reason"]
    assert parse(frames(status="ERROR", error={"code": "quota_exceeded"}))["outcome"] == "quota"


def test_agy_denied_schema_finish_keeps_native_telemetry_without_trusting_text():
    rows = frames(
        structured_output=None,
        response='{"ok":true}\n{"ok":true}\n',
        usage={
            "input_tokens": 21222,
            "output_tokens": 5068,
            "thinking_tokens": 4470,
            "cache_read_tokens": 0,
        },
    )
    rows.insert(
        2,
        {
            "event": "step_update",
            "step_update": {
                "conversation_id": "native",
                "step_index": 4,
                "state": "ERROR",
                "step_type": "tool",
                "tool_name": "finish",
                "tool_info": {
                    "name": "finish",
                    "parameters": {"ok": True},
                    "error": {"type": "TOOL_ERROR", "message": "tool call denied by pre-tool hook"},
                },
            },
        },
    )
    result = parse(rows)
    assert result["outcome"] == "failed" and result["failure_kind"] == "schema"
    assert result["result"] is None and result["native_terminal_completed"] is False
    assert result["session_id"] == "native" and result["observed_model"] == "gemini-test"
    assert result["turns"] == 2 and result["usage"]["output_tokens"] == 5068


def test_only_native_schema_finish_is_allowed_by_the_no_tools_gate(tmp_path):
    schema = {
        "type": "object",
        "properties": {"ok": {"type": "boolean"}},
        "required": ["ok"],
        "additionalProperties": False,
    }
    state = tmp_path / "state"
    call = {"toolCall": {"name": "finish", "args": {"ok": True}}}
    assert launcher.hook_result("tool", call, 2, state, schema=schema) == {"decision": "allow"}
    for name in (
        "run_command",
        "view_file",
        "send_message",
        "call_mcp_tool",
        "mcp.finish",
        "finish_extra",
    ):
        call["toolCall"]["name"] = name
        assert launcher.hook_result("tool", call, 2, state, schema=schema)["decision"] == "deny"
    call["toolCall"]["name"] = "finish"
    for args in ({}, {"ok": "true"}, {"ok": True, "command": "shell"}):
        call["toolCall"]["args"] = args
        assert launcher.hook_result("tool", call, 2, state, schema=schema)["decision"] == "deny"
    call["toolCall"]["args"] = {"ok": True}
    assert launcher.hook_result("tool", call, 2, state)["decision"] == "deny"


@pytest.mark.parametrize(
    "changes,match",
    [
        ({"status": "RUNNING"}, "terminal state"),
        ({"status": "WAITING"}, "terminal state"),
        ({"num_turns": True}, "turn count"),
        ({"usage": {"input_tokens": -1}}, "invalid counter"),
        ({"model": "other"}, "model identities"),
        ({"conversation_id": "other"}, "session identities"),
    ],
)
def test_agy_rejects_invalid_native_receipts(changes, match):
    with pytest.raises(CampaignError, match=match):
        parse(frames(**changes))


def test_agy_rejects_missing_or_duplicate_terminal_and_initialization():
    rows = frames()
    for damaged, match in (
        (rows[:-1], "terminal receipt is missing"),
        (rows + [rows[-1]], "continues after"),
        ([rows[0], *rows], "ambiguous initialization"),
        ([rows[-1]], "primary model"),
    ):
        with pytest.raises(CampaignError, match=match):
            parse(damaged)
    partial = parse(rows[:-1], partial=True)
    assert partial["outcome"] == "failed" and partial["result"] is None
    assert partial["session_id"] == "native"


def test_native_hooks_deny_tools_and_bound_the_entire_job(tmp_path):
    state = tmp_path / "invocations.json"
    assert launcher.hook_result("tool", {}, 2, state)["decision"] == "deny"
    for number in range(2):
        assert (
            launcher.hook_result(
                "invocation", {"conversationId": "native", "invocationNum": number}, 2, state
            )
            == {}
        )
    # Resetting native invocation numbering cannot reset the whole-job allowance.
    assert (
        launcher.hook_result(
            "invocation", {"conversationId": "native", "invocationNum": 0}, 2, state
        )["terminationBehavior"]
        == "terminate"
    )
    assert (
        launcher.hook_result(
            "invocation", {"conversationId": "other", "invocationNum": 0}, 2, state
        )["terminationBehavior"]
        == "terminate"
    )


def test_invalid_hook_payload_is_denied_before_invocation(tmp_path):
    for payload in (
        {},
        {"conversationId": "native", "invocationNum": True},
        {"conversationId": "native", "invocationNum": 2},
    ):
        assert (
            launcher.hook_result("invocation", payload, 2, tmp_path / "state")[
                "terminationBehavior"
            ]
            == "terminate"
        )


def native_proposal(tmp_path, prepared):
    runner, _, _, _, cap = prepared
    binary = tmp_path / "agy-recipe"
    binary.write_text(launcher.launcher_text(), encoding="utf-8")
    binary.chmod(0o700)
    native = tmp_path / "fake-agy"
    native.write_text(
        f"#!{sys.executable}\n" + """import json,re,shlex,subprocess,sys
from pathlib import Path
args=sys.argv[1:]
hooks=json.loads(Path('.agents/hooks.json').read_text())['bmad-no-tools']
payload={'conversationId':'native','invocationNum':0}
def hook(command,data):
 p=subprocess.run(shlex.split(command),input=json.dumps(data),text=True,capture_output=True,check=True)
 return json.loads(p.stdout)
assert hook(hooks['PreToolUse'][0]['hooks'][0]['command'],{})['decision']=='deny'
command=hooks['PreInvocation'][0]['command']
assert hook(command,payload)=={}
payload['invocationNum']=1
assert hook(command,payload)=={}
payload['invocationNum']=2
assert hook(command,payload)['terminationBehavior']=='terminate'
text=json.load(sys.stdin)['message']['content']
nonce=re.search(r'QUALIFICATION NONCE ([a-f0-9]+)',text).group(1)
markers=re.findall(r'CANARY [0-9]+: ([a-f0-9]+)',text)
print(json.dumps({'event':'init','conversation_id':'native','init':{'model':args[args.index('--model')+1]}}))
print(json.dumps({'event':'result','result':{'conversation_id':'native','status':'SUCCESS','num_turns':2,'structured_output':{'nonce':nonce,'markers':markers},'usage':{'input_tokens':2000,'output_tokens':512}}}))
""",
        encoding="utf-8",
    )
    native.chmod(0o700)
    cap = {
        **cap,
        "protocol": "agy-json",
        "binary": str(binary),
        "binary_sha256": file_digest(binary),
        "input_mode": "prompt-file",
        "max_turns": 2,
        "argv": [
            str(binary),
            "--native",
            str(native),
            "--model",
            "{model}",
            "--effort",
            "{effort}",
            "--input",
            "{input_path}",
            "--schema",
            "{schema_path}",
            "--max-invocations",
            "{max_turns}",
        ],
        "dependencies": [
            {"path": str(path), "sha256": file_digest(path)}
            for path in (Path(launcher.__file__).resolve(), Path(sys.executable).resolve(), native)
        ],
    }
    cap.pop("qualification")
    # The fixture's selected effort is arbitrary; use an actual AGY effort here.
    cap["efforts"] = ["high"]
    proposal = tmp_path / "agy-proposal.json"
    proposal.write_text(json.dumps(cap), encoding="utf-8")
    return runner, cap, proposal


def test_exact_agy_recipe_earns_two_invocation_canary(tmp_path, prepared):
    runner, _, proposal = native_proposal(tmp_path, prepared)
    result = qualify(
        proposal,
        tmp_path / "agy-job",
        input_bytes=4096,
        output_tokens=1024,
        timeout_s=20,
        runner=runner,
    )
    assert result["ok"], result
    capability = read_json(Path(result["capability_path"]))
    validate_capability(
        capability, RoleCandidate(capability["profile"], capability["model"], "high")
    )
    assert capability["max_turns"] == 2
    assert result["receipt"]["turns"] == 2
    assert result["receipt"]["quiescent"]


def test_agy_recipe_cannot_be_replaced_or_extended(tmp_path, prepared):
    _, cap, _ = native_proposal(tmp_path, prepared)
    _no_tools(cap)
    with pytest.raises(CampaignError, match="exact native hook"):
        _no_tools({**cap, "argv": [*cap["argv"], "--allow-tools"]})
    with pytest.raises(CampaignError, match="dependency binding"):
        _no_tools({**cap, "dependencies": []})
    Path(cap["binary"]).write_text("#!/bin/false\n", encoding="utf-8")
    with pytest.raises(CampaignError, match="exact native hook"):
        _no_tools(cap)
