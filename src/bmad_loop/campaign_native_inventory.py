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
import re
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

    def __init__(
        self,
        project_id: str,
        *,
        host=None,
        max_items=5000,
        clock=time.time,
        archived_workspaces=(),
    ):
        if not project_id or max_items < 1:
            raise ValueError("Exact native project ID and positive observation bound required")
        self.project_id, self.host, self.max_items, self.clock = project_id, host, max_items, clock
        self.archived_workspaces = tuple(dict.fromkeys(archived_workspaces))
        if len(self.archived_workspaces) > max_items or any(
            not isinstance(value, str) or not value for value in self.archived_workspaces
        ):
            raise ValueError("Archived workspace identities must be bounded and explicit")

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
            # The ordinary list can omit archived objects. Transfer observation
            # explicitly re-reads only its selected historical UUIDs, and includes
            # their terminals instead of treating archive state as proof of death.
            for identity in self.archived_workspaces:
                archived = host.rpc("workspace.get", {"id": identity})
                if (
                    not isinstance(archived, dict)
                    or archived.get("id") != identity
                    or archived.get("projectId") != self.project_id
                ):
                    raise MigrationError("Selected archived workspace identity differs")
                workspaces = [row for row in workspaces if row.get("id") != identity]
                workspaces.append(archived)
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
                        "archiveReason",
                        "worktreeExists",
                        "headSha",
                        "tags",
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
                if row.get("archivedAt") and row["id"] not in self.archived_workspaces:
                    continue
                listing = host.rpc("terminal.list", {"workspaceId": row["id"]})
                terminals = listing.get("sessions") if isinstance(listing, dict) else None
                if not isinstance(terminals, list):
                    raise MigrationError("Native terminal inventory schema unsupported")
                details["terminal_inventory_observed"] = True
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
            for ident, actual in ptys.items():
                if (
                    ident not in seen_terminals
                    and actual.get("workspaceId") in self.archived_workspaces
                ):
                    raise MigrationError("Archived workspace has an unbound native daemon session")
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


class CampaignTransferObserver:
    """Observe exact campaign releases and current scoped native ownership.

    Permanent predecessor retirement is database authority. Native absence is
    only a current safety check; it never creates or changes that authority.
    """

    def __init__(self, snapshot, *, store=None, host=None, clock=time.time):
        self.snapshot, self.store, self.host, self.clock = snapshot, store, host, clock

    def __call__(self, project, common_dir):
        from .campaign_migration import _native_items
        from .campaign_runtime import store_for
        from .campaign_transfer import archived_workspace_ids, read_release

        store = self.store or store_for(project)
        entries = sorted(store.root.iterdir()) if store.root.exists() else []
        if len(entries) > 5000:
            raise MigrationError("campaign transfer inventory exceeds bound")
        records, count = {}, 0
        for directory in entries:
            if not (directory / "snapshot.json").exists():
                continue
            for item in _native_items(
                directory, common_dir, max_bytes=4 * 1024 * 1024, max_items=5000 - count
            ):
                count += 1
                candidate = (item.details or {}).get("ownership_transfer") or {}
                if candidate.get("successor") != self.snapshot.id:
                    continue
                record = read_release(store, candidate["predecessor"], candidate["unit"])
                if record != candidate or record["successor_unit"] not in self.snapshot.units:
                    raise MigrationError(
                        "campaign transfer projection differs from durable release"
                    )
                if record["successor_unit"] in records:
                    raise MigrationError("multiple predecessors claim the same recipient unit")
                records[record["successor_unit"]] = record
        predecessors = {
            record["predecessor"]: store.load(record["predecessor"]) for record in records.values()
        }
        observer = NativeServiceObserver(
            self.snapshot.spec.paradev.project_id,
            host=self.host,
            clock=self.clock,
            archived_workspaces=tuple(
                identity
                for previous in predecessors.values()
                for identity in archived_workspace_ids(previous)
            ),
        )
        native = observer(project, common_dir)
        actors = []
        for record in records.values():
            observation = validate_transfer_observation(
                record,
                predecessors[record["predecessor"]],
                self.snapshot,
                project=project,
                native_inventory=native,
                store=store,
            )
            details = {
                key: record[key]
                for key in (
                    "repository",
                    "pr_number",
                    "pr_node_id",
                    "head",
                    "base",
                    "branch",
                    "source_identity",
                    "binding_revision",
                    "fence_revision",
                )
            }
            details.update(
                source_unit=record["successor_unit"],
                workspace="",
                workspace_id="",
                preserved_clean=True,
                preservation_basis="exact committed startup anchor",
                resumable_binding_verified=True,
                admissions_open=False,
                active_workers=0,
                release=record,
                ownership_observation=observation,
            )
            actors.append(
                MigrationItem(
                    "campaign-transfer:" + record["id"],
                    "publisher",
                    record["id"],
                    record["predecessor"],
                    "released",
                    "dead",
                    "unowned",
                    (record["successor_unit"],),
                    details,
                )
            )
        return ServiceInventory(
            "campaign-transfer",
            str(common_dir.resolve()),
            native.observed_at,
            _digest({"native": native.revision, "records": records}),
            tuple(actors),
            native.complete,
            native.faults,
        )


