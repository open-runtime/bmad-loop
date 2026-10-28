"""How a coding-CLI session's last turn ended, read from its own transcript.

open-runtime fork (F3, F4, F5). The stall gate used to know only that a session
went quiet. This module tells it what the quiet means, from the transcript the
CLI itself writes — never from the pane, which carries the model's own output:

* the last assistant message, and whether it asks a human a question;
* the tool calls and sub-agents the session started and has not seen finish;
* whether the turn ended on an API error, and which class of error.

Two transcript formats, the two the factory runs:

* ``claude-jsonl`` (Claude Code): ``assistant`` entries carry ``message.content``
  items ``text`` and ``tool_use``; ``user`` entries carry ``tool_result`` items
  that close a call by ``tool_use_id``. A failed API call is an assistant entry
  with ``isApiErrorMessage: true`` and an ``error`` code (``server_error``,
  ``rate_limit``, ...), its text the rendered ``API Error: ...`` line.
* ``codex-rollout`` (Codex): ``response_item`` payloads ``message`` (role
  ``assistant``), ``function_call`` / ``custom_tool_call`` and their
  ``*_output``; ``event_msg`` payloads ``task_started`` and ``task_complete``,
  whose ``error`` object (``message``, ``codex_error_info``) marks a turn that
  ended in error. Codex fires no Stop hook on such a turn, so this read is the
  only place the engine can see it (16.1-dev-1, 2026-10-28).

Read-only and bounded: only the last ``tail_bytes`` of the file are parsed, a
torn or non-JSON line is skipped, and a missing or non-regular file reads as
``None``. Every judgement here is a heuristic that may only ever choose the
cautious branch: a false "question" parks a session (the upstream outcome); a
false "status report" costs one bounded nudge.
"""

from __future__ import annotations

import json
import os
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path

TAIL_BYTES = 1_048_576
MESSAGE_MAX = 4000

# Tool names that start a sub-agent the session then waits on.
CLAUDE_AGENT_TOOLS = frozenset({"Task", "Agent"})
CODEX_AGENT_TOOLS = frozenset({"spawn_agent", "wait_agent", "send_input"})
# The tool a session asks a human through; an open one is a question.
QUESTION_TOOLS = frozenset({"AskUserQuestion"})

# Phrases that, in the last lines of a message, ask a human to decide. Lower-case.
_ASK_PHRASES = (
    "should i ",
    "shall i ",
    "do you want",
    "would you like",
    "do you prefer",
    "would you prefer",
    "please confirm",
    "can you confirm",
    "could you confirm",
    "please advise",
    "please choose",
    "please decide",
    "let me know which",
    "let me know if you",
    "let me know how",
    "which option",
    "which would you",
    "what would you like",
    "how would you like",
    "need your input",
    "need your decision",
    "need your approval",
    "awaiting your",
    "waiting for your",
    "your call",
    "ok to proceed",
    "okay to proceed",
)
# Characters a markdown-rendered question may end in after its "?".
_TRAILING_DECORATION = "*_`'\")]} \t"

_POOL_RETURN_RE = re.compile(r"Earliest estimated usable return:\s*([0-9][0-9T:\-+.Z]*)")

# Error codes and message fragments, by class. Lower-case fragments.
_POOL_CODES = frozenset({"rate_limit", "rate_limit_error", "too_many_requests"})
_USAGE_CODES = frozenset({"billing_error", "usage_limit", "credits_exhausted"})
_ENV_CODES = frozenset(
    {"authentication_failed", "authentication_error", "permission_error", "invalid_api_key"}
)
_TRANSPORT_CODES = frozenset(
    {
        "server_error",
        "overloaded",
        "overloaded_error",
        "api_error",
        "timeout",
        "max_output_tokens",
        "unknown",
    }
)
_POOL_TEXT = ("pool unavailable", "request rejected (429)", "429 too many requests")
_USAGE_TEXT = (
    "out of usage credit",
    "out of extra usage",
    "usage limit reached",
    "credit balance is too low",
    "hit your session limit",
)
_ENV_TEXT = (
    "failed to load workspace requirements",
    "failed to load config",
    "not logged in",
    "invalid api key",
    "unauthorized",
    "authentication failed",
)
_TRANSPORT_TEXT = (
    "stream disconnected",
    "exceeded retry limit",
    "reconnecting",
    "connection lost",
    "connection closed",
    "connection reset",
    "connection refused",
    "unable to connect",
    "error sending request",
    "overloaded",
    "internal server error",
    "bad gateway",
    "service unavailable",
    "timed out",
)

ERROR_CLASSES = ("transport", "pool", "usage", "environment")


