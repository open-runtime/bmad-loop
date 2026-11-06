"""Atomic campaign authority, repository coordinator fencing and durable commands.

SQLite atomically owns hot state, events and outbound intents. Legacy JSON state
remains readable and writable until explicit quiescent migration. The JSONL
projection is disposable. No transaction here spans network I/O.
The coordinator lock is held for its lifetime and lives in the canonical Git
common directory so alternate checkouts/state roots cannot create two owners.
"""

from __future__ import annotations

import copy
import dataclasses
import json
import os
import re
import time
import uuid
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .campaign_database import MARKER, CampaignDatabase, EventJournal
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

if TYPE_CHECKING:
    from .campaign_external_merge import ExternalMergeEvidence


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


def _state_document(snapshot: CampaignSnapshot) -> dict[str, Any]:
    """Detach JSON authority without recursively copying every scalar twice.

    JSON's encoder handles the nested ordinary containers in C. Dataclass nodes
    expose fields only; no arbitrary object's attributes enter persisted state.
    The lazy event journal is a separate append-only transaction component.
    """

    def fields(value):
        if not dataclasses.is_dataclass(value) or isinstance(value, type):
            raise TypeError(f"Unsupported campaign value: {type(value).__name__}")
        return {field.name: getattr(value, field.name) for field in dataclasses.fields(value)}

    raw = fields(snapshot)
    raw["events"] = []
    from .campaign_model import spec_to_dict

    raw["spec"] = spec_to_dict(snapshot.spec)
    return json.loads(
        json.dumps(raw, default=fields, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    )


class CampaignStore:
    """Persist snapshots beneath an external repository-associated root.

    Integrations call load, make a copy, then save with the observed revision.
    A stale callback cannot commit after a coordinator generation change. Callers
    must persist outbound intents in that same save before executing effects.
    """

    def __init__(
        self, root: Path | str, *, clock: Callable[[], float] = time.time, storage: str = "sqlite"
    ):
        self.root = Path(root).resolve()
        self.clock = clock
        if storage not in {"sqlite", "json"}:
            raise CampaignError("unknown campaign storage")
        self.storage = storage

    @staticmethod
    def _normalize(snapshot: CampaignSnapshot) -> CampaignSnapshot:
        result = snapshot_from_dict(_state_document(snapshot))
        result.events = copy.deepcopy(snapshot.events)
        return result

    def directory(self, campaign_id: str) -> Path:
        return self.root / _segment(campaign_id)

    def _write(self, path: Path, value: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(path, _json(value), follow_symlinks=False)

    def create(self, snapshot: CampaignSnapshot) -> CampaignSnapshot:
        snapshot = self._normalize(snapshot)
        from .campaign_acceptance import ensure

        for unit in snapshot.units.values():
            ensure(unit)
        directory = self.directory(snapshot.id)
        with file_lock(self.root / ".state.lock"):
            if directory.exists():
                raise CampaignError(f"campaign already exists: {snapshot.id}")
            self._validate(snapshot)
            candidate = snapshot  # _normalize already detached this entire document.
            candidate.created_at = candidate.created_at or self.clock()
            candidate.updated_at = candidate.created_at
            for event in candidate.events:
                event.setdefault("observed_at", candidate.created_at)
            if self.storage == "sqlite":
                directory.mkdir(parents=True, mode=0o700)
                database = CampaignDatabase(directory)
                database.initialize(_state_document(candidate), list(candidate.events))
                self._write(directory / "snapshot.json", MARKER)
                candidate.events = EventJournal(database, len(candidate.events))
            else:
                self._write(directory / "snapshot.json", snapshot_to_dict(candidate))
        self.project_events(candidate)
        return candidate

    def load(self, campaign_id: str) -> CampaignSnapshot:
        path = self.directory(campaign_id) / "snapshot.json"
        try:
            if path.is_symlink() or path.parent.is_symlink():
                raise CampaignError("campaign snapshot path is symlinked")
            value = json.loads(path.read_text(encoding="utf-8"))
            if value == MARKER:
                database = CampaignDatabase(path.parent)
                value, count = database.read()
                snapshot = snapshot_from_dict(value)
                snapshot.events = EventJournal(database, count)
            else:
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
        snapshot = self._normalize(snapshot)
        directory = self.directory(snapshot.id)
        with file_lock(self.root / ".state.lock"):
            previous = self.load(snapshot.id)
            from .campaign_acceptance import ensure

            for key, unit in snapshot.units.items():
                prior = previous.units.get(key)
                if prior is not None:
                    original = ensure(prior)
                    ledger = unit.evidence.setdefault("acceptance", copy.deepcopy(original))
                    retained = {row["id"] for row in ledger["criteria"]}
                    for criterion in original["criteria"]:
                        if criterion["id"] not in retained:
                            ledger["criteria"].append(copy.deepcopy(criterion))
                            ledger["revision"] += 1
                ensure(unit)
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
                if any(
                    job.get("claim") and not job["claim"]["quiescent"]
                    for job in previous.jobs.values()
                ):
                    raise CampaignError("configuration change requires quiescent reasoning jobs")
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
                    if (
                        unit.state == UnitState.PENDING
                        and replacement.state == UnitState.DELIVERED
                        and replacement.evidence.get("prior_delivery")
                    ):
                        from .campaign_existing import PriorDeliveryProof

                        audit = snapshot.clone()
                        audit.units[key].state = unit.state
                        reconcile_prior_delivery(
                            audit, key, PriorDeliveryProof(**replacement.evidence["prior_delivery"])
                        )
                    elif (
                        unit.state == UnitState.READY_TO_PUBLISH
                        and replacement.state == UnitState.MERGED_PENDING_SYNC
                        and replacement.evidence.get("external_merge")
                    ):
                        from .campaign_external_merge import ExternalMergeEvidence

                        raw = dict(replacement.evidence["external_merge"])
                        raw["parents"] = tuple(raw["parents"])
                        raw["checkpoint_heads"] = tuple(raw["checkpoint_heads"])
                        audit = snapshot.clone()
                        audit.units[key].state = unit.state
                        reconcile_external_merge(audit, key, ExternalMergeEvidence(**raw))
                    else:
                        check_campaign_transition(key, unit.state, replacement.state)
            from .campaign_jobs import live as job_live

            for key, job in previous.jobs.items():
                replacement = snapshot.jobs.get(key)
                if replacement is None:
                    if job_live(job) or job["state"] not in {"applied", "stale", "canceled"}:
                        raise CampaignError("active reasoning job cannot be removed")
                elif replacement["request"] != job["request"]:
                    raise CampaignError("reasoning request is immutable")
            if any(
                key not in snapshot.units or snapshot.units[key].spec != unit.spec
                for key, unit in previous.units.items()
            ) or set(snapshot.units) != set(previous.units):
                if snapshot.graph_revision <= previous.graph_revision:
                    raise CampaignError("graph change requires a new graph revision")
            database = (
                previous.events.database if isinstance(previous.events, EventJournal) else None
            )
            if database is not None and isinstance(snapshot.events, EventJournal):
                snapshot.events.validate_prefix(database, len(previous.events))
            elif snapshot.events[: len(previous.events)] != previous.events:
                raise CampaignError("campaign events are append-only")
            candidate = snapshot  # _normalize already detached this entire document.
            candidate.revision = expected_revision + 1
            candidate.updated_at = self.clock()
            for event in candidate.events[len(previous.events) :]:
                event.setdefault("observed_at", candidate.updated_at)
            self._validate(candidate)
            if database is not None:
                archived = database.commit(
                    _state_document(candidate),
                    revision=expected_revision,
                    event_count=len(previous.events),
                    events=list(candidate.events[len(previous.events) :]),
                )
                candidate.outbox = [row for row in candidate.outbox if row.id not in archived]
                candidate.events = EventJournal(database, len(candidate.events))
            else:
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
        from .campaign_jobs import validate_record

        for key, job in snapshot.jobs.items():
            validate_record(job)
            if key != job["id"]:
                raise CampaignError("reasoning job key differs from its identity")
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
        if kind not in {
            "stop",
            "hold",
            "release",
            "reconcile",
            "terminate-workers",
            "adopt",
            "handoff",
            "authorize-scope",
        }:
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
        if isinstance(snapshot.events, EventJournal):
            self._project_database_events(snapshot)
            return
        path = self.directory(snapshot.id) / "events.jsonl"
        text = "".join(
            _json({"sequence": index + 1, **event}) for index, event in enumerate(snapshot.events)
        )
        atomic_write_text(path, text, follow_symlinks=False)

    def _project_database_events(self, snapshot: CampaignSnapshot) -> None:
        directory = self.directory(snapshot.id)
        path, cursor_path = directory / "events.jsonl", directory / "events.cursor.json"
        with file_lock(directory / ".projection.lock"):
            if path.is_symlink() or cursor_path.is_symlink():
                raise CampaignError("campaign event projection is symlinked")
            cursor = {"sequence": 0, "bytes": 0}
            if cursor_path.exists() and path.exists():
                try:
                    observed = json.loads(cursor_path.read_text(encoding="utf-8"))
                    if (
                        0 <= observed["sequence"] <= len(snapshot.events)
                        and observed["bytes"] == path.stat().st_size
                    ):
                        cursor = observed
                except (ValueError, KeyError, TypeError):
                    pass  # A disposable projection is rebuilt from authority below.
            flags = os.O_WRONLY | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
            flags |= os.O_APPEND if cursor["sequence"] else os.O_TRUNC
            descriptor = os.open(path, flags, 0o600)
            with os.fdopen(descriptor, "ab") as stream:
                database = CampaignDatabase(directory)
                while cursor["sequence"] < len(snapshot.events):
                    rows = database.events(
                        after=cursor["sequence"], limit=1000, through=len(snapshot.events)
                    )
                    if not rows:
                        raise CampaignError("campaign event projection is missing authority")
                    for sequence, event in rows:
                        payload = _json({"sequence": sequence, **event}).encode()
                        stream.write(payload)
                        cursor = {"sequence": sequence, "bytes": cursor["bytes"] + len(payload)}
                stream.flush()
                os.fsync(stream.fileno())
            self._write(cursor_path, cursor)

    def history(
        self, campaign_id: str, *, after: int = 0, limit: int = 100, kind: str = "", unit: str = ""
    ) -> list[dict[str, Any]]:
        snapshot = self.load(campaign_id)
        if after < 0 or not 1 <= limit <= 10000:
            raise CampaignError("invalid campaign history pagination")
        if isinstance(snapshot.events, EventJournal):
            return [
                {"sequence": sequence, **event}
                for sequence, event in snapshot.events.database.events(
                    after=after, limit=limit, kind=kind, unit=unit, through=len(snapshot.events)
                )
            ]
        rows = [
            {"sequence": index + 1, **event}
            for index, event in enumerate(snapshot.events)
            if index >= after
            and (not kind or event.get("kind") == kind)
            and (not unit or event.get("unit") == unit)
        ]
        return rows[:limit]

    def jobs(
        self,
        campaign_id: str,
        *,
        after: str = "",
        limit: int = 100,
        unit: str = "",
        job_id: str = "",
    ) -> list[dict[str, Any]]:
        snapshot = self.load(campaign_id)
        if isinstance(snapshot.events, EventJournal):
            return snapshot.events.database.jobs(after=after, limit=limit, unit=unit, job_id=job_id)
        if snapshot.jobs:
            raise CampaignError("reasoning job history requires campaign migrate-state")
        return []

    def observations(
        self, campaign_id: str, *, after: int = 0, limit: int = 100, unit: str = ""
    ) -> list[dict[str, Any]]:
        snapshot = self.load(campaign_id)
        if after < 0 or not 1 <= limit <= 1000:
            raise CampaignError("invalid observation history pagination")
        if isinstance(snapshot.events, EventJournal):
            return snapshot.events.database.observations(after=after, limit=limit, unit=unit)
        return []

    def migrate_state(self, campaign_id: str) -> CampaignSnapshot:
        """Switch quiescent v1 authority atomically; old binaries reject the marker."""
        initial = self.load(campaign_id)
        if isinstance(initial.events, EventJournal):
            return initial
        common = Path(initial.common_dir)
        if not initial.common_dir or not common.is_dir():
            raise CampaignError("state migration requires the canonical Git common directory")
        with file_lock(common / "bmad-loop-campaign.lock", blocking=False):
            with file_lock(self.root / ".state.lock"):
                snapshot = self.load(campaign_id)
                if isinstance(snapshot.events, EventJournal):
                    return snapshot
                if any(unit.claim and not unit.claim.quiescent for unit in snapshot.units.values()):
                    raise CampaignError("state migration requires quiescent workers")
                if any(
                    job.get("claim") and not job["claim"]["quiescent"]
                    for job in snapshot.jobs.values()
                ):
                    raise CampaignError("state migration requires quiescent reasoning jobs")
                if any(action.state in {"prepared", "sent_unknown"} for action in snapshot.outbox):
                    raise CampaignError("state migration requires reconciled external effects")
                directory = self.directory(campaign_id)
                raw = (directory / "snapshot.json").read_bytes()
                backup = directory / "snapshot.v1.backup.json"
                if backup.exists():
                    if backup.is_symlink() or backup.read_bytes() != raw:
                        raise CampaignError("state migration backup differs from current authority")
                else:
                    descriptor = os.open(backup, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                    with os.fdopen(descriptor, "wb") as stream:
                        stream.write(raw)
                        stream.flush()
                        os.fsync(stream.fileno())
                database = CampaignDatabase(directory)
                expected = json.loads(_json(snapshot_to_dict(snapshot, include_events=False)))
                if not database.path.exists():
                    database.initialize(expected, list(snapshot.events))
                observed, count = database.read(include_terminal_jobs=True)
                if observed != expected or count != len(snapshot.events):
                    raise CampaignError("state migration database does not match v1 authority")
                existing = EventJournal(database, count)
                if existing != snapshot.events:
                    raise CampaignError("state migration event verification failed")
                self._write(directory / "snapshot.json", MARKER)
        migrated = self.load(campaign_id)
        self.project_events(migrated)
        return migrated


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


def reconcile_external_merge(
    snapshot: CampaignSnapshot, unit_id: str, proof: ExternalMergeEvidence
) -> None:
    """Record a canonical external landing without inventing native submission."""
    from .campaign_external_merge import ExternalMergeEvidence
    from .statemachine import check_campaign_external_merge

    unit = snapshot.units[unit_id]
    check_campaign_external_merge(unit_id, unit.state)
    integrated = unit.evidence.get("integrated", {})
    if (
        not isinstance(proof, ExternalMergeEvidence)
        or proof.schema_version != 1
        or proof.repository != snapshot.repository
        or proof.unit_id != unit.id
        or proof.head != integrated.get("head")
        or proof.base != integrated.get("base")
        or proof.target != snapshot.spec.target
        or not proof.merge_commit
        or not proof.canonical_head
        or not proof.covered_tree
        or not proof.quiescent
        or not proof.parents
        or not unit.claim
        or not unit.claim.quiescent
        or unit.hold
        or proof.source_revision != unit.bindings.get("source_revision", "")
        or proof.graph_revision != snapshot.graph_revision
        or proof.config_revision != snapshot.config_revision
        or not proof.verification_launch
        or not proof.review_launch
        or proof.verification_launch != unit.evidence.get("verification", {}).get("launch_id")
        or proof.review_launch != unit.evidence.get("review", {}).get("launch_id")
    ):
        raise CampaignError(
            "external merge proof is stale, uncovered or does not match accepted unit authority"
        )
    from dataclasses import asdict

    unit.state = UnitState.MERGED_PENDING_SYNC
    unit.evidence["external_merge"] = asdict(proof)
    add_event(
        snapshot,
        "external-merge-reconciled",
        unit=unit.id,
        head=proof.head,
        merge_commit=proof.merge_commit,
        canonical_head=proof.canonical_head,
    )


def reconcile_prior_delivery(snapshot: CampaignSnapshot, unit_id: str, proof) -> None:
    """Use a typed actual earlier delivery proof, never a source-status shortcut."""
    from dataclasses import asdict

    from .campaign_existing import PriorDeliveryProof, specification_digest
    from .statemachine import check_campaign_prior_delivery

    unit = snapshot.units[unit_id]
    check_campaign_prior_delivery(unit.id, unit.state)
    if (
        not isinstance(proof, PriorDeliveryProof)
        or proof.schema_version != 1
        or proof.repository != snapshot.repository
        or proof.unit_id != unit.id
        or proof.specification_digest != specification_digest(unit.spec)
        or proof.graph_revision != snapshot.graph_revision
        or proof.config_revision != snapshot.config_revision
        or not proof.source_verified
        or not proof.tracking_verified
        or not proof.source_sha256
        or not proof.prior_campaign
        or proof.prior_campaign == snapshot.id
        or proof.prior_revision < 0
        or proof.prior_pr < 1
        or not proof.head
        or proof.tracking_issue.get("number", 0) < 1
        or proof.pull_request.get("number") != proof.prior_pr
        or not proof.merge_commit
        or not proof.canonical_head
        or unit.claim
        or unit.hold
    ):
        raise CampaignError("prior delivery proof is stale, unbound or the unit already owns work")
    unit.state = UnitState.DELIVERED
    unit.evidence["prior_delivery"] = asdict(proof)
    unit.bindings["issue"] = proof.tracking_issue
    unit.bindings["pr"] = proof.pull_request
    add_event(
        snapshot,
        "prior-delivery-reconciled",
        unit=unit.id,
        prior_campaign=proof.prior_campaign,
        pr=proof.prior_pr,
        head=proof.head,
        canonical_head=proof.canonical_head,
    )
