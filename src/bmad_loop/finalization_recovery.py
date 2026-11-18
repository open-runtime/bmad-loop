"""Durable finalization compensation without resetting worktree contents.

Records and original-history refs precede staging. Compensation requires proven
cleanup of the observed process tree and exact recorded ref/index preconditions.
It does not prove universal daemon containment or authorize replay of a commit.
Cooperating index writers honor index.lock; writers bypassing it are outside this
protocol. An incomplete operation remains pending across coordinator restart.
"""

from __future__ import annotations

import contextlib
import dataclasses
import hashlib
import json
import math
import os
import re
import stat
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, cast

from . import runs, verify
from .platform_util import (
    atomic_replace,
    atomic_write_bytes,
    atomic_write_text,
    file_lock,
    is_link_like,
)


class FinalizationRecoveryError(verify.GitError):
    """Recovery evidence is incomplete or another owner changed the subject."""


def _safe_error(exc: BaseException) -> str:
    # This exact class is raised here with controller-authored messages. Git,
    # hook and provider exceptions may embed credentials or raw command output.
    return str(exc) if type(exc) is FinalizationRecoveryError else type(exc).__name__


@contextlib.contextmanager
def _operation_lock(path: Path):
    lock = file_lock(path, blocking=False)
    lock.__enter__()
    try:
        yield
    except BaseException as exc:
        try:
            lock.__exit__(type(exc), exc, exc.__traceback__)
        except BaseException as cleanup_error:
            exc.add_note(
                "Finalization operation-lock cleanup failed: " + _safe_error(cleanup_error)
            )
        raise
    else:
        lock.__exit__(None, None, None)


def _sync_directory(path: Path) -> None:
    if os.name == "posix":
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _write(path: Path, value: dict[str, Any]) -> None:
    atomic_write_text(
        path, json.dumps(value, sort_keys=True, indent=2, allow_nan=False), follow_symlinks=False
    )
    _sync_directory(path.parent)


def _read(path: Path) -> dict[str, Any]:
    if is_link_like(path):
        raise FinalizationRecoveryError("finalization record is redirected")
    value = json.loads(path.read_text(encoding="utf-8"))
    if (
        not isinstance(value, dict)
        or type(value.get("schema_version")) is not int
        or value["schema_version"] != 1
    ):
        raise FinalizationRecoveryError("unsupported finalization recovery record")
    return value


def _root(repo: Path) -> Path:
    root = runs.state_root().resolve() / runs.project_tag(repo) / "_finalization"
    if root.resolve().is_relative_to(repo.resolve()):
        raise FinalizationRecoveryError("finalization authority must be outside the worktree")
    if is_link_like(root):
        raise FinalizationRecoveryError("finalization authority is redirected")
    return root


def _git_text(repo: Path, *args: str) -> str:
    result = verify.git_bytes(repo, *args)
    if result.returncode:
        raise FinalizationRecoveryError("cannot observe finalization Git authority: " + args[0])
    return os.fsdecode(result.stdout.removesuffix(b"\n"))


def _branch(repo: Path) -> str:
    result = verify.git_bytes(repo, "symbolic-ref", "--no-recurse", "-q", "HEAD")
    if result.returncode == 1:
        return "HEAD"
    if result.returncode:
        raise FinalizationRecoveryError("cannot observe finalization branch identity")
    ref = os.fsdecode(result.stdout.removesuffix(b"\n"))
    alias = verify.git_bytes(repo, "symbolic-ref", "--no-recurse", "-q", ref)
    if alias.returncode != 1 or not ref.startswith("refs/heads/"):
        raise FinalizationRecoveryError("finalization requires a direct branch or detached HEAD")
    return ref


def _identity(path: Path) -> list[int]:
    value = path.stat()
    return [value.st_dev, value.st_ino]


def _image_metadata(value, data: bytes) -> dict[str, Any]:
    return {
        "exists": True,
        "sha256": hashlib.sha256(data).hexdigest(),
        "size": len(data),
        "mode": stat.S_IMODE(value.st_mode),
        "atime_ns": value.st_atime_ns,
        "mtime_ns": value.st_mtime_ns,
        "ctime_ns": value.st_ctime_ns,
        "identity": [value.st_dev, value.st_ino],
    }


def _index_image(path: Path) -> tuple[dict[str, Any], bytes | None]:
    try:
        descriptor = os.open(
            path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
        )
    except FileNotFoundError:
        return {
            "exists": False,
            "sha256": None,
            "mode": None,
            "atime_ns": None,
            "mtime_ns": None,
        }, None
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or is_link_like(path):
            raise FinalizationRecoveryError("finalization index is not a regular file")
        chunks = []
        while chunk := os.read(descriptor, 1024 * 1024):
            chunks.append(chunk)
        after = os.fstat(descriptor)
        if (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ) or _identity(
            path
        ) != [
            before.st_dev,
            before.st_ino,
        ]:
            raise FinalizationRecoveryError("finalization index changed while being captured")
        data = b"".join(chunks)
        return _image_metadata(before, data), data
    finally:
        os.close(descriptor)


