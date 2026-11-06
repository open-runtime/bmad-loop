"""Native workspace reflection, distinct from worker execution and completion.

Allocation intents are written before calling Paradev. A lost create response is
reconciled by project, exact branch and unique action tag; absence never authorizes
a duplicate create. No command here spawns an agent. Git identity is checked before
an engine receives the checkout. Campaign state remains the hierarchy authority.
"""

from __future__ import annotations

import hashlib
import os
import re
import time
import unicodedata
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable

from .adapters.paradev_backend import ParadevMultiplexer
from .adapters.paradev_host import Host
from .adapters.paradev_runtime import WORKSPACE_KEY, private_json, read_private_json
from .platform_util import file_lock
from .verify import git_bytes


class ReflectionError(RuntimeError):
    """Reflection is unavailable, conflicting or has an uncertain mutation."""


@dataclass(frozen=True)
class WorkspaceBinding:
    unit_id: str
    action_id: str
    workspace_id: str
    project_id: str
    path: str
    branch: str
    common_dir: str
    git_dir: str
    group: str
    task_id: str | None = None

    def worker_env(self) -> dict[str, str]:
        return {WORKSPACE_KEY: self.workspace_id}


def _git_path(repo: Path, flag: str) -> Path:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    result = git_bytes(repo, "rev-parse", flag, timeout_s=10, env=env)
    if result.returncode:
        raise ReflectionError("Cannot verify workspace Git identity")
    path = Path(os.fsdecode(result.stdout.removesuffix(b"\n")))
    return (repo / path).resolve() if not path.is_absolute() else path.resolve()


def _tags(row: dict) -> list[str]:
    tags = row.get("tags", [])
    if isinstance(tags, str):
        # The local host persists a comma-delimited string.
        return [v.strip() for v in tags.split(",") if v.strip()]
    if not isinstance(tags, list) or any(not isinstance(v, str) for v in tags):
        raise ReflectionError("Workspace tags have an unknown schema")
    return tags


