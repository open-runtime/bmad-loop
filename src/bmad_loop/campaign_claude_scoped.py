"""Claude SDK recipe exposing only the campaign's confined stdio MCP tool.

The account/hcom wrapper runs normally. Model commands execute in disposable
scoped tool processes; the model cannot select a different grants file or gain
the wrapper's filesystem authority. Native JSON and lifecycle telemetry stay
unchanged for the ordinary headless runner.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .campaign_model import CampaignError
from .platform_util import atomic_write_text, is_link_like

CONFINEMENT = "claude-scoped-mcp-v1"
MAX_FRAME_BYTES = 131072


def launcher_text():
    return (
        f"#!{sys.executable}\n"
        "from bmad_loop.campaign_claude_scoped import main\n"
        "raise SystemExit(main())\n"
    )


def validate_recipe(capability):
    from . import campaign_scoped_tools
    from .campaign_execution_sandbox import __file__ as sandbox_file
    from .campaign_headless import file_digest

    launcher = Path(capability["binary"])
    argv = capability["argv"]
    expected = [
        str(launcher),
        "--native",
        argv[2] if len(argv) > 2 else "",
        "--grants",
        "{grants_path}",
        "--input",
        "{input_path}",
        "--schema",
        "{schema_path}",
        "--model",
        "{model}",
        "--effort",
        "{effort}",
        "--max-turns",
        "{max_turns}",
    ]
    if (
        capability.get("confinement") != CONFINEMENT
        or capability.get("permissions") != "workspace-tools"
        or capability.get("protocol") != "claude-stream"
        or capability.get("input_mode") != "claude-stream-stdin"
        or capability.get("runtime_write_paths")
        or argv != expected
        or not Path(expected[2]).is_absolute()
        or launcher.read_text(encoding="utf-8") != launcher_text()
    ):
        raise CampaignError("Claude writer requires the exact confined MCP recipe")
    dependencies = {item["path"]: item["sha256"] for item in capability.get("dependencies", [])}
    for path in (
        Path(__file__).resolve(),
        Path(sys.executable).resolve(),
        Path(expected[2]),
        Path(campaign_scoped_tools.__file__).resolve(),
        Path(sandbox_file).resolve(),
    ):
        if dependencies.get(str(path)) != file_digest(path):
            raise CampaignError("Claude scoped tool/native dependency lacks an exact binding")


def serve(source, sink, executor):
    """Minimal synchronous MCP; every command retains its immutable request ID."""
    from .campaign_scoped_tools import DESCRIPTION, JSON_SCHEMA

    for _ in range(4096):
        line = source.readline(MAX_FRAME_BYTES + 1)
        if not line:
            return 0
        if len(line.encode("utf-8")) > MAX_FRAME_BYTES:
            raise CampaignError("Scoped MCP frame exceeds its byte bound")
        try:
            request = json.loads(line)
        except ValueError as exc:
            raise CampaignError("Scoped MCP request is invalid JSON") from exc
        if not isinstance(request, dict) or request.get("jsonrpc") != "2.0":
            raise CampaignError("Scoped MCP request is not JSON-RPC 2.0")
        if "id" not in request:
            continue
        identity = request["id"]
        if type(identity) not in (int, str) or len(str(identity)) > 128:
            raise CampaignError("Scoped MCP request identity is invalid")
        response = {"jsonrpc": "2.0", "id": identity}
        method, params = request.get("method"), request.get("params") or {}
        if not isinstance(params, dict):
            response["error"] = {"code": -32602, "message": "Invalid parameters"}
        elif method == "initialize":
            version = params.get("protocolVersion")
            response["result"] = {
                "protocolVersion": (
                    version
                    if version in {"2024-11-05", "2025-03-26", "2025-06-18"}
                    else "2024-11-05"
                ),
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "bmad-scoped-tools", "version": "1"},
            }
        elif method == "tools/list":
            response["result"] = {
                "tools": [
                    {"name": "bmad_shell", "description": DESCRIPTION, "inputSchema": JSON_SCHEMA}
                ]
            }
        elif method == "tools/call" and params.get("name") == "bmad_shell":
            try:
                result = executor.execute("mcp:" + json.dumps(identity), params.get("arguments"))
                response["result"] = {
                    "content": [
                        {"type": "text", "text": item["text"]} for item in result["contentItems"]
                    ],
                    "isError": not result["success"],
                }
            except (CampaignError, OSError, ValueError) as exc:
                response["result"] = {
                    "content": [{"type": "text", "text": str(exc)}],
                    "isError": True,
                }
        elif method in {"ping", "resources/list", "prompts/list"}:
            response["result"] = (
                {"resources": []}
                if method == "resources/list"
                else ({"prompts": []} if method == "prompts/list" else {})
            )
        else:
            response["error"] = {"code": -32601, "message": "Method or tool is unavailable"}
        sink.write(json.dumps(response) + "\n")
        sink.flush()
    raise CampaignError("Scoped MCP request count exceeded")


def prepare_native(args, launcher):
    """Create one explicit MCP binding; inherit supported account routing only."""
    for path in (args.native, args.grants, args.input, args.schema):
        if not path or not path.is_absolute() or is_link_like(path) or not path.is_file():
            raise CampaignError("Claude scoped recipe requires exact native/input/grant files")
    if not 1 <= args.max_turns <= 64 or args.effort not in {
        "low",
        "medium",
        "high",
        "xhigh",
        "max",
    }:
        raise CampaignError("Claude scoped route needs finite turns and supported effort")
    grants = json.loads(args.grants.read_text(encoding="utf-8"))
    workspace = Path(grants["workspace"])
    directory = args.input.parent / "claude-native"
    if directory.exists():
        raise CampaignError("Claude scoped native launch directory already exists")
    directory.mkdir(mode=0o700)
    config = directory / "mcp.json"
    atomic_write_text(
        config,
        json.dumps(
            {
                "mcpServers": {
                    "bmad": {
                        "command": str(launcher),
                        "args": [
                            "--mcp",
                            "--grants",
                            str(args.grants),
                            "--directory",
                            str(args.input.parent / "scoped-tools"),
                        ],
                    }
                }
            }
        ),
        follow_symlinks=False,
    )
    native = [
        str(args.native),
        "--print",
        "--input-format",
        "stream-json",
        "--output-format",
        "stream-json",
        "--verbose",
        "--model",
        args.model,
        "--effort",
        args.effort,
        "--max-turns",
        str(args.max_turns),
        "--json-schema",
        args.schema.read_text(encoding="utf-8"),
        "--tools",
        "",
        "--strict-mcp-config",
        "--mcp-config",
        str(config),
        "--allowedTools",
        "mcp__bmad__bmad_shell",
        "--permission-mode",
        "dontAsk",
        "--disable-slash-commands",
        "--no-chrome",
        "--append-system-prompt",
        f"Your product workspace is {workspace}. Use only mcp__bmad__bmad_shell for reading, editing, Git and commands. Its frozen filesystem grants are authoritative. Do not launch background tasks or other agents. Native process cwd is private transport state, not the product checkout.",
    ]
    return native, directory


def native_tool_checks(job, records, calls):
    """Bind scoped call receipts to actual Claude tools and emitted tool inputs."""
    from .campaign_jobs import digest

    advertised = None
    arguments = set()
    for line in (job / "stdout.jsonl").read_text(encoding="utf-8").splitlines():
        frame = json.loads(line)
        if frame.get("type") == "system" and frame.get("subtype") == "init":
            advertised = frame.get("tools")
        if frame.get("type") == "assistant":
            for block in frame.get("message", {}).get("content", []):
                if block.get("type") != "tool_use":
                    continue
                if block.get("name") == "mcp__bmad__bmad_shell":
                    arguments.add(digest(block.get("input")))
                elif block.get("name") != "StructuredOutput":
                    raise CampaignError("Claude invoked an unqualified native tool")
    if (
        not isinstance(advertised, list)
        or "mcp__bmad__bmad_shell" not in advertised
        or set(advertised) - {"mcp__bmad__bmad_shell", "StructuredOutput"}
    ):
        raise CampaignError("Claude did not expose only the qualified scoped tool")
    if not records or any(
        digest(record.get("call_id")) != key
        or digest(record.get("arguments")) not in arguments
        or record.get("state") != "finished"
        or record.get("receipt", {}).get("processes_quiescent") is not True
        for key, record in zip(calls, records, strict=True)
    ):
        raise CampaignError("Claude scoped calls lack native provenance or quiescence")
    return {
        "native_builtin_confinement": True,
        "scoped_native_calls": any(
            record.get("result", {}).get("success") is True for record in records
        ),
        "scoped_native_quiescence": True,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mcp", action="store_true")
    parser.add_argument("--directory", type=Path)
    parser.add_argument("--native", type=Path)
    parser.add_argument("--grants", type=Path, required=True)
    parser.add_argument("--input", type=Path)
    parser.add_argument("--schema", type=Path)
    parser.add_argument("--model")
    parser.add_argument("--effort")
    parser.add_argument("--max-turns", type=int, default=1)
    args = parser.parse_args(argv)
    if args.mcp:
        from .campaign_scoped_tools import ScopedTools

        if not args.directory or not args.directory.is_absolute():
            raise CampaignError("Scoped MCP requires its owned directory")
        return serve(sys.stdin, sys.stdout, ScopedTools(args.grants, args.directory))
    native, cwd = prepare_native(args, Path(sys.argv[0]).absolute())
    os.chdir(cwd)
    # exec retains the owned process identity and the SDK's real stream. No
    # wrapper output is synthesized into a model receipt.
    with args.input.open("rb") as source:
        os.dup2(source.fileno(), 0)
    os.execve(
        native[0], native, dict(os.environ)
    )  # nosec B606 -- exact qualified owner launcher, argv only
    return 1