def validate_transfer_observation(
    record, predecessor, successor, *, project, native_inventory, store=None
):
    """Prove current exclusive archived-branch ownership, without releasing it.

    The transfer core separately earns the permanent predecessor admission
    fence and committed source/PR preservation. Native archive labels alone
    never establish worker quiescence or permission to reuse a branch.
    """
    from .campaign_migration import _native_items
    from .campaign_model import CampaignError, scopes_overlap
    from .campaign_transfer import archived_workspace_ids
    from .platform_util import is_link_like

    common = Path(record["common_dir"]).resolve()
    if store is None:
        from .campaign_runtime import store_for

        store = store_for(Path(project))
    native = record["predecessor_bindings"].get("native") or {}
    old_id, old_path = native.get("workspace_id"), native.get("path")
    unit = successor.units[record["successor_unit"]]
    if (
        not isinstance(native_inventory, ServiceInventory)
        or not native_inventory.complete
        or native_inventory.faults
        or not native_inventory.revision
        or Path(native_inventory.common_dir).resolve() != common
        or _common(Path(project)) != common
        or native.get("project_id") != successor.spec.paradev.project_id
        or not old_id
        or not isinstance(old_path, str)
        or not Path(old_path).is_absolute()
    ):
        raise CampaignError("transfer native scope observation is incomplete or foreign")
    old_rows = [
        row
        for row in native_inventory.items
        if row.kind == "workspace" and (row.details or {}).get("id") == old_id
    ]
    if len(old_rows) != 1:
        raise CampaignError("transfer requires exact archived workspace readback")
    old = old_rows[0].details or {}
    old_ids = set(archived_workspace_ids(predecessor))
    own = _transfer_allocation(store, successor, unit, record, native_inventory)
    own_id = own.get("workspace_id")
    reused_path = own_id and own.get("path") == old_path
    if (
        old.get("projectId") != native["project_id"]
        or old.get("branch") != record["branch"]
        or old.get("worktreePath") != old_path
        or not old.get("archivedAt")
        or old.get("terminal_inventory_observed") is not True
        or old.get("archiveReason") != "deleted"
        or (old.get("worktreeExists") is not False and not reused_path)
        or old.get("headSha") != record["head"]
        or is_link_like(Path(old_path))
        or (Path(old_path).exists() and not reused_path)
    ):
        raise CampaignError("archived transfer workspace or preserved head changed")
    for previous_unit in predecessor.units.values():
        previous_native = previous_unit.bindings.get("native") or {}
        identity = previous_native.get("workspace_id")
        if not identity or identity == old_id:
            continue
        other_release = (
            predecessor.bindings.get("ownership_transfers", {}).get(previous_unit.id) or {}
        )
        other_own = {}
        if (
            other_release.get("successor") == successor.id
            and other_release.get("successor_unit") in successor.units
        ):
            other_own = _transfer_allocation(
                store,
                successor,
                successor.units[other_release["successor_unit"]],
                other_release,
                native_inventory,
            )
        other_path = Path(previous_native["path"])
        reused_other = other_own.get("path") == str(other_path)
        rows = [
            row.details or {}
            for row in native_inventory.items
            if row.kind == "workspace" and (row.details or {}).get("id") == identity
        ]
        if (
            len(rows) != 1
            or any(
                rows[0].get(key) != value
                for key, value in {
                    "projectId": previous_native["project_id"],
                    "worktreePath": previous_native["path"],
                    "branch": previous_native["branch"],
                    "headSha": previous_unit.bindings["head"],
                    "archiveReason": "deleted",
                    "terminal_inventory_observed": True,
                }.items()
            )
            or not rows[0].get("archivedAt")
            or is_link_like(other_path)
            or (
                (rows[0].get("worktreeExists") is not False or other_path.exists())
                and not reused_other
            )
        ):
            raise CampaignError(
                "retired predecessor allocation lacks exact archive/terminal coverage"
            )
    scoped = []
    peer_target = None
    own_terminals = None
    for row in native_inventory.items:
        detail = row.details or {}
        workspace = detail.get("id") if row.kind == "workspace" else detail.get("workspace_id")
        if row.kind == "workspace":
            if workspace in old_ids:
                scoped.append(row.id)
                continue
            if workspace == own_id:
                if (
                    detail.get("worktreePath") != own.get("path")
                    or detail.get("branch") != record["branch"]
                    or detail.get("archivedAt")
                ):
                    raise CampaignError("successor native allocation identity changed")
                scoped.append(row.id)
                continue
            if detail.get("branch") == record["branch"] or detail.get("worktreePath") == old_path:
                raise CampaignError("another native workspace owns the preserved branch")
            if not detail.get("archivedAt"):
                if peer_target is None:
                    peer_target = _transfer_target(Path(project), record)
                if _transfer_workspace_overlap(
                    detail, {**record, "base": peer_target}, unit.spec.scopes
                ):
                    raise CampaignError("another native workspace retains overlapping work")
        elif row.kind == "process" and workspace in old_ids | {own_id}:
            if row.liveness != "dead":
                if workspace == own_id:
                    if own_terminals is None:
                        own_terminals = _transfer_owned_terminals(store, successor, unit)
                    if (
                        type(detail.get("pty_pid")) is int
                        and detail["pty_pid"] > 0
                        and own_terminals.get(detail.get("terminal_id")) == detail["pty_pid"]
                    ):
                        scoped.append(row.id)
                        continue
                raise CampaignError("transfer scope has a live or uncertain native actor")
            scoped.append(row.id)
        elif row.kind == "automation" and detail.get("v2WorkspaceId") in old_ids | {own_id}:
            raise CampaignError("transfer scope has automation authority without a release fence")
    entries = sorted(store.root.iterdir()) if store.root.exists() else []
    if len(entries) > 5000:
        raise CampaignError("transfer campaign scope inventory exceeds bound")
    count = 0
    for directory in entries:
        if not (directory / "snapshot.json").exists():
            continue
        for row in _native_items(
            directory, common, max_bytes=4 * 1024 * 1024, max_items=5000 - count
        ):
            count += 1
            detail = row.details or {}
            if detail.get("campaign_id") == predecessor.id:
                continue  # The core proves the entire predecessor is quiescent.
            if detail.get("campaign_id") == successor.id and detail.get("unit_id") == unit.id:
                continue
            overlap = (
                detail.get("branch") == record["branch"]
                or detail.get("workspace") == old_path
                or (
                    detail.get("workspace")
                    and scopes_overlap(unit.spec.scopes, tuple(detail.get("scopes") or ()))
                )
            )
            if overlap:
                raise CampaignError("another campaign retains the transferred ownership scope")
    return {
        "native_revision": native_inventory.revision,
        "observed_at": native_inventory.observed_at,
        "scoped_native_items": scoped,
        "campaign_items": count,
        "preservation": "exact committed startup anchor",
    }


