"""Forward-only retirement of an archived startup campaign to one successor.

This is deliberately narrower than worker migration: no unit writer may ever
have launched. The old campaign stays stopped with its original units, effects
and histories. Only this operation can earn the SQLite schema-4 retirement fence;
ordinary saves cannot manufacture, rewrite or remove its release authority.
Observation grants no authority and never allocates a workspace or resumes work.
"""

from __future__ import annotations

import copy
import dataclasses
import json
import re
import time
from pathlib import Path

from . import campaign_adoption as adoption
from .campaign_database import TRANSFER_MARKER, CampaignDatabase, EventJournal
from .campaign_jobs import digest
from .campaign_migration import ServiceInventory
from .campaign_model import (
    CampaignError,
    CampaignSnapshot,
    SourceSelector,
    UnitState,
    source_to_dict,
)
from .campaign_store import CampaignStore, RevisionConflict, add_event


@dataclasses.dataclass(frozen=True)
class TransferRequest:
    predecessor: str
    unit: str
    successor: str
    successor_unit: str
    pr_number: int
    expected_revision: int
    expected_successor_revision: int
    reason: str


def retired(snapshot: CampaignSnapshot) -> bool:
    return bool(snapshot.bindings.get("ownership_retirement"))


def released(snapshot: CampaignSnapshot, unit_id: str) -> bool:
    return unit_id in snapshot.bindings.get("ownership_transfers", {})


def require_not_retired(snapshot: CampaignSnapshot, operation: str = "write") -> None:
    if retired(snapshot):
        raise CampaignError(f"retired predecessor cannot {operation}")


def require_not_released(snapshot, unit_id, operation="write") -> None:
    # The first mapped release retires the entire startup campaign, including
    # unmapped intake/planning units. Removing a hold cannot reopen admissions.
    require_not_retired(snapshot, operation)


def _records(snapshot):
    value = snapshot.bindings.get("ownership_transfers", {})
    if not isinstance(value, dict):
        raise CampaignError("invalid ownership transfer records")
    return value


def _validate_record(record):
    if not isinstance(record, dict) or record.get("schema_version") != 1:
        raise CampaignError("unsupported ownership release record")
    body = {k: v for k, v in record.items() if k not in {"id", "fence_revision"}}
    if (
        record.get("state") != "released"
        or record.get("id") != digest(body)
        or record.get("fence_revision") != record.get("id")
    ):
        raise CampaignError("ownership release digest differs")


def validate_transfer_preservation(previous, candidate, *, release_record=None):
    """Store boundary: immutable releases and no ordinary retired authority writes."""
    old, new = _records(previous), _records(candidate)
    retirement = previous.bindings.get("ownership_retirement")
    proposed = candidate.bindings.get("ownership_retirement")
    if any(new.get(key) != value for key, value in old.items()):
        raise CampaignError("ownership releases are immutable")
    additions = set(new) - set(old)
    if release_record is None:
        if additions or retirement != proposed:
            raise CampaignError("ownership retirement requires explicit transfer release")
    else:
        _validate_record(release_record)
        if (
            additions != {release_record["unit"]}
            or new[release_record["unit"]] != release_record
            or release_record["predecessor"] != previous.id
            or release_record["committed_revision"] != previous.revision + 1
            or release_record["predecessor_revision"] != previous.revision
        ):
            raise CampaignError("explicit ownership release coordinates differ")
        expected = retirement or {
            "schema_version": 1,
            "state": "retired",
            "successor": release_record["successor"],
            "release_id": release_record["id"],
            "committed_revision": previous.revision + 1,
        }
        if proposed != expected or proposed["successor"] != release_record["successor"]:
            raise CampaignError("retirement can name only its original successor")
    if retirement or release_record is not None:
        # Freeze all authority except the append-only release map. In particular
        # this covers new effects, reconfigure, job resurrection and hold release.
        from .campaign_store import _state_document

        left, right = _state_document(previous), _state_document(candidate)
        for document in (left, right):
            for key in ("revision", "updated_at", "events"):
                document.pop(key, None)
            for key in ("ownership_transfers", "ownership_retirement"):
                document["bindings"].pop(key, None)
        if candidate.phase != "stopped" or left != right:
            raise CampaignError("retired predecessor authority cannot change")