@dataclass(frozen=True)
class TurnState:
    """What the transcript says about the session's latest turn.

    ``last_message`` is the latest assistant text of the current turn (None when
    the turn has none yet, or it ended on an error with no message).
    ``pending_tools`` lists the tool calls opened and not answered, oldest first;
    ``pending_agent`` is True when one of them is a sub-agent, or a Codex
    ``wait_agent`` timed out with no final answer from the agent since.
    ``error_*`` describe an API error the turn ended on (``error_class`` one of
    :data:`ERROR_CLASSES`); ``pool_return`` is the ISO time a pool refusal names.
    ``turn_ended`` is True once the turn is over: a Codex ``task_complete``, or a
    Claude turn whose last entry is an assistant message with no open call."""

    last_message: str | None = None
    asks_question: bool = False
    pending_tools: tuple[str, ...] = ()
    pending_agent: bool = False
    error_code: str | None = None
    error_text: str | None = None
    error_class: str | None = None
    pool_return: str | None = None
    turn_ended: bool = False
    phase: str | None = None
    extra: dict[str, str] = field(default_factory=dict)

    @property
    def pending(self) -> str | None:
        """A short label of the newest pending work, for a nudge text."""
        if self.pending_tools:
            return self.pending_tools[-1]
        if self.pending_agent:
            return "a sub-agent"
        return None


def asks_question(text: str | None) -> bool:
    """Whether a message asks a human to answer or decide.

    A message whose last line ends in ``?`` (markdown decoration ignored), or
    whose last three lines carry one of the ask phrases. Deliberately generous:
    a false positive parks the session, which is what the upstream gate did for
    every idle Claude turn."""
    if not text:
        return False
    lines = [line.strip() for line in text.strip()[-1200:].splitlines() if line.strip()]
    if not lines:
        return False
    last = lines[-1].rstrip(_TRAILING_DECORATION)
    if last.endswith("?"):
        return True
    window = " ".join(lines[-3:]).lower()
    return any(phrase in window for phrase in _ASK_PHRASES)


def classify_error(code: str | None, text: str | None) -> str | None:
    """The class of an API error from its code and its text, or None for no error.

    ``pool``: a 429 / pool refusal (TeamRouter's "pool unavailable" sentence).
    ``usage``: the CLI's own credit or session limit. ``environment``: config or
    credentials the session cannot fix. ``transport``: a dropped or failed
    connection, and any other error — nudgeable, and bounded."""
    if not code and not text:
        return None
    c = (code or "").strip().lower()
    t = (text or "").lower()
    if c in _USAGE_CODES or any(f in t for f in _USAGE_TEXT):
        return "usage"
    if c in _POOL_CODES or any(f in t for f in _POOL_TEXT):
        return "pool"
    if c in _ENV_CODES or any(f in t for f in _ENV_TEXT):
        return "environment"
    return "transport"


def pool_return_of(text: str | None) -> str | None:
    """The ``Earliest estimated usable return: <ISO>`` time a pool refusal names."""
    if not text:
        return None
    match = _POOL_RETURN_RE.search(" ".join(text.split()))
    return match.group(1).rstrip(".") if match else None


def _clip(text: str) -> str:
    text = text.strip()
    return text if len(text) <= MESSAGE_MAX else text[-MESSAGE_MAX:]


def _tail_lines(path: Path, tail_bytes: int) -> list[bytes] | None:
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
    except OSError:
        return None
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode):
            return None
        start = max(0, info.st_size - tail_bytes)
        stream.seek(start)
        data = stream.read(tail_bytes)
    lines = data.split(b"\n")
    if start > 0 and lines:
        lines = lines[1:]  # a partial first line
    return lines


def _entries(lines: list[bytes]):
    for raw in lines:
        raw = raw.strip()
        if not raw:
            continue
        try:
            entry = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        if isinstance(entry, dict):
            yield entry


def _content_text(content: object, kinds: tuple[str, ...]) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = [
        item.get("text", "")
        for item in content
        if isinstance(item, dict)
        and item.get("type") in kinds
        and isinstance(item.get("text"), str)
    ]
    return "\n".join(p for p in parts if p)


