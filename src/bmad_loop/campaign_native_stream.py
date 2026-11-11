"""Bounded, incremental Claude v2 request accounting, independent of prose.

Claude's documented ``--include-partial-messages`` forwards Messages API
``message_start/message_delta/message_stop`` events. Delta usage is cumulative
within one model message, not additive across deltas. A StructuredOutput tool
result is not another model request; native ``num_turns`` is a different count.

The worker samples its owned stdout every 100ms. Typed ``max_tokens`` or observed
budget exhaustion causes reactive owned-process cancellation, not atomic API
admission: a native continuation may already be dispatched before cancellation.
Partial usage is a lower bound, never fabricated complete billing. Legacy
recipes do not use this contract. No thought/text content is interpreted.
"""

from __future__ import annotations

import json
from typing import BinaryIO

from .campaign_model import CampaignError

MAX_RECORD_BYTES = 1024 * 1024
MAX_FRAMES = 100000
MAX_MESSAGES = 1024
READ_BYTES = 65536


def _integer(value):
    if type(value) is not int or not 0 <= value <= 2**53 - 1:
        raise CampaignError("Native stream contains invalid token telemetry")
    return value


def _identity(value):
    if not isinstance(value, str) or not value or len(value) > 256:
        raise CampaignError("Native stream identity is missing or invalid")
    return value


def _usage(value):
    if not isinstance(value, dict) or "output_tokens" not in value:
        raise CampaignError("Native stream lacks cumulative output usage")
    for key in (
        "output_tokens",
        "input_tokens",
        "cache_read_input_tokens",
        "cache_creation_input_tokens",
    ):
        if key in value:
            _integer(value[key])
    return _integer(value["output_tokens"])


