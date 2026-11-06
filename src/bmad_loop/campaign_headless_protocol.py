"""Native structured envelopes, kept separate from model-authored JSON content."""

from __future__ import annotations

import json
import math
from typing import Any

from .campaign_headless import provider_family
from .campaign_model import CampaignError

_ERRORS = {
    "rate_limit": "quota",
    "rate_limit_error": "quota",
    "rate_limit_exceeded": "quota",
    "usage_limit_reached": "quota",
    "quota_exceeded": "quota",
    "insufficient_quota": "quota",
    "authentication_error": "authentication",
    "invalid_api_key": "authentication",
    "unauthorized": "authentication",
    "connection_error": "transport",
    "overloaded_error": "transport",
    "server_error": "transport",
    "context_length_exceeded": "limit",
    "max_tokens": "limit",
}


def _muse_envelopes(frames):
    """Decode MSP v1 native telemetry, never model-authored output deltas."""
    session = run = ""
    usage = terminal = None
    for frame in frames:
        if frame.get("schema_version") != 1 or frame.get("payload_schema_version") != 1:
            raise CampaignError("Unsupported Muse native event schema")
        kind = frame.get("payload_type")
        payload = frame.get("payload", {})
        stream = frame.get("stream", {})
        if kind == "session.run.linked":
            if (
                session
                or stream.get("kind") != "session"
                or payload.get("run_stream", {}).get("kind") != "run"
            ):
                raise CampaignError("Muse native output has ambiguous session/run binding")
            session = stream.get("id", "")
            run = payload["run_stream"].get("id", "")
        elif kind in {
            "run.usage",
            "run.terminal.completed",
            "run.terminal.failed",
            "run.terminal.cancelled",
        }:
            if not run or payload.get("run_stream", {}).get("id") != run:
                raise CampaignError("Muse native result belongs to another run")
            if kind == "run.usage":
                usage = payload
            else:
                terminal = payload
    if not session or not usage or not terminal:
        raise CampaignError("Muse native session/usage/terminal receipt is incomplete")
    models = usage.get("models", [])
    if (
        not isinstance(models, list)
        or not models
        or any(not isinstance(row, dict) or not isinstance(row.get("model"), str) for row in models)
    ):
        raise CampaignError("Muse native model usage is missing")
    calls = usage.get("calls")
    if type(calls) is not int or calls < 1:
        raise CampaignError("Muse native call count is missing")
    totals = {}
    for row in models:
        for key in ("input_tokens", "output_tokens", "reasoning_tokens"):
            value = row.get(key)
            if type(value) is int and value >= 0:
                totals[key] = totals.get(key, 0) + value
    failed = (
        terminal.get("terminal") != "completed"
        or usage.get("failed_calls", 0) > 0
        or usage.get("cancelled_calls", 0) > 0
    )
    return [{"type": "system", "model": row["model"], "session_id": session} for row in models] + [
        {
            "type": "result",
            "session_id": session,
            "model": models[0]["model"],
            "result": terminal.get("text", ""),
            "usage": totals,
            "num_turns": calls,
            "is_error": failed,
            "error": terminal.get("reason") if isinstance(terminal.get("reason"), dict) else {},
        }
    ]