def quiescent(store: CampaignStore, snapshot: CampaignSnapshot):
    """Validate all ended optional attempts and both hot/cold ownership history."""
    from .campaign_action_history import actions
    from .campaign_requalification import _job_records, _optional_reasoning, _pending_observations

    if snapshot.phase != "stopped":
        raise CampaignError("startup transfer requires a stopped predecessor")
    if any(
        unit.attempt
        or unit.claim is not None
        or unit.state not in {UnitState.PENDING, UnitState.DRAFT}
        or unit.failure_attempts
        or unit.resolution_attempts
        or unit.review_attempts
        for unit in snapshot.units.values()
    ) or actions(snapshot, kinds=("launch",)):
        raise CampaignError("startup transfer refuses any unit writer attempt")
    rows = _job_records(snapshot)
    if any(
        not row.get("attempt")
        and (
            row.get("claim") is not None
            or row.get("state") not in {"proposed", "capacity_wait", "stale", "canceled"}
        )
        for row in rows.values()
    ):
        raise CampaignError("startup transfer refuses unproved reasoning ownership")
    _pending_observations(snapshot, rows)
    _optional_reasoning(snapshot, store.directory(snapshot.id), rows)
    if any(row.get("claim") and row["claim"].get("quiescent") is not True for row in rows.values()):
        raise CampaignError("startup transfer requires quiescent stored reasoning jobs")


def archived_workspace_ids(snapshot: CampaignSnapshot) -> tuple[str, ...]:
    """Every allocated predecessor workspace participates in whole retirement."""
    identities = []
    for unit in snapshot.units.values():
        native = unit.bindings.get("native")
        if not unit.bindings.get("workspace") and not native:
            continue
        if (
            not isinstance(native, dict)
            or not isinstance(native.get("workspace_id"), str)
            or not native["workspace_id"]
            or native["workspace_id"] in identities
        ):
            raise CampaignError(
                "startup retirement native allocation identity is missing or ambiguous"
            )
        identities.append(native["workspace_id"])
    return tuple(identities)


def _prepared(snapshot, unit_id):
    from .campaign_action_history import actions

    unit = snapshot.units[unit_id]
    binding = unit.bindings
    receipts = [
        row.receipt
        for row in actions(snapshot, kinds=("prepare-unit",), unit=unit_id)
        if row.state == "confirmed" and not row.error
    ]
    keys = ("workspace", "branch", "head", "base", "native", "spec_path")
    if (
        unit.state != UnitState.DRAFT
        or not receipts
        or any(any(row.get(k) != receipts[0].get(k) for k in keys) for row in receipts)
        or any(binding.get(k) != receipts[0].get(k) for k in keys)
        or binding.get("effective_source") != receipts[0].get("effective_source")
        or not all(isinstance(binding.get(k), str) and binding[k] for k in keys if k != "native")
    ):
        raise CampaignError("archived allocation lacks exact confirmed preparation receipt")
    native = binding.get("native")
    if (
        not isinstance(native, dict)
        or not native.get("workspace_id")
        or native.get("project_id") != snapshot.spec.paradev.project_id
        or native.get("path") != binding["workspace"]
        or native.get("branch") != binding["branch"]
        or native.get("common_dir") != snapshot.common_dir
        or Path(binding["workspace"]).exists()
        or Path(binding["workspace"]).is_symlink()
    ):
        raise CampaignError("transfer requires the exact archived native allocation")
    return binding


def _previous_source(snapshot, unit_id, binding):
    """Freeze an omitted initiative to the confirmed preparation's selected folder."""
    source_id = snapshot.units[unit_id].spec.source
    source = next((s for s in snapshot.spec.sources if s.id == source_id), None)
    effective = binding.get("effective_source")
    if (
        source is None
        or not isinstance(effective, dict)
        or (effective.get("kind") != source.kind or effective.get("id") != source.id)
    ):
        raise CampaignError("prepared source identity differs from selected source")
    try:
        selected = SourceSelector(**effective)
    except (TypeError, ValueError) as exc:
        raise CampaignError("prepared source selector is malformed") from exc
    if source.initiative and source.initiative != selected.initiative:
        raise CampaignError("prepared source initiative conflicts with selected source")
    return dataclasses.replace(
        snapshot,
        spec=dataclasses.replace(
            snapshot.spec,
            sources=tuple(selected if s.id == source_id else s for s in snapshot.spec.sources),
        ),
    )