def _same_index(observed, expected) -> bool:
    # Reading the index may update atime. It is retained for restoration but is
    # not an ownership discriminator; inode, content, mode, mtime and ctime are.
    return {k: v for k, v in observed.items() if k != "atime_ns"} == {
        k: v for k, v in expected.items() if k != "atime_ns"
    }


def _capture_document(value):
    from .owned_capture import CaptureOwnership, ProcessTreeEvidence

    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        result = {
            field.name: _capture_document(getattr(value, field.name))
            for field in dataclasses.fields(value)
        }
        if isinstance(value, (CaptureOwnership, ProcessTreeEvidence)):
            result["quiescent"] = value.quiescent
        return result
    if isinstance(value, bytes):
        return {"bytes": len(value), "sha256": hashlib.sha256(value).hexdigest()}
    if value is None or type(value) in {str, bool, int}:
        return value
    if type(value) is float and math.isfinite(cast(float, value)):
        return value
    if isinstance(value, (list, tuple)):
        return [_capture_document(item) for item in value]
    raise ValueError("unsupported capture evidence value")


def _context_document(value):
    if value is None:
        return None
    if not isinstance(value, dict):
        raise FinalizationRecoveryError("finalization context must be a JSON object")

    def keys(item):
        if isinstance(item, dict):
            if any(not isinstance(key, str) for key in item):
                raise ValueError("context keys must be strings")
            for nested in item.values():
                keys(nested)
        elif isinstance(item, (list, tuple)):
            for nested in item:
                keys(nested)

    try:
        keys(value)
        return json.loads(json.dumps(value, sort_keys=True, allow_nan=False))
    except (TypeError, ValueError) as exc:
        raise FinalizationRecoveryError("finalization context is not JSON-serializable") from exc


def pending(repo: Path) -> dict[str, Any] | None:
    """Read the current operation without deleting or adopting any old record."""
    root = _root(repo)
    try:
        (root / "active.json").lstat()
    except FileNotFoundError:
        return None
    pointer = _read(root / "active.json")
    identity = pointer.get("id")
    if (
        not isinstance(identity, str)
        or len(identity) != 32
        or any(c not in "0123456789abcdef" for c in identity)
    ):
        raise FinalizationRecoveryError("finalization active operation identity is invalid")
    record = _read(root / identity / "record.json")
    if record.get("id") != identity or record.get("repo") != str(repo.resolve()):
        raise FinalizationRecoveryError("finalization active record belongs to another subject")
    required = {
        "repo_identity",
        "git_dir",
        "git_dir_identity",
        "branch_ref",
        "original_head",
        "baseline",
        "message",
        "index_path",
        "index_preimage",
        "recovery_ref",
        "state",
        "events",
    }
    if not required <= set(record) or not isinstance(record.get("index_preimage"), dict):
        raise FinalizationRecoveryError("finalization active record is incomplete")
    if record["state"] == "completed" and not record.get("committed_head"):
        raise FinalizationRecoveryError("finalization completion evidence is incomplete")
    if record["state"] == "refused" and record.get("refusal_acknowledged") is not True:
        raise FinalizationRecoveryError("finalization refusal evidence is incomplete")
    if record.get("state") not in {"completed", "refused"}:
        return record
    lease = record.get("index_lock")
    if lease is not None:
        expected_lock = str(
            Path(record["index_path"]).with_name(Path(record["index_path"]).name + ".lock")
        )
        if not isinstance(lease, dict) or lease.get("path") != expected_lock:
            raise FinalizationRecoveryError("finalization index lease binding is incomplete")
        if lease.get("state") != "release-authorized":
            return record
        try:
            Path(expected_lock).lstat()
        except FileNotFoundError:
            return None
        return record
    return None


