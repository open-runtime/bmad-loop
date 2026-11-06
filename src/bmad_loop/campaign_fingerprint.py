"""Bounded read-only candidate observations for campaign session verification.

Hash tracked/index content, nonignored untracked files, and explicitly selected
private source trees. Never scan arbitrary ignored dependencies. Link-like paths,
unsupported confinement, changing files and exceeded bounds produce a visible
error; they cannot become an empty healthy fingerprint. File reads use the
portable handle-anchored seam and refuse symlinks at every component.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import stat
from pathlib import Path
from typing import Any

from . import platform_util as fs
from . import verify
from .campaign_model import CampaignError


class FingerprintUnavailable(CampaignError):
    """A complete candidate observation could not be established safely."""


@dataclasses.dataclass(frozen=True)
class CandidateFingerprint:
    head: str
    index: str
    worktree: str
    untracked: str
    source: str
    file_count: int
    byte_count: int

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


def _hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True).encode()).hexdigest()


def _git(root: Path, *args: str) -> bytes:
    result = verify.git_bytes(root, *args, timeout_s=30)
    if result.returncode:
        raise FingerprintUnavailable(f"candidate Git observation failed: {args[0]}")
    return result.stdout


def capture(
    workspace: Path,
    source_roots: tuple[Path, ...] = (),
    exclude_paths: tuple[Path, ...] = (),
    *,
    max_files: int = 50_000,
    max_bytes: int = 256 * 1024 * 1024,
) -> CandidateFingerprint:
    """Observe bounded content; exclusions only name known run-output trees.

    External state/log paths are naturally out of the observation. Inside the
    checkout only .bmad-loop/runs may be excluded: callers cannot hide an owned
    source or arbitrary tracked acceptance files with an exclusion argument.
    """
    workspace = workspace.expanduser().resolve(strict=True)
    identity = os.stat(workspace)
    if not fs.HANDLE_ANCHORED_WRITES:
        raise FingerprintUnavailable("candidate fingerprint requires handle-anchored file reads")
    if max_files < 1 or max_bytes < 1:
        raise FingerprintUnavailable("candidate fingerprint bounds must be positive")
    excluded: list[Path] = []
    known_outputs = workspace / ".bmad-loop" / "runs"

    def anchored(path: Path) -> Path:
        if ".." in path.parts:
            raise FingerprintUnavailable(
                "candidate observation paths cannot contain parent references"
            )
        value = path if path.is_absolute() else workspace / path
        if not value.is_relative_to(workspace):
            raise FingerprintUnavailable("private source must be inside the worker checkout")
        return value

    for path in exclude_paths:
        if not path.is_absolute():
            path = workspace / path
        if ".." in path.parts:
            raise FingerprintUnavailable("candidate exclusions cannot contain parent references")
        if not path.is_relative_to(workspace):
            continue  # explicit external state paths never enter the observation
        if not path.is_relative_to(known_outputs):
            raise FingerprintUnavailable("only known run-output paths may be excluded")
        excluded.append(path)

    def ignored(path: Path) -> bool:
        return any(path == exclusion or path.is_relative_to(exclusion) for exclusion in excluded)

    total_files = total_bytes = 0
    cache: dict[str, tuple[str, int, str]] = {}

    def parent_descriptor(path: Path) -> int | None:
        """Distinguish an ordinary deletion from a refused parent redirect."""
        descriptor = fs.open_dir_confined(workspace, workspace, root_identity=identity)
        if descriptor is None:
            raise FingerprintUnavailable("candidate root identity is unavailable")
        try:
            for component in path.relative_to(workspace).parts[:-1]:
                try:
                    child = fs.open_at(
                        descriptor,
                        component,
                        os.O_RDONLY | fs.AT_DIRECTORY | fs.AT_NOFOLLOW | fs.AT_NONBLOCK,
                    )
                except FileNotFoundError:
                    os.close(descriptor)
                    return None
                except OSError as exc:
                    raise FingerprintUnavailable(
                        f"candidate parent is redirected or unavailable: {path}"
                    ) from exc
                os.close(descriptor)
                descriptor = child
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise

    def file_record(path: Path) -> tuple[str, int, str]:
        nonlocal total_files, total_bytes
        relative = path.relative_to(workspace).as_posix()
        if relative in cache:
            return cache[relative]
        parent = parent_descriptor(path)
        if parent is None:
            return (relative, 0, "missing")
        try:
            try:
                info = fs.stat_at(parent, path.name)
            except FileNotFoundError:
                return (relative, 0, "missing")
            if fs.link_like_stat(info) or not stat.S_ISREG(info.st_mode):
                raise FingerprintUnavailable(
                    f"candidate path is link-like or nonregular: {relative}"
                )
            if total_files >= max_files or total_bytes + info.st_size > max_bytes:
                raise FingerprintUnavailable(
                    "candidate fingerprint exceeded configured file/byte bounds"
                )
            try:
                descriptor = fs.open_at(
                    parent, path.name, os.O_RDONLY | fs.AT_NOFOLLOW | fs.AT_NONBLOCK
                )
            except OSError as exc:
                raise FingerprintUnavailable(
                    f"candidate content could not be opened safely: {relative}"
                ) from exc
            try:
                opened = os.fstat(descriptor)
                if (opened.st_dev, opened.st_ino, opened.st_size) != (
                    info.st_dev,
                    info.st_ino,
                    info.st_size,
                ) or not stat.S_ISREG(opened.st_mode):
                    raise FingerprintUnavailable(
                        f"candidate content changed during open: {relative}"
                    )
                digest = hashlib.sha256()
                read = 0
                while True:
                    chunk = os.read(descriptor, min(65536, max_bytes - total_bytes - read + 1))
                    if not chunk:
                        break
                    read += len(chunk)
                    if total_bytes + read > max_bytes:
                        raise FingerprintUnavailable(
                            "candidate fingerprint exceeded configured byte bound"
                        )
                    digest.update(chunk)
                after = os.fstat(descriptor)

                def stable(value):
                    return (
                        value.st_dev,
                        value.st_ino,
                        value.st_size,
                        value.st_mtime_ns,
                        value.st_ctime_ns,
                    )

                if stable(opened) != stable(after) or read != opened.st_size:
                    raise FingerprintUnavailable(
                        f"candidate content changed while reading: {relative}"
                    )
            finally:
                os.close(descriptor)
            record = (relative, stat.S_IMODE(info.st_mode), digest.hexdigest())
            total_files += 1
            total_bytes += read
            cache[relative] = record
            return record
        finally:
            os.close(parent)

    def entries(raw: bytes) -> tuple[Path, ...]:
        paths = []
        for name in raw.split(b"\0"):
            if name:
                path = anchored(Path(os.fsdecode(name)))
                if not ignored(path):
                    paths.append(path)
        if len(paths) > max_files:
            raise FingerprintUnavailable("candidate path inventory exceeded configured bound")
        return tuple(paths)

    head = _git(workspace, "rev-parse", "HEAD").decode().strip()
    index_bytes = _git(workspace, "ls-files", "--stage", "-z")
    flags = _git(workspace, "ls-files", "-v", "-z")
    tracked = entries(_git(workspace, "ls-files", "-z"))
    untracked = entries(_git(workspace, "ls-files", "--others", "--exclude-standard", "-z"))
    worktree_records = [file_record(path) for path in sorted(set(tracked))]
    untracked_records = [file_record(path) for path in sorted(set(untracked))]
    sources: list[tuple[str, int, str]] = []

    def walk(path: Path) -> None:
        if ignored(path):
            return
        directory = fs.open_dir_confined(workspace, path, root_identity=identity)
        if directory is None:
            # A regular leaf is read through its confined parent; a missing root
            # is an incomplete source observation and must refuse instead.
            try:
                info = os.lstat(path)
            except OSError as exc:
                raise FingerprintUnavailable(f"private source is unavailable: {path}") from exc
            if fs.link_like_stat(info) or not stat.S_ISREG(info.st_mode):
                raise FingerprintUnavailable(f"private source is redirected or nonregular: {path}")
            sources.append(file_record(path))
            return
        try:
            # Names only; every subsequent content open is independently rooted
            # in the pinned workspace descriptor. On POSIX enumerate that actual
            # directory descriptor too, eliminating a parent-name race.
            listing: Any = directory if os.scandir in os.supports_fd else path
            with os.scandir(listing) as iterator:
                names = sorted(entry.name for entry in iterator)
            for name in names:
                if len(sources) >= max_files:
                    raise FingerprintUnavailable(
                        "private source inventory exceeded configured bound"
                    )
                child = path / name
                if ignored(child):
                    continue
                info = fs.stat_at(directory, name)
                if fs.link_like_stat(info):
                    raise FingerprintUnavailable(
                        f"private source contains a link-like path: {child}"
                    )
                if stat.S_ISDIR(info.st_mode):
                    walk(child)
                elif stat.S_ISREG(info.st_mode):
                    sources.append(file_record(child))
                else:
                    raise FingerprintUnavailable(
                        f"private source contains a nonregular path: {child}"
                    )
        finally:
            os.close(directory)

    for root in sorted({anchored(path) for path in source_roots}):
        walk(root)
    if (
        _git(workspace, "rev-parse", "HEAD").decode().strip() != head
        or _git(workspace, "ls-files", "--stage", "-z") != index_bytes
        or _git(workspace, "ls-files", "-v", "-z") != flags
    ):
        raise FingerprintUnavailable("candidate HEAD/index changed during observation")
    fs.require_root_pinned(workspace, identity)
    return CandidateFingerprint(
        head,
        hashlib.sha256(index_bytes + b"\0" + flags).hexdigest(),
        _hash(worktree_records),
        _hash(untracked_records),
        _hash(sources),
        total_files,
        total_bytes,
    )
