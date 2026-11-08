"""Qualified native request limits use real scripted children, never inference."""

from dataclasses import replace
from pathlib import Path

import pytest
from test_campaign_headless import finish, fixture_capability
from test_campaign_headless import prepared as prepared

from bmad_loop import campaign_headless_worker, campaign_native_limits
from bmad_loop.campaign_headless import file_digest, read_json, validate_capability, write_json
from bmad_loop.campaign_model import CampaignError


def bounded(cap):
    return {
        **cap,
        "protocol": "claude-stream",
        "native_limits": dict(campaign_native_limits.CLAUDE_LIMITS),
        "dependencies": [
            *cap.get("dependencies", []),
            *(
                {"path": str(path), "sha256": file_digest(path)}
                for path in (
                    Path(campaign_native_limits.__file__).resolve(),
                    Path(campaign_headless_worker.__file__).resolve(),
                )
            ),
        ],
    }


@pytest.mark.parametrize("output", [128, 4096])
def test_exact_native_limits_override_inherited_values(prepared, monkeypatch, tmp_path, output):
    runner, _, request, candidate, original = prepared
    script = tmp_path / "claude-native-limits.py"
    script.write_text(
        """import json,os,sys
sys.stdin.buffer.read()
print(json.dumps({'type':'system','subtype':'init','model':'gpt-test','session_id':'limits'}))
print(json.dumps({'type':'result','subtype':'success','num_turns':1,
'structured_output':{k:os.environ.get(k) for k in ['CLAUDE_CODE_MAX_OUTPUT_TOKENS','MAX_STRUCTURED_OUTPUT_RETRIES']},
'usage':{'input_tokens':12,'output_tokens':8}}))
""",
        encoding="utf-8",
    )
    cap = bounded(
        {
            **original,
            "argv": [
                original["binary"],
                str(script),
                "--tools",
                "",
                "--strict-mcp-config",
                "--mcp-config",
                '{"mcpServers":{}}',
            ],
            "dependencies": [{"path": str(script), "sha256": file_digest(script)}],
        }
    )
    cap = fixture_capability(tmp_path, cap, candidate)
    monkeypatch.setenv("CLAUDE_CODE_MAX_OUTPUT_TOKENS", "128000")
    monkeypatch.setenv("MAX_STRUCTURED_OUTPUT_RETRIES", "99")
    request = replace(
        request,
        output_schema={"type": "object"},
        limits=replace(request.limits, output_tokens=output),
    )
    path = runner.prepare(
        tmp_path / "bounded", request, candidate, cap, launch_id="bounded", generation=1
    )
    runner.launch(path)
    receipt = finish(runner, path)
    assert receipt["outcome"] == "completed", receipt
    assert receipt["quiescent"]
    expected = {"CLAUDE_CODE_MAX_OUTPUT_TOKENS": str(output), "MAX_STRUCTURED_OUTPUT_RETRIES": "1"}
    assert receipt["result"] == expected
    assert receipt["native_request_limits"] == expected
    assert read_json(path)["capability"]["native_limits"] == campaign_native_limits.CLAUDE_LIMITS


def test_native_limit_contract_cannot_borrow_old_transport_proof(prepared):
    _, _, _, candidate, cap = prepared
    validate_capability(cap, candidate)
    changed = bounded(cap)
    with pytest.raises(CampaignError, match="route, executable or permissions changed"):
        validate_capability(changed, candidate)


@pytest.mark.parametrize("mutation", ["protocol", "unknown", "bool", "missing", "stale"])
def test_native_limit_contract_requires_exact_supported_dependencies(prepared, mutation):
    _, _, _, _, cap = prepared
    cap = bounded(cap)
    campaign_native_limits.validate(cap)
    if mutation == "protocol":
        cap["protocol"] = "codex-app-server"
    elif mutation == "unknown":
        cap["native_limits"]["unbounded"] = True
    elif mutation == "bool":
        cap["native_limits"]["structured_output_retries"] = True
    elif mutation == "missing":
        cap["dependencies"].pop()
    else:
        cap["dependencies"][-1]["sha256"] = "0" * 64
    with pytest.raises(CampaignError, match="native request-limit|Native request limits"):
        campaign_native_limits.validate(cap)


def test_legacy_recipe_has_no_new_native_environment(prepared):
    _, _, _, _, cap = prepared
    assert campaign_native_limits.environment(cap, 128) == {}


def test_native_limit_qualification_requires_actual_receipt_controls(prepared, tmp_path):
    _, _, _, candidate, original = prepared
    cap = bounded(
        {
            **original,
            "argv": [
                *original["argv"],
                "--tools",
                "",
                "--strict-mcp-config",
                "--mcp-config",
                '{"mcpServers":{}}',
            ],
        }
    )
    cap = fixture_capability(tmp_path, cap, candidate)
    validate_capability(cap, candidate)
    path = Path(cap["qualification"]["path"])
    report = read_json(path)
    report["receipt"].pop("native_request_limits")
    native_path = path.parent / "receipt.json"
    native = read_json(native_path)
    native.pop("native_request_limits")
    write_json(native_path, native)
    write_json(path, report)
    cap["qualification"]["sha256"] = file_digest(path)
    with pytest.raises(CampaignError, match="did not apply its requested output/retry limits"):
        validate_capability(cap, candidate)
