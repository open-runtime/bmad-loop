"""Zero-token Claude MCP recipe, protocol and native tool provenance checks."""

import argparse
import io
import json
from pathlib import Path

import pytest

from bmad_loop.campaign_claude_scoped import native_tool_checks, prepare_native, serve
from bmad_loop.campaign_jobs import digest
from bmad_loop.campaign_model import CampaignError


class Executor:
    def __init__(self):
        self.calls = []

    def execute(self, identity, arguments):
        self.calls.append((identity, arguments))
        return {"success": True, "contentItems": [{"type": "inputText", "text": "confined"}]}


def rpc(method, identity=1, **params):
    return {"jsonrpc": "2.0", "id": identity, "method": method, "params": params}


def test_mcp_exposes_only_one_tool_and_preserves_call_identity():
    executor, output = Executor(), io.StringIO()
    frames = [
        rpc("initialize", protocolVersion="2025-03-26"),
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        rpc("tools/list", 2),
        rpc("tools/call", "native-call", name="bmad_shell", arguments={"command": ["pwd"]}),
        rpc("tools/call", 4, name="unapproved", arguments={"command": ["pwd"]}),
        rpc("resources/list", 5),
    ]
    assert serve(io.StringIO("\n".join(map(json.dumps, frames))), output, executor) == 0
    results = [json.loads(line) for line in output.getvalue().splitlines()]
    assert results[0]["result"]["protocolVersion"] == "2025-03-26"
    assert [tool["name"] for tool in results[1]["result"]["tools"]] == ["bmad_shell"]
    assert results[2]["result"] == {
        "content": [{"type": "text", "text": "confined"}],
        "isError": False,
    }
    assert results[3]["error"]["code"] == -32601
    assert executor.calls == [('mcp:"native-call"', {"command": ["pwd"]})]
    assert results[4]["result"] == {"resources": []}


@pytest.mark.parametrize("frame", ["x" * 131073, "not-json", '{"jsonrpc":"1.0"}'])
def test_mcp_rejects_bad_frames(frame):
    executor = Executor()
    with pytest.raises(CampaignError):
        serve(io.StringIO(frame), io.StringIO(), executor)
    assert not executor.calls


def test_native_recipe_disables_builtins_and_user_mcp(tmp_path):
    for name in ("native", "input", "schema", "grants"):
        (tmp_path / name).write_text(json.dumps({"workspace": str(tmp_path / "workspace")}))
    args = argparse.Namespace(
        **{name: tmp_path / name for name in ("native", "input", "schema", "grants")},
        model="claude-fable",
        effort="max",
        max_turns=8,
    )
    launcher = tmp_path / "launcher"
    argv, cwd = prepare_native(args, launcher)
    assert argv[0] == str(args.native)
    assert argv[argv.index("--tools") + 1] == ""
    assert "--strict-mcp-config" in argv
    assert argv[argv.index("--permission-mode") + 1] == "dontAsk"
    assert argv[argv.index("--allowedTools") + 1] == "mcp__bmad__bmad_shell"
    config = json.loads(Path(argv[argv.index("--mcp-config") + 1]).read_text())
    assert list(config["mcpServers"]) == ["bmad"]
    server = config["mcpServers"]["bmad"]
    assert server["command"] == str(launcher)
    assert server["args"][server["args"].index("--grants") + 1] == str(args.grants)
    assert cwd == tmp_path / "claude-native"
    with pytest.raises(CampaignError, match="already exists"):
        prepare_native(args, launcher)


@pytest.mark.parametrize("defect", ["", "tools", "call", "arguments", "quiescence"])
def test_native_writer_proof_requires_scoped_calls_and_confined_advertised_tools(tmp_path, defect):
    arguments = {"command": ["pwd"]}
    frames = [
        {
            "type": "system",
            "subtype": "init",
            "tools": ["mcp__bmad__bmad_shell", "StructuredOutput"],
        },
        {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "tool_use", "name": "mcp__bmad__bmad_shell", "input": arguments}
                ]
            },
        },
    ]
    record = {
        "call_id": "mcp:1",
        "arguments": arguments,
        "state": "finished",
        "receipt": {"processes_quiescent": True},
        "result": {"success": True},
    }
    if defect == "tools":
        frames[0]["tools"].append("Bash")
    elif defect == "call":
        frames[1]["message"]["content"][0]["name"] = "Bash"
    elif defect == "arguments":
        record["arguments"] = {"command": ["different"]}
    elif defect == "quiescence":
        record["receipt"]["processes_quiescent"] = False
    (tmp_path / "stdout.jsonl").write_text("\n".join(map(json.dumps, frames)))
    if defect:
        with pytest.raises(CampaignError):
            native_tool_checks(tmp_path, [record], [digest("mcp:1")])
    else:
        assert all(native_tool_checks(tmp_path, [record], [digest("mcp:1")]).values())