def _transfer_target(project, record):
    """Observe current target ancestry without changing immutable release anchors."""
    from .campaign_model import CampaignError

    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    url = git_bytes(project, "remote", "get-url", record["remote"], timeout_s=10, env=env)
    if url.returncode or os.fsdecode(url.stdout).strip() != record["remote_url"]:
        raise CampaignError("transfer target remote identity changed")
    ref = "refs/heads/" + record["target"]
    result = git_bytes(
        project, "ls-remote", "--exit-code", record["remote"], ref, timeout_s=10, env=env
    )
    rows = os.fsdecode(result.stdout).splitlines()
    if result.returncode or len(rows) != 1 or len(rows[0].split()) != 2:
        raise CampaignError("transfer current target is unavailable or ambiguous")
    head, observed_ref = rows[0].split()
    if observed_ref != ref or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", head):
        raise CampaignError("transfer current target lacks immutable identity")
    ancestry = git_bytes(
        project, "merge-base", "--is-ancestor", record["base"], head, timeout_s=10, env=env
    )
    if ancestry.returncode:
        raise CampaignError(
            "transfer current target lost preserved ancestry or requires fresh Git objects"
        )
    return head


def _transfer_workspace_overlap(detail, record, scopes):
    """A paused checkout retains its scoped work even without a live terminal."""
    from .campaign_model import CampaignError, scopes_overlap

    path = detail.get("worktreePath")
    if not isinstance(path, str) or not Path(path).is_absolute():
        raise CampaignError("native workspace has no inspectable ownership path")
    workspace = Path(path)
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    paths = set()
    bases = git_bytes(
        workspace, "merge-base", "--all", record["base"], "HEAD", timeout_s=10, env=env
    )
    if bases.returncode or len(bases.stdout) > 65536:
        raise CampaignError("native workspace common ancestry is unavailable or unbounded")
    revisions = os.fsdecode(bases.stdout).splitlines()
    if not revisions or any(
        not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", value) for value in revisions
    ):
        raise CampaignError("native workspace common ancestry lacks immutable identities")
    for args in (
        *(("diff", "--name-only", "--no-renames", "-z", base, "HEAD") for base in revisions),
        ("diff", "--name-only", "--no-renames", "-z", "HEAD"),
        ("ls-files", "--others", "--exclude-standard", "-z"),
    ):
        result = git_bytes(workspace, *args, timeout_s=10, env=env)
        if result.returncode or len(result.stdout) > 4 * 1024 * 1024:
            raise CampaignError("native workspace ownership paths are unavailable or unbounded")
        paths.update(os.fsdecode(value) for value in result.stdout.split(b"\0") if value)
    return any(scopes_overlap(scopes, (path,)) for path in paths)