class Transaction:
    def __init__(self, repo: Path, path: Path, record: dict[str, Any]):
        self.repo, self.path, self._record = repo, path, record
        self._index_lock_identity = None
        self._persistence_failed = False

    @property
    def id(self) -> str:
        return self._record["id"]

    @property
    def record(self) -> dict[str, Any]:
        return json.loads(json.dumps(self._record))

    @property
    def branch_ref(self) -> str:
        return self._record["branch_ref"]

    @property
    def index_path(self) -> Path:
        return Path(self._record["index_path"])

    @property
    def state(self) -> str:
        return self._record["state"]

    def _save(self, event: str, **changes: Any) -> None:
        self._record.update(changes)
        self._record["events"].append(
            {
                "event": event,
                "at": time.time(),
                **({"reason": changes["reason"]} if "reason" in changes else {}),
            }
        )
        try:
            _write(self.path, self._record)
        except BaseException:
            self._persistence_failed = True
            raise

    def _record_failure(self, exc: BaseException) -> None:
        try:
            self.uncertain(_safe_error(exc))
        except BaseException as recording_error:
            exc.add_note(
                "Finalization recovery recording failed: "
                + type(recording_error).__name__
                + ": "
                + _safe_error(recording_error)
            )

    def _observe_exception(self, command: str, exc: BaseException) -> None:
        try:
            self.observe_command(
                command, getattr(exc, "capture", None) or getattr(exc, "owned_capture", None)
            )
        except BaseException as recording_error:
            exc.add_note(
                "Finalization command evidence could not be recorded: "
                + type(recording_error).__name__
            )

    @contextlib.contextmanager
    def _locked_index(self):
        lock = self.index_path.with_name(self.index_path.name + ".lock")
        if self._index_lock_identity is not None:
            if is_link_like(lock) or _identity(lock) != self._index_lock_identity:
                raise FinalizationRecoveryError("owned finalization index lock changed")
            yield
            return
        descriptor = os.open(
            lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600
        )
        held = os.fstat(descriptor)
        identity = [held.st_dev, held.st_ino]
        self._index_lock_identity = identity
        failure = None
        admitted = False
        try:
            lease = {"path": str(lock), "identity": identity, "state": "held", "operation": self.id}
            remaining = memoryview(
                json.dumps({"schema_version": 1, **lease, "record": str(self.path)}).encode()
            )
            while remaining:
                count = os.write(descriptor, remaining)
                if count <= 0:
                    raise OSError("finalization index lease could not be written")
                remaining = remaining[count:]
            os.fsync(descriptor)
            _sync_directory(lock.parent)
            self._save("index-lock-acquired", index_lock=lease)
            admitted = True
            yield
        except BaseException as exc:
            if not admitted:
                self._persistence_failed = True
            failure = exc
            raise
        finally:
            self._index_lock_identity = None
            try:
                os.close(descriptor)
                try:
                    if not is_link_like(lock) and _identity(lock) == identity:
                        uncertain_writer = (
                            self._persistence_failed
                            or self._record.get("stage_observation_pending")
                            or self._record.get("commit_observation_pending")
                            or self._record.get("restore_writer_pending")
                            or any(
                                not row["known"] or not row["quiescent"]
                                for row in self._record.get("commands", [])
                            )
                        )
                        if uncertain_writer:
                            self._save(
                                "index-lock-retained",
                                index_lock={
                                    **lease,
                                    "state": "retained",
                                    "reason": "Writer cleanup or durable command evidence remains uncertain; reconcile this owned lease before releasing it",
                                },
                            )
                        else:
                            self._save(
                                "index-lock-release-authorized",
                                index_lock={**lease, "state": "release-authorized"},
                            )
                            lock.unlink()
                            _sync_directory(lock.parent)
                except FileNotFoundError:
                    pass
            except BaseException as cleanup_error:
                if failure is None:
                    raise
                failure.add_note(
                    "Finalization index-lock cleanup failed: "
                    + type(cleanup_error).__name__
                    + ": "
                    + _safe_error(cleanup_error)
                )

    def _assert_subject(self) -> None:
        if (
            _identity(self.repo) != self._record["repo_identity"]
            or _branch(self.repo) != self.branch_ref
        ):
            raise FinalizationRecoveryError("finalization checkout or branch changed")
        if _identity(Path(self._record["git_dir"])) != self._record["git_dir_identity"]:
            raise FinalizationRecoveryError("finalization Git directory changed")
        index = verify._bound_real_index_path(self.repo)
        if index.parent.resolve() / index.name != self.index_path:
            raise FinalizationRecoveryError("finalization index binding changed")

    def _assert_index(self, expected) -> None:
        if not _same_index(_index_image(self.index_path)[0], expected):
            raise FinalizationRecoveryError(
                "finalization index changed outside its recorded operation"
            )

    def observe_command(self, command: str, capture: object) -> None:
        """Persist typed cleanup facts, never raw command output or arbitrary argv."""
        from .owned_capture import CaptureOwnership, CaptureResult, ProcessTreeEvidence

        if command not in {"add", "update-ref", "commit", "diff", "reflog", "show", "write-tree"}:
            raise FinalizationRecoveryError("unsupported finalization command observation")
        if self.state in {"completed", "refused"}:
            raise FinalizationRecoveryError(
                "settled finalization cannot acquire new command evidence"
            )
        known = isinstance(capture, (CaptureResult, ProcessTreeEvidence))
        try:
            document = _capture_document(capture) if known else None
            ownership = capture.ownership if isinstance(capture, CaptureResult) else capture
            quiescent = (
                isinstance(ownership, (CaptureOwnership, ProcessTreeEvidence))
                and ownership.quiescent
            )
        except (TypeError, ValueError, AttributeError):
            known, document, quiescent = False, None, False
        observations = [
            *self._record.get("commands", []),
            {
                "command": command,
                "stage": self.state,
                "known": known,
                "quiescent": quiescent,
                "capture": document,
            },
        ]
        self._save(
            "command-observed",
            commands=observations,
            **(
                {"stage_observation_pending": False}
                if command == "add"
                else (
                    {"commit_observation_pending": False}
                    if command == "commit"
                    else (
                        {"restore_writer_pending": False}
                        if command == "update-ref" and self._record.get("restore_writer_pending")
                        else {}
                    )
                )
            ),
        )

    def validate_staged_head(self, expected_head: str) -> None:
        """Recheck the frozen subject while the caller holds a prepared ref CAS."""
        owned = {
            self._record["original_head"],
            self._record.get("head_move", {}).get("intended_target"),
        }
        if expected_head not in owned or self._record.get("index_postimage") is None:
            raise FinalizationRecoveryError("finalization has no staged authority for this HEAD")
        self._assert_subject()
        self._assert_index(self._record["index_postimage"])
        if verify.rev_parse_head(self.repo) != expected_head:
            raise FinalizationRecoveryError("finalization staged HEAD changed")

    def stage_started(self) -> None:
        if self.state != "prepared":
            raise FinalizationRecoveryError("finalization staging is already started or uncertain")
        if self._index_lock_identity is None or not self._record.get("staging_index"):
            raise FinalizationRecoveryError(
                "finalization staging requires its private index and real-index lease"
            )
        self._assert_subject()
        self._assert_index(self._record["index_preimage"])
        if verify.rev_parse_head(self.repo) != self._record["original_head"]:
            raise FinalizationRecoveryError("finalization HEAD changed before staging")
        self._save("stage-started", state="staging", stage_observation_pending=True)

    def staged(self) -> None:
        if self.state != "staging":
            raise FinalizationRecoveryError("finalization lacks its staging intent")
        if not self._record.get("staging_published"):
            raise FinalizationRecoveryError(
                "finalization staged index has not been published with cleanup proof"
            )
        self._assert_subject()
        if verify.rev_parse_head(self.repo) != self._record["original_head"]:
            raise FinalizationRecoveryError("finalization HEAD changed during staging")
        image, data = _index_image(self.index_path)
        if data is not None:
            atomic_write_bytes(
                self.path.parent / "index-postimage.bin", data, follow_symlinks=False
            )
            _sync_directory(self.path.parent)
        self._save("staged", state="staged", index_postimage=image)

    @contextlib.contextmanager
    def staging_index(self):
        """Own the real index before add can observe or replace any staged bytes."""
        if self.state != "prepared" or self._record.get("staging_index"):
            raise FinalizationRecoveryError("finalization staging index is already prepared")
        with self._locked_index():
            self._assert_subject()
            self._assert_index(self._record["index_preimage"])
            image, data = _index_image(self.index_path)
            path = self.path.parent / "stage-index"
            if data is not None:
                atomic_write_bytes(path, data, follow_symlinks=False)
                os.chmod(path, image["mode"])
                os.utime(path, ns=(image["atime_ns"], image["mtime_ns"]))
                _sync_directory(path.parent)
            self._save(
                "staging-index-prepared", staging_index={"path": str(path), "initial": image}
            )
            yield path

    def publish_staged_index(self, *, cleanup_proven: bool = False) -> None:
        adds = [row for row in self._record.get("commands", []) if row["command"] == "add"]
        if (
            self.state != "staging"
            or cleanup_proven is not True
            or self._index_lock_identity is None
            or not adds
            or any(
                not row["known"]
                or not row["quiescent"]
                or (row["capture"] or {}).get("timed_out")
                or (row["capture"] or {}).get("interrupted")
                for row in self._record.get("commands", [])
            )
            or adds[-1]["capture"].get("returncode") != 0
        ):
            raise FinalizationRecoveryError(
                "staged index publication requires acknowledged quiescent add evidence"
            )
        with self._locked_index():
            self._assert_subject()
            self._assert_index(self._record["index_preimage"])
            if verify.rev_parse_head(self.repo) != self._record["original_head"]:
                raise FinalizationRecoveryError("finalization HEAD changed during private staging")
            private = self._record["staging_index"]
            if private.get("path") != str(self.path.parent / "stage-index"):
                raise FinalizationRecoveryError("private staging index binding changed")
            image, data = _index_image(Path(private["path"]))
            if data is None:
                raise FinalizationRecoveryError("acknowledged add did not produce an index")
            atomic_write_bytes(
                self.path.parent / "index-postimage.bin", data, follow_symlinks=False
            )
            self._save(
                "staged-index-publication-started", staging_index={**private, "completed": image}
            )
            published = self._publish_index_bytes(image, data)
            self._save("staged-index-published", staging_published=published)

    def head_move_started(self, target_head: str) -> None:
        if self.state != "staged" or target_head != self._record["baseline"]:
            raise FinalizationRecoveryError("finalization head move is outside its frozen plan")
        self._assert_subject()
        self._assert_index(self._record["index_postimage"])
        if verify.rev_parse_head(self.repo) != self._record["original_head"]:
            raise FinalizationRecoveryError("finalization HEAD changed before its guarded move")
        self._save(
            "head-move-started",
            state="head-move-pending",
            head_move={
                "expected_old": self._record["original_head"],
                "intended_target": target_head,
                "acknowledged": False,
            },
        )

    def head_moved(self, target_head: str) -> None:
        move = self._record.get("head_move", {})
        if self.state != "head-move-pending" or target_head != move.get("intended_target"):
            raise FinalizationRecoveryError("finalization lacks its exact head-move intent")
        self._assert_subject()
        self._assert_index(self._record["index_postimage"])
        if verify.rev_parse_head(self.repo) != target_head:
            raise FinalizationRecoveryError("finalization head move was not observed")
        self._save("head-moved", state="head-moved", head_move={**move, "acknowledged": True})

    def commit_started(self) -> None:
        if self.state not in {"staged", "head-moved"}:
            raise FinalizationRecoveryError(
                "finalization commit lacks confirmed staging/head authority"
            )
        if self._index_lock_identity is None or not self._record.get("private_index"):
            raise FinalizationRecoveryError(
                "finalization commit requires its private index and real-index lease"
            )
        if not self._record.get("commit_environment"):
            raise FinalizationRecoveryError(
                "finalization commit lacks its operation-bound reflog environment"
            )
        self._assert_subject()
        self._assert_index(self._record["index_postimage"])
        expected = self._record.get("head_move", {}).get(
            "intended_target", self._record["original_head"]
        )
        if verify.rev_parse_head(self.repo) != expected:
            raise FinalizationRecoveryError("finalization HEAD changed before commit")
        self._save(
            "commit-started",
            state="commit-started",
            commit_expected_head=expected,
            commit_observation_pending=True,
        )

    def commit_environment(self, private_index: Path) -> dict[str, str]:
        """Bind Git's actual ref update to this operation without changing host config."""
        private = self._record.get("private_index", {})
        if (
            self.state not in {"staged", "head-moved"}
            or self._index_lock_identity is None
            or str(private_index) != private.get("path")
        ):
            raise FinalizationRecoveryError("commit environment requires the owned private index")
        environment = verify._bound_git_env()
        raw_count = environment.get("GIT_CONFIG_COUNT", "0") or "0"
        if not re.fullmatch(r"0|[1-9][0-9]{0,3}", raw_count) or int(raw_count) > 1024:
            raise FinalizationRecoveryError(
                "invalid or unbounded command-scoped Git configuration count"
            )
        count = int(raw_count)
        entries = []
        for index in range(count):
            key, value = (
                environment.get(f"GIT_CONFIG_KEY_{index}"),
                environment.get(f"GIT_CONFIG_VALUE_{index}"),
            )
            if not key or value is None:
                raise FinalizationRecoveryError("incomplete command-scoped Git configuration")
            entries.append([key, value])
        binding = {
            "nonce": "bmad-finalization-" + self.id,
            "private_index": str(private_index),
            "config_count": count,
            "config_digest": hashlib.sha256(
                json.dumps(entries, sort_keys=True).encode()
            ).hexdigest(),
        }
        prior = self._record.get("commit_environment")
        if prior is not None and prior != binding:
            raise FinalizationRecoveryError("command-scoped finalization Git configuration changed")
        if prior is None:
            self._save("commit-environment-bound", commit_environment=binding)
        environment.update(
            {
                "GIT_INDEX_FILE": str(private_index),
                "GIT_REFLOG_ACTION": binding["nonce"],
                "GIT_CONFIG_COUNT": str(count + 1),
                f"GIT_CONFIG_KEY_{count}": "core.logAllRefUpdates",
                f"GIT_CONFIG_VALUE_{count}": "true",
            }
        )
        return environment

    def _observed_git(self, command: str, *arguments: str, env=None) -> bytes:
        try:
            result = verify.git_bytes(
                self.repo, command, *arguments, env=verify._bound_git_env() if env is None else env
            )
        except BaseException as exc:
            self._observe_exception(command, exc)
            raise
        self.observe_command(command, getattr(result, "owned_capture", None))
        if result.returncode != 0 or not self._record["commands"][-1]["quiescent"]:
            raise FinalizationRecoveryError(
                "finalization " + command + " observation lacks acknowledged cleanup"
            )
        return result.stdout

    def observe_committed(self) -> str:
        """Find and pin the operation's commit, never adopt the current foreign tip."""
        commits = [row for row in self._record.get("commands", []) if row["command"] == "commit"]
        binding = self._record.get("commit_environment")
        if (
            self.state != "commit-started"
            or not binding
            or not commits
            or any(
                not row["known"]
                or not row["quiescent"]
                or (row["capture"] or {}).get("timed_out")
                or (row["capture"] or {}).get("interrupted")
                for row in self._record.get("commands", [])
            )
            or commits[-1]["capture"].get("returncode") != 0
        ):
            raise FinalizationRecoveryError(
                "commit identity requires acknowledged quiescent commit evidence"
            )
        self._assert_subject()
        prefix = (binding["nonce"] + ": ").encode()
        raw = self._observed_git(
            "reflog", "show", "--max-count=256", "--format=%H%x00%gs%x00", self.branch_ref
        )
        fields = raw.split(b"\0")
        matches = []
        for index in range(0, len(fields) - 1, 2):
            oid, action = fields[index].strip(), fields[index + 1]
            if action.startswith(prefix):
                if re.fullmatch(rb"[a-f0-9]{40}|[a-f0-9]{64}", oid) is None:
                    raise FinalizationRecoveryError(
                        "operation reflog entry has invalid commit identity"
                    )
                matches.append(oid.decode("ascii"))
        if len(matches) != 1:
            raise FinalizationRecoveryError(
                "operation commit reflog identity is missing or ambiguous within its bound"
            )
        committed = matches[0]
        reference = self._record["recovery_ref"] + "-commit"
        evidence = {
            "head": committed,
            "nonce": binding["nonce"],
            "recovery_ref": reference,
            "validated": False,
            "reflog_limit": 256,
        }
        self._save("commit-identified", commit_evidence=evidence)
        # Retain even a later-rejected own commit before further probes or any
        # comparison to a branch that a peer may already have advanced.
        if verify.ref_exists_checked(self.repo, reference):
            if verify.rev_parse_revision(self.repo, reference) != committed:
                raise FinalizationRecoveryError("operation commit recovery reference conflicts")
        else:
            self._observed_git("update-ref", reference, committed, "")
        self._save("commit-pinned", commit_evidence={**evidence, "pinned": True})
        raw = self._observed_git("show", "-s", "--format=%T%x00%P", committed).rstrip(b"\n")
        values = raw.split(b"\0")
        if len(values) != 2:
            raise FinalizationRecoveryError("operation commit tree and parents are unavailable")
        tree, parents = values[0].decode("ascii"), values[1].decode("ascii").split()
        expected_tree = (
            self._observed_git(
                "write-tree",
                env={**verify._bound_git_env(), "GIT_INDEX_FILE": binding["private_index"]},
            )
            .decode("ascii")
            .strip()
        )
        if parents != [self._record["commit_expected_head"]] or tree != expected_tree:
            raise FinalizationRecoveryError(
                "operation commit differs from its expected parent or private index tree"
            )
        evidence.update(pinned=True, tree=tree, parents=parents, validated=True)
        self._save("commit-validated", commit_evidence=evidence)
        if verify.rev_parse_head(self.repo) != committed:
            raise FinalizationRecoveryError(
                "current HEAD differs from the pinned operation commit; preserve both histories"
            )
        return committed

    @contextlib.contextmanager
    def private_index(self):
        """Let commit/hooks refresh only an operation-owned index, retaining the real lock."""
        if self.state not in {"staged", "head-moved"} or self._record.get("private_index"):
            raise FinalizationRecoveryError(
                "private commit index already exists or finalization is not staged"
            )
        with self._locked_index():
            self._assert_subject()
            self._assert_index(self._record["index_postimage"])
            image, data = _index_image(self.index_path)
            path = self.path.parent / "commit-index"
            if data is not None:
                atomic_write_bytes(path, data, follow_symlinks=False)
                os.chmod(path, image["mode"])
                os.utime(path, ns=(image["atime_ns"], image["mtime_ns"]))
                _sync_directory(path.parent)
            self._save(
                "private-index-prepared", private_index={"path": str(path), "initial": image}
            )
            yield path

    def publish_index(self, committed_head: str, *, cleanup_proven: bool = False) -> None:
        """Publish the completed private index only under the unchanged real-index lock."""
        commits = [row for row in self._record.get("commands", []) if row["command"] == "commit"]
        evidence = self._record.get("commit_evidence", {})
        if (
            evidence.get("head") != committed_head
            or evidence.get("validated") is not True
            or evidence.get("pinned") is not True
        ):
            raise FinalizationRecoveryError(
                "index publication requires the exact pinned operation commit"
            )
        if (
            cleanup_proven is not True
            or self._index_lock_identity is None
            or not commits
            or any(
                not row["known"]
                or not row["quiescent"]
                or (row["capture"] or {}).get("timed_out")
                or (row["capture"] or {}).get("interrupted")
                for row in self._record.get("commands", [])
            )
            or commits[-1]["capture"].get("returncode") != 0
        ):
            raise FinalizationRecoveryError(
                "private index publication requires acknowledged quiescent commit evidence"
            )
        with self._locked_index():
            self._assert_subject()
            self._assert_index(self._record["index_postimage"])
            if verify.rev_parse_head(self.repo) != committed_head:
                raise FinalizationRecoveryError(
                    "finalization HEAD changed before private index publication"
                )
            private = self._record.get("private_index", {})
            if private.get("path") != str(self.path.parent / "commit-index"):
                raise FinalizationRecoveryError("private finalization index binding is missing")
            image, data = _index_image(Path(private["path"]))
            if data is None:
                raise FinalizationRecoveryError("completed commit has no private index")
            atomic_write_bytes(
                self.path.parent / "index-committed-postimage.bin", data, follow_symlinks=False
            )
            self._save("index-publication-started", private_index={**private, "completed": image})
            published = self._publish_index_bytes(image, data)
            self._save("index-published", published_index=published)

    def completed(self, committed_head: str) -> None:
        if self.state not in {"prepared", "staged", "head-moved", "commit-started"}:
            raise FinalizationRecoveryError("uncertain finalization cannot be declared completed")
        if (
            self._record.get("stage_observation_pending")
            or self._record.get("commit_observation_pending")
            or any(
                not row["known"]
                or not row["quiescent"]
                or (row["capture"] or {}).get("timed_out")
                or (row["capture"] or {}).get("interrupted")
                or (row["capture"] or {}).get("returncode") is None
                or (row["capture"] or {}).get("returncode", -1) < 0
                for row in self._record.get("commands", [])
            )
        ):
            raise FinalizationRecoveryError(
                "finalization completion requires settled command evidence"
            )
        self._assert_subject()
        if self._record.get("commit_expected_head"):
            evidence = self._record.get("commit_evidence", {})
            if (
                evidence.get("head") != committed_head
                or evidence.get("validated") is not True
                or evidence.get("pinned") is not True
            ):
                raise FinalizationRecoveryError(
                    "finalization completion requires its exact operation commit"
                )
        if self._record.get("private_index") and not self._record.get("published_index"):
            raise FinalizationRecoveryError("finalization private index has not been reconciled")
        if verify.rev_parse_head(self.repo) != committed_head:
            raise FinalizationRecoveryError("finalization completion HEAD is not current")
        self._save("completed", state="completed", committed_head=committed_head)

    def uncertain(self, reason: str) -> None:
        if self.state not in {"completed", "refused"}:
            self._save("uncertain", state="uncertain", reason=reason)

    def refused(self, reason: str) -> None:
        """Settle an acknowledged refusal, never infer it from a compensated timeout.

        The caller supplies definitive nonzero-command evidence and quiescence.
        A prepared operation may instead be refused before any mutation starts.
        """
        if any(
            not row["known"]
            or not row["quiescent"]
            or (row["capture"] or {}).get("timed_out")
            or (row["capture"] or {}).get("interrupted")
            or (row["capture"] or {}).get("returncode") is None
            or (row["capture"] or {}).get("returncode", -1) < 0
            for row in self._record.get("commands", [])
        ):
            raise FinalizationRecoveryError(
                "uncertain command evidence cannot become an acknowledged refusal"
            )
        if self._record.get("restoration") == "restored":
            expected = self._record["restored_index"]
        elif self.state == "prepared":
            expected = self._record["index_preimage"]
        else:
            raise FinalizationRecoveryError(
                "finalization refusal requires exact compensation or no started mutation"
            )
        self._assert_subject()
        self._assert_index(expected)
        if verify.rev_parse_head(self.repo) != self._record["original_head"]:
            raise FinalizationRecoveryError("finalization refusal cannot discard a changed HEAD")
        self._save("refused", state="refused", reason=reason, refusal_acknowledged=True)

    def _restore_index(self) -> dict[str, Any]:
        before = self._record["index_preimage"]
        if not before["exists"]:
            self.index_path.unlink(missing_ok=True)
            _sync_directory(self.index_path.parent)
            return dict(before)
        data = self._blob("index-preimage.bin", before)
        return self._publish_index_bytes(before, data)

    def _blob(self, name, image) -> bytes:
        path = self.path.parent / name
        if is_link_like(path):
            raise FinalizationRecoveryError("finalization index evidence is redirected")
        data = path.read_bytes()
        if len(data) != image["size"] or hashlib.sha256(data).hexdigest() != image["sha256"]:
            raise FinalizationRecoveryError("finalization index evidence is missing or changed")
        return data

    def _publish_index_bytes(self, before, data: bytes) -> dict[str, Any]:
        descriptor, name = tempfile.mkstemp(
            prefix=".bmad-finalization-index-", dir=self.index_path.parent
        )
        temporary = Path(name)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temporary, before["mode"])
            os.utime(temporary, ns=(before["atime_ns"], before["mtime_ns"]))
            atomic_replace(temporary, self.index_path)
            _sync_directory(self.index_path.parent)
            return _image_metadata(self.index_path.stat(), data)
        finally:
            temporary.unlink(missing_ok=True)

    def restore(self, *, cleanup_proven: bool = False) -> bool:
        """Compensate exact owned ref/index changes; uncertainty still blocks replay."""
        if (
            cleanup_proven is not True
            or self.state in {"completed", "refused"}
            or self._record.get("stage_observation_pending")
            or self._record.get("commit_observation_pending")
            or any(
                not row["known"] or not row["quiescent"] for row in self._record.get("commands", [])
            )
        ):
            self.uncertain("finalization restoration requires observed-tree cleanup proof")
            return False
        try:
            if self._record.get("restoration"):
                raise FinalizationRecoveryError(
                    "finalization restoration already attempted; reconcile its record"
                )
            expected_index = self._record.get("index_postimage")
            if expected_index is None:
                if self._record.get("staging_index") and not self._record.get("staging_published"):
                    expected_index = self._record["index_preimage"]
                else:
                    raise FinalizationRecoveryError("finalization has no confirmed index postimage")
            return self._restore_locked(expected_index)
        except (OSError, ValueError, verify.GitError) as exc:
            self.uncertain("finalization restoration refused: " + _safe_error(exc))
            return False

    def _restore_locked(self, expected_index) -> bool:
        with self._locked_index():
            if expected_index["exists"] and self._record.get("index_postimage"):
                self._blob("index-postimage.bin", expected_index)
            if self._record["index_preimage"]["exists"]:
                self._blob("index-preimage.bin", self._record["index_preimage"])
            self._assert_subject()
            self._assert_index(expected_index)
            current = verify.rev_parse_head(self.repo)
            original = self._record["original_head"]
            move = self._record.get("head_move")
            allowed = (
                {original} if move is None else {move["expected_old"], move["intended_target"]}
            )
            if current not in allowed:
                raise FinalizationRecoveryError(
                    "finalization HEAD changed; preserve possible landed or concurrent commit"
                )
            if self._record.get("commit_expected_head") not in {None, current}:
                raise FinalizationRecoveryError(
                    "finalization commit effect may already have landed"
                )
            if verify.rev_parse_revision(self.repo, self._record["recovery_ref"]) != original:
                raise FinalizationRecoveryError(
                    "finalization original-history recovery ref changed"
                )
            self._save(
                "restoration-started",
                state="uncertain",
                restoration="pending",
                restore_expected_head=current,
                cleanup_proven=True,
                restore_writer_pending=True,
            )

            def restore_while_prepared(remaining_s: float) -> None:
                del remaining_s
                self._assert_subject()
                self._assert_index(expected_index)
                if verify.rev_parse_head(self.repo) != current:
                    raise FinalizationRecoveryError("finalization HEAD changed during compensation")
                self._record["restored_index"] = self._restore_index()

            update = verify._PreparedRefUpdate(
                ref=self.branch_ref,
                new_oid=original,
                old_oid=current,
                validate_while_prepared=restore_while_prepared,
            )
            try:
                restored = verify.prepared_ref_update(self.repo, update)
            except BaseException as exc:
                self._observe_exception("update-ref", exc)
                raise
            self.observe_command("update-ref", getattr(restored, "owned_capture", None))
            if restored.returncode != 0 or not self._record["commands"][-1]["quiescent"]:
                raise FinalizationRecoveryError("ref compensation cleanup remains uncertain")
            self._assert_subject()
            if verify.rev_parse_head(self.repo) != original:
                raise FinalizationRecoveryError("finalization HEAD changed after compensation")
            self._save(
                "restored",
                state="uncertain",
                restoration="restored",
                reason="Owned ref/index compensated; original effect remains uncertain and must not be replayed",
            )
            return True


