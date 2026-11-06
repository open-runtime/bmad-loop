"""Worker-owned progress receipts and read-only checkpoint qualification.

Only the coding actor commits. A child-local monitor reads coherent Git/native
source observations and atomically publishes them outside the checkout. The
coordinator validates those facts before backing up an immutable commit; neither
side stages a live tree or treats dirty progress as remotely backed up.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from pathlib import Path
from typing import Any

from . import verify
from .campaign_action_history import action as historical_action
from .campaign_model import CampaignError, SourceSelector, UnitSpec
from .platform_util import atomic_write_text, is_link_like, safe_segment

NAME = "checkpoint-progress.json"
LIMIT = 2 * 1024 * 1024
IDENTITY = ("campaign_id", "unit_id", "attempt", "generation", "launch_id", "branch")


def owned_paths(workspace: Path, request: dict[str, Any]) -> list[str]:
    """Code ownership plus the selected private native artifact footprint."""
    from . import stories
    from .campaign_sources import source_paths

    unit = UnitSpec(**request["unit"])
    code = list(unit.paths or unit.scopes)
    allowed = [value for value in code if not value.startswith("resource:")]
    allowed.extend(request.get("checkpoint_extra_paths", []))
    if not code:
        allowed.append(".")  # Unknown scope reserves the repository in admission.
    namespace = "docs/bmad-campaign/" + safe_segment(unit.id)
    allowed.extend((namespace, namespace + ".md"))
    if unit.kind == "planning":
        allowed.append("docs")
    sources = request.get("sources") or [request["source"]]
    for raw in sources:
        source = SourceSelector(**raw)
        if source.kind not in {"tickets", "stories", "sprint-status"}:
            continue
        paths = source_paths(workspace, source)
        root = (
            stories.resolve_spec_folder(workspace, source.path)
            if source.kind == "stories"
            else (
                paths.initiative_folder
                if source.kind == "tickets"
                else paths.implementation_artifacts
            )
        )
        if root is not None:
            if not root.is_relative_to(workspace):
                raise CampaignError("checkpoint native source escaped private checkout")
            if unit.kind in {"planning", "tracking", "closure"}:
                allowed.append(root.relative_to(workspace).as_posix())
            elif source.kind == "stories":
                allowed.append(
                    "story-prefix:"
                    + (root / stories.STORIES_SUBDIR / (unit.selector + "-"))
                    .relative_to(workspace)
                    .as_posix()
                )
            elif source.kind == "tickets":
                from .tickettree import TicketTree

                found = TicketTree(workspace, folder=root).find(unit.selector)
                allowed.append(Path(found.plan).relative_to(workspace).as_posix())
                allowed.append((root / "deferred-work.md").relative_to(workspace).as_posix())
            else:
                allowed.extend(
                    (root / name).relative_to(workspace).as_posix()
                    for name in (
                        unit.selector + ".md",
                        "spec-" + unit.selector + ".md",
                        "sprint-status.yaml",
                        "deferred-work.md",
                    )
                )
    return sorted(set(allowed))


def seal(workspace: Path, request: dict[str, Any], *, baseline: str) -> dict[str, Any]:
    """Mint scope/baseline authority before launch, for the durable action only."""
    if verify.rev_parse_head(workspace) != baseline:
        raise CampaignError("checkpoint launch baseline differs from the accepted unit head")
    return {"checkpoint_base": baseline, "checkpoint_paths": owned_paths(workspace, request)}


def instruction(interval_s: float) -> str:
    return (
        f"\nCampaign backup contract: commit meaningful, coherent product progress at least every {interval_s:g} seconds "
        "when useful changes are ready, and before a repair/stage handoff. Use this owned branch and preserve every commit; "
        "never reset, rebase, squash, force-push or stage another actor's work. The coordinator only backs up existing commits. "
        "Dirty and ignored private source changes remain unbacked until the worker records their reviewed tracked artifact mirrors. "
        "Do not invent tests or claim a checkpoint is verified; preserve incomplete work honestly in commit messages.\n"
    )


def _git(workspace: Path, *args: str) -> bytes:
    result = verify.git_bytes(
        workspace,
        "-c",
        "diff.autoRefreshIndex=false",
        *args,
        timeout_s=5,
        env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"},
    )
    if result.returncode or len(result.stdout) > LIMIT:
        raise CampaignError("checkpoint observation unavailable or exceeds its bound: " + args[0])
    return result.stdout


def _native(workspace: Path, request: dict[str, Any]) -> dict[str, str]:
    from . import stories
    from .campaign_closure import _read
    from .campaign_sources import source_paths

    values, size = {}, 0
    for raw in request.get("sources") or [request["source"]]:
        source = SourceSelector(**raw)
        if source.kind not in {"tickets", "stories", "sprint-status"}:
            continue
        paths = source_paths(workspace, source)
        root = (
            stories.resolve_spec_folder(workspace, source.path)
            if source.kind == "stories"
            else (
                paths.initiative_folder
                if source.kind == "tickets"
                else paths.implementation_artifacts
            )
        )
        if root is None or not root.exists():
            continue
        if not root.is_relative_to(workspace) or is_link_like(root):
            raise CampaignError("checkpoint native source is redirected")
        for directory, dirs, files in os.walk(root, followlinks=False):
            if any(is_link_like(Path(directory) / name) for name in dirs):
                raise CampaignError("checkpoint native source has redirected directory")
            for name in sorted(files):
                path = Path(directory) / name
                data = _read(workspace, path)
                size += len(data)
                if len(values) >= 5000 or size > 32 * 1024 * 1024:
                    raise CampaignError("checkpoint native source observation exceeds its bound")
                values[path.relative_to(workspace).as_posix()] = hashlib.sha256(data).hexdigest()
    return values


def _dirty_content(workspace: Path, status: bytes) -> bytes:
    """Hash dirty contents, not just unchanged status letters/file names."""
    from .campaign_closure import _read

    content = hashlib.sha256(status)
    for args in (
        ("diff", "--binary", "--no-ext-diff", "--no-textconv", "HEAD"),
        ("diff", "--cached", "--binary", "--no-ext-diff", "--no-textconv", "HEAD"),
    ):
        content.update(_git(workspace, *args))
    size = 0
    for relative in (
        _git(workspace, "ls-files", "--others", "--exclude-standard", "-z")
        .decode("utf-8")
        .split("\0")
    ):
        if not relative:
            continue
        data = _read(workspace, workspace / relative)
        size += len(data)
        if size > LIMIT:
            raise CampaignError("checkpoint untracked progress exceeds its byte bound")
        content.update(relative.encode())
        content.update(data)
    return content.digest()


def _committed_paths(workspace: Path, baseline: str, head: str) -> list[str]:
    commits = (
        _git(workspace, "rev-list", "--max-count=1001", baseline + ".." + head)
        .decode("ascii")
        .splitlines()
    )
    if len(commits) > 1000:
        raise CampaignError("checkpoint commit history exceeds its observation bound")
    paths = set()
    for commit in commits:
        paths.update(
            value
            for value in _git(
                workspace,
                "diff-tree",
                "--root",
                "--no-commit-id",
                "--name-only",
                "--no-renames",
                "-r",
                "-m",
                "-z",
                commit,
            )
            .decode("utf-8")
            .split("\0")
            if value
        )
    return sorted(paths)


def _backed(workspace: Path, head: str, path: str, digest: str) -> bool:
    result = verify.git_bytes(workspace, "show", head + ":" + path, timeout_s=5)
    return (
        result.returncode == 0
        and len(result.stdout) <= 8 * 1024 * 1024
        and hashlib.sha256(result.stdout).hexdigest() == digest
    )


def _record(directory: Path, name: str, limit: int) -> dict[str, Any]:
    from .campaign_closure import _read

    data = _read(directory, directory / name)
    if len(data) > limit:
        raise CampaignError("checkpoint record exceeds its byte bound")
    value = json.loads(data.decode("utf-8"))
    if not isinstance(value, dict):
        raise CampaignError("checkpoint record is not an object")
    return value


class ProgressMonitor:
    def __init__(self, request: dict[str, Any], directory: Path, *, clock=time.time):
        self.request, self.directory, self.clock = request, directory, clock
        self.workspace = Path(request["workspace"])
        self.base = verify.rev_parse_head(self.workspace)
        if request.get("checkpoint_base", self.base) != self.base:
            raise CampaignError("worker checkpoint baseline differs from its launch")
        self.interval = max(0.1, min(5.0, request["checkpoint_interval_s"] / 4))
        self.stopped = threading.Event()
        self.thread: threading.Thread | None = None
        self.previous: dict[str, Any] = {}
        self.native_base = _native(self.workspace, request)
        self.mirrors: dict[str, str] = {}
        self.path_cache: dict[str, list[str]] = {}

    def observe(self) -> dict[str, Any]:
        now = self.clock()
        identity = {key: self.request[key] for key in IDENTITY}
        try:
            head = verify.rev_parse_head(self.workspace)
            from .campaign_object_promotion import promote

            if head not in self.path_cache:
                promote(self.workspace, head, self.base)
            if (
                _git(self.workspace, "symbolic-ref", "--short", "HEAD").decode().strip()
                != identity["branch"]
            ):
                raise CampaignError("checkpoint branch identity changed")
            if not verify.is_ancestor(self.workspace, self.base, head):
                raise CampaignError("worker rewrote checkpoint ancestry")
            dirty = _git(
                self.workspace, "status", "--porcelain=v1", "-z", "--untracked-files=normal"
            )
            if head not in self.path_cache:
                self.path_cache[head] = _committed_paths(self.workspace, self.base, head)
            paths = self.path_cache[head]
            native = _native(self.workspace, self.request)
            metadata = (
                _git(self.workspace, "log", "-1", "--format=%s%n%ct", head).decode("utf-8").strip()
            )
            private_dirty = native != self.native_base
            private_unbacked = any(
                digest != self.native_base.get(path)
                and not _backed(self.workspace, head, path, digest)
                and not (
                    path in self.mirrors
                    and _backed(self.workspace, head, self.mirrors[path], digest)
                )
                for path, digest in native.items()
            ) or any(path not in native for path in self.native_base)
            signature = hashlib.sha256(
                _dirty_content(self.workspace, dirty) + json.dumps(native, sort_keys=True).encode()
            ).hexdigest()
            if verify.rev_parse_head(self.workspace) != head:
                raise CampaignError("checkpoint changed during observation")
            progress = hashlib.sha256((head + signature).encode()).hexdigest()
            row = {
                **identity,
                "schema_version": 1,
                "state": "observed",
                "observed_at": now,
                "baseline": self.base,
                "head": head,
                "commit_metadata": metadata,
                "committed_paths": [path for path in paths if path],
                "dirty": bool(dirty),
                "dirty_digest": signature,
                "private_source_changed": private_dirty,
                "private_source_unbacked": private_unbacked,
                "progress_digest": progress,
                "progress_at": (
                    self.previous.get("progress_at", now)
                    if self.previous.get("progress_digest") == progress
                    else now
                ),
                "dirty_since": (
                    self.previous.get("dirty_since") or now if dirty or private_unbacked else None
                ),
            }
        except Exception as exc:
            row = {
                **identity,
                "schema_version": 1,
                "state": "unavailable",
                "observed_at": now,
                "error": f"{type(exc).__name__}: {exc}",
            }
        atomic_write_text(
            self.directory / NAME, json.dumps(row, sort_keys=True), follow_symlinks=False
        )
        self.previous = row
        return row

    def start(self) -> None:
        self.observe()

        def monitor():
            while not self.stopped.wait(self.interval):
                self.observe()

        self.thread = threading.Thread(
            target=monitor, name="campaign-checkpoint-observer", daemon=True
        )
        self.thread.start()

    def stop(self, result: dict[str, Any] | None = None) -> None:
        self.stopped.set()
        if self.thread is not None:
            self.thread.join()
        if result:
            self.mirrors.update(result.get("artifact_mirrors", {}))
            self.mirrors.update(
                {
                    row["path"]: row.get("review_path", row["path"])
                    for row in result.get("source_artifacts", [])
                }
            )
        self.observe()


def read(
    directory: Path,
    expected: dict[str, Any],
    *,
    now: float,
    max_age_s: float | None,
    receipt: dict[str, Any] | None = None,
) -> dict[str, Any]:
    path = directory / NAME
    if receipt is None:
        if not path.exists():
            raise CampaignError("worker checkpoint progress has not been observed")
        row = _record(directory, NAME, LIMIT)
    else:
        row = receipt
    if (
        not isinstance(row, dict)
        or row.get("schema_version") != 1
        or any(row.get(key) != expected[key] for key in IDENTITY)
        or row.get("state") != "observed"
        or type(row.get("observed_at")) not in (int, float)
        or (max_age_s is not None and not 0 <= now - row["observed_at"] <= max_age_s)
    ):
        raise CampaignError("worker checkpoint identity, freshness or observation is unavailable")
    workspace = Path(expected["workspace"])
    from .campaign_object_promotion import promote

    if row["baseline"] != expected.get("checkpoint_base", row["baseline"]):
        raise CampaignError("checkpoint baseline differs from the sealed launch")
    promote(workspace, row["head"], row["baseline"])
    actual = _git(workspace, "symbolic-ref", "--short", "HEAD").decode().strip()
    if actual != row["branch"] or not verify.is_ancestor(
        workspace, row["head"], verify.rev_parse_head(workspace)
    ):
        raise CampaignError("checkpoint branch/head no longer belongs to this worker")
    if not verify.is_ancestor(workspace, row["baseline"], row["head"]):
        raise CampaignError("checkpoint baseline ancestry differs")
    paths = _committed_paths(workspace, row["baseline"], row["head"])
    if paths != row.get("committed_paths"):
        raise CampaignError("checkpoint committed path receipt differs from Git")
    allowed = expected["checkpoint_paths"]

    def owned(path):
        return any(
            value in {"*", "."}
            or path == value.rstrip("/")
            or path.startswith(value.rstrip("/") + "/")
            or (
                value.startswith("story-prefix:")
                and path.startswith(value.removeprefix("story-prefix:"))
                and path.endswith(".md")
                and "/" not in path[len(value.removeprefix("story-prefix:")) :]
            )
            for value in allowed
        )

    if any(not owned(path) for path in paths):
        bases = expected.get("checkpoint_preserved_bases", [])
        if not bases:
            raise CampaignError("checkpoint contains committed paths outside the owned scope")
        base_changes = {}
        for base in bases:
            ancestor = _git(workspace, "merge-base", row["baseline"], base).decode("ascii").strip()
            base_changes[base] = set(
                value
                for value in _git(
                    workspace, "diff", "--name-only", "--no-renames", "-z", ancestor, base
                )
                .decode("utf-8")
                .split("\0")
                if value
            )
        commits = (
            _git(workspace, "rev-list", row["baseline"] + ".." + row["head"], "--not", *bases)
            .decode("ascii")
            .splitlines()
        )
        if len(commits) > 1000:
            raise CampaignError("checkpoint actor history exceeds its bound")
        for commit in commits:
            parents = (
                _git(workspace, "rev-list", "--parents", "-n", "1", commit)
                .decode("ascii")
                .split()[1:]
            )
            if not parents:
                raise CampaignError("checkpoint actor history lost its baseline parent")
            changed = (
                _git(workspace, "diff", "--name-only", "--no-renames", "-z", parents[0], commit)
                .decode("utf-8")
                .split("\0")
            )
            for path in (path for path in changed if path and not owned(path)):
                if not any(
                    len(parents) > 1
                    and base in parents[1:]
                    and path in base_changes[base]
                    and _git(workspace, "ls-tree", "-z", commit, "--", path)
                    == _git(workspace, "ls-tree", "-z", base, "--", path)
                    for base in bases
                ):
                    raise CampaignError(
                        "checkpoint actor changed a path outside the owned scope or preserved main changes"
                    )
    return row


def request(runtime, snapshot, unit, launch_id: str) -> tuple[Path, dict[str, Any]]:
    directory = runtime.store.directory(snapshot.id) / "workers" / launch_id
    path = directory / "request.json"
    value = _record(directory, path.name, 8 * 1024 * 1024)
    authority = unit.claim if unit.claim and unit.claim.launch_id == launch_id else None
    if authority is not None:
        attempt, generation = authority.attempt, authority.generation
    else:
        prior = next(
            (
                row
                for row in unit.evidence.get("worker_history", [])
                if row.get("launch_id") == launch_id
            ),
            None,
        )
        if prior is None:
            raise CampaignError("checkpoint has no independently registered worker identity")
        attempt, generation = prior["attempt"], prior["generation"]
    if any(
        value.get(key) != expected
        for key, expected in {
            "campaign_id": snapshot.id,
            "unit_id": unit.id,
            "launch_id": launch_id,
            "branch": unit.bindings["branch"],
            "workspace": unit.bindings["workspace"],
            "attempt": attempt,
            "generation": generation,
        }.items()
    ):
        raise CampaignError("checkpoint launch binding differs")
    launch = historical_action(snapshot, launch_id)
    if (
        launch is None
        or launch.kind != "launch"
        or not all(key in launch.payload for key in ("checkpoint_base", "checkpoint_paths"))
        or any(
            value.get(key, []) != launch.payload.get(key, [])
            for key in (
                "checkpoint_base",
                "checkpoint_paths",
                "checkpoint_preserved_bases",
                "checkpoint_extra_paths",
            )
        )
    ):
        raise CampaignError(
            "checkpoint scope/baseline/repair authority differs from its durable launch"
        )
    return directory, value


def status(runtime, snapshot, unit) -> dict[str, Any]:
    """Visible progress and backup debt; observation failure cannot resemble clean."""
    now = time.time()
    try:
        directory, expected = request(runtime, snapshot, unit, unit.claim.launch_id)
        row = read(directory, expected, now=now, max_age_s=30.0)
        backed = unit.bindings.get("last_pushed_head", "")
        value: dict[str, Any] = {
            **row,
            "progress_age_s": max(0.0, now - row["progress_at"]),
            "dirty_age_s": max(0.0, now - row["dirty_since"]) if row["dirty_since"] else 0.0,
            "unbacked_committed": row["head"] != backed,
        }
    except (OSError, ValueError, KeyError, CampaignError) as exc:
        value = {"state": "unavailable", "reason": f"{type(exc).__name__}: {exc}"}
    try:
        published = {
            row.action_id: row.head for row in runtime._pusher(snapshot, unit).observations()
        }
    except Exception as exc:
        published = {}
        value["backup_observation_unavailable"] = f"{type(exc).__name__}: {exc}"
    debt = [
        {
            "action_id": action.id,
            "head": published.get(action.id)
            or action.receipt.get("head")
            or action.payload.get("progress", {}).get("head"),
            "state": action.state,
            "reason": action.error,
            "retry_at": action.next_retry_at,
            "failures": action.receipt.get("backup_failures", 0),
        }
        for action in snapshot.outbox
        if action.unit == unit.id
        and action.kind == "checkpoint"
        and action.state in {"prepared", "sent_unknown"}
    ]
    value["backup_debt"] = debt
    value["backup_retry_exhausted"] = any(
        row["failures"] >= snapshot.spec.checkpoints.max_backup_failures for row in debt
    )
    return value
