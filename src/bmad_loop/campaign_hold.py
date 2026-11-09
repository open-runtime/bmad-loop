"""Scoped operator takeover channel, separate from destructive stop requests.

Only the coordinator writes this external channel. A child checks it before
automatic input or another session; an already running agent may finish through
its ordinary Stop/death path. Unreadable or redirected channels fail held.
"""

from __future__ import annotations

import contextvars
import dataclasses
import json
import os
import stat
from contextlib import contextmanager, nullcontext
from pathlib import Path
from typing import Callable

from . import platform_util as fs
from .platform_util import file_lock, safe_segment


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


def initialize(directory: Path, snapshot) -> None:
    """Mint channels once from trusted state under the campaign store-save lock.

    An initialized directory is never recreated. Legacy state may initialize it
    once, preserving every existing record and materializing persisted holds.
    """
    from .campaign_model import CampaignError

    if snapshot.bindings.get("hold_channels_version") == 1:
        return
    directory = directory.absolute()
    fs.make_dirs_confined(directory / "holds", confine_root=Path(directory.anchor))
    for unit in snapshot.units.values():
        if unit.hold is None or unit.hold.kind != "operator":
            continue
        path = channel(directory, unit.id)
        try:
            descriptor = fs.create_exclusive_confined(path, confine_root=Path(directory.anchor))
        except FileExistsError:
            if not active(path, campaign=snapshot.id, unit=unit.id):
                raise CampaignError("legacy hold channel contradicts its committed operator hold")
            continue
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", closefd=False) as stream:
                json.dump(
                    {"campaign": snapshot.id, "unit": unit.id, "hold": unit.hold.id or "operator"},
                    stream,
                )
        finally:
            os.close(descriptor)
    snapshot.bindings["hold_channels_version"] = 1


def write(path: Path, *, campaign: str, unit: str, hold: str) -> None:
    from .campaign_model import CampaignError

    path = path.absolute()
    root = Path(path.anchor)
    try:
        fs.atomic_write_text_confined(
            path, json.dumps({"campaign": campaign, "unit": unit, "hold": hold}), confine_root=root
        )
    except OSError as exc:
        raise CampaignError("campaign hold authority is missing or redirected") from exc


def active(path: Path, *, campaign: str, unit: str) -> bool:
    """Read beneath the authority handle; redirected ancestors are never release."""
    path = path.absolute()
    authority = path.parent.parent
    descriptor = parent = record = None
    try:
        if not fs.HANDLE_ANCHORED_WRITES or ".." in path.parts:
            return True
        # Campaign paths are canonical absolute authority paths. Starting at
        # the filesystem anchor checks even an ancestor above the campaign
        # directory; a fresh lstat pin on only that last component would follow
        # a redirected ancestor and accidentally pin the attacker's directory.
        descriptor = fs.open_dir_confined(Path(authority.anchor), authority)
        if descriptor is None:
            return True
        parent = fs.open_at(
            descriptor, path.parent.name, os.O_RDONLY | fs.AT_DIRECTORY | fs.AT_NOFOLLOW
        )
        try:
            record = fs.open_at(parent, path.name, os.O_RDONLY | fs.AT_NOFOLLOW | fs.AT_NONBLOCK)
        except FileNotFoundError:
            return False
        info = os.fstat(record)
        if not stat.S_ISREG(info.st_mode) or info.st_size > 16384:
            return True
        with os.fdopen(record, "rb", closefd=False) as stream:
            payload = stream.read(16385)
        if len(payload) > 16384:
            return True
        data = json.loads(payload.decode("utf-8"))
        # A wrong binding is uncertainty, never permission to nudge.
        return (
            bool(data.get("hold")) or data.get("campaign") != campaign or data.get("unit") != unit
        )
    except (OSError, ValueError, AttributeError):
        return True
    finally:
        for handle in (record, parent, descriptor):
            if handle is not None:
                os.close(handle)


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
