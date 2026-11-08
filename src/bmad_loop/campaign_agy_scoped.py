"""AGY writer through its documented pre-tool argument rewrite.

Only native run_command and schema finish are admitted. Each run_command is
rewritten to the pinned bounded command worker, which owns the actual filesystem
grant. Native invocation limits and raw terminal telemetry remain independent.
"""

from __future__ import annotations

import argparse
import base64
import json
import shlex
import subprocess
import sys
from pathlib import Path

from .campaign_agy_launcher import hook_result as bounded_hook
from .campaign_jobs import digest
from .campaign_model import CampaignError
from .platform_util import file_lock, is_link_like

CONFINEMENT = "agy-scoped-command-v1"


def launcher_text():
    return f"#!{sys.executable}\nfrom bmad_loop.campaign_agy_scoped import main\nraise SystemExit(main())\n"


def validate_recipe(capability):
    from . import campaign_agy_launcher, campaign_execution_sandbox, campaign_scoped_tools
    from .campaign_headless import file_digest

    argv = capability["argv"]
    launcher = Path(capability["binary"])
    expected = [
        str(launcher),
        "--native",
        argv[2] if len(argv) > 2 else "",
        "--grants",
        "{grants_path}",
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
    ]
    if (
        capability.get("confinement") != CONFINEMENT
        or capability.get("permissions") != "workspace-tools"
        or capability.get("protocol") != "agy-json"
        or capability.get("input_mode") != "prompt-file"
        or capability.get("runtime_write_paths")
        or argv != expected
        or not Path(expected[2]).is_absolute()
        or launcher.read_text(encoding="utf-8") != launcher_text()
    ):
        raise CampaignError("AGY writer requires its exact scoped-command hook recipe")
    dependencies = {row["path"]: row["sha256"] for row in capability.get("dependencies", [])}
    for path in (
        Path(__file__).resolve(),
        Path(sys.executable).resolve(),
        Path(expected[2]),
        Path(campaign_agy_launcher.__file__).resolve(),
        Path(campaign_scoped_tools.__file__).resolve(),
        Path(campaign_execution_sandbox.__file__).resolve(),
    ):
        if dependencies.get(str(path)) != file_digest(path):
            raise CampaignError("AGY scoped command dependency lacks an exact binding")


def tool_hook(payload, *, launcher, grants, directory, schema):
    """Bind native call identity before replacing its executable command text."""
    call = payload.get("toolCall", {})
    if call.get("name") == "finish":
        return bounded_hook("tool", payload, 1, directory / "unused", schema=schema)
    denied = {"decision": "deny", "reason": "Only the bounded BMAD command tool is allowed"}
    session, step = payload.get("conversationId"), payload.get("stepIdx")
    command = call.get("args", {}).get("CommandLine")
    if (
        call.get("name") != "run_command"
        or not isinstance(session, str)
        or not session
        or type(step) is not int
        or step < 0
        or not isinstance(command, str)
        or not command
        or len(command.encode()) > 24576
        or "\0" in command
    ):
        return denied
    with file_lock(directory / "calls.lock"):
        invocation = json.loads((directory / "invocations.json").read_text(encoding="utf-8"))
        if invocation.get("session") != session:
            return denied
        path = directory / "calls.json"
        if is_link_like(path):
            return denied
        calls = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        identity = session + ":" + str(step)
        arguments = {"command": ["/bin/sh", "-c", command]}
        key = digest(identity)
        binding = {"call_id": identity, "arguments": arguments}
        if (key in calls and calls[key] != binding) or (key not in calls and len(calls) >= 32):
            return denied
        calls[key] = binding
        path.write_text(json.dumps(calls), encoding="utf-8")
    encoded = base64.b64encode(json.dumps(arguments).encode()).decode()
    executable = [
        str(launcher),
        "--execute",
        "--grants",
        str(grants),
        "--directory",
        str(directory),
        "--call-id",
        identity,
        "--arguments",
        encoded,
    ]
    return {"decision": "allow", "overwrite": {"CommandLine": shlex.join(executable)}}