class ClaudeStream:
    """Retain bounded accounting metadata and at most one partial JSONL record."""

    def __init__(self, *, output_tokens: int, max_messages: int, max_bytes: int):
        self.output_limit = output_tokens
        self.message_limit = max_messages
        self.byte_limit = max_bytes
        self.buffer = bytearray()
        self.bytes_read = 0
        self.frames = 0
        self.session = ""
        self.messages: dict[str, dict] = {}
        self.current = ""
        self.output = 0
        self.terminal_output: int | None = None
        self.terminal_failed = False
        self.limit_kind = ""
        self.error = ""

    def read(self, source: BinaryIO, *, final=False):
        """Consume each byte once; a finite wire bound limits even the EOF drain."""
        try:
            while not self.limit_kind:
                chunk = source.read(READ_BYTES)
                if not chunk:
                    break
                self.feed(chunk)
                if not final:
                    break
            if final and not self.limit_kind:
                self.finish()
        except CampaignError as exc:
            self.error = str(exc)
            raise

    def feed(self, chunk: bytes):
        if self.limit_kind:
            return
        self.bytes_read += len(chunk)
        if self.bytes_read > self.byte_limit:
            self.limit_kind = "stdout-wire-bytes"
            return
        # Split the incoming bounded read without growing an unbounded backlog.
        for part in chunk.splitlines(keepends=True):
            self.buffer.extend(part)
            if len(self.buffer) > MAX_RECORD_BYTES:
                raise CampaignError("Native stream record exceeds its byte bound")
            if part.endswith(b"\n"):
                self._line(bytes(self.buffer))
                self.buffer.clear()
                if self.limit_kind:
                    return

    def _line(self, line):
        if not line.strip():
            return
        self.frames += 1
        if self.frames > MAX_FRAMES:
            raise CampaignError("Native stream exceeds its frame bound")
        try:
            frame = json.loads(line)
        except (ValueError, UnicodeError, RecursionError) as exc:
            raise CampaignError("Native stream contains invalid JSON") from exc
        if not isinstance(frame, dict):
            raise CampaignError("Native stream frame is not an object")
        kind = frame.get("type")
        if not isinstance(kind, str):
            raise CampaignError("Native stream frame type is invalid")
        if frame.get("parent_tool_use_id") is not None:
            raise CampaignError("No-tools native stream contains nested agent activity")
        if kind == "error":
            self.terminal_failed = True
            return
        if kind not in {"stream_event", "result"}:
            return
        if kind == "result" and frame.get("is_error") is True and not frame.get("session_id"):
            self._result(frame)
            return
        session = _identity(frame.get("session_id"))
        if self.session and self.session != session:
            raise CampaignError("Native stream mixes session identities")
        self.session = session
        if kind == "result":
            self._result(frame)
            return
        if "parent_tool_use_id" not in frame:
            raise CampaignError("Native stream lacks root identity")
        if self.terminal_output is not None or self.terminal_failed:
            raise CampaignError("Native stream continues after its terminal result")
        event = frame.get("event")
        if not isinstance(event, dict):
            raise CampaignError("Native stream event is not an object")
        event_type = event.get("type")
        if event_type == "message_start":
            message = event.get("message")
            if not isinstance(message, dict):
                raise CampaignError("Native stream message is not an object")
            identifier = _identity(message.get("id"))
            if self.current or identifier in self.messages:
                raise CampaignError("Native stream repeats or interleaves model messages")
            if len(self.messages) >= MAX_MESSAGES:
                raise CampaignError("Native stream exceeds its message tracking bound")
            value = _usage(message.get("usage"))
            self.messages[identifier] = {
                "output_tokens": value,
                "complete": False,
                "stop_reason": None,
            }
            self.current = identifier
            self.output += value
            if len(self.messages) > self.message_limit:
                self.limit_kind = "native-request-count"
        elif event_type == "message_delta":
            if not self.current:
                raise CampaignError("Native stream usage has no active model message")
            delta = event.get("delta")
            if not isinstance(delta, dict):
                raise CampaignError("Native stream message delta is invalid")
            value = _usage(event.get("usage"))
            row = self.messages[self.current]
            if value < row["output_tokens"]:
                raise CampaignError("Native stream cumulative output usage decreased")
            self.output += value - row["output_tokens"]
            row["output_tokens"] = value
            stop = delta.get("stop_reason")
            if stop is not None and not isinstance(stop, str):
                raise CampaignError("Native stream stop reason is invalid")
            if stop is not None:
                row["stop_reason"] = stop
            if stop == "max_tokens":
                self.limit_kind = "native-max-tokens"
        elif event_type == "message_stop":
            if not self.current or self.messages[self.current]["stop_reason"] is None:
                raise CampaignError("Native stream model message ended without a typed stop")
            self.messages[self.current]["complete"] = True
            self.current = ""
        # Other event payloads can contain model prose or estimated thinking
        # counters; neither supplies accounting authority.
        if not self.limit_kind and self.output > self.output_limit:
            self.limit_kind = "native-output-tokens"

    def _result(self, frame):
        if frame.get("is_error") is True:
            # Quota/auth/transport errors can precede the first model message
            # or interrupt one. Preserve their typed terminal cause for the
            # ordinary protocol parser; incomplete usage earns no grant.
            if frame.get("usage") is not None:
                _usage(frame["usage"])
            self.terminal_failed = True
            return
        if self.terminal_output is not None or self.current or not self.messages:
            raise CampaignError("Native stream result lacks complete model-message coverage")
        value = _usage(frame.get("usage"))
        if value != self.output:
            raise CampaignError("Native stream and terminal output usage disagree")
        iterations = frame["usage"].get("iterations")
        if "iterations" in frame["usage"]:
            if not isinstance(iterations, list) or len(iterations) != len(self.messages):
                raise CampaignError("Native stream and terminal iterations disagree")
            for iteration, row in zip(iterations, self.messages.values()):
                if (
                    not isinstance(iteration, dict)
                    or iteration.get("type") != "message"
                    or _usage(iteration) != row["output_tokens"]
                ):
                    raise CampaignError("Native stream and terminal iterations disagree")
        self.terminal_output = value
        if value > self.output_limit:
            self.limit_kind = "native-output-tokens"

    def finish(self):
        if self.buffer:
            self._line(bytes(self.buffer))
            self.buffer.clear()
        if (
            not self.limit_kind
            and not self.terminal_failed
            and (self.terminal_output is None or self.current)
        ):
            raise CampaignError("Native stream ended without complete accounting")

    def receipt(self):
        return {
            "schema_version": 1,
            "contract": "claude-code-v2",
            "bytes_read": self.bytes_read,
            "frames": self.frames,
            "messages": [{"id": identifier, **row} for identifier, row in self.messages.items()],
            "output_tokens_lower_bound": self.output,
            "terminal_usage_complete": self.terminal_output is not None
            and not self.limit_kind
            and not self.error
            and not self.terminal_failed,
            "terminal_output_tokens": self.terminal_output,
            "limit_kind": self.limit_kind,
            "error": self.error,
        }
