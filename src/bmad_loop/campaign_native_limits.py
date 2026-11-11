"""Explicit native request limits for separately qualified Claude recipes.

Claude's output setting bounds each provider request. Its schema retry setting
also bounds internal structured-output repair. Neither replaces the worker's
whole-job timeout or final aggregate token/turn checks. Legacy recipes retain
their original behavior and evidence; adding this contract requires a new proof.
"""

from pathlib import Path

from .campaign_model import CampaignError

CLAUDE_LIMITS = {
    "kind": "claude-code-v1",
    "output_tokens": "job-limit",
    "structured_output_retries": 1,
}
CLAUDE_STREAM_LIMITS = {
    **CLAUDE_LIMITS,
    "kind": "claude-code-v2",
    "streaming": "message-usage-v1",
    "message_limit": "job-turn-limit",
}


def streaming(capability):
    return capability.get("native_limits") == CLAUDE_STREAM_LIMITS


def validate(capability, *, verify_files=True):
    """Accept only the supported frozen recipe, with its implementation pinned."""
    if "native_limits" not in capability:
        return
    limits = capability["native_limits"]
    if (
        not isinstance(limits, dict)
        or limits not in (CLAUDE_LIMITS, CLAUDE_STREAM_LIMITS)
        or type(limits.get("structured_output_retries")) is not int
        or capability.get("protocol") != "claude-stream"
    ):
        raise CampaignError("Unsupported native request-limit contract")
    if streaming(capability):
        argv = capability.get("argv", [])
        if (
            capability.get("permissions") != "no-tools"
            or argv.count("--include-partial-messages") != 1
            or not any(flag in argv for flag in ("--print", "-p"))
            or not any(
                left == "--output-format" and right == "stream-json"
                for left, right in zip(argv, argv[1:])
            )
        ):
            raise CampaignError(
                "Native streaming limits require the no-tools partial-message recipe"
            )
    from .campaign_headless import file_digest

    dependencies = {
        row.get("path"): row.get("sha256")
        for row in capability.get("dependencies", [])
        if isinstance(row, dict)
    }
    names = ["campaign_native_limits.py", "campaign_headless_worker.py"]
    if streaming(capability):
        names.append("campaign_native_stream.py")
    for name in names:
        path = Path(__file__).resolve().with_name(name)
        if str(path) not in dependencies or (
            verify_files and dependencies[str(path)] != file_digest(path)
        ):
            raise CampaignError("Native request limits require exact helper/worker dependencies")


def environment(capability, output_tokens):
    """Return forced provider settings; caller applies them after inherited env.

    The native setting is named MAX_STRUCTURED_OUTPUT_RETRIES. Claude documents
    its value as an attempt cap (the default five includes the initial attempt).
    Passing one disallows a further schema-repair attempt. Record the native
    value rather than inventing a harness-independent retries/turns conversion.
    """
    validate(capability)
    if "native_limits" not in capability:
        return {}
    if type(output_tokens) is not int or not 1 <= output_tokens <= capability["output_tokens"]:
        raise CampaignError("Native request output limit exceeds the qualified bound")
    return {
        "CLAUDE_CODE_MAX_OUTPUT_TOKENS": str(output_tokens),
        "MAX_STRUCTURED_OUTPUT_RETRIES": str(
            capability["native_limits"]["structured_output_retries"]
        ),
    }
