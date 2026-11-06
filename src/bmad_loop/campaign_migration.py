"""Read-only legacy ownership inventory and explicit campaign migration gates.

Observation never grants adoption. A dead engine or an idle worktree is not
proof of abandonment. Scheduler/publisher fences come from a fresh read-back by
an explicitly supplied service observer, not a local file or user-authored JSON
claim. This module never stops processes, resumes runs, changes environments,
disables automations or writes another repository.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Literal, Protocol

from . import runs
from .campaign_github import GithubGateway
from .campaign_model import CampaignError, CampaignSnapshot, Hold, snapshot_from_dict
from .campaign_store import add_event
from .verify import git_bytes


class MigrationError(CampaignError):
    """The selected migration cannot establish exclusive, preserved ownership."""


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


@dataclass(frozen=True)
class MigrationItem:
    id: str
    kind: str
    revision: str
    source: str
    state: str = "unknown"
    liveness: str = "unknown"
    ownership: str = "unknown"
    units: tuple[str, ...] = ()
    details: dict[str, Any] | None = None


@dataclass(frozen=True)
class ServiceInventory:
    """A service adapter's authenticated, bounded read-back of selected actors.

    An adapter must identify the exact common directory and service revision.
    Only adapters passed by host configuration are called; manifest text cannot
    supply a command, provider, admission fence or service response.
    """

    service: str
    common_dir: str
    observed_at: float
    revision: str
    items: tuple[MigrationItem, ...]
    complete: bool = True
    faults: tuple[str, ...] = ()


class ServiceObserver(Protocol):
    def __call__(self, project: Path, common_dir: Path) -> ServiceInventory: ...


@dataclass(frozen=True)
class MigrationInventory:
    schema_version: int
    project: str
    common_dir: str
    observed_at: float
    items: tuple[MigrationItem, ...]
    services: tuple[ServiceInventory, ...] = ()
    unavailable: tuple[str, ...] = ()
    truncated: tuple[str, ...] = ()

    @property
    def digest(self) -> str:
        return _digest(asdict(self))

    @property
    def complete(self) -> bool:
        return not self.unavailable and not self.truncated


@dataclass(frozen=True)
class MigrationSelection:
    id: str
    revision: str
    disposition: Literal["preserve", "drain", "adopt", "replace"]
    units: tuple[str, ...] = ()


@dataclass(frozen=True)
class MigrationManifest:
    schema_version: int
    common_dir: str
    inventory_digest: str
    authorized_by: str
    selections: tuple[MigrationSelection, ...]
    # Authorization is deliberately separate from a successful read-back.
    disable_automations: tuple[str, ...] = ()


@dataclass(frozen=True)
class MigrationQualification:
    inventory_digest: str
    manifest_digest: str
    common_dir: str
    qualified: bool
    problems: tuple[str, ...]
    held_units: tuple[str, ...]
    adopted_units: tuple[str, ...]
    replaceable_automations: tuple[str, ...]
    observed_at: float


def _git(project: Path, *args: str) -> str:
    result = git_bytes(project, *args)
    if result.returncode:
        raise MigrationError(f"migration git observation failed: {args[0]}")
    return result.stdout.decode("utf-8", errors="strict").strip()


def _read_json(path: Path, *, bound: int) -> tuple[dict[str, Any], str]:
    if path.is_symlink() or not path.is_file():
        raise MigrationError("inventory input is missing, nonregular or symlinked")
    with path.open("rb") as stream:
        raw = stream.read(bound + 1)
    if len(raw) > bound:
        raise MigrationError("inventory input exceeds byte bound")
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise MigrationError("inventory input is not an object")
    return data, hashlib.sha256(raw).hexdigest()


def inventory(
    project: Path,
    *,
    campaign_root: Path | None = None,
    gateway: GithubGateway | None = None,
    observers: tuple[ServiceObserver, ...] = (),
    clock: Callable[[], float] = time.time,
    max_items: int = 5000,
    max_bytes: int = 4 * 1024 * 1024,
) -> MigrationInventory:
    """Inspect this Git common directory, its runs and explicitly named services.

    No recursive disk scan, fetch, checkout, launcher or settings mutation occurs.
    Missing external-service observers are visible as unavailable coverage. A
    caller may still inspect the partial inventory but cannot qualify migration.
    """
    if max_items < 1 or max_bytes < 1:
        raise ValueError("migration inventory bounds must be positive")
    project = project.resolve()
    common = (project / _git(project, "rev-parse", "--git-common-dir")).resolve()
    items: list[MigrationItem] = []
    unavailable: list[str] = []
    truncated: list[str] = []
    workspaces: list[Path] = []
    raw = git_bytes(project, "worktree", "list", "--porcelain", "-z")
    if raw.returncode:
        unavailable.append("git:worktrees")
    else:
        for block in raw.stdout.decode("utf-8").split("\0\0"):
            fields: dict[str, str] = {}
            for field in block.split("\0"):
                if field:
                    key, _, value = field.partition(" ")
                    fields[key] = value
            if "worktree" not in fields:
                continue
            path = Path(fields["worktree"])
            identity = "worktree:" + _digest(str(path))[:24]
            try:
                if (
                    path.is_symlink()
                    or (path / _git(path, "rev-parse", "--git-common-dir")).resolve() != common
                ):
                    raise MigrationError("worktree common directory changed")
                dirty = _git(path, "status", "--porcelain=v1", "--untracked-files=normal")
                details = {
                    "path": str(path),
                    "head": fields.get("HEAD", ""),
                    "branch": fields.get("branch", ""),
                    "dirty": dirty,
                    "locked": fields.get("locked"),
                    "prunable": fields.get("prunable"),
                }
                workspaces.append(path)
                items.append(
                    MigrationItem(
                        identity,
                        "worktree",
                        _digest(details),
                        str(path),
                        state="dirty" if dirty else "clean",
                        details=details,
                    )
                )
            except (OSError, ValueError, MigrationError) as exc:
                unavailable.append(f"{identity}:{type(exc).__name__}")
    seen_runs: set[Path] = set()
    for workspace in workspaces:
        run_dirs, fault = runs.list_run_dirs(workspace)
        if fault:
            unavailable.append(f"runs:{workspace}:{fault}")
        for run_dir in run_dirs:
            if run_dir.resolve() in seen_runs:
                continue
            seen_runs.add(run_dir.resolve())
            if len(items) >= max_items:
                truncated.append("legacy-runs")
                break
            try:
                if any(
                    path.is_symlink()
                    for path in (workspace / ".bmad-loop", workspace / runs.RUNS_DIR, run_dir)
                ):
                    raise MigrationError("legacy run directory is symlinked")
                data, revision = _read_json(run_dir / "state.json", bound=max_bytes)
                if Path(data["project"]).resolve() != workspace.resolve():
                    raise MigrationError("legacy run project differs from owning workspace")
                pid, process_identity = runs.read_pid_identity(run_dir)
                live = runs.engine_liveness(run_dir) if pid is not None else "unknown"
                tasks = data.get("tasks", {})
                units = tuple(sorted(tasks)) if isinstance(tasks, dict) else ()
                details = {
                    "run_id": data.get("run_id", run_dir.name),
                    "project": str(workspace),
                    "pid": pid,
                    "process_identity": process_identity,
                    "source": data.get("source", "sprint-status"),
                    "hold": data.get("paused_reason"),
                    "launcher_preservation_required": live != "dead",
                    "bindings": {
                        key: {
                            name: task.get(name)
                            for name in (
                                "branch",
                                "worktree_path",
                                "phase",
                                "spec_file",
                                "ticket_id",
                            )
                        }
                        for key, task in tasks.items()
                        if isinstance(task, dict)
                    },
                }
                state = (
                    "finished"
                    if data.get("finished")
                    else (
                        "stopped"
                        if data.get("stopped")
                        else "held" if data.get("paused_reason") else "unfinished"
                    )
                )
                items.append(
                    MigrationItem(
                        "legacy-run:" + _digest(str(run_dir))[:24],
                        "legacy-run",
                        revision,
                        str(run_dir),
                        state,
                        live,
                        "owned" if live == "alive" else "unknown",
                        units,
                        details,
                    )
                )
            except (OSError, ValueError, KeyError, TypeError, MigrationError) as exc:
                unavailable.append(f"run:{run_dir}:{type(exc).__name__}")
    if campaign_root is not None:
        root = campaign_root.resolve()
        try:
            for directory in sorted(root.iterdir()) if root.exists() else ():
                if not (directory / "snapshot.json").exists():
                    continue
                if len(items) >= max_items:
                    truncated.append("native-campaigns")
                    break
                if directory.is_symlink():
                    raise MigrationError("native snapshot directory is symlinked")
                data, _ = _read_json(directory / "snapshot.json", bound=max_bytes)
                snapshot = snapshot_from_dict(data)
                if snapshot.id != directory.name:
                    raise MigrationError("native campaign snapshot ID differs from directory")
                if Path(snapshot.common_dir).resolve() != common:
                    raise MigrationError("native campaign belongs to another common directory")
                for unit in snapshot.units.values():
                    details = {
                        "campaign_id": snapshot.id,
                        "unit_id": unit.id,
                        "hold": asdict(unit.hold) if unit.hold else None,
                        "claim": asdict(unit.claim) if unit.claim else None,
                        "workspace": unit.bindings.get("workspace"),
                        "pr": unit.bindings.get("pr"),
                        "admissions_open": snapshot.phase == "active",
                    }
                    items.append(
                        MigrationItem(
                            f"native-unit:{snapshot.id}:{unit.id}",
                            "native-unit",
                            _digest(details),
                            str(directory),
                            str(unit.state),
                            "unknown",
                            "owned" if unit.claim else "unknown",
                            (unit.id,),
                            details,
                        )
                    )
        except (OSError, ValueError, CampaignError) as exc:
            unavailable.append(f"native-campaigns:{type(exc).__name__}")
    if gateway is None:
        unavailable.append("github:observer-not-configured")
    else:
        try:
            for pull in gateway.pulls():
                details = {
                    key: pull.get(key)
                    for key in (
                        "number",
                        "node_id",
                        "head",
                        "base",
                        "draft",
                        "state",
                        "html_url",
                        "updated_at",
                    )
                }
                items.append(
                    MigrationItem(
                        f"pr:{gateway.repository}:{pull['number']}",
                        "pr",
                        _digest(details),
                        gateway.repository,
                        str(pull.get("state", "unknown")),
                        details=details,
                    )
                )
        except (OSError, ValueError, RuntimeError) as exc:
            unavailable.append(f"github:{type(exc).__name__}")
    services = []
    now = clock()
    for observer in observers:
        try:
            observed = observer(project, common)
            if (
                not isinstance(observed, ServiceInventory)
                or Path(observed.common_dir).resolve() != common
            ):
                raise MigrationError("service inventory identity differs")
            if (
                not observed.service
                or not observed.revision
                or not observed.complete
                or observed.faults
            ):
                raise MigrationError("service inventory incomplete or unversioned")
            if not 0 <= now - observed.observed_at <= 120:
                raise MigrationError("service inventory stale")
            if any(
                not item.id
                or not item.revision
                or item.kind not in {"automation", "scheduler", "publisher", "process", "workspace"}
                for item in observed.items
            ):
                raise MigrationError("service inventory item identity/type invalid")
            services.append(observed)
            items.extend(observed.items)
        except (OSError, ValueError, RuntimeError) as exc:
            unavailable.append(
                f"service:{getattr(observer, '__name__', type(observer).__name__)}:{type(exc).__name__}"
            )
    if not observers:
        unavailable.append("external-schedulers:observer-not-configured")
    if len(items) > max_items:
        items = items[:max_items]
        truncated.append("items")
    if len({item.id for item in items}) != len(items):
        unavailable.append("duplicate-inventory-identities")
    return MigrationInventory(
        1,
        str(project),
        str(common),
        now,
        tuple(items),
        tuple(services),
        tuple(unavailable),
        tuple(truncated),
    )


def manifest_from_dict(value: dict[str, Any]) -> MigrationManifest:
    """Read explicit owner selections; no service responses are accepted here."""
    allowed = {
        "schema_version",
        "common_dir",
        "inventory_digest",
        "authorized_by",
        "selections",
        "disable_automations",
    }
    if (
        set(value) - allowed
        or type(value.get("schema_version")) is not int
        or value["schema_version"] != 1
    ):
        raise MigrationError("unsupported migration manifest fields/version")
    selections = []
    for row in value.get("selections", ()):
        if not isinstance(row, dict) or set(row) - {"id", "revision", "disposition", "units"}:
            raise MigrationError("invalid migration selection")
        if row.get("disposition") not in {"preserve", "drain", "adopt", "replace"}:
            raise MigrationError("unknown migration disposition")
        if any(not isinstance(row.get(key), str) or not row[key] for key in ("id", "revision")):
            raise MigrationError("migration selection requires exact identity/revision")
        units = row.get("units", ())
        if not isinstance(units, (list, tuple)) or any(
            not isinstance(u, str) or not u for u in units
        ):
            raise MigrationError("migration units must be explicit identifiers")
        selections.append(
            MigrationSelection(row["id"], row["revision"], row["disposition"], tuple(units))
        )
    fields = [value.get(key) for key in ("common_dir", "inventory_digest", "authorized_by")]
    disabled = value.get("disable_automations", ())
    if (
        any(not isinstance(v, str) or not v.strip() for v in fields)
        or not Path(value["common_dir"]).is_absolute()
        or not isinstance(disabled, (list, tuple))
        or any(not isinstance(item, str) or not item for item in disabled)
    ):
        raise MigrationError(
            "migration manifest requires owner, repository identity and inventory revision"
        )
    return MigrationManifest(
        1,
        value["common_dir"],
        value["inventory_digest"],
        value["authorized_by"],
        tuple(selections),
        tuple(disabled),
    )


def validate_manifest(
    observed: MigrationInventory,
    manifest: MigrationManifest,
    replacement: dict[str, Any],
    *,
    now: float | None = None,
    max_age_s: float = 120,
) -> MigrationQualification:
    """Qualify exact selections against current service fence/replacement receipts.

    ``replacement`` is the actual native preflight result, not a manifest field.
    A stopped scheduler admits no work, while its live workers may still own
    units. Replacement is safe only after both responsibilities are accounted for.
    Unrelated automations remain untouched and never enter the returned list.
    """
    now = time.time() if now is None else now
    problems = list(observed.unavailable) + list(observed.truncated)
    if not 0 <= now - observed.observed_at <= max_age_s:
        problems.append("migration inventory stale")
    if manifest.schema_version != 1 or not manifest.authorized_by.strip():
        problems.append("explicit versioned owner authorization required")
    if (
        manifest.inventory_digest != observed.digest
        or Path(manifest.common_dir).resolve() != Path(observed.common_dir).resolve()
    ):
        problems.append("migration inventory/common-directory revision changed")
    capabilities = replacement.get("capabilities", {})
    qualified_replacement = all(
        capabilities.get(area, {}).get("ready") is True
        for area in ("planning", "execution", "publication")
    )
    if (
        replacement.get("schema_version") != 1
        or not replacement.get("config_digest")
        or Path(replacement.get("common_dir", "/unknown")).resolve()
        != Path(observed.common_dir).resolve()
    ):
        qualified_replacement = False
    if not qualified_replacement:
        problems.append("native planning/execution/publication replacement is not qualified")
    items = {item.id: item for item in observed.items}
    selected = {item.id: item for item in manifest.selections}
    if len(selected) != len(manifest.selections):
        problems.append("duplicate migration selections")
    service_ids = {item.id for service in observed.services for item in service.items}
    held: set[str] = set()
    adopted: set[str] = set()
    replaceable: set[str] = set()
    for selection in manifest.selections:
        item = items.get(selection.id)
        if item is None or item.revision != selection.revision:
            problems.append(f"selection identity/revision changed: {selection.id}")
            held.update(selection.units)
            continue
        units = selection.units or item.units
        if item.units and not set(units).issubset(item.units):
            problems.append(f"selected units differ from observed actor binding: {item.id}")
            held.update(units)
            continue
        details = item.details or {}
        if selection.disposition == "preserve":
            held.update(units)
        elif selection.disposition == "drain":
            if item.liveness != "dead" or item.state not in {"finished", "stopped", "delivered"}:
                held.update(units)
        elif selection.disposition == "adopt":
            # Supported resumability is an independently checked binding, not a
            # guessed legacy state import. This module never resumes the actor.
            if (
                item.id not in service_ids
                or item.ownership != "unowned"
                or item.liveness != "dead"
                or details.get("preserved_clean") is not True
                or details.get("resumable_binding_verified") is not True
                or not details.get("head")
                or not details.get("binding_revision")
            ):
                problems.append(f"adoption ownership/preservation not proven: {item.id}")
                held.update(units)
            else:
                adopted.update(units)
        elif selection.disposition == "replace":
            if (
                item.id not in service_ids
                or item.kind not in {"scheduler", "publisher", "automation"}
                or details.get("admissions_open") is not False
                or not details.get("fence_revision")
            ):
                problems.append(f"external admissions fence not proven: {item.id}")
                held.update(units)
            elif (
                type(details.get("active_workers")) is not int
                or details["active_workers"] != 0
                or item.liveness != "dead"
            ):
                held.update(units)
                problems.append(f"replacement still owns in-flight work: {item.id}")
            elif (
                item.kind == "automation"
                and item.id in manifest.disable_automations
                and qualified_replacement
            ):
                replaceable.add(item.id)
    for item in observed.items:
        if item.kind in {"scheduler", "publisher", "automation"}:
            details = item.details or {}
            if details.get("campaign_conflict") is True:
                if item.id not in selected:
                    problems.append(f"conflicting admissions owner is not mapped: {item.id}")
                    held.update(item.units)
                if (
                    item.id not in service_ids
                    or details.get("admissions_open") is not False
                    or not details.get("fence_revision")
                ):
                    problems.append(
                        f"conflicting admissions owner has no verified fence: {item.id}"
                    )
    for identity in manifest.disable_automations:
        if identity not in replaceable:
            problems.append(f"automation disable is not qualified/selected: {identity}")
    return MigrationQualification(
        observed.digest,
        _digest(asdict(manifest)),
        observed.common_dir,
        not problems,
        tuple(problems),
        tuple(sorted(held)),
        tuple(sorted(adopted)),
        tuple(sorted(replaceable)) if not problems else (),
        now,
    )


def record_qualification(snapshot: CampaignSnapshot, qualification: MigrationQualification) -> None:
    """Record native admission holds; external fences are never created here.

    Call inside the coordinator's normal snapshot CAS transaction. A receipt
    cannot release an operator hold or authorize migration of a different repo.
    """
    if Path(snapshot.common_dir).resolve() != Path(qualification.common_dir).resolve():
        raise MigrationError("migration receipt belongs to another repository")
    unknown = set(qualification.held_units) - set(snapshot.units)
    if unknown:
        raise MigrationError(f"migration selection names unknown campaign units: {sorted(unknown)}")
    snapshot.bindings["migration"] = asdict(qualification)
    for identity in qualification.held_units:
        unit = snapshot.units[identity]
        if unit.hold is None:
            unit.hold = Hold(
                "migration",
                "legacy actor ownership must be drained or explicitly adopted",
                id="migration:" + identity,
                actor="migration",
            )
    add_event(
        snapshot,
        "migration-qualified" if qualification.qualified else "migration-held",
        inventory_digest=qualification.inventory_digest,
        problems=list(qualification.problems),
    )