class ParadevWorkspaceProvider:
    """Registered, project-correct checkouts under an explicitly allowed root.

    ``state_dir`` is outside worktrees. The caller persists the returned binding
    in campaign state too. Calls are synchronous bounded I/O; supervisors run them
    through their bounded executor. Allocation never changes project settings.
    """

    def __init__(
        self,
        *,
        project_id: str,
        allowed_root: Path,
        repository: Path,
        state_dir: Path,
        host: Any = None,
        clock: Callable[[], float] = time.time,
        registration_window_s: float = 30,
    ):
        if not allowed_root.is_absolute() or not state_dir.is_absolute():
            raise ReflectionError("Workspace and reflection state roots must be absolute")
        self.project_id, self.allowed_root = project_id, allowed_root.resolve()
        self.repository, self.root = repository.resolve(), state_dir.resolve()
        if self.root.is_relative_to(self.repository):
            raise ReflectionError("Reflection state must be outside worker editing trees")
        self.host = host or Host()
        self.clock, self.registration_window_s = clock, registration_window_s

    def _rows(self, procedure: str) -> list[dict]:
        try:
            rows = self.host.rpc(procedure, {} if procedure == "workspace.list" else None)
        except Exception as exc:
            raise ReflectionError(
                f"{procedure} observation unavailable ({type(exc).__name__})"
            ) from None
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise ReflectionError(f"{procedure} schema is unsupported")
        return rows

    def preflight(self) -> dict:
        """Read capabilities without allocating a workspace or native session."""
        projects = [
            p
            for p in self._rows("project.list")
            if p.get("id") == self.project_id and not p.get("deletedAt")
        ]
        if len(projects) != 1:
            raise ReflectionError("Paradev project is missing or ambiguous")
        project = projects[0]
        base = project.get("worktreeBaseDir")
        if not isinstance(base, str) or not Path(base).is_absolute():
            raise ReflectionError("Project has no explicit absolute worktree base")
        configured = Path(base).resolve()
        if not configured.is_relative_to(self.allowed_root):
            # The native host appends its normalized project folder to this
            # shared base. Resolve that exact layout before authorizing effects.
            def folder(name):
                replaced = "".join(
                    "-" if c in '<>:"/\\|?*' or ord(c) < 32 else c
                    for c in unicodedata.normalize("NFC", name)
                )
                value = re.sub(r"\s+", " ", replaced)[:80].strip(". ")
                if re.match(r"^(con|prn|aux|nul|com[0-9]|lpt[0-9])(\..*)?$", value, re.I):
                    value = "_" + value
                return value

            name = folder(project.get("name", ""))
            others = [p for p in self._rows("project.list") if p.get("id") != self.project_id]
            if any(folder(p.get("name", "")).lower() == name.lower() for p in others):
                name += "-" + self.project_id[:8]
            derived = (configured / (name or self.project_id)).resolve()
            if derived != self.allowed_root:
                raise ReflectionError("Project worktree base is outside the allowed root")
        registered = project.get("repoPath")
        if not isinstance(registered, str) or _git_path(
            Path(registered), "--git-common-dir"
        ) != _git_path(self.repository, "--git-common-dir"):
            raise ReflectionError("Paradev project belongs to a different repository")
        self._rows("workspace.list")
        daemon = self.host.rpc("terminal.daemon.listSessions")
        if not isinstance(daemon, list):
            raise ReflectionError("Native terminal daemon schema is unsupported")
        return {
            "ready": True,
            "project_id": self.project_id,
            "worktree_root": base,
            "native_binding": "read-back-required",
            "platforms": ["darwin"],
            "tasks": "optional",
            "automatic_peer_delivery": False,
        }

    def _path(self, action_id: str) -> Path:
        return (
            self.root / "allocations" / (hashlib.sha256(action_id.encode()).hexdigest() + ".json")
        )

    def _verify(self, row: dict, intent: dict) -> WorkspaceBinding:
        if row.get("projectId") != self.project_id or row.get("branch") != intent["branch"]:
            raise ReflectionError(
                "Workspace project or returned branch differs from allocation intent"
            )
        raw = row.get("worktreePath")
        if not isinstance(raw, str) or not Path(raw).is_absolute():
            raise ReflectionError("Workspace has no absolute checkout path")
        path = Path(raw).resolve()
        if not path.is_relative_to(self.allowed_root) or path == self.repository:
            raise ReflectionError("Workspace checkout is outside the allowed worktree root")
        if row.get("archivedAt") or row.get("worktreeExists") is False:
            raise ReflectionError("Workspace checkout is archived or absent")
        if _git_path(path, "--show-toplevel") != path:
            raise ReflectionError("Workspace checkout is not its Git top-level")
        common, git_dir = _git_path(path, "--git-common-dir"), _git_path(path, "--absolute-git-dir")
        if common != _git_path(self.repository, "--git-common-dir") or git_dir == _git_path(
            self.repository, "--absolute-git-dir"
        ):
            raise ReflectionError("Workspace has a foreign or shared per-worktree Git identity")
        branch = git_bytes(
            path,
            "symbolic-ref",
            "--short",
            "HEAD",
            timeout_s=10,
            env={k: v for k, v in os.environ.items() if not k.startswith("GIT_")},
        )
        if branch.returncode or os.fsdecode(branch.stdout).strip() != intent["branch"]:
            raise ReflectionError("Workspace branch changed during allocation")
        return WorkspaceBinding(
            intent["unit_id"],
            intent["action_id"],
            row["id"],
            self.project_id,
            str(path),
            intent["branch"],
            str(common),
            str(git_dir),
            intent["group"],
        )

    def adopt(
        self,
        unit_id: str,
        branch: str,
        workspace_id: str,
        path: str | Path,
        action_id: str,
        *,
        group: str = "",
    ) -> WorkspaceBinding:
        """Link an explicitly qualified existing workspace without native writes.

        The caller separately proves ownership release through its trusted handoff
        observer. Registration/Git identity alone is never that permission.
        """
        self.preflight()
        requested = Path(path)
        if not requested.is_absolute() or not workspace_id:
            raise ReflectionError("Workspace adoption requires exact UUID and absolute path")
        intent = {
            "schema_version": 1,
            "mode": "adopt",
            "state": "confirmed",
            "unit_id": unit_id,
            "branch": branch,
            "workspace_id": workspace_id,
            "path": str(requested.resolve()),
            "action_id": action_id,
            "project_id": self.project_id,
            "group": group or unit_id,
            "tag": "",
        }
        rows = [
            row
            for row in self._rows("workspace.list")
            if row.get("id") == workspace_id and row.get("projectId") == self.project_id
        ]
        if (
            len(rows) != 1
            or not isinstance(rows[0].get("worktreePath"), str)
            or Path(rows[0]["worktreePath"]).resolve() != requested.resolve()
        ):
            raise ReflectionError("Explicit workspace UUID/project/path registration differs")
        binding = self._verify(rows[0], intent)
        record = {**intent, "binding": asdict(binding)}
        with file_lock(self.root / "allocation.lock"):
            target = self._path(action_id)
            if target.exists() and read_private_json(target) != record:
                raise ReflectionError("Workspace adoption action/Git identity changed")
            private_json(target, record)
        return binding

    def allocate(
        self,
        unit_id: str,
        branch: str,
        name: str,
        action_id: str,
        *,
        group: str = "",
        target: str = "main",
    ) -> WorkspaceBinding:
        self.preflight()
        path = self._path(action_id)
        rows = self._rows("workspace.list")
        existing = False
        with file_lock(self.root / "allocation.lock"):
            existing = path.exists()
            if existing:
                prior = read_private_json(path)
                if any(
                    prior.get(k) != v
                    for k, v in {
                        "unit_id": unit_id,
                        "branch": branch,
                        "name": name,
                        "group": group or unit_id,
                        "project_id": self.project_id,
                        "target": target,
                    }.items()
                ):
                    raise ReflectionError(
                        "Workspace action identity reused for different allocation input"
                    )
            if not existing:
                if any(
                    r.get("projectId") == self.project_id and r.get("branch") == branch
                    for r in rows
                ):
                    raise ReflectionError(
                        "Branch already belongs to a workspace; explicit adoption is required"
                    )
                tag = "zz-bmad-action-" + hashlib.sha256(action_id.encode()).hexdigest()[:24]
                intent = {
                    "schema_version": 1,
                    "state": "sent_unknown",
                    "unit_id": unit_id,
                    "branch": branch,
                    "name": name,
                    "action_id": action_id,
                    "project_id": self.project_id,
                    "tag": tag,
                    "group": group or unit_id,
                    "target": target,
                }
                private_json(path, intent)
        if existing:
            return self.reconcile(action_id)
        try:
            self.host.cli(
                [
                    "workspaces",
                    "create",
                    "--local",
                    "--project",
                    self.project_id,
                    "--checkout",
                    "worktree",
                    "--name",
                    name,
                    "--branch",
                    branch,
                    "--base-branch",
                    target,
                    "--skip-branch-prefix",
                    "--tag",
                    tag,
                ]
            )
        except Exception as exc:
            raise ReflectionError(
                f"Workspace creation outcome unknown ({type(exc).__name__}); reconcile action {action_id}"
            ) from None
        return self.reconcile(action_id)

    def reconcile(self, action_id: str) -> WorkspaceBinding:
        path = self._path(action_id)
        intent = read_private_json(path)
        pinned = intent.get("binding") if intent.get("state") == "confirmed" else None
        matches = [
            r
            for r in self._rows("workspace.list")
            if r.get("projectId") == self.project_id
            and (r.get("id") == pinned["workspace_id"] if pinned else intent["tag"] in _tags(r))
        ]
        if len(matches) != 1:
            raise ReflectionError(
                "Workspace create remains uncertain or has conflicting matches; no replacement launched"
            )
        binding = self._verify(matches[0], intent)
        if pinned and asdict(binding) != pinned:
            raise ReflectionError("Pinned workspace UUID has a different Git/worktree identity")
        intent.update(state="confirmed", binding=asdict(binding))
        private_json(path, intent)
        # The temporary create marker is needed only until this exact UUID and
        # per-worktree Git identity are durable. Native tags are visible sidebar
        # folders; internal action IDs must not remain as product navigation.
        row = matches[0]
        if intent["tag"] in _tags(row):
            tags = [tag for tag in _tags(row) if tag != intent["tag"]]
            fresh = [r for r in self._rows("workspace.list") if r.get("id") == binding.workspace_id]
            if len(fresh) != 1 or _tags(fresh[0]) != _tags(row):
                raise ReflectionError("Workspace tags changed before temporary marker removal")
            args = ["workspaces", "update", binding.workspace_id, "--local"]
            for tag in tags or [""]:
                args += ["--tag", tag]
            failure = None
            try:
                self.host.cli(args)
            except Exception as exc:
                failure = type(exc).__name__
            after = [r for r in self._rows("workspace.list") if r.get("id") == binding.workspace_id]
            if len(after) != 1 or set(_tags(after[0])) != set(tags):
                raise ReflectionError(
                    "Temporary create marker removal unconfirmed"
                    + (": " + failure if failure else "")
                )
        return binding

    def reflect(
        self,
        binding: WorkspaceBinding,
        *,
        campaign_id: str,
        epic: str,
        phase: str,
        attempt: int,
        pr_url: str = "",
        held: bool = False,
    ) -> dict:
        """Reconcile one sortable managed folder while retaining operator tags.

        Local Paradev folders are flat and choose the first sorted tag. A single
        canonical story/epic tag is therefore used for the managed hierarchy;
        complete ancestry and links remain in campaign metadata, not fake folders.
        """
        rows = [r for r in self._rows("workspace.list") if r.get("id") == binding.workspace_id]
        if len(rows) != 1:
            raise ReflectionError("Bound workspace disappeared; execution identity remains owned")
        row = rows[0]
        observed_binding = self._verify(
            row,
            {
                "unit_id": binding.unit_id,
                "action_id": binding.action_id,
                "branch": binding.branch,
                "group": binding.group,
            },
        )
        if (
            observed_binding.git_dir != binding.git_dir
            or observed_binding.common_dir != binding.common_dir
        ):
            raise ReflectionError("Workspace per-worktree identity changed after allocation")
        prefix = f"bmad-{campaign_id}-"
        folder = prefix + binding.group.replace(",", "-")
        tags = [t for t in _tags(row) if not t.startswith(prefix)]
        tags.append(folder)
        if set(tags) != set(_tags(row)):
            # Replacement-only host API: read back and refuse concurrent human
            # changes instead of repeatedly erasing them.
            fresh = next(
                r for r in self._rows("workspace.list") if r.get("id") == binding.workspace_id
            )
            if _tags(fresh) != _tags(row):
                raise ReflectionError("Workspace tags changed during reflection")
            args = ["workspaces", "update", binding.workspace_id, "--local"]
            for tag in tags:
                args += ["--tag", tag]
            self.host.cli(args)
            observed = next(
                r for r in self._rows("workspace.list") if r.get("id") == binding.workspace_id
            )
            if set(_tags(observed)) != set(tags):
                raise ReflectionError("Workspace tag update unconfirmed")
        metadata = {
            "schema_version": 1,
            **asdict(binding),
            "campaign_id": campaign_id,
            "epic": epic,
            "phase": phase,
            "attempt": attempt,
            "pr_url": pr_url,
            "held": held,
            "folder": folder,
            "competing_tags": [
                t for t in tags if t != folder and not t.startswith("zz-bmad-action-")
            ],
            "folder_selection": "confirmed" if min(tags) == folder else "operator-tag-precedence",
        }
        private_json(self.root / "workspaces" / f"{binding.workspace_id}.json", metadata)
        return metadata

    def bind_terminal(
        self,
        binding: WorkspaceBinding,
        mux: ParadevMultiplexer,
        terminal_id: str,
        *,
        phase: str,
        attempt: int,
        agent: str,
    ) -> dict:
        row = mux._address(terminal_id)
        if row["workspace"] != binding.workspace_id or Path(row["cwd"]).resolve() != Path(
            binding.path
        ):
            raise ReflectionError("Worker terminal belongs to another workspace")
        mux.set_display_title(terminal_id, f"{binding.unit_id} · {phase} · {attempt} · {agent}")
        pane = mux._ensure_pane(terminal_id, binding.workspace_id)
        native = mux.native_identity(terminal_id)
        record_path = self.root / "terminals" / f"{terminal_id}.json"
        old = read_private_json(record_path) if record_path.exists() else {}
        first = old.get("first_observed_at", self.clock())
        attributed = native and native.get("agentSessionId")
        attribution = (
            "confirmed"
            if attributed
            else "pending" if self.clock() - first < self.registration_window_s else "unknown"
        )
        result = {
            "schema_version": 1,
            "workspace_id": binding.workspace_id,
            "terminal_id": terminal_id,
            "pane_id": pane,
            "phase": phase,
            "attempt": attempt,
            "native": native,
            "first_observed_at": first,
            "attribution": attribution,
            "detail": (
                "Native registration not confirmed within its bounded window"
                if attribution == "unknown"
                else ""
            ),
        }
        private_json(record_path, result)
        return result

    def history_view(self, binding: WorkspaceBinding, log: Path) -> dict:
        """Project retained output into the native editor's workspace confinement.

        The durable source log remains untouched. The native editor cannot open
        external run state, so its full bounded copy is ignored locally and can
        never enter the product PR. No PTY or coding agent is created.
        """
        from .install import _worktree_local_exclude
        from .platform_util import atomic_write_bytes_confined, is_link_like

        if not log.is_file():
            raise ReflectionError("Retained output is unavailable")
        if is_link_like(log) or log.stat().st_size > 16 * 1024 * 1024:
            raise ReflectionError("Retained output is redirected or exceeds native view bound")
        rows = [
            row for row in self._rows("workspace.list") if row.get("id") == binding.workspace_id
        ]
        if len(rows) != 1:
            raise ReflectionError("History workspace is missing or ambiguous")
        actual = self._verify(
            rows[0],
            {
                "unit_id": binding.unit_id,
                "action_id": binding.action_id,
                "branch": binding.branch,
                "group": binding.group,
            },
        )
        if actual.path != binding.path or actual.git_dir != binding.git_dir:
            raise ReflectionError("History workspace Git identity changed")
        workspace = Path(binding.path)
        relative = (
            ".bmad-loop/campaign-history/"
            + hashlib.sha256(str(log.resolve()).encode()).hexdigest()
            + ".log"
        )
        tracked = git_bytes(workspace, "ls-files", "-z", "--", ":(literal)" + relative)
        if tracked.returncode or tracked.stdout:
            raise ReflectionError(
                "Native history projection cannot overwrite tracked product content"
            )
        refusal = _worktree_local_exclude(workspace, ["/.bmad-loop/campaign-history/"])
        if refusal:
            raise ReflectionError("Cannot exclude native history projection: " + refusal)
        target = workspace / relative
        for ancestor in (workspace / ".bmad-loop", target.parent):
            if is_link_like(ancestor) or (ancestor.exists() and not ancestor.is_dir()):
                raise ReflectionError(
                    "Native history projection parent is redirected or non-directory"
                )
        data = log.read_bytes()
        projection = (
            self.root
            / "history-views"
            / (hashlib.sha256((binding.workspace_id + relative).encode()).hexdigest() + ".json")
        )
        if projection.exists():
            receipt = read_private_json(projection)
            if (
                receipt["workspace_id"] != binding.workspace_id
                or receipt["source"] != str(log.resolve())
                or receipt["sha256"] != hashlib.sha256(data).hexdigest()
            ):
                raise ReflectionError(
                    "Native history projection authority or completed source changed"
                )
            if (
                target.exists()
                and hashlib.sha256(target.read_bytes()).hexdigest() != receipt["sha256"]
            ):
                raise ReflectionError("Owner edited native history projection; preserve its bytes")
        else:
            if target.exists():
                raise ReflectionError("Native history projection already exists without ownership")
            private_json(
                projection,
                {
                    "schema_version": 1,
                    "workspace_id": binding.workspace_id,
                    "source": str(log.resolve()),
                    "target": relative,
                    "sha256": hashlib.sha256(data).hexdigest(),
                },
            )
        target.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_bytes_confined(
            target, data, confine_root=workspace, require_writable_target=True
        )
        return self.host.cli(
            [
                "panes",
                "open",
                "file",
                str(target),
                "--workspace",
                binding.workspace_id,
                "--background",
            ]
        )

    def reflect_task(
        self,
        binding: WorkspaceBinding,
        *,
        campaign_id: str,
        title: str,
        state: str,
        links: tuple[str, ...] = (),
        status_ids: dict[str, str] | None = None,
    ) -> str:
        """Optional flat task projection with durable create uncertainty.

        Only status and PR link are subsequently managed; operator text and labels
        are never overwritten. The caller supplies an explicit status mapping for
        held/blocked states when the installed tracker has no such native status.
        """
        action = f"task:{campaign_id}:{binding.unit_id}"
        path = self.root / "tasks" / (hashlib.sha256(action.encode()).hexdigest() + ".json")
        marker = "[bmad-unit:" + hashlib.sha256(action.encode()).hexdigest() + "]"
        with file_lock(self.root / "task.lock"):
            intent = read_private_json(path) if path.exists() else None
            if intent is None:
                intent = {
                    "schema_version": 1,
                    "action_id": action,
                    "state": "sent_unknown",
                    "marker": marker,
                    "unit_id": binding.unit_id,
                }
                private_json(path, intent)
                try:
                    created = self.host.cli(
                        [
                            "tasks",
                            "create",
                            "--tracker",
                            "superset",
                            "--title",
                            title,
                            "--description",
                            marker + "\n" + "\n".join(links),
                        ]
                    )
                except Exception as exc:
                    raise ReflectionError(
                        f"Task creation outcome unknown ({type(exc).__name__}); reconcile before retry"
                    ) from None
                if not isinstance(created, dict) or not isinstance(created.get("id"), str):
                    raise ReflectionError("Task creation acknowledgement has unknown schema")
                intent.update(state="confirmed", task_id=created["id"])
                private_json(path, intent)
            if intent["state"] != "confirmed":
                matches_by_id = {}
                statuses = self.host.cli(["tasks", "statuses", "list", "--tracker", "superset"])
                if not isinstance(statuses, list):
                    raise ReflectionError("Task status schema unsupported")
                for status in statuses:
                    for offset in range(0, 1000, 100):
                        rows = self.host.cli(
                            [
                                "tasks",
                                "list",
                                "--tracker",
                                "superset",
                                "--status",
                                status["id"],
                                "--search",
                                marker,
                                "--limit",
                                "100",
                                "--offset",
                                str(offset),
                            ]
                        )
                        if not isinstance(rows, list):
                            raise ReflectionError("Task search schema unsupported")
                        for row in rows:
                            if marker in (row.get("description") or ""):
                                matches_by_id[row["id"]] = row
                        if len(rows) < 100:
                            break
                    else:
                        raise ReflectionError("Task reconciliation search truncated")
                matches = list(matches_by_id.values())
                if len(matches) != 1:
                    raise ReflectionError(
                        "Task create remains uncertain or ambiguous; no duplicate created"
                    )
                intent.update(state="confirmed", task_id=matches[0]["id"])
                private_json(path, intent)
            statuses = self.host.cli(["tasks", "statuses", "list", "--tracker", "superset"])
            if not isinstance(statuses, list):
                raise ReflectionError("Task status schema unsupported")
            mapping = {
                "pending": "backlog",
                "ready": "unstarted",
                "running": "started",
                "reviewing": "started",
                "merged_pending_sync": "started",
                "delivered": "completed",
                "canceled": "canceled",
            }
            status_id = (status_ids or {}).get(state)
            if status_id is None:
                choices = [row for row in statuses if row.get("type") == mapping.get(state)]
                if not choices:
                    raise ReflectionError(
                        f"No native status mapping for {state}; task remains visibly pending projection"
                    )
                status_id = min(choices, key=lambda row: (row.get("position", 0), row["id"]))["id"]
            if not any(row.get("id") == status_id for row in statuses):
                raise ReflectionError("Configured native task status is absent")
            task_id = intent["task_id"]
            self.host.cli(
                ["tasks", "update", task_id, "--tracker", "superset", "--status-id", status_id]
            )
            current = next(
                (
                    row
                    for row in self._rows("workspace.list")
                    if row.get("id") == binding.workspace_id
                ),
                None,
            )
            if current is None:
                raise ReflectionError("Task workspace disappeared")
            if current.get("taskId") not in (None, "", task_id):
                raise ReflectionError(
                    "Operator linked a different task; automatic relinking refused"
                )
            if current.get("taskId") != task_id:
                self.host.cli(
                    ["workspaces", "update", binding.workspace_id, "--local", "--task-id", task_id]
                )
            return task_id
