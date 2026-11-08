"""One bounded Codex app-server turn or native review over owned stdio.

The native thread/start response supplies model identity. No model-generated
JSON or requested model is promoted to observed telemetry. This transport is a
tools-capable route; it cannot earn no-tools qualification. Writers run under the
campaign's separately qualified outer filesystem confinement.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from .campaign_model import CampaignError
from .childrun import kill_tree

MAX_FRAME_BYTES = 16 * 1024 * 1024
ERRORS = {
    "usageLimitExceeded": "usage_limit_reached",
    "rateLimitExceeded": "rate_limit_exceeded",
    "contextWindowExceeded": "context_length_exceeded",
    "sessionBudgetExceeded": "max_tokens",
    "unauthorized": "unauthorized",
    "serverOverloaded": "overloaded_error",
    "internalServerError": "server_error",
}


def envelopes(frames, *, partial=False, review=False):
    """Decode only native RPC response IDs and notifications from this recipe."""
    thread = model = turn_id = ""
    terminal = None
    usage = {}
    items = {}
    error = {}
    observed_turns = set()
    child_turns = set()
    for frame in frames:
        if frame.get("id") == 2 and "result" in frame:
            result = frame["result"]
            if thread or not isinstance(result, dict):
                raise CampaignError("Codex native thread initialization is ambiguous")
            thread = result.get("thread", {}).get("id", "")
            model = result.get("model", "")
            if not isinstance(thread, str) or not thread or not isinstance(model, str) or not model:
                raise CampaignError("Codex native thread/model identity is missing")
        method = frame.get("method", "")
        params = frame.get("params", {})
        if not isinstance(params, dict):
            raise CampaignError("Codex native notification parameters are invalid")
        if "threadId" in params and thread and params["threadId"] != thread:
            raise CampaignError("Codex native notification belongs to another thread")
        if params.get("turnId"):
            observed_turns.add(params["turnId"])
        if frame.get("id") == 3 and isinstance(frame.get("result"), dict):
            turn_id = frame["result"].get("turn", {}).get("id", "")
        if review and method == "turn/started":
            started = params.get("turn", {})
            if started.get("id") != turn_id:
                if not turn_id or started.get("rootTurnId") != turn_id or not started.get("id"):
                    raise CampaignError("Codex review delegate has invalid native turn lineage")
                child_turns.add(started["id"])
                if len(child_turns) > 1:
                    raise CampaignError("Codex review contains multiple native delegates")
        if method == "thread/tokenUsage/updated":
            totals = params.get("tokenUsage", {}).get("total")
            if not isinstance(totals, dict):
                raise CampaignError("Codex native cumulative usage is missing")
            for source, target in (
                ("inputTokens", "input_tokens"),
                ("outputTokens", "output_tokens"),
                ("cachedInputTokens", "cache_read_tokens"),
                ("reasoningOutputTokens", "reasoning_tokens"),
            ):
                value = totals.get(source)
                if type(value) is not int or value < usage.get(target, 0):
                    raise CampaignError("Codex native cumulative usage is invalid or decreased")
                usage[target] = value
        if method == "item/completed":
            item = params.get("item", {})
            if isinstance(item, dict) and isinstance(item.get("id"), str):
                items[item["id"]] = item
        if method == "error" and params.get("willRetry") is False:
            error = params.get("error") or {}
        if method == "turn/completed":
            if terminal is not None:
                raise CampaignError("Codex native stream contains multiple completed turns")
            terminal = params.get("turn")
            if not isinstance(terminal, dict):
                raise CampaignError("Codex native terminal turn is missing")
    init = {"type": "system", "session_id": thread, "model": model}
    if terminal is None:
        if partial:
            return [init]
        raise CampaignError("Codex native terminal turn is missing")
    if not turn_id or terminal.get("id") != turn_id or observed_turns - {turn_id, *child_turns}:
        raise CampaignError("Codex native terminal turn identity changed")
    status = terminal.get("status")
    if status not in {"completed", "failed", "interrupted"}:
        raise CampaignError("Codex native turn is not terminal")
    for item in terminal.get("items", []):
        if isinstance(item, dict) and isinstance(item.get("id"), str):
            items[item["id"]] = item
    messages = [
        item.get("text", "")
        for item in items.values()
        if item.get("type") == "agentMessage" and item.get("phase") != "commentary"
    ]
    reviews = [
        item.get("review", "") for item in items.values() if item.get("type") == "exitedReviewMode"
    ]
    error = terminal.get("error") or error
    code = error.get("codexErrorInfo") if isinstance(error, dict) else ""
    if isinstance(code, dict):
        code = (
            "connection_error"
            if set(code)
            & {
                "httpConnectionFailed",
                "responseStreamConnectionFailed",
                "responseStreamDisconnected",
                "responseTooManyFailedAttempts",
            }
            else ""
        )
    if not isinstance(code, str):
        code = ""
    return [
        init,
        {
            "type": "result",
            "session_id": thread,
            "model": model,
            "result": (reviews or messages or [""])[-1],
            "usage": usage,
            "is_error": status != "completed",
            "error": {"code": ERRORS.get(code, code)},
            "native_canceled": status == "interrupted",
        },
    ]


def launcher_text():
    return f"#!{sys.executable}\nfrom bmad_loop.campaign_codex_appserver import main\nraise SystemExit(main())\n"


def validate_recipe(capability):
    """Pin the native RPC launcher and its dependencies beneath OS confinement."""
    from .campaign_headless import file_digest

    argv = capability["argv"]
    native_sandbox = capability.get("confinement") == "codex-native-read-only-v1"
    scoped_tools = capability.get("confinement") == "codex-scoped-tools-v1"
    recipe = argv if native_sandbox or scoped_tools else argv[4:]
    readonly = capability["permissions"] == "read-only-tools"
    if len(recipe) < 3:
        raise CampaignError("Codex app-server recipe is incomplete")
    launcher, native = Path(recipe[0]), Path(recipe[2])
    expected = [
        str(launcher),
        "--native",
        str(native),
        "--workspace",
        "{workspace}",
        "--model",
        "{model}",
        "--effort",
        "{effort}",
        "--input",
        "{input_path}",
        "--schema",
        "{schema_path}",
    ]
    review = capability["protocol"] == "codex-native-review"
    variants = (
        [expected + ["--review"]]
        if review and readonly
        else [expected + ["--read-only"]] if readonly else [expected]
    )
    if native_sandbox:
        variants = [
            variant + ["--native-sandbox", "--grants", "{grants_path}"] for variant in variants
        ]
    if scoped_tools:
        variants = [expected + ["--scoped-tools", "--grants", "{grants_path}"]]
    if (
        recipe not in variants
        or not launcher.is_absolute()
        or not native.is_absolute()
        or launcher.read_text(encoding="utf-8") != launcher_text()
        or capability["input_mode"] != "prompt-file"
        or capability["max_turns"] != (2 if review else 1)
        or (review and not readonly)
        or (native_sandbox and (not readonly or capability.get("runtime_write_paths")))
        or (scoped_tools and (readonly or review or capability.get("runtime_write_paths")))
    ):
        raise CampaignError("Codex app-server requires its exact single-turn scoped recipe")
    dependencies = {row["path"]: row["sha256"] for row in capability.get("dependencies", [])}
    from . import (
        campaign_codex_review,
        campaign_codex_review_evidence,
        campaign_readonly_qualification,
    )

    paths = [
        Path(__file__).resolve(),
        Path(campaign_codex_review.__file__).resolve(),
        Path(campaign_codex_review_evidence.__file__).resolve(),
        Path(campaign_readonly_qualification.__file__).resolve(),
        launcher,
        native,
        Path(sys.executable).resolve(),
    ]
    if scoped_tools:
        from . import campaign_execution_sandbox, campaign_scoped_tools

        paths.extend(
            [
                Path(campaign_execution_sandbox.__file__).resolve(),
                Path(campaign_scoped_tools.__file__).resolve(),
            ]
        )
    for path in paths:
        if dependencies.get(str(path)) != file_digest(path):
            raise CampaignError("Codex app-server dependency binding is missing or changed")


def run(
    native,
    *,
    workspace,
    model,
    effort,
    prompt,
    schema,
    review=False,
    read_only=False,
    native_sandbox=False,
    scoped_tools=False,
    grants_path=None,
):
    """Launch the frozen owner wrapper; its TeamRouter/hcom admission stays intact."""
    grants = {}
    tool_worker = None
    if scoped_tools:
        from .campaign_scoped_tools import ScopedTools

        if not grants_path or read_only or review or native_sandbox:
            raise CampaignError("Scoped Codex writing requires its distinct frozen route")
        grants = json.loads(grants_path.read_text(encoding="utf-8"))
        if (
            grants.get("schema_version") != 1
            or Path(grants.get("workspace", "")).resolve() != workspace.resolve()
            or not grants.get("config_digest")
            or not grants.get("scopes")
            or grants.get("runtime_write_paths")
            or grants.get("permissions") == "read-only-tools"
        ):
            raise CampaignError("Scoped Codex writer grants are invalid")
        tool_worker = ScopedTools(grants_path, grants_path.parent / "scoped-tools")
    if native_sandbox:
        if not grants_path or not (read_only or review):
            raise CampaignError("Native read-only route requires exact frozen grants")
        grants = json.loads(grants_path.read_text(encoding="utf-8"))
        if (
            grants.get("schema_version") != 1
            or grants.get("permissions") != "read-only-tools"
            or Path(grants.get("workspace", "")).resolve() != workspace.resolve()
            or not grants.get("config_digest")
            or any(
                grants.get(key)
                for key in ("scopes", "write_paths", "git_branch", "runtime_write_paths")
            )
        ):
            raise CampaignError("Native read-only grants cannot authorize writes")
    process = subprocess.Popen(
        [str(native), "app-server", "--listen", "stdio://"],
        cwd=workspace,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    completed = False
    terminal = {}
    review_items = {}
    thread = active_turn = ""
    review_child_turn = ""
    tool_requests = {}

    def send(message):
        assert process.stdin is not None
        process.stdin.write(json.dumps(message) + "\n")
        process.stdin.flush()

    def receive():
        nonlocal completed, terminal, active_turn, review_child_turn
        assert process.stdout is not None
        line = process.stdout.readline(MAX_FRAME_BYTES + 1)
        if not line or len(line.encode()) > MAX_FRAME_BYTES:
            raise CampaignError("Codex app-server stream ended or exceeded its frame bound")
        frame = json.loads(line)
        if not isinstance(frame, dict):
            raise CampaignError("Codex app-server emitted a non-object frame")
        print(line, end="", flush=True)
        if frame.get("method") == "turn/completed":
            completed = True
            terminal = frame.get("params", {}).get("turn", {})
        params = frame.get("params", {})
        if frame.get("id") in {3, 4} and isinstance(frame.get("result"), dict):
            active_turn = frame["result"].get("turn", {}).get("id", "")
        if review and frame.get("method") == "turn/started":
            started = params.get("turn", {})
            if started.get("id") != active_turn and started.get("rootTurnId") == active_turn:
                if review_child_turn and review_child_turn != started["id"]:
                    raise CampaignError("Codex review delegate identity changed")
                review_child_turn = started["id"]
        items = (
            [params.get("item", {})]
            if frame.get("method") == "item/completed"
            else terminal.get("items", []) if frame.get("method") == "turn/completed" else []
        )
        for item in items:
            if item.get("type") == "exitedReviewMode" and item.get("id"):
                review_items[item["id"]] = item.get("review")
        if "id" in frame and "method" in frame:
            if frame["method"] == "item/tool/call" and tool_worker is not None:
                if (
                    completed
                    or not active_turn
                    or params.get("threadId") != thread
                    or params.get("turnId") != active_turn
                    or params.get("namespace") is not None
                    or params.get("tool") != "bmad_shell"
                    or not isinstance(params.get("callId"), str)
                    or not params["callId"]
                    or not isinstance(params.get("arguments"), dict)
                ):
                    raise CampaignError(
                        "Codex scoped tool request has invalid identity or authority"
                    )
                identifier = json.dumps(frame["id"])
                prior = tool_requests.get(identifier)
                if prior is not None and prior != params:
                    raise CampaignError("Codex scoped RPC request identity was reused")
                tool_requests[identifier] = params
                response = tool_worker.execute(params["callId"], params["arguments"])
                send({"id": frame["id"], "result": response})
                return frame
            # Permission escalation, client tools and interactive requests are
            # never automatically granted by a bounded campaign transport.
            send(
                {
                    "id": frame["id"],
                    "error": {"code": -32601, "message": "Unsupported campaign client request"},
                }
            )
        return frame

    def rpc(identifier, method, params):
        send({"id": identifier, "method": method, "params": params})
        while True:
            frame = receive()
            if frame.get("id") == identifier and "method" not in frame:
                if "error" in frame:
                    raise CampaignError("Codex app-server rejected " + method)
                return frame["result"]

    try:
        rpc(
            1,
            "initialize",
            {
                "clientInfo": {"name": "bmad-loop", "version": "1"},
                "capabilities": {"experimentalApi": True},
            },
        )
        send({"method": "initialized"})
        thread_options = {}
        if scoped_tools:
            from .campaign_scoped_tools import DESCRIPTION, JSON_SCHEMA

            thread_options = {
                "dynamicTools": [
                    {
                        "type": "function",
                        "name": "bmad_shell",
                        "description": DESCRIPTION,
                        "inputSchema": JSON_SCHEMA,
                        "deferLoading": False,
                    }
                ],
                "developerInstructions": "The native tools are read-only. Use bmad_shell for every authorized write, Git commit and command requiring writes. It applies the frozen campaign ownership grant; do not request broader permissions.",
            }
        configured = rpc(
            2,
            "thread/start",
            {
                "model": model,
                "cwd": str(workspace),
                "approvalPolicy": "never",
                "sandbox": (
                    "read-only" if read_only or review or scoped_tools else "danger-full-access"
                ),
                "allowProviderModelFallback": False,
                "config": {"model_reasoning_effort": effort, "review_model": model},
                **thread_options,
            },
        )
        if configured.get("model") != model or configured.get("reasoningEffort") != effort:
            raise CampaignError("Codex app-server selected another model or effort")
        sandbox = configured.get("sandbox", {})
        expected = "readOnly" if read_only or review or scoped_tools else "dangerFullAccess"
        if sandbox.get("type") != expected or sandbox.get("networkAccess", False):
            raise CampaignError("Codex app-server permission receipt differs from its frozen route")
        thread = configured["thread"]["id"]
        if (native_sandbox or scoped_tools) and grants.get("native_probe"):
            from .campaign_readonly_qualification import NATIVE_POLICY, native_checks, probe_command

            native_checks(
                rpc(
                    5,
                    "command/exec",
                    {
                        "command": probe_command(grants["native_probe"]),
                        "cwd": str(workspace),
                        "sandboxPolicy": NATIVE_POLICY,
                        "timeoutMs": 20000,
                        "outputBytesCap": 65536,
                    },
                )
            )
        if review:
            rpc(
                3,
                "review/start",
                {
                    "threadId": thread,
                    "delivery": "inline",
                    "target": {"type": "custom", "instructions": prompt},
                },
            )
        else:
            rpc(
                3,
                "turn/start",
                {
                    "threadId": thread,
                    "input": [{"type": "text", "text": prompt}],
                    "outputSchema": schema,
                    "effort": effort,
                },
            )
        while not completed:
            receive()
        if review and terminal.get("status") == "completed":
            from .campaign_codex_review import validate_review

            if len(review_items) != 1:
                raise CampaignError("Native Codex review artifact is missing or ambiguous")
            if native_sandbox:
                from .campaign_codex_review_evidence import evidence, snapshot

                assert grants_path is not None
                binding = snapshot(
                    configured["thread"]["path"],
                    grants_path.parent / "codex-review-evidence",
                    thread=thread,
                    root_turn=active_turn,
                    child_turn=review_child_turn,
                    item_id=next(iter(review_items)),
                )
                observed = evidence(binding)
                if observed["model"] != model or observed["effort"] != effort:
                    raise CampaignError("Native review delegate selected another model or effort")
                print(
                    json.dumps({"method": "bmad/nativeReviewEvidence", "params": binding}),
                    flush=True,
                )
            else:
                validate_review(next(iter(review_items.values())))
            completed = False
            rpc(
                4,
                "turn/start",
                {
                    "threadId": thread,
                    "input": [
                        {
                            "type": "text",
                            "text": (
                                "Map the completed native review and the original task context into the requested output schema. "
                                "Retain every unresolved finding. Supply campaign coordinates and acceptance evidence only from "
                                "the original task and observed proof. Do not waive criteria or invent execution results."
                            ),
                        }
                    ],
                    "outputSchema": schema,
                    "effort": effort,
                },
            )
            while not completed:
                receive()
        assert process.stdin is not None
        process.stdin.close()
        try:
            return process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            return 0
    finally:
        kill_tree(process)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--native", type=Path, required=True)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--effort", required=True)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--schema", type=Path, required=True)
    parser.add_argument("--review", action="store_true")
    parser.add_argument("--read-only", action="store_true")
    parser.add_argument("--native-sandbox", action="store_true")
    parser.add_argument("--scoped-tools", action="store_true")
    parser.add_argument("--grants", type=Path)
    args = parser.parse_args(argv)
    return run(
        args.native,
        workspace=args.workspace,
        model=args.model,
        effort=args.effort,
        prompt=args.input.read_text(encoding="utf-8"),
        schema=json.loads(args.schema.read_text(encoding="utf-8")),
        review=args.review,
        read_only=args.read_only,
        native_sandbox=args.native_sandbox,
        scoped_tools=args.scoped_tools,
        grants_path=args.grants,
    )


if __name__ == "__main__":
    raise SystemExit(main())
