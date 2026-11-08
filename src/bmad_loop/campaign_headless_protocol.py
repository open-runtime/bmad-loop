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


def _agy_envelopes(frames, *, partial=False):
    """Normalize AGY's cumulative native receipt, not assistant-authored JSON.

    Stream init carries the native model configuration; the final result carries
    cumulative usage and turns. Step usage must not be added to those totals.
    A bare JSON result without native model attribution still fails admission.
    """
    session = model = ""
    terminal = None
    initialized = False
    for frame in frames:
        event = frame.get("event")
        if terminal is not None:
            raise CampaignError("AGY native stream continues after its terminal receipt")
        if event == "init":
            if initialized or not isinstance(frame.get("init"), dict):
                raise CampaignError("AGY native stream has ambiguous initialization")
            initialized = True
            session = frame.get("conversation_id", "")
            model = frame["init"].get("model", "")
            if not isinstance(session, str) or not session or not isinstance(model, str):
                raise CampaignError("AGY native initialization lacks session identity")
        elif event == "step_update":
            step = frame.get("step_update")
            if not isinstance(step, dict) or not session or step.get("conversation_id") != session:
                raise CampaignError("AGY native step belongs to another session")
        elif event == "result" or (event is None and "status" in frame):
            value = frame.get("result") if event else frame
            if not isinstance(value, dict):
                raise CampaignError("AGY native result is not an object")
            identifier = value.get("conversation_id", "")
            if not isinstance(identifier, str) or (session and identifier != session):
                raise CampaignError("AGY native output mixes session identities")
            session = identifier
            selected = value.get("model", "")
            if not isinstance(selected, str) or (selected and model and selected != model):
                raise CampaignError("AGY native output mixes model identities")
            model = selected or model
            terminal = value
        else:
            raise CampaignError("AGY native stream contains an unknown event")
    if terminal is None:
        if partial:
            return [{"type": "system", "session_id": session, "model": model}]
        raise CampaignError("AGY native terminal receipt is missing")
    status = terminal.get("status")
    if status not in {"SUCCESS", "ERROR", "CANCELED", "INTERRUPTED", "INVALID"}:
        raise CampaignError("AGY native receipt is not a recognized terminal state")
    turns = terminal.get("num_turns")
    if type(turns) is not int or turns < (1 if status == "SUCCESS" else 0):
        raise CampaignError("AGY native turn count is missing")
    usage = terminal.get("usage")
    if not isinstance(usage, dict):
        raise CampaignError("AGY native usage is missing")
    totals = {}
    for source, target in (
        ("input_tokens", "input_tokens"),
        ("output_tokens", "output_tokens"),
        ("thinking_tokens", "reasoning_tokens"),
        ("cache_read_tokens", "cache_read_tokens"),
    ):
        if source not in usage:
            continue
        value = usage[source]
        if type(value) is not int or value < 0:
            raise CampaignError("AGY native usage has an invalid counter")
        totals[target] = value
    error = terminal.get("error")
    # A native free-text error is retained but never guessed into a quota code.
    # Only a machine-classified error object may select automatic quota recovery.
    return [
        {"type": "system", "model": model, "session_id": session},
        {
            "type": "result",
            "session_id": session,
            "model": model,
            "structured_output": terminal.get("structured_output"),
            "result": terminal.get("response", ""),
            "usage": totals,
            "num_turns": turns,
            "is_error": status != "SUCCESS",
            "error": error if isinstance(error, dict) else {},
            "native_error": error if isinstance(error, str) else "",
            "native_canceled": status in {"CANCELED", "INTERRUPTED"},
        },
    ]