def prepare(directory, launcher, grants, schema, maximum):
    directory.mkdir(mode=0o700)
    (directory / ".agents").mkdir(mode=0o700)

    def hook(event):
        return shlex.join(
            [
                str(launcher),
                "--hook",
                event,
                "--grants",
                str(grants),
                "--directory",
                str(directory),
                "--schema",
                str(schema),
                "--max-invocations",
                str(maximum),
            ]
        )

    hooks = {
        "bmad-scoped-writer": {
            "PreToolUse": [
                {
                    "matcher": "*",
                    "hooks": [{"type": "command", "command": hook("tool"), "timeout": 10}],
                }
            ],
            "PreInvocation": [{"type": "command", "command": hook("invocation"), "timeout": 10}],
        }
    }
    (directory / ".agents/hooks.json").write_text(json.dumps(hooks), encoding="utf-8")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--native", type=Path)
    parser.add_argument("--grants", type=Path, required=True)
    parser.add_argument("--input", type=Path)
    parser.add_argument("--schema", type=Path)
    parser.add_argument("--model")
    parser.add_argument("--effort")
    parser.add_argument("--max-invocations", type=int)
    parser.add_argument("--hook", choices=("tool", "invocation"))
    parser.add_argument("--directory", type=Path)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--call-id")
    parser.add_argument("--arguments")
    args = parser.parse_args(argv)
    if args.execute:
        from .campaign_scoped_tools import ScopedTools

        if not args.directory or not args.arguments or len(args.arguments) > 65536:
            raise CampaignError("AGY scoped command arguments are invalid")
        arguments = json.loads(base64.b64decode(args.arguments, validate=True))
        expected = json.loads((args.directory / "calls.json").read_text(encoding="utf-8"))
        if expected.get(digest(args.call_id)) != {"call_id": args.call_id, "arguments": arguments}:
            raise CampaignError("AGY scoped command differs from its native hook binding")
        response = ScopedTools(args.grants, args.grants.parent / "scoped-tools").execute(
            args.call_id, arguments
        )
        print(json.dumps(response))
        return 0 if response["success"] else 1
    if args.hook:
        try:
            payload = json.loads(sys.stdin.buffer.read(131073))
            schema = json.loads(args.schema.read_text(encoding="utf-8"))
            if args.hook == "tool":
                result = tool_hook(
                    payload,
                    launcher=Path(sys.argv[0]).absolute(),
                    grants=args.grants,
                    directory=args.directory,
                    schema=schema,
                )
            else:
                result = bounded_hook(
                    "invocation", payload, args.max_invocations, args.directory / "invocations.json"
                )
                if not result:
                    workspace = json.loads(args.grants.read_text(encoding="utf-8"))["workspace"]
                    result = {
                        "injectSteps": [
                            {
                                "systemMessage": {
                                    "systemMessage": "Only run_command is available for repository reads/writes. Each command runs in "
                                    + workspace
                                    + " under exact BMAD ownership grants. All commands must be foreground; do not start daemons. Use finish for the final schema result."
                                }
                            }
                        ]
                    }
        except (OSError, ValueError, TypeError, KeyError, AttributeError, CampaignError):
            result = (
                {"decision": "deny"}
                if args.hook == "tool"
                else {"terminationBehavior": "terminate"}
            )
        print(json.dumps(result))
        return 0
    if (
        not args.native
        or not args.native.is_absolute()
        or not args.input
        or not args.schema
        or not args.model
        or args.effort not in {"", "low", "medium", "high"}
        or type(args.max_invocations) is not int
        or not 1 <= args.max_invocations <= 64
    ):
        parser.error("Exact native route and finite invocation allowance required")
    if any(
        is_link_like(path) or not path.is_file()
        for path in (args.native, args.input, args.schema, args.grants)
    ):
        raise CampaignError("AGY writer input is missing or redirected")
    grants = json.loads(args.grants.read_text(encoding="utf-8"))
    if (
        grants.get("runtime_write_paths")
        or not grants.get("scopes")
        or not grants.get("config_digest")
    ):
        raise CampaignError("AGY writer requires frozen ownership scopes")
    directory = args.grants.absolute().parent / "agy-native"
    prepare(
        directory,
        Path(sys.argv[0]).absolute(),
        args.grants.absolute(),
        args.schema.absolute(),
        args.max_invocations,
    )
    native = [
        str(args.native),
        "--model",
        args.model,
        "--input-format",
        "stream-json",
        "--output-format",
        "stream-json",
        "--json-schema",
        str(args.schema.absolute()),
        "--disable-slash-commands",
        "--settings",
        json.dumps({"trustedWorkspaces": [str(directory)]}),
    ]
    if args.effort:
        native.extend(["--effort", args.effort])
    message = {"event": "user", "message": {"content": args.input.read_text(encoding="utf-8")}}
    return subprocess.run(
        native, cwd=directory, input=json.dumps(message) + "\n", text=True
    ).returncode


if __name__ == "__main__":
    raise SystemExit(main())
