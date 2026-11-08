"""Scoped operator takeover channel, separate from destructive stop requests.

Only the coordinator writes this external channel. A child checks it before
automatic input or another session; an already running agent may finish through
its ordinary Stop/death path. Unreadable or redirected channels fail held.
"""

from __future__ import annotations

import contextvars
import dataclasses
import json
from contextlib import contextmanager, nullcontext
from pathlib import Path
from typing import Callable

from .platform_util import atomic_write_text, file_lock, is_link_like, safe_segment


@dataclasses.dataclass(frozen=True)
class _Probe:
    held: Callable[[], bool]
    admission_lock: Path | None = None
    admission_held: Callable[[], bool] | None = None


_probe: contextvars.ContextVar[_Probe | None] = contextvars.ContextVar(
    "campaign_operator_hold", default=None
)

PROTECTED_HOLDS = frozenset(
    {
        "operator",
        "owner",
        "owner_stop",
        "human_only",
        "human_action",
        "product_choice",
        "planning_decision",
        "design_choice",
        "decision",
        "plan_checkpoint",
        "spec_checkpoint",
        "checkpoint",
        "github_review",
        "required_review",
        "intake_plan_change",
        "intake_plan_changed",
    }
)


def defer_failure(unit, hold) -> None:
    """Retain unrelated pending gates while coalescing a repeated technical fault."""
    raw = dataclasses.asdict(hold)
    pending = unit.evidence.get("pending_failure_hold")
    if pending is None or (pending["kind"] == hold.kind and hold.kind not in PROTECTED_HOLDS):
        unit.evidence["pending_failure_hold"] = raw
    elif pending != raw:
        queue = unit.evidence.setdefault("pending_failure_holds", [])
        if raw not in queue:
            queue.append(raw)


def promote_pending(unit) -> bool:
    """A satisfied gate exposes the next independently retained gate."""
    from .campaign_model import Hold

    if unit.hold is not None or unit.evidence.get("operator_prior_hold"):
        return False
    raw = unit.evidence.pop("pending_failure_hold", None)
    queue = unit.evidence.get("pending_failure_holds", [])
    if raw is None and queue:
        raw = queue.pop(0)
    if raw is None:
        return False
    unit.hold = Hold(**raw)
    return True


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


def install(probe: Callable[[], bool], *, admission_lock=None, admission_probe=None):
    return _probe.set(_Probe(probe, admission_lock, admission_probe))


def reset(token) -> None:
    _probe.reset(token)


def held() -> bool:
    probe = _probe.get()
    return probe is not None and probe.held()


@contextmanager
def admission():
    """Serialize final child session admission with committed operator controls."""
    from .adapters.multiplexer import MultiplexerError
    from .childrun import hard_stop_pending

    probe = _probe.get()
    lock = probe.admission_lock if probe is not None else None
    with file_lock(lock) if lock is not None else nullcontext():
        if held() or (probe and probe.admission_held and probe.admission_held()):
            raise MultiplexerError("campaign operator hold suppresses automatic session launch")
        if hard_stop_pending():
            raise MultiplexerError("campaign hard stop suppresses automatic session launch")
        yield