def _transfer_allocation(store, snapshot, unit, record, observed):
    """Authenticate owned or uncertain-but-journalled recipient allocation only."""
    from .adapters.paradev_runtime import read_private_json
    from .campaign_model import CampaignError
    from .campaign_paradev import ParadevWorkspaceProvider
    from .platform_util import is_link_like

    adoption = unit.bindings.get("adoption") or {}
    if not adoption:
        return {}
    manifest = adoption.get("manifest") or {}
    if (
        manifest.get("actor_id") != "campaign-transfer:" + record.get("id", "")
        or manifest.get("unit_id") != unit.id
        or manifest.get("campaign_id") != snapshot.id
    ):
        raise CampaignError("successor adoption names another release")
    provider = ParadevWorkspaceProvider(
        project_id=snapshot.spec.paradev.project_id,
        allowed_root=Path(snapshot.spec.paradev.worktree_root),
        repository=Path(snapshot.root),
        state_dir=store.directory(snapshot.id) / "reflection",
        host=object(),  # _verify uses only the freshly supplied native row + Git.
    )
    own = unit.bindings.get("native") or {}
    if own:
        if adoption.get("state") != "owned" or unit.bindings.get("branch") != record["branch"]:
            raise CampaignError("successor allocation is not bound to its owned adoption")
        rows = [
            row.details or {}
            for row in observed.items
            if row.kind == "workspace" and (row.details or {}).get("id") == own.get("workspace_id")
        ]
        if len(rows) != 1:
            raise CampaignError("successor native allocation is absent or ambiguous")
        checked = dataclasses.asdict(
            provider._verify(
                rows[0],
                {
                    "unit_id": unit.id,
                    "action_id": own["action_id"],
                    "branch": record["branch"],
                    "group": own["group"],
                },
            )
        )
        if any(checked[key] != own.get(key) for key in checked if key != "task_id"):
            raise CampaignError("successor native Git identity changed")
        return own
    actions = [
        action
        for action in snapshot.outbox
        if action.unit == unit.id
        and action.kind == "prepare-unit"
        and action.payload.get("adoption")
        and action.state in {"prepared", "sent_unknown"}
    ]
    if len(actions) > 1:
        raise CampaignError("successor has ambiguous adoption preparations")
    if not actions:
        return {}
    action = actions[0]
    path = provider._path(action.id)
    if is_link_like(path):
        raise CampaignError("successor allocation journal is redirected")
    if not path.exists():
        return {}
    intent = read_private_json(path)
    if any(
        intent.get(key) != value
        for key, value in {
            "mode": "preserved",
            "unit_id": unit.id,
            "action_id": action.id,
            "release_id": record.get("id"),
            "expected_head": record["head"],
            "project_id": snapshot.spec.paradev.project_id,
            "branch": record["branch"],
        }.items()
    ):
        raise CampaignError("successor allocation journal is not bound to the release")
    pinned = intent.get("binding")
    matches = []
    for row in observed.items:
        detail = row.details or {}
        if row.kind != "workspace":
            continue
        tags = detail.get("tags") or []
        if isinstance(tags, str):
            tags = [tag.strip() for tag in tags.split(",")]
        if detail.get("id") == pinned.get("workspace_id") if pinned else intent["tag"] in tags:
            matches.append(detail)
    if not matches:
        return {}  # A possibly submitted create remains uncertain; never replayed.
    if len(matches) != 1 or matches[0].get("id") == intent["archived_workspace_id"]:
        raise CampaignError("successor create receipt is ambiguous or restored the archived UUID")
    checked = dataclasses.asdict(provider._verify(matches[0], intent))
    if pinned and checked != pinned:
        raise CampaignError("successor pending allocation Git identity changed")
    return checked


