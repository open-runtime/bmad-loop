"""Atomic campaign authority, repository coordinator fencing and durable commands.

The snapshot owns events and outbound intents. The JSONL projection is disposable:
rebuild it from snapshot events after a crash. No lock here spans network I/O.
The coordinator lock is held for its lifetime and lives in the canonical Git
common directory so alternate checkouts/state roots cannot create two owners.
"""

from __future__ import annotations

import json
import os
import re
import time
import uuid
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .campaign_model import (
    CampaignError,
    CampaignSnapshot,
    OutboxAction,
    UnitState,
    snapshot_from_dict,
    snapshot_to_dict,
)
from .platform_util import atomic_write_text, file_lock
from .statemachine import check_campaign_transition


class RevisionConflict(CampaignError):
    """The state changed since the caller's observation."""


class CoordinatorMismatch(CampaignError):
    """The caller's generation no longer owns the campaign."""


def _segment(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", value):
        raise CampaignError("campaign/action ID must be a safe path segment")
    return value


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")) + "\n"


class CampaignStore:
    """Persist snapshots beneath an external repository-associated root.

    Integrations call load, make a copy, then save with the observed revision.
    A stale callback cannot commit after a coordinator generation change. Callers
    must persist outbound intents in that same save before executing effects.
    """

    def __init__(self, root: Path | str, *, clock: Callable[[], float] = time.time):
        self.root = Path(root).resolve()
        self.clock = clock

    def directory(self, campaign_id: str) -> Path:
        return self.root / _segment(campaign_id)

    def _write(self, path: Path, value: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(path, _json(value), follow_symlinks=False)

    def create(self, snapshot: CampaignSnapshot) -> CampaignSnapshot:
        snapshot = snapshot_from_dict(json.loads(_json(snapshot_to_dict(snapshot))))
        directory = self.directory(snapshot.id)
        with file_lock(self.root / ".state.lock"):
            if directory.exists():
                raise CampaignError(f"campaign already exists: {snapshot.id}")
            self._validate(snapshot)
            candidate = snapshot.clone()
            candidate.created_at = candidate.created_at or self.clock()
            candidate.updated_at = candidate.created_at
            self._write(directory / "snapshot.json", snapshot_to_dict(candidate))
        self.project_events(candidate)
        return candidate

    def load(self, campaign_id: str) -> CampaignSnapshot:
        path = self.directory(campaign_id) / "snapshot.json"
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            snapshot = snapshot_from_dict(value)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise CampaignError(f"cannot read campaign {campaign_id}: {exc}") from exc
        if snapshot.id != campaign_id:
            raise CampaignError("campaign snapshot ID differs from its directory")
        self._validate(snapshot)
        return snapshot

    def list(self) -> list[CampaignSnapshot]:
        if not self.root.exists():
            return []
        return [
            self.load(path.name)
            for path in sorted(self.root.iterdir())
            if path.is_dir() and (path / "snapshot.json").exists()
        ]

    def save(
        self,
        snapshot: CampaignSnapshot,
        expected_revision: int,
        *,
        expected_generation: int | None = None,
    ) -> CampaignSnapshot:
        """CAS commit; authority and outbound intents replace as one document."""
        snapshot = snapshot_from_dict(json.loads(_json(snapshot_to_dict(snapshot))))
        directory = self.directory(snapshot.id)
        with file_lock(self.root / ".state.lock"):
            previous = self.load(snapshot.id)
            if previous.revision != expected_revision:
                raise RevisionConflict(
                    f"campaign revision {previous.revision}, expected {expected_revision}"
                )
            if expected_generation is not None and previous.generation != expected_generation:
                raise CoordinatorMismatch("campaign coordinator generation changed")
            if snapshot.generation < previous.generation:
                raise CoordinatorMismatch("stale campaign coordinator generation")
            if (snapshot.repository, snapshot.common_dir, snapshot.remote_url, snapshot.root) != (
                previous.repository,
                previous.common_dir,
                previous.remote_url,
                previous.root,
            ):
                raise CampaignError("campaign repository identity is immutable")
            if (
                snapshot.spec != previous.spec
                or snapshot.config_snapshot != previous.config_snapshot
            ):
                if snapshot.config_revision <= previous.config_revision:
                    raise CampaignError("configuration change requires a new explicit revision")
                if any(u.claim and not u.claim.quiescent for u in previous.units.values()):
                    raise CampaignError("configuration change requires quiescent workers")
            for key, unit in previous.units.items():
                replacement = snapshot.units.get(key)
                if unit.state == UnitState.DELIVERED and (
                    replacement is None
                    or replacement.spec != unit.spec
                    or replacement.state != unit.state
                ):
                    raise CampaignError(f"landed unit cannot be removed or redefined: {key}")
                if unit.claim and not unit.claim.quiescent and replacement is None:
                    raise CampaignError(f"uncertain/live claim cannot be removed: {key}")
                if replacement and replacement.state != unit.state:
                    check_campaign_transition(key, unit.state, replacement.state)
            if any(
                key not in snapshot.units or snapshot.units[key].spec != unit.spec
                for key, unit in previous.units.items()
            ) or set(snapshot.units) != set(previous.units):
                if snapshot.graph_revision <= previous.graph_revision:
                    raise CampaignError("graph change requires a new graph revision")
            if snapshot.events[: len(previous.events)] != previous.events:
                raise CampaignError("campaign events are append-only")
            candidate = snapshot.clone()
            candidate.revision = expected_revision + 1
            candidate.updated_at = self.clock()
            self._validate(candidate)
            self._write(directory / "snapshot.json", snapshot_to_dict(candidate))
        self.project_events(candidate)
        return candidate

    def update(
        self,
        campaign_id: str,
        change: Callable[[CampaignSnapshot], None],
        *,
        expected_generation: int | None = None,
    ) -> CampaignSnapshot:
        snapshot = self.load(campaign_id)
        revision = snapshot.revision
        change(snapshot)
        return self.save(snapshot, revision, expected_generation=expected_generation)

    def _validate(self, snapshot: CampaignSnapshot) -> None:
        if snapshot.phase not in {"active", "draining", "stopped", "delivered", "held"}:
            raise CampaignError("invalid campaign phase")
        if (
            snapshot.revision < 0
            or snapshot.generation < 0
            or snapshot.graph_revision < 1
            or snapshot.config_revision < 1
        ):
            raise CampaignError("invalid campaign revisions")
        for key, unit in snapshot.units.items():
            if key != unit.id:
                raise CampaignError("unit key must equal qualified ID")
            if unit.claim and unit.claim.unit != key:
                raise CampaignError("worker claim unit mismatch")
            if unit.state == UnitState.DELIVERED and unit.claim and not unit.claim.quiescent:
                raise CampaignError("delivery cannot retain an uncertain/live writer")
        action_ids = [action.id for action in snapshot.outbox]
        if len(set(action_ids)) != len(action_ids):
            raise CampaignError("duplicate outbox action IDs")
        if any(
            action.state not in {"prepared", "sent_unknown", "confirmed", "rejected"}
            for action in snapshot.outbox
        ):
            raise CampaignError("invalid outbox state")

    @contextmanager
    def coordinator(
        self, repository_common_dir: Path | str, target: str, *, campaign_id: str = ""
    ) -> Iterator[None]:
        common = Path(repository_common_dir).resolve(strict=True)
        if not common.is_dir():
            raise CampaignError("Git common directory is not a directory")
        lock = common / "bmad-loop-campaign.lock"
        with file_lock(lock, blocking=False):
            self._write(
                common / "bmad-loop-campaign-owner.json",
                {
                    "pid": os.getpid(),
                    "target": target,
                    "campaign_id": campaign_id,
                    "state_root": str(self.root),
                    "started_at": self.clock(),
                },
            )
            yield

    def enqueue(
        self,
        campaign_id: str,
        kind: str,
        *,
        unit: str = "",
        reason: str = "",
        request_id: str | None = None,
        **payload: Any,
    ) -> dict[str, Any]:
        """Append an idempotent command without touching coordinator state."""
        if kind not in {"stop", "hold", "release", "reconcile", "terminate-workers"}:
            raise CampaignError(f"unknown campaign control {kind}")
        self.load(campaign_id)
        request_id = _segment(request_id or uuid.uuid4().hex)
        directory = self.directory(campaign_id)
        request = {
            "id": request_id,
            "kind": kind,
            "unit": unit,
            "reason": reason,
            "payload": payload,
        }
        path = directory / "inbox" / f"{request_id}.json"
        with file_lock(directory / ".inbox.lock"):
            if path.exists():
                existing = json.loads(path.read_text(encoding="utf-8"))
                if {k: v for k, v in existing.items() if k != "created_at"} != request:
                    raise CampaignError("control request ID reused with different input")
                return existing
            request["created_at"] = self.clock()
            self._write(path, request)
        return request

    def commands(
        self, campaign_id: str, after: list[str] | tuple[str, ...] = ()
    ) -> list[dict[str, Any]]:
        directory = self.directory(campaign_id) / "inbox"
        if not directory.exists():
            return []
        processed = set(after)
        commands = []
        for path in directory.glob("*.json"):
            try:
                command = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise CampaignError(f"invalid campaign command {path.name}: {exc}") from exc
            if command.get("id") not in processed:
                commands.append(command)
        return sorted(commands, key=lambda command: (command["created_at"], command["id"]))

    def project_events(self, snapshot: CampaignSnapshot) -> None:
        """Recoverable observer projection: never the state writer's authority."""
        path = self.directory(snapshot.id) / "events.jsonl"
        text = "".join(
            _json({"sequence": index + 1, **event}) for index, event in enumerate(snapshot.events)
        )
        atomic_write_text(path, text, follow_symlinks=False)


def add_event(snapshot: CampaignSnapshot, kind: str, *, unit: str = "", **details: Any) -> None:
    snapshot.events.append(
        {
            "kind": kind,
            "unit": unit,
            "revision": snapshot.revision + 1,
            "generation": snapshot.generation,
            **details,
        }
    )


def add_action(
    snapshot: CampaignSnapshot,
    kind: str,
    unit: str,
    payload: Mapping[str, Any],
    *,
    action_id: str | None = None,
) -> OutboxAction:
    """Prepare alongside a state change; caller saves before side effects."""
    action = OutboxAction(
        action_id or uuid.uuid4().hex, kind, unit, dict(payload), snapshot.generation
    )
    if any(existing.id == action.id for existing in snapshot.outbox):
        raise CampaignError("outbox action ID already exists")
    snapshot.outbox.append(action)
    return action


def transition(snapshot: CampaignSnapshot, unit: str, target: UnitState, **details: Any) -> None:
    current = snapshot.units[unit]
    check_campaign_transition(unit, current.state, target)
    source = current.state
    current.state = target
    add_event(snapshot, "transition", unit=unit, source=str(source), target=str(target), **details)