def _muse_envelopes(frames, *, partial=False):
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
    if partial and (not session or not usage):
        return [{"type": "system", "session_id": session}]
    if not partial and (not session or not usage or not terminal):
        raise CampaignError("Muse native session/usage/terminal receipt is incomplete")
    terminal = terminal or {}
    usage = usage or {}
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
    text: str,
    *,
    protocol: str,
    selected_model: str,
    served_model_aliases: tuple[str, ...] = (),
    partial: bool = False,
) -> dict[str, Any]:
    """Decode native stream frames; never classify prose as provider failure."""
    try:
        whole = json.loads(text)
        frames = whole if isinstance(whole, list) else [whole]
    except ValueError:
        frames = []
        for line in text.splitlines():
            if not line.strip():
                continue
            try:
                frames.append(json.loads(line))
            except ValueError as exc:
                if partial:
                    break  # An interrupted tail cannot erase preceding native telemetry.
                raise CampaignError("Native headless output is not framed JSON") from exc
    if (not frames and not partial) or any(not isinstance(row, dict) for row in frames):
        raise CampaignError("Native headless output has no object frames")
    native_confinement = None
    if protocol in {"codex-app-server", "codex-native-review"}:
        commands = [
            frame["result"] for frame in frames if frame.get("id") == 5 and "result" in frame
        ]
        if len(commands) > 1:
            raise CampaignError("Native containment command is ambiguous")
        native_confinement = commands[0] if commands else None
    if protocol == "muse-json" and any("payload_type" in row for row in frames):
        frames = _muse_envelopes(frames, partial=partial)
    if protocol == "agy-json":
        frames = _agy_envelopes(frames, partial=partial)
    if protocol == "codex-app-server":
        from .campaign_codex_appserver import envelopes

        frames = envelopes(frames, partial=partial)
    if protocol == "codex-native-review":
        from .campaign_codex_review import envelopes as review_envelopes

        frames = review_envelopes(frames, partial=partial)
    native_review = next(
        (frame["native_review"] for frame in frames if "native_review" in frame), None
    )
    native_review_evidence = next(
        (frame["native_review_evidence"] for frame in frames if "native_review_evidence" in frame),
        None,
    )
    result = None
    models: list[str] = []
    session_id = ""
    usage: dict[str, int | float] = {}
    outcome, reason, truncated = "completed", "", False
    failure_kind = ""
    native_terminal_completed = False
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
            if frame.get("structuredOutputError"):
                outcome, failure_kind = "failed", "schema"
                reason = (
                    "Native structured result rejected: "
                    + str(frame["structuredOutputError"])[:512]
                )
                native_terminal_completed = frame.get("stopReason") == "end_turn"
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
                if session_id and identifier and session_id != identifier:
                    raise CampaignError("Native output mixes different session identities")
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
        if protocol in {"agy-json", "codex-app-server", "codex-native-review"} and frame.get(
            "native_canceled"
        ):
            outcome, reason = "canceled", "Native run was canceled or interrupted"
        elif protocol == "agy-json" and frame.get("is_error") and frame.get("native_error"):
            reason = "AGY native failure: " + frame["native_error"][:512]
        if (
            frame.get("finish_reason") in {"length", "max_tokens"}
            or frame.get("stop_reason") == "max_tokens"
        ):
            outcome, reason, truncated = "limit", "Native output was truncated", True
        if kind in {"result", "turn.completed", "response"}:
            if protocol in {"agy-json", "codex-native-review"}:
                turns = frame.get("num_turns", 1)
            else:
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
    if (
        not partial
        and (outcome == "completed" or failure_kind == "schema")
        and (not observed or observed not in {selected_model, *served_model_aliases})
    ):
        raise CampaignError("Native primary model is missing or differs from selected route")
    if not partial and outcome == "completed" and not session_id:
        raise CampaignError("Native session identity or structured result is missing")
    if not partial and outcome == "completed" and result is None:
        outcome, failure_kind = "failed", "schema"
        reason = "Native structured result is missing or malformed"
    if not partial and failure_kind == "schema" and not session_id:
        raise CampaignError("Native schema rejection lacks session identity")
    return {
        **({"native_confinement": native_confinement} if native_confinement is not None else {}),
        **({"native_review": native_review} if native_review is not None else {}),
        **(
            {"native_review_evidence": native_review_evidence}
            if native_review_evidence is not None
            else {}
        ),
        "outcome": "failed" if partial else outcome,
        "reason": "Partial native telemetry; no completion evidence" if partial else reason,
        "result": None if partial else result,
        "usage": usage,
        "usage_known": bool(usage),
        "session_id": session_id,
        "selected_model": selected_model,
        "observed_model": observed,
        "observed_models": models,
        "provider_family": provider_family(observed),
        "turns": turns,
        "truncated": partial or truncated,
        "failure_kind": failure_kind,
        "native_terminal_completed": not partial and native_terminal_completed,
    }