def _coordinates(store, request, project, gateway, observer, now):
    from .campaign_github import GhTransport, GithubGateway
    from .campaign_native_inventory import NativeServiceObserver, validate_transfer_observation

    if (
        not isinstance(request, TransferRequest)
        or request.predecessor == request.successor
        or type(request.pr_number) is not int
        or request.pr_number < 1
        or not isinstance(request.reason, str)
        or not request.reason.strip()
        or any(
            type(n) is not int or n < 0
            for n in (request.expected_revision, request.expected_successor_revision)
        )
    ):
        raise CampaignError(
            "explicit startup transfer identities, revisions, PR and reason required"
        )
    previous, successor = store.load(request.predecessor), store.load(request.successor)
    for snapshot, revision in (
        (previous, request.expected_revision),
        (successor, request.expected_successor_revision),
    ):
        if snapshot.revision != revision:
            raise RevisionConflict("startup transfer campaign revision changed")
        if not isinstance(snapshot.events, EventJournal):
            raise CampaignError("startup transfer requires explicit SQLite state migration")
    if request.unit not in previous.units or request.successor_unit not in successor.units:
        raise CampaignError("startup transfer unit identity is absent")
    if released(previous, request.unit):
        raise CampaignError("unit already has an immutable release")
    if retired(previous) and previous.bindings["ownership_retirement"]["successor"] != successor.id:
        raise CampaignError("retirement can name only its original successor")
    if any(row["successor_unit"] == request.successor_unit for row in _records(previous).values()):
        raise CampaignError("transfer recipient already has an immutable source mapping")
    require_not_retired(successor, "receive a transfer")
    quiescent(store, previous)
    # The recipient is prepared without launch; no accidental reset of a live unit.
    if successor.phase != "stopped" or any(
        u.attempt or u.claim is not None for u in successor.units.values()
    ):
        raise CampaignError("transfer requires a stopped unstarted successor")
    recipient = successor.units[request.successor_unit]
    if recipient.state != UnitState.PENDING or recipient.bindings:
        raise CampaignError("transfer recipient must be unallocated")
    binding = _prepared(previous, request.unit)
    common = str(adoption.common(project))
    if any(
        (s.repository, s.common_dir, s.remote_url, s.spec.remote, s.spec.target)
        != (
            previous.repository,
            common,
            previous.remote_url,
            previous.spec.remote,
            previous.spec.target,
        )
        for s in (previous, successor)
    ):
        raise CampaignError("transfer repository/common-directory/remote/target differs")
    if (
        not previous.remote_url
        or adoption._git(project, "remote", "get-url", previous.spec.remote) != previous.remote_url
    ):
        raise CampaignError("transfer remote identity changed")
    _, old_binding, old_identity = adoption.source_binding(
        project, _previous_source(previous, request.unit, binding), request.unit
    )
    source, source_binding, source_identity = adoption.source_binding(
        project, successor, request.successor_unit
    )
    old_spec, new_spec = previous.units[request.unit].spec, recipient.spec
    # Selection IDs may change, but selected authored content and ownership may not.
    if {k: v for k, v in old_binding.items() if k != "unit_id"} != {
        k: v for k, v in source_binding.items() if k != "unit_id"
    } or (old_spec.selector, old_spec.initiative, old_spec.scopes, old_spec.acceptance) != (
        new_spec.selector,
        new_spec.initiative,
        new_spec.scopes,
        new_spec.acceptance,
    ):
        raise CampaignError("transfer selected source or ownership scope differs")
    gateway = gateway or GithubGateway(GhTransport(), previous.repository)
    pr = gateway.pull(request.pr_number)
    head, base = pr.get("head") or {}, pr.get("base") or {}
    if (
        pr.get("number") != request.pr_number
        or not isinstance(pr.get("node_id"), str)
        or not pr["node_id"]
        or pr.get("state") != "open"
        or pr.get("merged") is not False
        or head.get("ref") != binding["branch"]
        or head.get("sha") != binding["head"]
        or base.get("ref") != previous.spec.target
        or any(
            (row.get("repo") or {}).get("full_name") != previous.repository for row in (head, base)
        )
        or any(
            not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", str(row.get("sha", "")))
            for row in (head, base)
        )
        or binding["branch"] == previous.spec.target
        or (binding.get("pr") is not None and binding["pr"] != request.pr_number)
    ):
        raise CampaignError("preservation PR differs from exact startup branch/head/repository")
    if (
        adoption._remote_head(project, previous.spec.remote, binding["branch"]) != head["sha"]
        or adoption._git(project, "rev-parse", "refs/heads/" + binding["branch"]) != head["sha"]
        or adoption._remote_head(project, previous.spec.remote, previous.spec.target) != base["sha"]
    ):
        raise CampaignError("preservation branch or target moved")
    # Authenticate the retained bootstrap tree, not just an arbitrary PR headline.
    spec_path = binding["spec_path"]
    if (
        spec_path.startswith("/")
        or ".." in Path(spec_path).parts
        or not adoption._git(project, "cat-file", "-e", head["sha"] + ":" + spec_path) == ""
    ):
        raise CampaignError("preserved startup specification is unavailable")
    native = binding["native"]
    record = {
        "schema_version": 1,
        "state": "released",
        "predecessor": previous.id,
        "unit": request.unit,
        "successor": successor.id,
        "successor_unit": request.successor_unit,
        "repository": previous.repository,
        "common_dir": common,
        "remote": previous.spec.remote,
        "remote_url": previous.remote_url,
        "target": previous.spec.target,
        "pr_number": request.pr_number,
        "pr_node_id": pr["node_id"],
        "branch": head["ref"],
        "head": head["sha"],
        "base": base["sha"],
        "source_identity": source_identity,
        "predecessor_source_identity": old_identity,
        "source": {"selector": source_to_dict(source), "binding": source_binding},
        "predecessor_revision": previous.revision,
        "successor_revision": successor.revision,
        "committed_revision": previous.revision + 1,
        "predecessor_bindings": copy.deepcopy(binding),
        "owner_receipt": dataclasses.asdict(request),
        "preservation": {
            "kind": "confirmed-startup-commit",
            "head": head["sha"],
            "spec_path": spec_path,
            "native": copy.deepcopy(native),
        },
    }
    record["binding_revision"] = digest(
        {k: record[k] for k in ("predecessor_bindings", "source", "successor", "successor_unit")}
    )
    observed = observer or NativeServiceObserver(
        previous.spec.paradev.project_id, archived_workspaces=archived_workspace_ids(previous)
    )
    inventory = observed(project, Path(common)) if callable(observed) else observed
    observed_now = time.time() if now is None else now
    if (
        not isinstance(inventory, ServiceInventory)
        or not 0 <= observed_now - inventory.observed_at <= 120
    ):
        raise CampaignError("transfer native ownership observation is stale")
    record["ownership_observation"] = validate_transfer_observation(
        record, previous, successor, project=project, native_inventory=inventory, store=store
    )
    record = json.loads(json.dumps(record))
    record["id"] = digest(record)
    record["fence_revision"] = record["id"]
    return previous, successor, record


