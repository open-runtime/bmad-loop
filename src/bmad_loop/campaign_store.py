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
import hashlib
import json
import os
import re
import time
import uuid
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .campaign_database import (
    LEGACY_MARKER,
    MARKER,
    TRANSFER_MARKER,
    CampaignDatabase,
    EventJournal,
)
from .campaign_model import (
    CampaignError,
    CampaignSnapshot,
    Hold,
    OutboxAction,
    UnitState,
    snapshot_from_dict,
    snapshot_to_dict,
)
from .platform_util import atomic_write_text, file_lock
from .statemachine import check_campaign_transition

if TYPE_CHECKING:
    from .campaign_external_merge import ExternalLandingEvidence, ExternalMergeEvidence


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
        if snapshot.bindings.get("ownership_transfers") or snapshot.bindings.get(
            "ownership_retirement"
        ):
            raise CampaignError("ownership retirement requires explicit transfer release")
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
            directory.mkdir(parents=True, mode=0o700)
            from .campaign_hold import initialize

            initialize(directory, candidate)
            if self.storage == "sqlite":
                database = CampaignDatabase(directory)
                database.initialize(_state_document(candidate), list(candidate.events))
                self._write(directory / "snapshot.json", MARKER)
                candidate.events = EventJournal(database, len(candidate.events))
                candidate.jobs = {
                    key: row
                    for key, row in candidate.jobs.items()
                    if row["state"] not in {"applied", "stale", "canceled"}
                }
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
            if value in (MARKER, LEGACY_MARKER, TRANSFER_MARKER):
                database = CampaignDatabase(path.parent)
                value, count = database.read(minimum_schema=value["schema_version"])
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
        return self._save(snapshot, expected_revision, expected_generation=expected_generation)

    def _save_transfer(self, snapshot, expected_revision, *, record, successor_revision):
        """Internal release transaction; ordinary save cannot mint this authority."""
        return self._save(
            snapshot,
            expected_revision,
            transfer_record=record,
            successor_revision=successor_revision,
        )

    def _save(
        self,
        snapshot,
        expected_revision,
        *,
        expected_generation=None,
        transfer_record=None,
        successor_revision=None,
    ):
        snapshot = self._normalize(snapshot)
        directory = self.directory(snapshot.id)
        with file_lock(self.root / ".state.lock"):
            previous = self.load(snapshot.id)
            from .campaign_transfer import validate_transfer_preservation

            validate_transfer_preservation(previous, snapshot, release_record=transfer_record)
            if transfer_record is not None:
                if not isinstance(previous.events, EventJournal):
                    raise CampaignError("startup transfer requires explicit SQLite state migration")
                successor = self.load(transfer_record["successor"])
                if successor.revision != successor_revision:
                    raise RevisionConflict("startup transfer successor revision changed")
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
                _audit_rearm_cancellation(previous, snapshot, key)
                prior_permits = unit.evidence.get("continuation_permits", {})
                next_permits = (
                    replacement.evidence.get("continuation_permits", {}) if replacement else {}
                )
                if any(
                    next_permits.get(ident) != record for ident, record in prior_permits.items()
                ):
                    raise CampaignError("consumed continuation authority is immutable")
                if len(next_permits.keys() - prior_permits.keys()) > 1:
                    raise CampaignError("one successor may consume only one continuation permit")
                for ident in next_permits.keys() - prior_permits.keys():
                    if replacement is None:
                        raise CampaignError("continuation requires its retained unit")
                    record = next_permits[ident]
                    expected = _continuation_record(snapshot, replacement, record.get("permit"))
                    from .campaign_usage import remaining as remaining_allowance

                    prior_dev, prior_review = remaining_allowance(previous, unit)
                    technical = expected["permit"]["schema_version"] == 3
                    evidence_key = "technical_rearm" if technical else "handoff"
                    identity_key = "rearm_id" if technical else "handoff_id"
                    prior_handoff = unit.evidence.get(evidence_key, {})
                    if technical:
                        from .campaign_rearm import validate_capture, validate_current

                        validate_current(previous, unit, prior_handoff)
                        validate_capture(previous, unit, prior_handoff, directory=directory)
                    expected_hold = (
                        Hold(
                            "technical_rearm",
                            "Exact retained development continuation is in progress",
                            id=prior_handoff.get("id", ""),
                        )
                        if technical
                        else unit.hold
                    )
                    if (
                        record != expected
                        or ident != expected["permit"]["permit_id"]
                        or prior_handoff.get("phase") != "ready"
                        or prior_handoff.get("id") != expected["permit"]["binding"][identity_key]
                        or not unit.claim
                        or not unit.claim.quiescent
                        or unit.claim.launch_id
                        != expected["permit"]["binding"]["predecessor"]["launch_id"]
                        or (
                            not technical and unit.hold
                            and (
                                unit.hold.kind != "handoff"
                                or unit.hold.id != prior_handoff.get("id")
                            )
                        )
                        or replacement.hold != expected_hold
                        or replacement.attempt != unit.attempt + 1
                        or any(
                            getattr(replacement, field) != getattr(unit, field)
                            for field in (
                                "failure_attempts",
                                "resolution_attempts",
                                "review_attempts",
                            )
                        )
                        or any(
                            replacement.evidence.get(field) != unit.evidence.get(field)
                            for field in (
                                "developer_family",
                                "developer_families",
                                "developer_attribution_incomplete",
                                "acceptance",
                                "usage",
                                "decisions",
                                "worker_history",
                                "unresolved_approval_requirements",
                            )
                        )
                        or snapshot.config_revision != previous.config_revision
                        or snapshot.bindings.get("execution_template")
                        != previous.bindings.get("execution_template")
                        or replacement.spec != unit.spec
                        or any(
                            replacement.bindings.get(field) != unit.bindings.get(field)
                            for field in (
                                "workspace",
                                "branch",
                                "pr",
                                "effective_source",
                                "effective_sources",
                                "private_paths",
                                "source_revision",
                            )
                        )
                        or any(
                            replacement.evidence.get(evidence_key, {}).get(field)
                            != prior_handoff.get(field)
                            for field in (
                                "id",
                                "role",
                                "native_role",
                                "route_role",
                                "candidate",
                                "predecessor",
                                "capture",
                                "packet",
                                *(('binding', 'condition', 'capacity', 'original_payload') if technical else ()),
                            )
                        )
                        or expected["permit"]["binding"]["remaining_dev_attempts"] != prior_dev
                        or expected["permit"]["binding"]["remaining_review_cycles"]
                        != max(0, prior_review)
                        or expected["permit"]["binding"]["successor"]["directory"]
                        != str(
                            directory
                            / "workers"
                            / expected["permit"]["binding"]["successor"]["launch_id"]
                        )
                    ):
                        raise CampaignError(
                            "continuation consumption lacks exact prior ownership and successor claim"
                        )
                if unit.evidence.get("external_landing"):
                    if (
                        replacement is None
                        or replacement.evidence.get("external_landing")
                        != unit.evidence["external_landing"]
                    ):
                        raise CampaignError("observed external landing identity is immutable")
                    prior_writers = set(unit.evidence.get("developer_families", ()))
                    next_writers = set(replacement.evidence.get("developer_families", ()))
                    if unit.evidence.get("developer_family"):
                        prior_writers.add(unit.evidence["developer_family"])
                    if replacement.evidence.get("developer_family"):
                        next_writers.add(replacement.evidence["developer_family"])
                    if not prior_writers.issubset(next_writers) or (
                        unit.evidence.get("developer_attribution_incomplete")
                        and not replacement.evidence.get("developer_attribution_incomplete")
                    ):
                        raise CampaignError(
                            "external landing must retain every contributing family"
                        )
                    _validate_external_landing_debt(replacement, snapshot)
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
                        and replacement.evidence.get("external_landing")
                    ):
                        from .campaign_external_merge import ExternalLandingEvidence

                        raw = dict(replacement.evidence["external_landing"])
                        raw["parents"] = tuple(raw["parents"])
                        raw["checkpoint_heads"] = tuple(raw["checkpoint_heads"])
                        # Audit against persisted authority, not a replacement
                        # that could have removed a hold or claimed quiescence.
                        audit = previous.clone()
                        if (
                            snapshot.graph_revision != previous.graph_revision
                            or snapshot.config_revision != previous.config_revision
                        ):
                            raise CampaignError("external landing authority revision changed")
                        reconcile_external_landing(audit, key, ExternalLandingEvidence(**raw))
                        expected = audit.units[key]
                        for evidence_key in ("external_landing", "landing_acceptance", "merge"):
                            if _json(replacement.evidence.get(evidence_key)) != _json(
                                expected.evidence[evidence_key]
                            ):
                                raise CampaignError(
                                    "external landing requires its exact pending acceptance debt"
                                )
                        if any(
                            getattr(replacement, field) != getattr(unit, field)
                            for field in (
                                "spec",
                                "claim",
                                "hold",
                                "attempt",
                                "failure_attempts",
                                "resolution_attempts",
                                "review_attempts",
                            )
                        ):
                            raise CampaignError(
                                "external landing cannot rewrite retained unit authority"
                            )
                        if any(
                            replacement.bindings.get(field) != unit.bindings.get(field)
                            for field in (
                                "workspace",
                                "branch",
                                "pr",
                                "source_revision",
                            )
                        ) or any(
                            replacement.evidence.get(field) != unit.evidence.get(field)
                            for field in (
                                "developer_family",
                                "developer_families",
                                "developer_attribution_incomplete",
                                "acceptance",
                                "usage",
                            )
                        ):
                            raise CampaignError(
                                "external landing must preserve ownership and acceptance obligations"
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
            from .campaign_hold import initialize

            initialize(directory, previous)
            candidate.bindings["hold_channels_version"] = previous.bindings["hold_channels_version"]
            if database is not None:
                archived = database.commit(
                    _state_document(candidate),
                    revision=expected_revision,
                    event_count=len(previous.events),
                    events=list(candidate.events[len(previous.events) :]),
                    transfer=transfer_record is not None,
                )
                candidate.outbox = [row for row in candidate.outbox if row.id not in archived]
                candidate.events = EventJournal(database, len(candidate.events))
                candidate.jobs = {
                    key: row
                    for key, row in candidate.jobs.items()
                    if row["state"] not in {"applied", "stale", "canceled"}
                }
                marker_path = directory / "snapshot.json"
                marker = TRANSFER_MARKER if database.schema_version == 4 else MARKER
                if json.loads(marker_path.read_text(encoding="utf-8")) != marker:
                    self._write(marker_path, marker)
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

    def action_history(self, campaign_id: str, **query: Any) -> list[dict[str, Any]]:
        """Paginated immutable receipts; current effects remain in the hot snapshot."""
        snapshot = self.load(campaign_id)
        if isinstance(snapshot.events, EventJournal):
            return snapshot.events.database.actions(through=snapshot.revision, **query)
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
    from .campaign_action_history import action as recorded_action

    if recorded_action(snapshot, action.id) is not None or (
        isinstance(snapshot.events, EventJournal)
        and snapshot.events.database.archived_observation_exists(action.id)
    ):
        raise CampaignError("outbox action ID already exists")
    snapshot.outbox.append(action)
    return action


def _audit_rearm_cancellation(previous, snapshot, key) -> None:
    """A spent successor can return ownership only through proven no-start evidence."""
    old, new = previous.units[key], snapshot.units.get(key)
    if new is None:
        return
    before = old.evidence.get("technical_rearm", {})
    after = new.evidence.get("technical_rearm", {})
    if after.get("phase") != "canceled" or after.get("cancellation") == before.get("cancellation"):
        return
    if before.get("phase") != "continuing" or old.claim is None:
        raise CampaignError("technical cancellation has no consumed successor")
    proof = after.get("cancellation") or {}
    projected = previous.clone()
    projected.phase = snapshot.phase
    action = next((row for row in projected.outbox if row.id == old.claim.launch_id), None)
    result = next((row for row in snapshot.outbox if row.id == old.claim.launch_id), None)
    if action is None or result is None:
        raise CampaignError("technical cancellation lost its durable launch")
    from .campaign_rearm import cancel_unlaunched

    expected = cancel_unlaunched(
        projected, projected.units[key], action,
        mode=proof.get("mode"),
        result=result.receipt if proof.get("mode") == "not-launched" else None,
    )
    restored = projected.units[key]
    if (
        proof != expected or new.claim != restored.claim or new.attempt != old.attempt
        or new.spec != old.spec
        or any(new.bindings.get(field) != old.bindings.get(field) for field in (
            "workspace", "branch", "pr", "issue", "head", "base", "canonical_source",
            "effective_source", "effective_sources", "private_paths", "source_revision",
        ))
        or any(getattr(new, field) != getattr(old, field) for field in ("failure_attempts", "resolution_attempts", "review_attempts"))
        or any(new.evidence.get(field) != old.evidence.get(field) for field in (
            "continuation_permits", "worker_history", "acceptance", "usage", "developer_family",
            "developer_families", "developer_attribution_incomplete", "unresolved_approval_requirements",
        ))
        or {k: v for k, v in after.items() if k not in {"phase", "cancellation"}}
        != {k: v for k, v in before.items() if k not in {"phase", "cancellation"}}
    ):
        raise CampaignError("technical cancellation changed retained authority or allowance")


def _continuation_record(snapshot, unit, permit) -> dict[str, Any]:
    """Bind a development permit to pinned configuration and captured task bytes."""
    from .campaign_usage import remaining

    if (
        not isinstance(permit, dict)
        or set(permit) != {"schema_version", "permit_id", "binding"}
        or type(permit.get("schema_version")) is not int
        or permit["schema_version"] not in {2, 3}
    ):
        raise CampaignError("unsupported continuation permit")
    binding = permit["binding"]
    if (
        not isinstance(binding, dict)
        or permit["permit_id"]
        != hashlib.sha256(
            json.dumps(binding, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    ):
        raise CampaignError("continuation permit identity changed")
    claim = unit.claim
    technical = permit["schema_version"] == 3
    evidence_key = "technical_rearm" if technical else "handoff"
    identity_key = "rearm_id" if technical else "handoff_id"
    handoff = unit.evidence.get(evidence_key, {})
    if technical:
        from .campaign_rearm import _validate_condition, validate_capture, validate_current

        if binding.get("purpose") != "technical_rearm":
            raise CampaignError("unsupported technical continuation purpose")
        validate_current(snapshot, unit, handoff, continuing=True)
        validate_capture(snapshot, unit, handoff, directory=Path(claim.run_dir).parents[1])
        _validate_condition(handoff["binding"], handoff["condition"], now=time.time())
    predecessor = handoff.get("predecessor", {})
    successor = binding.get("successor", {})
    dev, review = remaining(snapshot, unit)
    if (
        snapshot.phase != "active"
        or not claim
        or claim.quiescent
        or binding.get("campaign_id") != snapshot.id
        or binding.get("unit_id") != unit.id
        or binding.get("native_role") != "dev"
        or (not technical and handoff.get("native_role") != "dev")
        or binding.get("config_revision") != snapshot.config_revision
        or binding.get(identity_key) != handoff.get("id")
        or handoff.get("phase") != "continuing"
        or binding.get("capture_digest") != handoff.get("capture", {}).get("capture_digest")
        or binding.get("fingerprint")
        != handoff.get("capture", {}).get("current", {}).get("fingerprint")
        or any(
            binding.get(key) != handoff.get("capture", {}).get("current", {}).get(key)
            for key in ("source_roots", "original_state_sha256", "predecessor_receipt_sha256")
        )
        or binding.get("workspace") != unit.bindings.get("workspace")
        or binding.get("common_dir") != snapshot.common_dir
        or binding.get("branch") != unit.bindings.get("branch")
        or type(binding.get("pr_number")) is not int
        or binding["pr_number"] <= 0
        or binding["pr_number"] != unit.bindings.get("pr", {}).get("number")
        or predecessor.get("quiescent") is not True
        or any(
            binding.get("predecessor", {}).get(key) != predecessor.get(key)
            for key in (
                "launch_id",
                "run_id",
                "run_dir",
                "attempt",
                "generation",
            )
        )
        or any(
            successor.get(key) != value
            for key, value in {
                "launch_id": claim.launch_id,
                "run_id": claim.run_id,
                "directory": claim.run_dir,
                "attempt": claim.attempt,
                "generation": snapshot.generation,
            }.items()
        )
        or successor.get("launch_id") == predecessor.get("launch_id")
        or successor.get("run_id") == predecessor.get("run_id")
        or type(binding.get("remaining_dev_attempts")) is not int
        or binding.get("remaining_dev_attempts") != dev
        or dev <= 0
        or type(binding.get("remaining_review_cycles")) is not int
        or binding.get("remaining_review_cycles") != max(0, review)
        or not any(
            action.id == claim.launch_id
            and action.unit == unit.id
            and action.kind == "launch"
            and action.generation == claim.generation
            and action.state in {"prepared", "sent_unknown"}
            and action.payload.get("kind") == "execution"
            and action.payload.get(identity_key) == handoff.get("id")
            for action in snapshot.outbox
        )
        or (unit.hold and (unit.hold.kind != evidence_key or unit.hold.id != handoff.get("id")))
    ):
        raise CampaignError(
            "continuation permit is stale, exhausted or not owned by its exact successor"
        )
    from .campaign_continuation import prepare_permit
    from .campaign_execution import CampaignExecution
    from .campaign_worker_authority import continuation_request, execution_envelope

    template_binding = snapshot.bindings.get("execution_template")
    if not isinstance(template_binding, dict):
        raise CampaignError("continuation has no pinned execution configuration")
    template = CampaignExecution.load(Path(template_binding["path"]), template_binding["digest"])
    if template.campaign_id != snapshot.id or template.config_revision != snapshot.config_revision:
        raise CampaignError("continuation execution configuration belongs to another authority")
    action = next(row for row in snapshot.outbox if row.id == claim.launch_id)
    from .campaign_model import RoleCandidate

    try:
        selected_raw = action.payload["candidate"]
        approved_raw = handoff["candidate"]
        selected_route = RoleCandidate(
            **{**selected_raw, "capacity_command": tuple(selected_raw.get("capacity_command", ()))}
        )
        approved_route = RoleCandidate(
            **{**approved_raw, "capacity_command": tuple(approved_raw.get("capacity_command", ()))}
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise CampaignError("continuation requires its approved frozen route") from exc
    if selected_route != approved_route:
        raise CampaignError("continuation route differs from its approved handoff")
    envelope = execution_envelope(
        template, snapshot, unit, claim, action.payload, Path(claim.run_dir)
    )
    request = continuation_request(snapshot, unit, claim)
    if prepare_permit(request, envelope) != permit:
        raise CampaignError(
            "continuation permit differs from its pinned source, task or configuration"
        )
    receipt = {
        "schema_version": 1,
        "permit_id": permit["permit_id"],
        "successor_launch_id": claim.launch_id,
        "successor_run_id": claim.run_id,
        "state": "consumed",
    }
    return {"permit": permit, "consumption": receipt}


def consume_continuation_permit(snapshot, unit_id, permit) -> dict[str, Any]:
    """Consume once in the same authority transaction as successor adoption."""
    unit = snapshot.units[unit_id]
    record = _continuation_record(snapshot, unit, permit)
    permits = unit.evidence.setdefault("continuation_permits", {})
    if permit["permit_id"] in permits:
        raise CampaignError("continuation permit has already been consumed")
    if any(
        existing.get("consumption", {}).get("successor_launch_id") == unit.claim.launch_id
        for existing in permits.values()
    ):
        raise CampaignError("successor already consumed continuation authority")
    permits[permit["permit_id"]] = record
    add_event(
        snapshot,
        "continuation-consumed",
        unit=unit_id,
        permit_id=permit["permit_id"],
        successor=unit.claim.launch_id,
        predecessor=permit["binding"]["predecessor"]["launch_id"],
    )
    return record


def transition(snapshot: CampaignSnapshot, unit: str, target: UnitState, **details: Any) -> None:
    current = snapshot.units[unit]
    check_campaign_transition(unit, current.state, target)
    source = current.state
    current.state = target
    add_event(snapshot, "transition", unit=unit, source=str(source), target=str(target), **details)


def _external_merge_unit(snapshot, unit_id, proof):
    """Validate current authority shared by covered and unaccepted landings."""
    from .campaign_external_merge import ExternalMergeEvidence
    from .statemachine import check_campaign_external_merge

    unit = snapshot.units[unit_id]
    check_campaign_external_merge(unit_id, unit.state)
    integrated = unit.evidence.get("integrated", {})
    if (
        not isinstance(proof, ExternalMergeEvidence)
        or proof.repository != snapshot.repository
        or proof.unit_id != unit.id
        or proof.head != integrated.get("head")
        or proof.base != integrated.get("base")
        or proof.target != snapshot.spec.target
        or not proof.merge_commit
        or not proof.canonical_head
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
    return unit


def reconcile_external_merge(
    snapshot: CampaignSnapshot, unit_id: str, proof: ExternalMergeEvidence
) -> None:
    """Record a covered canonical landing without inventing native submission."""
    unit = _external_merge_unit(snapshot, unit_id, proof)
    if proof.schema_version != 1 or not proof.covered_tree:
        raise CampaignError(
            "external merge proof is stale, uncovered or does not match accepted unit authority"
        )

    unit.state = UnitState.MERGED_PENDING_SYNC
    unit.evidence["external_merge"] = dataclasses.asdict(proof)
    add_event(
        snapshot,
        "external-merge-reconciled",
        unit=unit.id,
        head=proof.head,
        merge_commit=proof.merge_commit,
        canonical_head=proof.canonical_head,
    )


def _external_landing_debt(proof) -> dict[str, Any]:
    from .campaign_publication import Candidate

    candidate = Candidate(
        proof.repository,
        proof.unit_id,
        proof.pr_number,
        proof.head,
        proof.base,
        proof.source_revision,
        proof.graph_revision,
        str(proof.config_revision),
        proof.target,
    )
    return {
        "schema_version": 1,
        "state": "pending",
        "candidate": dataclasses.asdict(candidate),
        "merge_commit": proof.merge_commit,
        "merge_tree": proof.merge_tree,
        "merge_parents": list(proof.parents),
        "canonical_head": proof.canonical_head,
        "reason": "Actual merged product requires selected verification and independent review",
    }


def _validate_external_landing_debt(unit, snapshot) -> None:
    """Retain the frozen merge obligation across later repair graph revisions."""
    from .campaign_external_merge import ExternalLandingEvidence

    raw = dict(unit.evidence["external_landing"])
    raw["parents"] = tuple(raw["parents"])
    raw["checkpoint_heads"] = tuple(raw["checkpoint_heads"])
    expected = _external_landing_debt(ExternalLandingEvidence(**raw))
    debt = unit.evidence.get("landing_acceptance")
    if (
        not isinstance(debt, dict)
        or debt.get("state")
        not in {
            "pending",
            "verifying",
            "reviewing",
            "repair_pending",
            "accepted",
        }
        or any(
            debt.get(key) != value
            for key, value in expected.items()
            if key not in {"state", "reason"}
        )
    ):
        raise CampaignError("external landing acceptance debt is missing or rebound")
    merge = unit.evidence.get("merge", {})
    if (
        merge.get("merge_commit") != expected["merge_commit"]
        or merge.get("canonical_head") != expected["canonical_head"]
    ):
        raise CampaignError("external landing merge identity changed")
    if debt["state"] == "accepted":
        repaired = debt.get("repaired")
        if repaired:
            repair = snapshot.units.get(debt.get("repair_unit"))
            if (
                repair is None
                or repair.state != UnitState.DELIVERED
                or repair.spec.kind != "landing_repair"
                or repair.spec.parent != unit.id
                or repair.bindings.get("landing_parent") != unit.id
                or repair.evidence.get("landing_original_merge") != expected["merge_commit"]
                or repaired.get("repair_unit") != repair.id
                or repaired.get("repair_merge") != repair.evidence.get("merge")
                or not repaired.get("acceptance", {}).get("complete")
                or repaired.get("acceptance")
                != repair.evidence.get("acceptance", {}).get("current")
            ):
                raise CampaignError("external landing requires its completed linked repair proof")
        else:
            from .campaign_acceptance import receipts_current
            from .campaign_landing_acceptance import acceptance, candidate

            verification = debt.get("verification", {})
            review = debt.get("review", {})
            writers = set(unit.evidence.get("developer_families", ()))
            if unit.evidence.get("developer_family"):
                writers.add(unit.evidence["developer_family"])
            reviewer = review.get("provider_family")
            if (
                verification.get("outcome") != "verified"
                or verification.get("head") != expected["merge_commit"]
                or not verification.get("launch_id")
                or review.get("outcome") != "stage_complete"
                or review.get("head") != expected["merge_commit"]
                or not review.get("launch_id")
                or review.get("validated", {}).get("verdict") != "approved"
                or not debt.get("acceptance", {}).get("complete")
                or not receipts_current(verification, review, candidate(unit))
                or any(
                    command.get("returncode") != 0
                    or command.get("timed_out")
                    or command.get("interrupted")
                    or command.get("spawn_error")
                    for command in verification.get("commands", [])
                )
                or not acceptance(unit, review).get("complete")
                or (
                    snapshot.spec.github.require_independent_review
                    and (
                        not writers
                        or not reviewer
                        or reviewer in writers
                        or unit.evidence.get("developer_attribution_incomplete")
                    )
                )
            ):
                raise CampaignError("external landing requires exact merged-product acceptance")


def reconcile_external_landing(
    snapshot: CampaignSnapshot, unit_id: str, proof: ExternalLandingEvidence
) -> None:
    """Atomically record an observed merge and its still-unaccepted product."""
    from .campaign_external_merge import ExternalLandingEvidence

    unit = _external_merge_unit(snapshot, unit_id, proof)
    review = unit.evidence.get("review", {}).get("validated", {})
    coordinates = review.get("coordinates", {})
    verification = unit.evidence.get("verification", {})
    writers = set(unit.evidence.get("developer_families", ()))
    if unit.evidence.get("developer_family"):
        writers.add(unit.evidence["developer_family"])
    reviewer = unit.evidence.get("review", {}).get("provider_family")
    if (
        not isinstance(proof, ExternalLandingEvidence)
        or proof.schema_version != 2
        or proof.covered_tree is not False
        or not re.fullmatch(r"[a-f0-9]{40}|[a-f0-9]{64}", proof.merge_tree)
        or type(proof.pr_number) is not int
        or proof.pr_number <= 0
        or proof.pr_number != unit.bindings.get("pr", {}).get("number")
        or verification.get("outcome") != "verified"
        or verification.get("head") != proof.head
        or review.get("verdict") != "approved"
        or (
            snapshot.spec.github.require_independent_review
            and (
                not writers
                or not reviewer
                or reviewer in writers
                or unit.evidence.get("developer_attribution_incomplete")
            )
        )
        or any(
            coordinates.get(key) != value
            for key, value in {
                "head": proof.head,
                "base": proof.base,
                "source_revision": proof.source_revision,
                "graph_revision": proof.graph_revision,
                "config_revision": proof.config_revision,
            }.items()
        )
        or unit.evidence.get("landing_acceptance")
    ):
        raise CampaignError(
            "external landing proof lacks exact current authority or pending acceptance"
        )
    unit.evidence["external_landing"] = dataclasses.asdict(proof)
    unit.evidence["landing_acceptance"] = _external_landing_debt(proof)
    from .campaign_publication import MergeReceipt

    unit.evidence["merge"] = dataclasses.asdict(
        MergeReceipt(
            "merged_pending_sync",
            merge_commit=proof.merge_commit,
            canonical_head=proof.canonical_head,
        )
    )
    unit.state = UnitState.MERGED_PENDING_SYNC
    add_event(
        snapshot,
        "external-landing-observed",
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
