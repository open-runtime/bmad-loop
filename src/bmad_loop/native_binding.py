"""Bind a native conversation to observed transcript identity and its launcher.

A logical adapter name does not identify the native CLI: a wrapper can fall
back from Codex to Claude.  Capture the requested profile separately from the
native transcript's structural identity, then require both before any resume.
Unknown evidence is a visible hold, never permission to start another harness
on the existing conversation.  These reads neither launch CLIs nor edit files.
"""

from __future__ import annotations

import codecs
import hashlib
import json
import os
import stat
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .adapters.profile import CLIProfile

TRANSCRIPT_READ_CAP = 1 << 20
_DIALECTS = {
    "claude-settings-json": "claude",
    "codex-hooks-json": "codex",
    "gemini-settings-json": "gemini",
    "copilot-settings-json": "copilot",
    "antigravity-hooks-json": "antigravity",
}


class NativeBindingError(ValueError):
    """A native resume lacks compatible, independently observed identity."""


def _launch_digest(profile: CLIProfile, binary: str, extra_args: tuple[str, ...] | None) -> str:
    data = asdict(profile)
    # Provenance and help text do not change the executable or conversation.
    data.pop("packaged", None)
    data.pop("first_run_note", None)
    data["binary"] = binary
    data["extra_args"] = extra_args
    return hashlib.sha256(
        json.dumps(data, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def transcript_identity(path: str | None) -> tuple[str, str]:
    """Read a bounded native header; filenames and model labels are not proof.

    Codex writes a session_meta header; Claude labels its own entries with
    sessionId; Gemini's chat object and Copilot's session.start carry theirs.
    A truncated/unknown/foreign file raises rather than silently guessing.
    """
    if not path:
        raise NativeBindingError("native transcript is missing")
    transcript = Path(path)
    try:
        before = transcript.lstat()
        if not stat.S_ISREG(before.st_mode):
            raise NativeBindingError("native transcript is not a regular file")
        with transcript.open("rb") as stream:
            opened = os.fstat(stream.fileno())
            if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
                raise NativeBindingError("native transcript changed while opening")
            raw = stream.read(TRANSCRIPT_READ_CAP + 1)
        after = transcript.lstat()
        if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
            raise NativeBindingError("native transcript changed while reading")
        # A bounded read may split a valid multibyte character after the
        # already-complete native header. Buffer only that trailing fragment;
        # invalid encoding within the prefix or at a real EOF still refuses.
        content = codecs.getincrementaldecoder("utf-8")().decode(
            raw[:TRANSCRIPT_READ_CAP], final=len(raw) <= TRANSCRIPT_READ_CAP
        )
    except (OSError, UnicodeError) as exc:
        raise NativeBindingError(f"native transcript cannot be read: {type(exc).__name__}") from exc

    # Gemini's transcript is one JSON object, usually pretty-printed. JSONL
    # cannot parse as one object, so examine complete bounded rows below.
    try:
        whole = json.loads(content)
    except (ValueError, RecursionError):
        whole = None
    if isinstance(whole, dict) and isinstance(whole.get("messages"), list):
        native_id = _text(whole.get("sessionId"))
        if native_id:
            return "gemini", native_id

    for line in content.splitlines()[:256]:
        try:
            row = json.loads(line)
        except (ValueError, RecursionError):
            continue
        if not isinstance(row, dict):
            continue
        kind = row.get("type")
        payload = row.get("payload")
        if kind == "session_meta" and isinstance(payload, dict):
            native_id = _text(payload.get("id"))
            if native_id:
                return "codex", native_id
        native_id = _text(row.get("sessionId"))
        if native_id and kind in {"user", "assistant", "system", "queue-operation", "progress"}:
            return "claude", native_id
        data = row.get("data")
        if kind == "session.start" and isinstance(data, dict):
            native_id = _text(data.get("sessionId"))
            if native_id:
                return "copilot", native_id
    raise NativeBindingError("native transcript has no recognized conversation identity")


def capture_binding(
    profile: CLIProfile,
    binary: str,
    session_id: str | None,
    transcript_path: str | None,
    *,
    extra_args: tuple[str, ...] | None = None,
) -> dict[str, Any]:
    """Persist launch facts even when the first hook precedes the transcript.

    A later resume may complete observation from the same transcript; it must
    still match this independently recorded profile digest and hook session id.
    """
    binding: dict[str, Any] = {
        "version": 1,
        "profile": profile.name,
        "launch_digest": _launch_digest(profile, binary, extra_args),
        "session_id": session_id,
    }
    try:
        harness, observed_id = transcript_identity(transcript_path)
    except NativeBindingError as exc:
        binding["error"] = str(exc)
    else:
        binding["harness"] = harness
        binding["observed_session_id"] = observed_id
        if session_id != observed_id:
            binding["error"] = "native transcript belongs to another conversation"
    return binding


def resume_refusal(
    binding: object,
    profile: CLIProfile,
    binary: str,
    session_id: str | None,
    transcript_path: str | None,
    *,
    extra_args: tuple[str, ...] | None = None,
) -> str | None:
    """Return an actionable refusal, or None for the same observed launcher."""
    if not profile.resumable:
        return "native resume profile no longer supports conversation continuation"
    if not isinstance(binding, dict) or binding.get("version") != 1:
        return "native resume has no recorded harness/profile binding"
    if not session_id or binding.get("session_id") != session_id:
        return "native resume session id differs from its recorded binding"
    if binding.get("profile") != profile.name or binding.get("launch_digest") != _launch_digest(
        profile, binary, extra_args
    ):
        return "native resume profile or launch configuration changed"
    expected = _DIALECTS.get(profile.hooks.dialect)
    if expected is None:
        return "native resume profile has no supported native harness identity"
    try:
        harness, observed_id = transcript_identity(transcript_path)
    except NativeBindingError as exc:
        return str(exc)
    if observed_id != session_id:
        return "native transcript belongs to another conversation"
    if harness != expected:
        return f"native conversation belongs to {harness}, but profile {profile.name} launches {expected}"
    if binding.get("harness") not in (None, harness) or binding.get("observed_session_id") not in (
        None,
        observed_id,
    ):
        return "native transcript identity differs from its recorded binding"
    return None