def _transfer_owned_terminals(store, snapshot, unit):
    """Only exact current claim requests/windows can explain a successor actor."""
    from . import envvars
    from .adapters.paradev_runtime import read_private_json
    from .campaign_headless import digest, read_json
    from .campaign_model import CampaignError
    from .platform_util import is_link_like

    claim = unit.claim
    if claim is None or claim.quiescent or store is None:
        return {}
    root = store.directory(snapshot.id) / "workers" / claim.launch_id
    if Path(claim.run_dir) != root or claim.workspace != unit.bindings.get("workspace"):
        raise CampaignError("successor native claim directory/workspace differs")
    outer = read_json(root / "request.json")
    if any(
        outer.get(key) != value
        for key, value in {
            "campaign_id": snapshot.id,
            "unit_id": unit.id,
            "launch_id": claim.launch_id,
            "generation": claim.generation,
        }.items()
    ):
        raise CampaignError("successor native claim request differs")
    if not isinstance(outer.get("headless_coordinates"), dict) or not outer["headless_coordinates"]:
        raise CampaignError("successor native claim lacks frozen execution coordinates")
    transport = store.directory(snapshot.id) / "native-transport"
    if outer.get("reflection_environment", {}).get(envvars.PARADEV_STATE_DIR) != str(transport):
        raise CampaignError("successor native claim transport differs")

    def transport_record(path):
        if any(
            is_link_like(parent)
            for parent in (path, *path.parents)
            if parent == transport or transport in parent.parents
        ):
            raise CampaignError("successor native transport record is redirected")
        try:
            value = read_private_json(path)
        except (OSError, ValueError) as exc:
            raise CampaignError("successor native transport record is unavailable") from exc
        if not isinstance(value, dict):
            raise CampaignError("successor native transport record is malformed")
        return value

    paths = list((root / "native").glob("*/headless/*/request.json"))
    if len(paths) > 128:
        raise CampaignError("successor native claim window inventory exceeds bound")
    terminals = {}
    for path in paths:
        if any(
            is_link_like(parent)
            for parent in (path, *path.parents)
            if parent == root or root in parent.parents
        ):
            raise CampaignError("successor native claim record is redirected")
        request = read_json(path)
        window_path = path.parent / "window.json"
        if not window_path.exists():
            if is_link_like(window_path):
                raise CampaignError("successor native claim window is redirected")
            continue
        window = read_json(window_path)
        if (
            window.get("launch_id") != request.get("launch_id")
            or request.get("request_digest") != digest(request.get("request"))
            or request.get("generation") != claim.generation
            or request.get("request", {}).get("coordinates") != outer.get("headless_coordinates")
            or request.get("presentation", {}).get("workspace_id")
            != unit.bindings["native"]["workspace_id"]
            or not isinstance(window.get("native_id"), str)
            or not window["native_id"]
        ):
            raise CampaignError("successor native window lacks exact launch provenance")
        terminal = window["native_id"]
        if "/" in terminal or "\\" in terminal or terminal in {".", ".."}:
            raise CampaignError("successor native terminal identity is invalid")
        registry = transport_record(transport / "windows" / (terminal + ".json"))
        launch_dir = transport / "launches" / terminal
        launch = transport_record(launch_dir / "launch.json")
        bootstrap = transport_record(launch_dir / "context.json")
        workspace = unit.bindings["native"]["workspace_id"]
        if (
            registry.get("schema_version") != 1
            or registry.get("id") != terminal
            or registry.get("workspace") != workspace
            or registry.get("state") not in {"active", "launching"}
            or not request.get("session")
            or registry.get("session") != request["session"]
            or not request.get("window_name")
            or registry.get("name") != request["window_name"]
            or launch.get("session") != request["session"]
            or launch.get("window") != request["window_name"]
            or any(
                item.get("terminalId") != terminal or item.get("workspaceId") != workspace
                for item in (launch, bootstrap)
            )
            or type(bootstrap.get("pid")) is not int
            or bootstrap["pid"] <= 0
            or any(
                path.exists() or is_link_like(path)
                for path in (launch_dir / "exit.json", launch_dir / "bootstrap-error.json")
            )
        ):
            raise CampaignError("successor native terminal bootstrap differs or ended")
        terminals[terminal] = bootstrap["pid"]
    return terminals


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