def _read_claude(lines: list[bytes]) -> TurnState:
    open_calls: dict[str, str] = {}
    last_message: str | None = None
    error_code: str | None = None
    error_text: str | None = None
    last_kind: str | None = None
    for entry in _entries(lines):
        kind = entry.get("type")
        message = entry.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if kind == "user":
            if entry.get("isMeta"):
                continue
            closed_any = False
            if isinstance(content, list):
                for item in content:
                    if isinstance(item, dict) and item.get("type") == "tool_result":
                        call_id = item.get("tool_use_id")
                        if isinstance(call_id, str):
                            open_calls.pop(call_id, None)
                            closed_any = True
            if not closed_any and _content_text(content, ("text",)).strip():
                # A new prompt (a human's, or a nudge) starts a new turn.
                last_message = None
                error_code = error_text = None
            last_kind = "user"
        elif kind == "assistant":
            if entry.get("isApiErrorMessage"):
                code = entry.get("error")
                error_code = code if isinstance(code, str) and code else "unknown"
                error_text = _clip(_content_text(content, ("text",))) or error_code
                last_kind = "error"
                continue
            text = _content_text(content, ("text",))
            if text.strip():
                last_message = _clip(text)
                error_code = error_text = None
            if isinstance(content, list):
                for item in content:
                    if isinstance(item, dict) and item.get("type") == "tool_use":
                        call_id = item.get("id")
                        name = item.get("name")
                        if isinstance(call_id, str):
                            open_calls[call_id] = name if isinstance(name, str) else "tool"
            last_kind = "assistant"
    pending = tuple(open_calls.values())
    error_class = classify_error(error_code, error_text) if error_code else None
    return TurnState(
        last_message=last_message,
        asks_question=asks_question(last_message) or any(p in QUESTION_TOOLS for p in pending),
        pending_tools=pending,
        pending_agent=any(p in CLAUDE_AGENT_TOOLS for p in pending),
        error_code=error_code,
        error_text=error_text,
        error_class=error_class,
        pool_return=pool_return_of(error_text) if error_class == "pool" else None,
        turn_ended=last_kind in ("assistant", "error") and not pending,
    )


def _read_codex(lines: list[bytes]) -> TurnState:
    open_calls: dict[str, str] = {}
    last_message: str | None = None
    phase: str | None = None
    error_code: str | None = None
    error_text: str | None = None
    turn_ended = False
    wait_timed_out = False
    for entry in _entries(lines):
        payload = entry.get("payload")
        if not isinstance(payload, dict):
            continue
        ptype = payload.get("type")
        if entry.get("type") == "event_msg":
            if ptype == "task_started":
                last_message = None
                phase = None
                error_code = error_text = None
                turn_ended = False
            elif ptype == "task_complete":
                turn_ended = True
                final = payload.get("last_agent_message")
                if isinstance(final, str) and final.strip():
                    last_message = _clip(final)
                    phase = "final_answer"
                error = payload.get("error")
                if isinstance(error, dict):
                    msg = error.get("message")
                    info = error.get("codex_error_info")
                    error_text = msg if isinstance(msg, str) and msg else "turn error"
                    error_code = info if isinstance(info, str) and info else "error"
                elif isinstance(error, str) and error:
                    error_text, error_code = error, "error"
            continue
        if entry.get("type") != "response_item":
            continue
        if ptype == "message" and payload.get("role") == "assistant":
            text = _content_text(payload.get("content"), ("output_text", "text"))
            if text.strip():
                last_message = _clip(text)
                raw_phase = payload.get("phase")
                phase = raw_phase if isinstance(raw_phase, str) else None
        elif ptype in ("function_call", "custom_tool_call", "local_shell_call"):
            call_id = payload.get("call_id")
            name = payload.get("name")
            if isinstance(call_id, str):
                open_calls[call_id] = name if isinstance(name, str) and name else str(ptype)
        elif ptype in (
            "function_call_output",
            "custom_tool_call_output",
            "local_shell_call_output",
        ):
            call_id = payload.get("call_id")
            name = open_calls.pop(call_id, None) if isinstance(call_id, str) else None
            if name == "wait_agent":
                output = payload.get("output")
                wait_timed_out = isinstance(output, str) and '"timed_out":true' in output.replace(
                    " ", ""
                )
        elif ptype == "agent_message":
            text = _content_text(payload.get("content"), ("input_text", "output_text", "text"))
            if "Message Type: FINAL_ANSWER" in text:
                wait_timed_out = False
    pending = tuple(open_calls.values())
    error_class = classify_error(error_code, error_text) if (error_code or error_text) else None
    return TurnState(
        last_message=last_message,
        asks_question=asks_question(last_message),
        pending_tools=pending,
        pending_agent=wait_timed_out or any(p in CODEX_AGENT_TOOLS for p in pending),
        error_code=error_code,
        error_text=_clip(error_text) if error_text else None,
        error_class=error_class,
        pool_return=pool_return_of(error_text) if error_class == "pool" else None,
        turn_ended=turn_ended,
        phase=phase,
    )


READERS = {"claude-jsonl": _read_claude, "codex-rollout": _read_codex}


def supports(parser: str) -> bool:
    """Whether a profile's ``usage_parser`` names a transcript format read here."""
    return parser in READERS


def read_turn_state(
    path: str | Path | None, parser: str, *, tail_bytes: int = TAIL_BYTES
) -> TurnState | None:
    """The latest turn's state from the transcript at ``path``, or None when the
    format is not read here or the file is missing, unreadable or not regular."""
    reader = READERS.get(parser)
    if reader is None or not path:
        return None
    lines = _tail_lines(Path(path), tail_bytes)
    if lines is None:
        return None
    return reader(lines)