def parse_output(
    text: str, *, protocol: str, selected_model: str, served_model_aliases: tuple[str, ...] = ()
) -> dict[str, Any]:
    """Decode native stream frames; never classify prose as provider failure."""
    try:
        whole = json.loads(text)
        frames = whole if isinstance(whole, list) else [whole]
    except ValueError:
        try:
            frames = [json.loads(line) for line in text.splitlines() if line.strip()]
        except ValueError as exc:
            raise CampaignError("Native headless output is not framed JSON") from exc
    if not frames or any(not isinstance(row, dict) for row in frames):
        raise CampaignError("Native headless output has no object frames")
    if protocol == "muse-json" and any("payload_type" in row for row in frames):
        frames = _muse_envelopes(frames)
    result = None
    models: list[str] = []
    session_id = ""
    usage: dict[str, int | float] = {}
    outcome, reason, truncated = "completed", "", False
    turns = 0
    for frame in frames:
        if protocol == "grok-json" and "sessionId" in frame:
            model_usage = frame.get("modelUsage") or {}
            if not isinstance(model_usage, dict):
                raise CampaignError("Grok native model usage has an invalid schema")
            observed = [
                model
                for model, usage_row in model_usage.items()
                if isinstance(model, str)
                and isinstance(usage_row, dict)
                and usage_row.get("modelCalls", 0) > 0
            ]
            # The native modelUsage keys are served identities, not model-authored text.
            frame = {
                **frame,
                "type": "result",
                "session_id": frame["sessionId"],
                "structured_output": frame.get("structuredOutput"),
                "model": observed[0] if observed else "",
                "stop_reason": frame.get("stopReason"),
            }
            for model in observed:
                if model not in models:
                    models.append(model)
            if type(frame.get("num_turns")) is not int or frame["num_turns"] < 1:
                raise CampaignError("Grok native turn count is missing")
            turns += frame["num_turns"] - 1
        if protocol == "muse-json" and frame.get("type") == "result":
            turns += max(0, frame.get("num_turns", 1) - 1)
        cost = frame.get("total_cost_usd")
        if (
            isinstance(cost, (int, float))
            and not isinstance(cost, bool)
            and math.isfinite(cost)
            and cost >= 0
        ):
            usage["cost_usd"] = cost
        kind = frame.get("type", "")
        if kind in {
            "system",
            "init",
            "session",
            "session.started",
            "thread.started",
            "turn.started",
            "result",
            "response",
            "turn.completed",
        }:
            model = frame.get("model") or (frame.get("metadata") or {}).get("model")
            if isinstance(model, str) and model and model not in models:
                models.append(model)
            identifier = (
                frame.get("session_id") or frame.get("thread_id") or frame.get("conversation_id")
            )
            if isinstance(identifier, str):
                session_id = identifier
        # Only native failure envelopes count. An assistant's result containing
        # an 'error' property is application content, never quota telemetry.
        if (
            protocol == "claude-stream"
            and kind == "assistant"
            and isinstance(frame.get("error"), str)
        ):
            code = frame["error"]
            outcome = _ERRORS.get(code, "failed")
            reason = f"native assistant failure: {code}"
        if kind in {"error", "turn.failed"} or (kind == "result" and frame.get("is_error") is True):
            error = frame.get("error") or {}
            code = error.get("code") or error.get("type") if isinstance(error, dict) else ""
            if code or outcome == "completed":
                outcome = _ERRORS.get(str(code), "failed")
                reason = f"native {kind}: {code or 'unclassified failure'}"
        if (
            frame.get("finish_reason") in {"length", "max_tokens"}
            or frame.get("stop_reason") == "max_tokens"
        ):
            outcome, reason, truncated = "limit", "Native output was truncated", True
        if kind in {"result", "turn.completed", "response"}:
            turns += 1
            raw_usage = frame.get("usage") or {}
            for name, target in (
                ("input_tokens", "input_tokens"),
                ("output_tokens", "output_tokens"),
                ("reasoning_tokens", "reasoning_tokens"),
                ("cache_read_input_tokens", "cache_read_tokens"),
                ("cache_read_tokens", "cache_read_tokens"),
                ("cache_creation_input_tokens", "cache_creation_tokens"),
            ):
                value = raw_usage.get(name)
                if type(value) is int and value >= 0:
                    usage[target] = usage.get(target, 0) + value
            if isinstance(frame.get("structured_output"), dict):
                result = frame["structured_output"]
            elif isinstance(frame.get("result"), (dict, list)):
                result = frame["result"]
            elif isinstance(frame.get("result"), str):
                try:
                    result = json.loads(frame["result"])
                except ValueError:
                    pass
        if protocol == "codex-jsonl" and kind == "item.completed":
            item = frame.get("item") or {}
            if item.get("type") == "agent_message" and isinstance(item.get("text"), str):
                try:
                    result = json.loads(item["text"])
                except ValueError:
                    pass
        if protocol in {"agy-json", "grok-json", "muse-json"} and not kind:
            # Single-response JSON envelopes are native only on these qualified
            # protocols; a bare result object does not manufacture attribution.
            if isinstance(frame.get("model"), str) and frame["model"] not in models:
                models.append(frame["model"])
            session_id = frame.get("session_id", frame.get("conversation_id", session_id))
            result = frame.get("structured_output", frame.get("result", result))
            if isinstance(result, str):
                try:
                    result = json.loads(result)
                except ValueError:
                    result = None
            native_usage = frame.get("usage") or {}
            for key in ("input_tokens", "output_tokens"):
                if type(native_usage.get(key)) is int and native_usage[key] >= 0:
                    usage[key] = native_usage[key]
            turns = max(turns, 1)
    observed = models[0] if models else ""
    if outcome == "completed" and (
        not observed or observed not in {selected_model, *served_model_aliases}
    ):
        raise CampaignError("Native primary model is missing or differs from selected route")
    if outcome == "completed" and (not session_id or result is None):
        raise CampaignError("Native session identity or structured result is missing")
    return {
        "outcome": outcome,
        "reason": reason,
        "result": result,
        "usage": usage,
        "usage_known": bool(usage),
        "session_id": session_id,
        "selected_model": selected_model,
        "observed_model": observed,
        "observed_models": models,
        "provider_family": provider_family(observed),
        "turns": turns,
        "truncated": truncated,
    }