@contextlib.contextmanager
def begin(
    repo: Path,
    original_head: str,
    baseline: str,
    message: str,
    *,
    context: dict[str, Any] | None = None,
):
    """Reserve one operation and persist recovery authority before yielding."""
    context = _context_document(context)
    repo = repo.resolve()
    root = _root(repo)
    root.mkdir(parents=True, exist_ok=True)
    with _operation_lock(root / "operation.lock"):
        previous = pending(repo)
        if previous is not None:
            raise FinalizationRecoveryError(
                "unresolved finalization operation "
                + previous["id"]
                + "; inspect "
                + str(root / previous["id"] / "record.json")
            )
        original_head = verify.rev_parse_revision(repo, original_head)
        baseline = verify.rev_parse_revision(repo, baseline)
        if verify.rev_parse_head(repo) != original_head:
            raise FinalizationRecoveryError("finalization original HEAD is no longer current")
        branch = _branch(repo)
        git_dir = Path(_git_text(repo, "rev-parse", "--absolute-git-dir")).resolve()
        index = verify._bound_real_index_path(repo)
        index = index.parent.resolve() / index.name
        image, data = _index_image(index)
        identity = uuid.uuid4().hex
        directory = root / identity
        directory.mkdir(mode=0o700)
        if data is not None:
            atomic_write_bytes(directory / "index-preimage.bin", data, follow_symlinks=False)
        record = {
            "schema_version": 1,
            "id": identity,
            "repo": str(repo),
            "repo_identity": _identity(repo),
            "git_dir": str(git_dir),
            "git_dir_identity": _identity(git_dir),
            "branch_ref": branch,
            "original_head": original_head,
            "baseline": baseline,
            "message": message,
            "context": context,
            "index_path": str(index),
            "index_preimage": image,
            "recovery_ref": f"refs/bmad-finalization/{runs.project_tag(repo)}/{identity}",
            "state": "capturing",
            "events": [],
        }
        transaction = Transaction(repo, directory / "record.json", record)
        transaction._save("captured")
        _write(root / "active.json", {"schema_version": 1, "id": identity})
        failure = None
        try:
            try:
                created = verify.git_bytes(
                    repo, "update-ref", record["recovery_ref"], original_head, ""
                )
            except BaseException as exc:
                transaction._observe_exception("update-ref", exc)
                raise
            transaction.observe_command("update-ref", getattr(created, "owned_capture", None))
            if created.returncode != 0 or not transaction.record["commands"][-1]["quiescent"]:
                raise FinalizationRecoveryError(
                    "recovery-ref creation lacks acknowledged owned process cleanup"
                )
            transaction._assert_subject()
            transaction._assert_index(image)
            if verify.rev_parse_head(repo) != original_head:
                raise FinalizationRecoveryError("finalization HEAD changed during recovery capture")
            transaction._save("protected", state="prepared")
            yield transaction
        except BaseException as exc:
            failure = exc
            transaction._record_failure(exc)
            raise
        finally:
            if failure is None and transaction.state not in {"completed", "refused", "uncertain"}:
                transaction.uncertain("finalization context ended without confirmed completion")
