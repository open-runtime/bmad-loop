"""Native structured envelopes, kept separate from model-authored JSON content."""

from __future__ import annotations

import json
from typing import Any

from .campaign_headless import provider_family
from .campaign_model import CampaignError

_ERRORS = {
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


def parse_output(text: str, *, protocol: str, selected_model: str) -> dict[str, Any]:
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
    result = None
    models: list[str] = []
    session_id = ""
    usage: dict[str, int] = {}
    outcome, reason, truncated = "completed", "", False
    turns = 0
    for frame in frames:
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
        if kind in {"error", "turn.failed"} or (kind == "result" and frame.get("is_error") is True):
            error = frame.get("error") or {}
            code = error.get("code") or error.get("type") if isinstance(error, dict) else ""
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
    if outcome == "completed" and (not observed or observed != selected_model):
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
        "observed_model": observed,
        "observed_models": models,
        "provider_family": provider_family(observed),
        "turns": turns,
        "truncated": truncated,
    }