def preview(
    store: CampaignStore,
    request: TransferRequest,
    *,
    project: Path,
    gateway=None,
    observer=None,
    now=None,
):
    previous, successor, record = _coordinates(
        store, request, Path(project).resolve(), gateway, observer, now
    )
    if (store.load(previous.id).revision, store.load(successor.id).revision) != (
        previous.revision,
        successor.revision,
    ):
        raise RevisionConflict("startup transfer observation became stale")
    return {
        "schema_version": 1,
        "operation": "startup-only-whole-predecessor-retirement",
        "preview": True,
        "record": record,
        "unmapped_units": sorted(set(previous.units) - set(_records(previous)) - {request.unit}),
        "predecessor_retired": True,
        "successor_resumed": False,
    }


def read_release(store: CampaignStore, predecessor: str, unit: str):
    snapshot = store.load(predecessor)
    record = _records(snapshot).get(unit)
    _validate_record(record)
    assert isinstance(record, dict)
    retirement = snapshot.bindings.get("ownership_retirement") or {}
    if (
        snapshot.phase != "stopped"
        or retirement.get("state") != "retired"
        or retirement.get("successor") != record["successor"]
        or record["predecessor"] != snapshot.id
        or record["unit"] != unit
        or record["committed_revision"] > snapshot.revision
        or not isinstance(snapshot.events, EventJournal)
    ):
        raise CampaignError("canonical ownership release authority differs")
    with CampaignDatabase(store.directory(predecessor)).connect() as connection:
        if (
            connection.execute("PRAGMA user_version").fetchone()[0]
            != TRANSFER_MARKER["schema_version"]
        ):
            raise CampaignError("canonical ownership release lacks its old-reader fence")
    return copy.deepcopy(record)


def release(
    store: CampaignStore,
    request: TransferRequest,
    *,
    project: Path,
    gateway=None,
    observer=None,
    now=None,
):
    project = Path(project).resolve()
    previous = store.load(request.predecessor)
    if released(previous, request.unit):
        record = read_release(store, request.predecessor, request.unit)
        if record["owner_receipt"] != dataclasses.asdict(request):
            raise CampaignError("ownership release request differs from immutable prior intent")
        # A commit may have succeeded before its JSON marker publication/reply.
        store._write(store.directory(previous.id) / "snapshot.json", TRANSFER_MARKER)
        return {"schema_version": 1, "preview": False, "record": record, "idempotent": True}
    with store.coordinator(adoption.common(project), previous.spec.target, campaign_id=previous.id):
        result = preview(
            store, request, project=project, gateway=gateway, observer=observer, now=now
        )
        record = result["record"]
        candidate = store.load(previous.id)
        candidate.bindings.setdefault("ownership_transfers", {})[request.unit] = record
        candidate.bindings.setdefault(
            "ownership_retirement",
            {
                "schema_version": 1,
                "state": "retired",
                "successor": request.successor,
                "release_id": record["id"],
                "committed_revision": request.expected_revision + 1,
            },
        )
        add_event(
            candidate,
            "ownership-released",
            unit=request.unit,
            release_id=record["id"],
            successor=request.successor,
        )
        store._save_transfer(
            candidate,
            request.expected_revision,
            record=record,
            successor_revision=request.expected_successor_revision,
        )
        return {**result, "preview": False, "idempotent": False}
