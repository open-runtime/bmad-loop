"""Append-only recovery and exact checkpoint publication for campaign workers.

The branch is never reset, rebased, force-pushed or recreated. Durable per-branch
fences coordinate immutable SHA pushes with recovery. A lost push reply remains
an explicit uncertainty until a server read proves the requested ancestry.
Recovery is an idempotent transaction: preserve residue, restore only owned
content through compensating commits, verify parent/tree/action, publish, then
unfence. Source lifecycle restoration is delegated to its existing sole writer.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import stat
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable

from . import verify
from .platform_util import atomic_write_bytes, atomic_write_text, file_lock, is_link_like


class RecoveryHold(RuntimeError):
    """Recovery cannot prove exclusive ownership, source state or ancestry."""


@dataclass(frozen=True)
class PushObservation:
    action_id: str
    head: str
    state: str  # prepared, sent_unknown, in_flight, confirmed, superseded
    remote_head: str | None = None


@dataclass(frozen=True)
class Quiescence:
    head: str
    generation: int
    worker_terminal: bool
    sessions_terminal: bool
    observation_id: str

    def require(self, head: str) -> None:
        if (
            self.head != head
            or not self.worker_terminal
            or not self.sessions_terminal
            or not self.observation_id
            or self.generation < 1
        ):
            raise RecoveryHold(
                "Worker and coding sessions must be independently confirmed quiescent at the recovery head"
            )


@dataclass(frozen=True)
class RecoveryReceipt:
    action_id: str
    parent: str
    head: str
    tree: str
    preservation_ref: str
    dirty_ref: str | None
    source_paths: tuple[str, ...]
    state: str
    index_ref: str | None = None


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _save(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(
        path, json.dumps(data, sort_keys=True, separators=(",", ":")), follow_symlinks=False
    )


def _load(path: Path) -> dict:
    if is_link_like(path) or path.stat().st_size > 32 * 1024 * 1024:
        raise RecoveryHold("Recovery record must be a bounded regular file")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        raise RecoveryHold("Unsupported recovery record")
    return value


def _oid(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", value):
        raise RecoveryHold("Full immutable commit identity required")
    return value


def _env(**extra: str) -> dict[str, str]:
    return {**{k: v for k, v in os.environ.items() if not k.startswith("GIT_")}, **extra}


class CheckpointPusher:
    """One immutable SHA per durable action, serialized only on its branch.

    This lock can span a bounded network call. It is deliberately per branch,
    not the coordinator revision or repository ownership lock.
    """

    def __init__(
        self,
        repo: Path,
        state_dir: Path,
        remote: str,
        branch: str,
        *,
        git_runner: Callable = verify.git_bytes,
    ):
        self.repo, self.root = repo.resolve(), state_dir.resolve()
        if self.root.is_relative_to(self.repo):
            raise RecoveryHold("Checkpoint records must be outside the editable checkout")
        self.remote, self.branch, self.git = remote, branch, git_runner
        if not remote or remote.startswith("-") or not branch or branch.startswith("-"):
            raise RecoveryHold("Explicit remote and branch required")
        self.scope = self.root / "branches" / _digest(remote + ":" + branch)
        self.scope.mkdir(parents=True, exist_ok=True)
        check = self.git(self.repo, "check-ref-format", "refs/heads/" + branch, env=_env())
        if check.returncode:
            raise RecoveryHold("Invalid checkpoint branch")

    def _run(self, *args: str, env: dict | None = None) -> bytes:
        result = self.git(self.repo, *args, timeout_s=30, env=env or _env())
        if result.returncode:
            raise RecoveryHold("Git operation failed: " + args[0])
        return result.stdout

    def remote_head(self) -> str | None:
        lines = self._run(
            "ls-remote", "--refs", self.remote, "refs/heads/" + self.branch
        ).splitlines()
        if not lines:
            return None
        if len(lines) != 1:
            raise RecoveryHold("Remote checkpoint branch is ambiguous")
        parts = lines[0].split(b"\t")
        if len(parts) != 2 or parts[1] != ("refs/heads/" + self.branch).encode():
            raise RecoveryHold("Remote checkpoint identity differs")
        return _oid(parts[0].decode("ascii"))

    def _ancestor(self, older: str, newer: str) -> bool:
        result = self.git(self.repo, "merge-base", "--is-ancestor", older, newer, env=_env())
        if result.returncode not in (0, 1):
            # A remote actor may have introduced an object not fetched locally.
            self._run(
                "fetch", "--no-tags", "--no-write-fetch-head", "--refmap=", self.remote, newer
            )
            result = self.git(self.repo, "merge-base", "--is-ancestor", older, newer, env=_env())
        if result.returncode not in (0, 1):
            raise RecoveryHold("Checkpoint ancestry observation unavailable")
        return result.returncode == 0

    def _path(self, action_id: str) -> Path:
        return self.scope / "pushes" / (_digest(action_id) + ".json")

    def observations(self) -> tuple[PushObservation, ...]:
        rows = [_load(p) for p in sorted((self.scope / "pushes").glob("*.json"))]
        return tuple(
            PushObservation(r["action_id"], r["head"], r["state"], r.get("remote_head"))
            for r in rows
        )

    def fenced(self) -> str | None:
        path = self.scope / "fence.json"
        if not path.exists():
            return None
        row = _load(path)
        return row["action_id"] if row["active"] else None

    def reconcile(self, action_id: str) -> PushObservation:
        with file_lock(self.scope / "push.lock"):
            return self._reconcile_locked(action_id)

    def observe_existing(self, action_id: str, head: str) -> PushObservation:
        """Record an already-remote checkpoint without authorizing any push."""
        _oid(head)
        with file_lock(self.scope / "push.lock"):
            path = self._path(action_id)
            if path.exists():
                if _load(path)["head"] != head:
                    raise RecoveryHold("Checkpoint observation cannot replace its durable head")
                return self._reconcile_locked(action_id)
            remote = self.remote_head()
            if remote is not None and self._ancestor(head, remote):
                _save(
                    path,
                    {
                        "schema_version": 1,
                        "action_id": action_id,
                        "head": head,
                        "state": "confirmed",
                        "remote_head": remote,
                        "recovery_action": None,
                        "observed_without_push": True,
                    },
                )
                return PushObservation(action_id, head, "confirmed", remote)
            return PushObservation(action_id, head, "not_observed", remote)

    def _reconcile_locked(self, action_id: str) -> PushObservation:
        path, row = self._path(action_id), _load(self._path(action_id))
        remote = self.remote_head()
        if remote is not None and self._ancestor(row["head"], remote):
            row.update(state="confirmed", remote_head=remote)
            _save(path, row)
        elif row["state"] in ("sent_unknown", "in_flight"):
            # An absent/stale server read does not prove the issued write failed.
            row["last_observed_remote"] = remote
            _save(path, row)
        return PushObservation(row["action_id"], row["head"], row["state"], row.get("remote_head"))

    def push(
        self, action_id: str, head: str, *, recovery_action: str | None = None
    ) -> PushObservation:
        _oid(head)
        with file_lock(self.scope / "push.lock"):
            fence = self.fenced()
            if fence and recovery_action != fence:
                raise RecoveryHold("Unit recovery fence rejects new checkpoint pushes")
            path = self._path(action_id)
            if path.exists():
                prior = _load(path)
                if prior["head"] != head or prior.get("recovery_action") != recovery_action:
                    raise RecoveryHold("Checkpoint action identity was reused for different input")
                observed = self._reconcile_locked(action_id)
                if prior["state"] != "prepared" or observed.state == "confirmed":
                    return observed
            remote = self.remote_head()
            if remote is not None and not self._ancestor(remote, head):
                if self._ancestor(head, remote):
                    row = {
                        "schema_version": 1,
                        "action_id": action_id,
                        "head": head,
                        "state": "confirmed",
                        "remote_head": remote,
                        "recovery_action": recovery_action,
                    }
                    _save(path, row)
                    return PushObservation(action_id, head, "confirmed", remote)
                raise RecoveryHold("Remote branch diverged; checkpoint requires integration")
            row = {
                "schema_version": 1,
                "action_id": action_id,
                "head": head,
                "state": "prepared",
                "remote_before": remote,
                "recovery_action": recovery_action,
            }
            _save(path, row)
            row["state"] = "sent_unknown"
            _save(path, row)
            try:
                self._run("push", "--porcelain", self.remote, head + ":refs/heads/" + self.branch)
            except (OSError, verify.GitError, RecoveryHold) as exc:
                row["failure"] = type(exc).__name__
                _save(path, row)
                # Read-back is useful even when transport reported failure after
                # the server accepted the exact write.
                try:
                    return self._reconcile_locked(action_id)
                except (OSError, verify.GitError, RecoveryHold):
                    return PushObservation(action_id, head, "sent_unknown")
            return self._reconcile_locked(action_id)


class CampaignRecovery:
    def __init__(
        self,
        repo: Path,
        state_dir: Path,
        remote: str,
        branch: str,
        *,
        git_runner: Callable = verify.git_bytes,
    ):
        self.repo, self.root = repo.resolve(), state_dir.resolve()
        if self.root.is_relative_to(self.repo):
            raise RecoveryHold("Recovery records must be outside the editable checkout")
        self.pusher = CheckpointPusher(repo, state_dir, remote, branch, git_runner=git_runner)

    def _path(self, action_id: str) -> Path:
        return self.root / "recovery" / _digest(action_id) / "action.json"

    def _file(self, rel: str) -> Path:
        if (
            not rel
            or rel.startswith(("/", "\\"))
            or ".." in Path(rel).parts
            or ".git" in Path(rel).parts
            or "\\" in rel
        ):
            raise RecoveryHold("Recovery path is not a confined literal file")
        path = self.repo / rel
        for component in (path, *path.parents):
            if component == self.repo:
                break
            if is_link_like(component):
                raise RecoveryHold("Recovery refuses symlinked content")
        if path.exists() and not path.is_file():
            raise RecoveryHold("Recovery refuses directory or special-file replacement")
        return path

    def _bytes(self, rel: str) -> bytes | None:
        path = self._file(rel)
        return path.read_bytes() if path.exists() else None

    @staticmethod
    def _encode(value: bytes | None) -> str | None:
        return base64.b64encode(value).decode("ascii") if value is not None else None

    @staticmethod
    def _decode(value: str | None) -> bytes | None:
        return base64.b64decode(value, validate=True) if value is not None else None

    def _verify_head(self, expected: str) -> None:
        if verify.rev_parse_head(self.repo) != expected:
            raise RecoveryHold("Recovery checkout head changed")
        branch = self.pusher._run("symbolic-ref", "--short", "HEAD").decode().strip()
        if branch != self.pusher.branch:
            raise RecoveryHold("Recovery checkout branch changed")

    def begin(
        self,
        action_id: str,
        *,
        expected_head: str,
        baseline: str,
        owned_paths: tuple[str, ...],
        baseline_untracked: tuple[str, ...] | None = (),
        preserve_paths: tuple[str, ...] = (),
        source_preimages: dict[str, bytes | None] | None = None,
        pending_pushes: tuple[PushObservation, ...] = (),
        quiescence: Quiescence,
    ) -> dict:
        _oid(expected_head)
        _oid(baseline)
        quiescence.require(expected_head)
        self._verify_head(expected_head)
        if not self.pusher._ancestor(baseline, expected_head):
            raise RecoveryHold("Attempt baseline is not an ancestor of current head")
        path = self._path(action_id)
        with file_lock(self.pusher.scope / "push.lock"):
            fence = self.pusher.fenced()
            if fence and fence != action_id:
                raise RecoveryHold("Another recovery transaction owns this unit")
            if path.exists():
                row = _load(path)
                if row["parent"] != expected_head or row["baseline"] != baseline:
                    raise RecoveryHold("Recovery action identity reused")
                return row
            # Persist fence before inspecting queued writes. A refusal leaves
            # the fence visible for reconciliation, never resumes a child.
            _save(
                self.pusher.scope / "fence.json",
                {"schema_version": 1, "action_id": action_id, "active": True},
            )
            by_action: dict[str, PushObservation] = {}
            for pending in (*self.pusher.observations(), *pending_pushes):
                if pending.state not in (
                    "prepared",
                    "sent_unknown",
                    "in_flight",
                    "confirmed",
                    "superseded",
                ):
                    raise RecoveryHold("Unknown checkpoint publication state")
                previous = by_action.get(pending.action_id)
                if previous and previous.head != pending.head:
                    raise RecoveryHold(
                        "Checkpoint action has conflicting published head observations"
                    )
                if previous and pending.state == "superseded" and previous.state != "prepared":
                    raise RecoveryHold("An issued checkpoint cannot be silently superseded")
                by_action[pending.action_id] = pending
            pushes = tuple(by_action.values())
            remote = self.pusher.remote_head()
            for head in [p.head for p in pushes if p.state != "superseded"] + (
                [remote] if remote else []
            ):
                if not self.pusher._ancestor(_oid(head), expected_head):
                    raise RecoveryHold(
                        "Published or potentially published checkpoint is not an ancestor of recovery head"
                    )
            sources = source_preimages or {}
            desired: dict[str, dict] = {}
            for rel in sorted(set(owned_paths) | set(sources)):
                self._file(rel)
                if rel in preserve_paths:
                    if rel in sources:
                        raise RecoveryHold(
                            "Source restoration conflicts with preserved authorized correction"
                        )
                    continue
                if rel in sources:
                    value, mode = sources[rel], None
                else:
                    value = verify.worktree_file_bytes_at_revision(self.repo, baseline, rel)
                    listing = self.pusher._run("ls-tree", "-z", baseline, "--", ":(literal)" + rel)
                    mode = listing.split(b" ", 1)[0].decode() if listing else "100644"
                    if mode not in ("100644", "100755"):
                        raise RecoveryHold("Baseline recovery content is not a regular file")
                    if value is None and (baseline_untracked is None or rel in baseline_untracked):
                        raise RecoveryHold(
                            "Recovery cannot delete pre-existing or unclassified content"
                        )
                desired[rel] = {
                    "bytes": self._encode(value),
                    "before": self._encode(self._bytes(rel)),
                    "mode": mode,
                    "source": rel in sources,
                    "tracked": bool(
                        self.pusher._run("ls-tree", "-z", expected_head, "--", ":(literal)" + rel)
                    ),
                }
            row = {
                "schema_version": 1,
                "action_id": action_id,
                "state": "prepared",
                "parent": expected_head,
                "baseline": baseline,
                "desired": desired,
                "quiescence": asdict(quiescence),
                "pushes": [asdict(p) for p in pushes],
                "baseline_untracked": (
                    list(baseline_untracked) if baseline_untracked is not None else None
                ),
                "preservation_ref": "refs/campaign-preserve/" + _digest(action_id) + "/head",
                "dirty_ref": "refs/campaign-preserve/" + _digest(action_id) + "/dirty",
                "index_ref": "refs/campaign-preserve/" + _digest(action_id) + "/index",
                "source_paths": list(sources),
                "tree": None,
                "head": None,
            }
            _save(path, row)
            return row

    def _preserve(self, row: dict) -> None:
        if not verify.ref_exists_checked(self.repo, row["preservation_ref"]):
            verify.create_ref(self.repo, row["preservation_ref"], row["parent"])
        if verify.ref_revision(self.repo, row["preservation_ref"]) != row["parent"]:
            raise RecoveryHold("Recovery preservation head differs")
        index_ref = row.get("index_ref")
        if index_ref and not verify.ref_exists_checked(self.repo, index_ref):
            tree = self.pusher._run("write-tree").decode().strip()
            if tree == verify.revision_tree_oid(self.repo, row["parent"]):
                row["index_ref"] = None
            else:
                identity = _env(
                    GIT_AUTHOR_NAME="bmad-loop",
                    GIT_AUTHOR_EMAIL="bmad-loop@localhost",
                    GIT_COMMITTER_NAME="bmad-loop",
                    GIT_COMMITTER_EMAIL="bmad-loop@localhost",
                )
                commit = (
                    self.pusher._run(
                        "commit-tree",
                        tree,
                        "-p",
                        row["parent"],
                        "-m",
                        "campaign staged residue: " + row["action_id"],
                        env=identity,
                    )
                    .decode()
                    .strip()
                )
                verify.create_ref(self.repo, index_ref, commit)
        dirty = row["dirty_ref"]
        if dirty and not verify.ref_exists_checked(self.repo, dirty):
            captured = verify.snapshot_worktree(
                self.repo,
                dirty,
                baseline_untracked=row["baseline_untracked"],
                force_include=tuple(
                    rel
                    for rel, d in row["desired"].items()
                    if d["source"] and d["before"] is not None and d["before"] != d["bytes"]
                ),
            )
            if not captured:
                row["dirty_ref"] = None

    def _tree(self, row: dict) -> str:
        folder = self._path(row["action_id"]).parent
        index = folder / "index"
        index.unlink(missing_ok=True)
        env = _env(GIT_INDEX_FILE=str(index))
        self.pusher._run("read-tree", row["parent"], env=env)
        for rel, desired in row["desired"].items():
            if desired["source"] and not desired["tracked"]:
                continue
            value = self._decode(desired["bytes"])
            if value is None:
                self.pusher._run("update-index", "--force-remove", "--", rel, env=env)
            else:
                blob_file = folder / ("blob-" + _digest(rel))
                atomic_write_bytes(blob_file, value, follow_symlinks=False)
                oid = self.pusher._run("hash-object", "-w", str(blob_file)).decode().strip()
                mode = desired["mode"]
                if mode is None:
                    current = self._file(rel)
                    mode = (
                        "100755"
                        if current.exists() and current.stat().st_mode & stat.S_IXUSR
                        else "100644"
                    )
                self.pusher._run("update-index", "--add", "--cacheinfo", mode, oid, rel, env=env)
        return self.pusher._run("write-tree", env=env).decode().strip()

    def _adopt(self, row: dict, head: str) -> None:
        parent = self.pusher._run("show", "-s", "--format=%P", head).decode().strip()
        message = self.pusher._run("show", "-s", "--format=%B", head).decode()
        if (
            parent != row["parent"]
            or verify.revision_tree_oid(self.repo, head) != row["tree"]
            or ("BMAD-Recovery-Action: " + row["action_id"]) not in message.splitlines()
        ):
            raise RecoveryHold(
                "Recovery commit cannot be adopted: parent, tree or action marker differs"
            )
        row.update(head=head, state="committed")
        _save(self._path(row["action_id"]), row)

    def recover(
        self,
        action_id: str,
        *,
        quiescence: Quiescence,
        restore_sources: Callable[[dict[str, bytes | None], str], Any] | None = None,
    ) -> RecoveryReceipt:
        with file_lock(self.pusher.scope / "recovery.lock"):
            row = _load(self._path(action_id))
            if quiescence.generation != row["quiescence"]["generation"]:
                raise RecoveryHold("Recovery generation changed")
            if self.pusher.fenced() != action_id:
                raise RecoveryHold("Recovery fence is absent or belongs to another action")
            head = verify.rev_parse_head(self.repo)
            quiescence.require(head)
            if row["state"] in ("committed", "complete") and head == row["parent"]:
                if verify.revision_tree_oid(self.repo, head) != row["tree"]:
                    raise RecoveryHold("Recovered no-op tree changed")
                return self._receipt(row)
            if head != row["parent"]:
                if row["tree"] is None:
                    raise RecoveryHold(
                        "Unexpected commit before intended recovery tree was persisted"
                    )
                self._adopt(row, head)
                return self._receipt(row)
            self._verify_head(row["parent"])
            for rel, desired in row["desired"].items():
                if self._encode(self._bytes(rel)) not in (desired["before"], desired["bytes"]):
                    raise RecoveryHold("Owned content changed outside the recovery transaction")
            self._preserve(row)
            row["state"] = "preserved"
            _save(self._path(action_id), row)
            sources = {
                rel: self._decode(value["bytes"])
                for rel, value in row["desired"].items()
                if value["source"]
            }
            if sources:
                if restore_sources is None:
                    raise RecoveryHold(
                        "Lifecycle restoration requires its source sole-writer callback"
                    )
                restore_sources(sources, action_id)
                if any(self._bytes(rel) != value for rel, value in sources.items()):
                    raise RecoveryHold(
                        "Source sole-writer restoration did not reproduce its authorized preimages"
                    )
            for rel, desired in row["desired"].items():
                if desired["source"]:
                    continue
                path, value = self._file(rel), self._decode(desired["bytes"])
                if value is None:
                    path.unlink(missing_ok=True)
                else:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    atomic_write_bytes(path, value, follow_symlinks=False)
                    os.chmod(path, 0o755 if desired["mode"] == "100755" else 0o644)
            tree = self._tree(row)
            if row["tree"] is not None and row["tree"] != tree:
                raise RecoveryHold("Recovery intended tree changed during resume")
            row.update(tree=tree, state="correction_prepared")
            _save(self._path(action_id), row)
            if tree == verify.revision_tree_oid(self.repo, row["parent"]):
                for rel, desired in row["desired"].items():
                    if not desired["source"] or desired["tracked"]:
                        verify.reset_index_path(self.repo, row["parent"], rel)
                row.update(head=row["parent"], state="committed")
                _save(self._path(action_id), row)
            else:
                committed = verify.commit_paths(
                    self.repo,
                    "Recover campaign attempt\n\nBMAD-Recovery-Action: " + action_id,
                    [
                        self._file(rel)
                        for rel, d in row["desired"].items()
                        if not d["source"] or d["tracked"]
                    ],
                    run_hooks=False,
                )
                if committed is None:
                    raise RecoveryHold("Recovery correction was not committed")
                self._adopt(row, committed)
            return self._receipt(row)

    @staticmethod
    def _receipt(row: dict) -> RecoveryReceipt:
        return RecoveryReceipt(
            row["action_id"],
            row["parent"],
            row["head"],
            row["tree"],
            row["preservation_ref"],
            row["dirty_ref"],
            tuple(row["source_paths"]),
            row["state"],
            row.get("index_ref"),
        )

    def finish(
        self,
        action_id: str,
        *,
        quiescence: Quiescence,
        verify_sources: Callable[[dict[str, bytes | None]], bool] | None = None,
    ) -> RecoveryReceipt:
        row = _load(self._path(action_id))
        if row["state"] == "complete" and self.pusher.fenced() is None:
            return self._receipt(row)
        if row["state"] not in ("committed", "complete"):
            raise RecoveryHold("Recovery has not committed its correction")
        quiescence.require(row["head"])
        if quiescence.generation != row["quiescence"]["generation"]:
            raise RecoveryHold("Recovery generation changed")
        self._verify_head(row["head"])
        sources = {
            rel: self._decode(d["bytes"]) for rel, d in row["desired"].items() if d["source"]
        }
        if sources and (verify_sources is None or not verify_sources(sources)):
            raise RecoveryHold("Recovered source state remains unconfirmed")
        published = self.pusher.push(
            "recovery:" + action_id, row["head"], recovery_action=action_id
        )
        if published.state != "confirmed" or published.remote_head != row["head"]:
            raise RecoveryHold("Recovery push remains unknown or remote advanced; fence retained")
        # Unknown old writes can only advance to ancestors of this exact remote
        # head. Their history remains; confirmed ancestry settles their risk.
        for prior in self.pusher.observations():
            if prior.state in ("sent_unknown", "in_flight"):
                self.pusher.reconcile(prior.action_id)
        with file_lock(self.pusher.scope / "push.lock"):
            if self.pusher.fenced() != action_id or self.pusher.remote_head() != row["head"]:
                raise RecoveryHold("Recovery publication changed before fence release")
            for prior in row["pushes"]:
                prior.setdefault("initial_state", prior["state"])
                prior["state"] = "superseded" if prior["state"] == "prepared" else "confirmed"
                prior["remote_head"] = row["head"]
            row["state"] = "complete"
            _save(self._path(action_id), row)
            _save(
                self.pusher.scope / "fence.json",
                {"schema_version": 1, "action_id": action_id, "active": False},
            )
        return self._receipt(row)
