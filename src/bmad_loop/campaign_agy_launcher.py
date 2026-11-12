"""Pinned no-tools AGY launch recipe with native pre-tool/invocation gates.

The CLI runs in a new private customization directory, never the product tree.
Hooks use AGY's documented camelCase protocol, not settings.json's ignored hooks
field. This recipe must still earn a native canary before ordinary admission.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path

from .campaign_model import CampaignError
from .platform_util import file_lock, is_link_like


def child_environment(source: Mapping[str, str]) -> dict[str, str]:
    """Keep the pinned native image stable without changing the caller's env.

    AGY compares this provider-owned setting to the literal ``true``; ``1``
    does not disable its background updater, including in headless sessions.
    """
    return {**source, "AGY_CLI_DISABLE_AUTO_UPDATE": "true"}


def launcher_text():
    return (
        f"#!{sys.executable}\n"
        "from bmad_loop.campaign_agy_launcher import main\n"
        "raise SystemExit(main())\n"
    )


def validate_recipe(capability):
    """Check exact argv and dependency bindings; an arbitrary script is not a gate."""
    from .campaign_headless import file_digest

    argv = capability["argv"]
    launcher = Path(capability["binary"])
    expected = [
        str(launcher),
        "--native",
        argv[2] if len(argv) > 2 else "",
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
        argv != expected
        or capability.get("input_mode") != "prompt-file"
        or not Path(expected[2]).is_absolute()
        or launcher.read_text(encoding="utf-8") != launcher_text()
    ):
        raise CampaignError("AGY no-tools qualification requires the exact native hook recipe")
    dependencies = {row["path"]: row["sha256"] for row in capability.get("dependencies", [])}
    for path in (Path(__file__).resolve(), Path(sys.executable).resolve(), Path(expected[2])):
        if dependencies.get(str(path)) != file_digest(path):
            raise CampaignError("AGY hook/native executable lacks an exact dependency binding")


def hook_result(event, payload, maximum, state, *, schema=None):
    """Native calls are denied by default; invocation counting is whole-job scoped."""
    if event == "tool":
        from .campaign_jobs import validate_result, validate_schema

        call = payload.get("toolCall", {})
        if isinstance(call, dict) and call.get("name") == "finish" and isinstance(schema, dict):
            try:
                validate_schema(schema)
                validate_result(call.get("args"), schema)
            except CampaignError:
                pass
            else:
                # AGY's finish operation returns schema output to its terminal
                # envelope; it cannot read/write files, execute or message.
                return {"decision": "allow"}
        return {"decision": "deny", "reason": "BMAD no-tools reasoning job"}
    denied = {"terminationBehavior": "terminate"}
    if event != "invocation" or type(maximum) is not int or not 1 <= maximum <= 64:
        return denied
    session = payload.get("conversationId")
    number = payload.get("invocationNum")
    if not isinstance(session, str) or not session or type(number) is not int or number < 0:
        return denied
    with file_lock(state.with_suffix(".lock")):
        if is_link_like(state):
            return denied
        previous = json.loads(state.read_text(encoding="utf-8")) if state.exists() else {}
        if previous and previous.get("session") != session:
            return denied
        count = previous.get("count", 0)
        if type(count) is not int or count >= maximum or number >= maximum:
            return denied
        state.write_text(json.dumps({"session": session, "count": count + 1}), encoding="utf-8")
    return {}


def prepare_context(directory, launcher, maximum, *, schema=None):
    """Do not modify inherited project/global customizations or existing contexts."""
    directory.mkdir(mode=0o700)
    customizations = directory / ".agents"
    customizations.mkdir(mode=0o700)
    state = directory / "invocations.json"

    def command(event):
        argv = [
            str(launcher),
            "--hook",
            event,
            "--max-invocations",
            str(maximum),
            "--state",
            str(state),
        ]
        if schema is not None:
            argv.extend(["--schema", str(schema)])
        return shlex.join(argv)

    hooks = {
        "bmad-no-tools": {
            "PreToolUse": [
                {
                    "matcher": "*",
                    "hooks": [
                        {"type": "command", "command": command("tool"), "timeout": 10},
                    ],
                }
            ],
            "PreInvocation": [
                {"type": "command", "command": command("invocation"), "timeout": 10},
            ],
        },
    }
    (customizations / "hooks.json").write_text(json.dumps(hooks), encoding="utf-8")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hook", choices=("tool", "invocation"))
    parser.add_argument("--state", type=Path)
    parser.add_argument("--max-invocations", type=int, required=True)
    parser.add_argument("--native", type=Path)
    parser.add_argument("--model")
    parser.add_argument("--effort")
    parser.add_argument("--input", type=Path)
    parser.add_argument("--schema", type=Path)
    args = parser.parse_args(argv)
    if args.hook:
        try:
            payload = json.load(sys.stdin)
            schema = (
                json.loads(args.schema.read_text(encoding="utf-8"))
                if args.schema and not is_link_like(args.schema)
                else None
            )
            result = hook_result(
                args.hook, payload, args.max_invocations, args.state, schema=schema
            )
        except (OSError, ValueError, TypeError, AttributeError):
            result = (
                {"decision": "deny"}
                if args.hook == "tool"
                else {"terminationBehavior": "terminate"}
            )
        print(json.dumps(result))
        return 0
    if (
        not 1 <= args.max_invocations <= 64
        or not args.native
        or not args.native.is_absolute()
        or not args.input
        or not args.schema
        or not args.model
        or args.effort not in {"", "low", "medium", "high"}
    ):
        parser.error("native route, input, schema and finite supported effort/budget required")
    if any(
        is_link_like(path) or not path.is_file() for path in (args.native, args.input, args.schema)
    ):
        raise CampaignError("AGY recipe input is missing or redirected")
    directory = args.input.absolute().parent / "agy-native"
    prepare_context(
        directory, Path(sys.argv[0]).absolute(), args.max_invocations, schema=args.schema.absolute()
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
    # Exactly one input turn. Native init/result envelopes remain unmodified.
    message = {"event": "user", "message": {"content": args.input.read_text(encoding="utf-8")}}
    return subprocess.run(
        native,
        cwd=directory,
        input=json.dumps(message) + "\n",
        text=True,
        env=child_environment(os.environ),
    ).returncode


if __name__ == "__main__":
    raise SystemExit(main())
