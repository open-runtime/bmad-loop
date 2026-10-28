"""Deliver a nudge into a live session as an hcom message (open-runtime fork, F7).

A loop session launched through hcom (https://github.com/aannoo/hcom) runs as a
named hcom instance in a room (``HCOM_INSTANCE_NAME`` / ``HCOM_DIR`` in its
environment, which the bmad-loop hook relay reports on every event). hcom then
delivers a message into that same session: a Claude session through hcom's own
delivery, a Codex session through hcom's launch wrapper. That is a nudge the room
can see, answered in the same conversation, instead of keystrokes typed into a
pane.

The request is an ``hcom send @<name> --intent request`` with an external
``--from`` sender, never ``hcom term inject``. It is sent only when ``hcom list``
shows the instance ``listening``: an instance hcom believes is mid-turn (Codex
fires no hook after a failed turn, so hcom can keep one ``active`` for good) may
hold the message for a hook that never comes. Every miss returns an outcome the
caller falls back to tmux on; nothing here raises.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

# The identity an hcom child must not inherit: the message speaks only as its
# `--from` sender, never as the hcom instance the orchestrator itself may run in.
HCOM_STRIP_ENV = frozenset(
    {
        "CLAUDECODE",
        "HCOM_LAUNCHED",
        "HCOM_PROCESS_ID",
        "HCOM_TOOL",
        "HCOM_INSTANCE_NAME",
        "HCOM_NAME",
        "TEAMROUTER_HCOM_PROCESS_ID_CONSUMED",
    }
)
TIMEOUT_S = 10.0
SENDER = "bmad-loop"


@dataclass(frozen=True)
class HcomOutcome:
    """``sent`` True when hcom accepted the message; ``reason`` says why not
    (``no-binary``, ``status:<status>``, ``list-failed: ...``, ``send-failed: ...``)."""

    sent: bool
    reason: str


def hcom_env(room: str | None, base: Mapping[str, str] | None = None) -> dict[str, str]:
    """The environment an hcom child runs with: this process's, without the hcom
    identity, and pinned to the session's room."""
    env = {k: v for k, v in (base if base is not None else os.environ).items()}
    for key in HCOM_STRIP_ENV:
        env.pop(key, None)
    if room:
        env["HCOM_DIR"] = room
    return env


def _run(
    runner: Callable[..., Any], argv: list[str], env: dict[str, str]
) -> tuple[int, str, str]:
    proc = runner(
        argv,
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=TIMEOUT_S,
    )
    return proc.returncode, proc.stdout or "", proc.stderr or ""


def instance_status(
    name: str,
    room: str | None,
    *,
    binary: str,
    runner: Callable[..., Any] = subprocess.run,
) -> tuple[str | None, str]:
    """The instance's hcom status (``listening``, ``active``, ``blocked``, ...) and
    a note; None when hcom could not say."""
    try:
        rc, out, err = _run(runner, [binary, "list", name, "--json"], hcom_env(room))
    except (OSError, subprocess.SubprocessError, ValueError) as e:
        return None, f"list-failed: {type(e).__name__}: {e}"
    if rc != 0:
        return None, f"list-failed: rc {rc}: {err.strip()[:200]}"
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return None, "list-failed: not JSON"
    if isinstance(data, list):
        data = next((d for d in data if isinstance(d, dict) and d.get("name") == name), None)
    status = data.get("status") if isinstance(data, dict) else None
    return (status if isinstance(status, str) else None), "listed"


def send_nudge(
    name: str,
    room: str | None,
    text: str,
    *,
    thread: str,
    binary: str | None = None,
    runner: Callable[..., Any] = subprocess.run,
    sender: str = SENDER,
) -> HcomOutcome:
    """Send ``text`` to instance ``name`` as an hcom request on ``thread``, when
    hcom shows it listening. Never raises."""
    binary = binary or shutil.which("hcom")
    if not binary:
        return HcomOutcome(False, "no-binary")
    status, note = instance_status(name, room, binary=binary, runner=runner)
    if status is None:
        return HcomOutcome(False, note)
    if status != "listening":
        return HcomOutcome(False, f"status:{status}")
    argv = [
        binary,
        "send",
        f"@{name}",
        "--intent",
        "request",
        "--thread",
        thread,
        "--from",
        sender,
        "--quiet",
        "--",
        text,
    ]
    try:
        rc, _out, err = _run(runner, argv, hcom_env(room))
    except (OSError, subprocess.SubprocessError, ValueError) as e:
        return HcomOutcome(False, f"send-failed: {type(e).__name__}: {e}")
    if rc != 0:
        return HcomOutcome(False, f"send-failed: rc {rc}: {err.strip()[:200]}")
    return HcomOutcome(True, "sent")
