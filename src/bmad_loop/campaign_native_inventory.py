"""Authenticated read-only native ownership inventory for one Paradev project.

Native terminal metadata joins the actual daemon by exact terminal UUID. Labels
and titles never establish ownership. Automation disabled flags do not prove an
admission fence or worker drain: the installed service exposes neither readback.
Nothing here stops, launches, archives, reconfigures or adopts an actor.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import time
from pathlib import Path

from .adapters.paradev_host import Host
from .campaign_migration import (
    MigrationError,
    MigrationItem,
    ServiceInventory,
    inventory,
    validate_manifest,
)
from .verify import git_bytes


def _digest(value) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _common(project: Path) -> Path:
    result = git_bytes(
        project,
        "rev-parse",
        "--path-format=absolute",
        "--git-common-dir",
        env={key: value for key, value in os.environ.items() if not key.startswith("GIT_")},
    )
    if result.returncode:
        raise MigrationError("Native project Git identity could not be verified")
    return Path(os.fsdecode(result.stdout).strip()).resolve()


class NativeServiceObserver:
    """Bounded installed Host/CLI observations, suitable for migration.inventory."""

    def __init__(self, project_id: str, *, host=None, max_items=5000, clock=time.time):
        if not project_id or max_items < 1:
            raise ValueError("Exact native project ID and positive observation bound required")
        self.project_id, self.host, self.max_items, self.clock = project_id, host, max_items, clock

    @staticmethod
    def capabilities() -> dict:
        return {
            "read_only": True,
            "native_workspace_inventory": True,
            "native_daemon_inventory": True,
            "automation_inventory": True,
            "admission_fence_readback": False,
            "worker_drain_readback": False,
            "limitation": "No supported native stop/drain fence readback; disabled schedules do not prove exclusive admission or zero active workers.",
        }

    def __call__(self, project: Path, common_dir: Path) -> ServiceInventory:
        items, faults = [], []
        now = self.clock()
        try:
            host = self.host or Host()
            projects = host.rpc("project.list")
            if not isinstance(projects, list):
                raise MigrationError("Native project inventory schema unsupported")
            selected = [
                row
                for row in projects
                if isinstance(row, dict)
                and row.get("id") == self.project_id
                and not row.get("deletedAt")
            ]
            if (
                len(selected) != 1
                or _common(Path(selected[0].get("repoPath", ""))) != common_dir.resolve()
                or _common(project) != common_dir.resolve()
            ):
                raise MigrationError("Native project/common-directory identity differs")
            workspaces = host.rpc("workspace.list")
            daemon = host.rpc("terminal.daemon.listSessions")
            if not isinstance(workspaces, list) or not isinstance(daemon, list):
                raise MigrationError("Native workspace/daemon inventory schema unsupported")
            if any(
                not isinstance(row, dict)
                or not isinstance(row.get("id"), str)
                or type(row.get("alive")) is not bool
                for row in daemon
            ):
                raise MigrationError("Native daemon identity/liveness is unavailable")
            ptys = {row["id"]: row for row in daemon}
            seen_terminals = set()
            for row in workspaces:
                if not isinstance(row, dict):
                    raise MigrationError("Native workspace row schema unsupported")
                if row.get("projectId") != self.project_id:
                    continue
                if not isinstance(row.get("id"), str) or not row["id"]:
                    raise MigrationError("Native workspace lacks exact UUID")
                details = {
                    key: row.get(key)
                    for key in (
                        "id",
                        "projectId",
                        "worktreePath",
                        "branch",
                        "createdAt",
                        "updatedAt",
                        "archivedAt",
                    )
                }
                details.update(admission_fence_readback=False, worker_drain_readback=False)
                if (
                    row.get("worktreePath")
                    and not row.get("archivedAt")
                    and row.get("worktreeExists") is not False
                ):
                    if _common(Path(row["worktreePath"])) != common_dir.resolve():
                        raise MigrationError(
                            "Registered native workspace has a foreign Git identity"
                        )
                items.append(
                    MigrationItem(
                        "paradev:workspace:" + row["id"],
                        "workspace",
                        _digest(details),
                        self.project_id,
                        "archived" if row.get("archivedAt") else "registered",
                        details=details,
                    )
                )
                if row.get("archivedAt"):
                    continue
                listing = host.rpc("terminal.list", {"workspaceId": row["id"]})
                terminals = listing.get("sessions") if isinstance(listing, dict) else None
                if not isinstance(terminals, list):
                    raise MigrationError("Native terminal inventory schema unsupported")
                for terminal in terminals:
                    ident = terminal.get("terminalId") if isinstance(terminal, dict) else None
                    if (
                        not isinstance(ident, str)
                        or not ident
                        or terminal.get("workspaceId") != row["id"]
                        or ident in seen_terminals
                    ):
                        raise MigrationError(
                            "Native terminal UUID/project/workspace identity differs"
                        )
                    seen_terminals.add(ident)
                    native = host.rpc(
                        "terminalAgents.get", {"workspaceId": row["id"], "terminalId": ident}
                    )
                    if native is not None and not isinstance(native, dict):
                        raise MigrationError("Native actor binding schema unsupported")
                    if native and native.get("terminalId") != ident:
                        raise MigrationError("Native actor binding names a different terminal")
                    actual = ptys.get(ident)
                    details = {
                        "terminal_id": ident,
                        "workspace_id": row["id"],
                        "project_id": self.project_id,
                        "worktree_path": row.get("worktreePath"),
                        "pty_pid": actual.get("pid") if actual else None,
                        "daemon_alive": actual.get("alive") if actual else False,
                        "native": (
                            {
                                key: native.get(key)
                                for key in (
                                    "agentId",
                                    "agentSessionId",
                                    "definitionId",
                                    "startedAt",
                                    "endedAt",
                                )
                            }
                            if native
                            else None
                        ),
                        "liveness_basis": "actual native terminal daemon",
                        "coding_child_quiescence": "unknown",
                        "admission_fence_readback": False,
                        "worker_drain_readback": False,
                    }
                    live = "alive" if actual and actual["alive"] else "unknown"
                    items.append(
                        MigrationItem(
                            "paradev:terminal:" + ident,
                            "process",
                            _digest(details),
                            self.project_id,
                            "running" if live == "alive" else "unconfirmed",
                            liveness=live,
                            details=details,
                        )
                    )
            automations = {}
            for archived in (False, True):
                rows = host.cli(["automations", "list", *(["--archived"] if archived else [])])
                if not isinstance(rows, list):
                    raise MigrationError("Native automation list is not a complete array")
                if len(rows) > self.max_items:
                    raise MigrationError("Native organization automation observation exceeds bound")
                for row in rows:
                    if not isinstance(row, dict):
                        raise MigrationError("Native automation schema unsupported")
                    if row.get("v2ProjectId") != self.project_id:
                        continue
                    if (
                        not isinstance(row.get("id"), str)
                        or not row["id"]
                        or not row.get("updatedAt")
                    ):
                        raise MigrationError("Native automation lacks exact ID/revision")
                    details = {
                        key: row.get(key)
                        for key in (
                            "id",
                            "v2ProjectId",
                            "v2WorkspaceId",
                            "agent",
                            "targetHostId",
                            "enabled",
                            "archivedAt",
                            "updatedAt",
                            "continueAgentSession",
                            "rrule",
                            "triggerCount",
                        )
                    }
                    details.update(
                        admissions_open=None,
                        fence_revision=None,
                        active_workers=None,
                        admission_fence_readback=False,
                        worker_drain_readback=False,
                    )
                    if row["id"] in automations and automations[row["id"]] != details:
                        raise MigrationError(
                            "Native automation changed during archive reconciliation"
                        )
                    automations[row["id"]] = details
            for ident, details in automations.items():
                items.append(
                    MigrationItem(
                        "paradev:automation:" + ident,
                        "automation",
                        _digest(details),
                        self.project_id,
                        (
                            "archived"
                            if details["archivedAt"]
                            else "enabled" if details["enabled"] else "disabled"
                        ),
                        details=details,
                    )
                )
            if len(items) > self.max_items:
                raise MigrationError("Native project ownership inventory exceeds bound")
        except Exception as exc:
            faults.append("Native ownership observation unavailable: " + type(exc).__name__)
        return ServiceInventory(
            "paradev-native:" + self.project_id,
            str(common_dir.resolve()),
            now,
            _digest([dataclasses.asdict(item) for item in items]),
            tuple(items),
            complete=not faults,
            faults=tuple(faults),
        )


def native_inventory(project, spec, *, gateway=None, campaign_root=None, host=None, observers=()):
    """Concrete migration inventory route, retaining unsupported fence evidence."""
    selected = tuple(observers)
    if spec.paradev.project_id:
        selected += (NativeServiceObserver(spec.paradev.project_id, host=host),)
    return inventory(project, gateway=gateway, campaign_root=campaign_root, observers=selected)


def qualify_native_migration(
    project,
    spec,
    manifest,
    *,
    replacement,
    gateway=None,
    campaign_root=None,
    host=None,
    observers=(),
    observed=None,
):
    # The manifest binds the exact trusted observation presented to its owner;
    # do not silently substitute a differently timestamped inventory digest.
    observed = observed or native_inventory(
        project, spec, gateway=gateway, campaign_root=campaign_root, host=host, observers=observers
    )
    if Path(observed.project).resolve() != Path(project).resolve() or Path(
        observed.common_dir
    ).resolve() != _common(Path(project)):
        raise MigrationError("Native migration observation belongs to another repository")
    return observed, validate_manifest(observed, manifest, replacement)
