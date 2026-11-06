"""Scoped operator takeover channel, separate from destructive stop requests.

Only the coordinator writes this external channel. A child checks it before
automatic input or another session; an already running agent may finish through
its ordinary Stop/death path. Unreadable or redirected channels fail held.
"""

from __future__ import annotations

import contextvars
import json
from pathlib import Path
from typing import Callable

from .platform_util import atomic_write_text, is_link_like, safe_segment

_probe: contextvars.ContextVar[Callable[[], bool] | None] = contextvars.ContextVar(
    "campaign_operator_hold", default=None
)


def channel(directory: Path, unit: str) -> Path:
    return directory / "holds" / (safe_segment(unit) + ".json")


def write(path: Path, *, campaign: str, unit: str, hold: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(
        path, json.dumps({"campaign": campaign, "unit": unit, "hold": hold}), follow_symlinks=False
    )


def active(path: Path, *, campaign: str, unit: str) -> bool:
    try:
        if is_link_like(path):
            return True
        if not path.exists():
            return False
        if not path.is_file() or path.stat().st_size > 16384:
            return True
        data = json.loads(path.read_text(encoding="utf-8"))
        # A wrong binding is uncertainty, never permission to nudge.
        return (
            bool(data.get("hold")) or data.get("campaign") != campaign or data.get("unit") != unit
        )
    except (OSError, ValueError, AttributeError):
        return True


def install(probe: Callable[[], bool]):
    return _probe.set(probe)


def reset(token) -> None:
    _probe.reset(token)


def held() -> bool:
    probe = _probe.get()
    return probe is not None and probe()
