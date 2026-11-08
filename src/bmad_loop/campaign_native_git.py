"""Private native refs/index; publish only sealed objects to the shared clone.

An untrusted writer's commit must never expose a bank-only object through shared
refs: unrelated fetches traverse those refs concurrently. The native Git view
keeps refs and staging private, while controller publication seals object graphs,
checks append-only ancestry and compare-and-swaps the exact owned branch.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from contextlib import contextmanager
from pathlib import Path

from . import verify
from .campaign_model import CampaignError
from .platform_util import atomic_write_text, file_lock, is_link_like


def _git(workspace, *args, env=None, check=True):
    result = verify.git_bytes(workspace, "--no-replace-objects", *args, env=env)
    if check and result.returncode:
        raise CampaignError(
            "Native Git publication failed: " + result.stderr.decode("utf-8", "replace")[-2000:]
        )
    return result


def _text(workspace, *args, env=None):
    return _git(workspace, *args, env=env).stdout.decode().strip()


def _audit_index(workspace, *, env=None):
    for record in _git(workspace, "ls-files", "-v", "-z", env=env).stdout.split(b"\0"):
        if record and (record[:1].islower() or record[:1] == b"S"):
            raise CampaignError(
                "Native Git index hides paths with assume-unchanged or skip-worktree flags; explicit reconciliation required"
            )


def _index_identity(workspace, *, env=None):
    """Bind staged paths, modes and flags, excluding harmless stat-cache refreshes."""
    return hashlib.sha256(
        _git(workspace, "ls-files", "--stage", "-v", "-z", env=env).stdout
    ).hexdigest()


def environment(record):
    return {
        **{key: value for key, value in os.environ.items() if not key.startswith("GIT_")},
        "GIT_DIR": record["git_dir"],
        "GIT_WORK_TREE": record["workspace"],
        "GIT_OBJECT_DIRECTORY": record["bank"],
        "GIT_ALTERNATE_OBJECT_DIRECTORIES": json.dumps(record["objects"], ensure_ascii=False),
        "GIT_NO_REPLACE_OBJECTS": "1",
    }


def prepare(bank, objects, workspace, directory, branch):
    """Initialize once under controller authority; later native calls reuse staging."""
    common = objects.parent
    path = bank.parent / (bank.name + ".native")
    private = bank.parent / (bank.name + ".git")
    identity = {
        "schema_version": 1,
        "workspace": str(workspace),
        "git_dir": str(private),
        "bank": str(bank),
        "objects": str(objects),
        "branch": branch,
        "launch_directory": str(directory.resolve()),
    }
    with file_lock(common / "bmad-loop-native-git.lock"):
        if is_link_like(path) or is_link_like(private):
            raise CampaignError("Native Git registration is redirected")
        if path.exists():
            record = json.loads(path.read_text(encoding="utf-8"))
            if (
                any(record.get(key) != value for key, value in identity.items())
                or not private.is_dir()
            ):
                raise CampaignError("Native Git registration differs from frozen launch")
            return record
        if private.exists():
            raise CampaignError(
                "Native Git initialization is incomplete; preserve and reconcile its directory"
            )
        _audit_index(workspace)
        baseline = _text(workspace, "rev-parse", "HEAD")
        format_name = _text(workspace, "rev-parse", "--show-object-format")
        _git(
            workspace,
            "init",
            "--bare",
            "--template=",
            "--object-format=" + format_name,
            str(private),
        )
        hooks = Path(_text(workspace, "rev-parse", "--path-format=absolute", "--git-path", "hooks"))
        worktree_config = Path(
            _text(workspace, "rev-parse", "--path-format=absolute", "--git-path", "config.worktree")
        )
        configurations = [(common / "config", private / "config")]
        if worktree_config.exists():
            configurations.append((worktree_config, private / "config.worktree"))
        for source, target in configurations:
            if is_link_like(source):
                raise CampaignError("Canonical Git configuration is redirected")
            shutil.copyfile(source, target)
            # An enabled worktreeConfig layer wins over common config. Preserve
            # its signing/hooks policy, but bind both layers to this private view.
            for key, value in (
                ("core.bare", "false"),
                ("core.worktree", str(workspace)),
                ("core.hooksPath", str(hooks)),
            ):
                _git(workspace, "config", "--file", str(target), key, value)
        # Keep ordinary main/origin/main/tag inspection available without
        # exposing canonical refs to native writes. The owned loose ref below
        # overrides this immutable launch-time snapshot.
        references = _text(workspace, "for-each-ref", "--format=%(objectname) %(refname)")
        atomic_write_text(
            private / "packed-refs",
            "# pack-refs with: sorted\n" + (references + "\n" if references else ""),
            follow_symlinks=False,
        )
        atomic_write_text(
            private / "HEAD", "ref: refs/heads/" + branch + "\n", follow_symlinks=False
        )
        ref = private / "refs/heads" / branch
        ref.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(ref, baseline + "\n", follow_symlinks=False)
        index = Path(_text(workspace, "rev-parse", "--path-format=absolute", "--git-path", "index"))
        if is_link_like(index):
            raise CampaignError("Canonical Git index is redirected")
        if index.exists():
            shutil.copyfile(index, private / "index")
        record = {
            **identity,
            "baseline": baseline,
            "initial_index": _index_identity(workspace, env=environment(identity)),
        }
        atomic_write_text(path, json.dumps(record, sort_keys=True), follow_symlinks=False)
        return record


def write_paths(record):
    """Permit normal commit/reset metadata, never another ref or shared metadata."""
    private = Path(record["git_dir"])
    paths = [
        private / name
        for name in (
            "index",
            "index.lock",
            "HEAD",
            "HEAD.lock",
            "COMMIT_EDITMSG",
            "ORIG_HEAD",
            "ORIG_HEAD.lock",
            "MERGE_HEAD",
            "MERGE_MSG",
            "MERGE_MODE",
            "AUTO_MERGE",
            "AUTO_MERGE.lock",
            # Git locks the packed-ref snapshot when deleting AUTO_MERGE even
            # when that pseudo-ref has no packed entry. The snapshot itself
            # remains immutable, so pack-refs cannot replace protected refs.
            "packed-refs.lock",
            "CHERRY_PICK_HEAD",
            "REVERT_HEAD",
            "sequencer",
            "rebase-merge",
            "rebase-apply",
            "logs",
        )
    ]
    ref = private / "refs/heads" / record["branch"]
    paths.extend((ref, Path(str(ref) + ".lock")))
    return tuple(paths)


def _records(workspace, common, *, directory=None, run_id=None):
    parent = common / "bmad-loop-object-banks"
    for path in sorted(parent.glob("*.native")):
        if is_link_like(path) or not re.fullmatch(r"[0-9a-f]{64}", path.stem):
            raise CampaignError("Native Git registration path is invalid")
        row = json.loads(path.read_text(encoding="utf-8"))
        if row.get("workspace") != str(workspace.resolve()):
            continue
        launch = Path(row["launch_directory"])
        if directory is not None and launch != directory.resolve():
            continue
        if run_id is not None and run_id not in launch.parts:
            continue
        if (
            row.get("schema_version") != 1
            or row.get("bank") != str(parent / path.stem)
            or row.get("git_dir") != str(parent / (path.stem + ".git"))
            or row.get("objects") != str(common / "objects")
            or any(is_link_like(Path(row[key])) for key in ("bank", "git_dir", "objects"))
        ):
            raise CampaignError("Native Git registration changed")
        yield row


def _inode(path):
    try:
        stat = path.lstat()
    except FileNotFoundError:
        return None
    return [stat.st_dev, stat.st_ino]


@contextmanager
def _index_lock(workspace):
    """Use Git's own lock; reclaim only our exact dead process's lock inode."""
    from .process_host import get_process_host

    index = Path(_text(workspace, "rev-parse", "--path-format=absolute", "--git-path", "index"))
    lock = Path(str(index) + ".lock")
    lease = index.parent / "bmad-loop-native-index-lease.json"
    if any(is_link_like(path) for path in (index, lock, lease)):
        raise CampaignError("Canonical Git index or lock is redirected")
    host = get_process_host()
    identity = host.identity(os.getpid())
    if identity is None:
        raise CampaignError("Cannot establish native index publisher identity")
    if lock.exists():
        prior = json.loads(lease.read_text(encoding="utf-8")) if lease.exists() else {}
        if (
            prior.get("index") != str(index)
            or prior.get("inode") != _inode(lock)
            or type(prior.get("pid")) is not int
            or not prior.get("identity")
            or host.liveness_of(prior["pid"], prior["identity"]) != "dead"
        ):
            raise CampaignError("Canonical Git index is locked by another or uncertain writer")
        lock.unlink()
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise CampaignError("Canonical Git index acquired by another writer") from exc
    stat = os.fstat(fd)
    owned = [stat.st_dev, stat.st_ino]
    try:
        atomic_write_text(
            lease,
            json.dumps(
                {"index": str(index), "inode": owned, "pid": os.getpid(), "identity": identity}
            ),
            follow_symlinks=False,
        )
        yield index, lock, fd
    finally:
        os.close(fd)
        if _inode(lock) == owned:
            lock.unlink()
        if lease.exists() and json.loads(lease.read_text(encoding="utf-8")).get("inode") == owned:
            lease.unlink()


def _desired_index(workspace, row, tree):
    path = Path(row["bank"]).with_suffix(".index")
    if is_link_like(path):
        raise CampaignError("Native publication index is redirected")
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env["GIT_INDEX_FILE"] = str(path)
    _git(workspace, "read-tree", tree, env=env)
    return path, _index_identity(workspace, env=env)


def _install_index(index, lock, fd, desired):
    # Holding this exact lock excludes ordinary git add/reset/status writers.
    # Never rename a lock replaced by another process.
    stat = os.fstat(fd)
    if _inode(lock) != [stat.st_dev, stat.st_ino]:
        raise CampaignError("Canonical Git index lock identity changed")
    with os.fdopen(os.dup(fd), "wb") as stream:
        stream.write(desired.read_bytes())
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(lock, index)


def _apply_publication(workspace, row, path, pending, locked):
    index, lock, fd = locked
    current_index = _index_identity(workspace)
    if current_index not in {pending["prior_index"], pending["desired_index"]}:
        raise CampaignError(
            "Canonical staging changed during native publication; reconcile explicitly"
        )
    current = _text(workspace, "rev-parse", "HEAD")
    if current == pending["before"]:
        _git(
            workspace,
            "update-ref",
            "refs/heads/" + row["branch"],
            pending["head"],
            pending["before"],
        )
    elif current != pending["head"]:
        raise CampaignError("Unfinished native publication diverged from the owned branch")
    if current_index != pending["desired_index"]:
        desired, identity = _desired_index(workspace, row, pending["tree"])
        if identity != pending["desired_index"]:
            raise CampaignError("Native publication staged-tree identity changed")
        _install_index(index, lock, fd, desired)
    else:
        # No install consumed the lock. Release it before recording completion,
        # so a death immediately after the receipt cannot strand an otherwise
        # completed no-op publication behind our own stale Git lock.
        stat = os.fstat(fd)
        if _inode(lock) != [stat.st_dev, stat.st_ino]:
            raise CampaignError("Canonical Git index lock identity changed")
        lock.unlink()
    pending["state"] = "complete"
    atomic_write_text(path, json.dumps(pending, sort_keys=True), follow_symlinks=False)


def _finish_publication(workspace, row, *, head=None, before=None, tree=None):
    """Recover exact prior/desired staging; never overwrite an intervening owner."""
    path = Path(row["bank"]).with_suffix(".publication")
    if is_link_like(path):
        raise CampaignError("Native publication receipt is redirected")
    prior = json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
    if prior:
        if (
            prior.get("workspace") != str(workspace)
            or prior.get("branch") != row["branch"]
            or prior.get("state") not in {"pending", "complete"}
            or any(
                not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", str(prior.get(key, "")))
                for key in ("head", "before", "tree")
            )
            or any(
                not re.fullmatch(r"[0-9a-f]{64}", str(prior.get(key, "")))
                for key in ("prior_index", "desired_index")
            )
        ):
            raise CampaignError("Native publication receipt identity differs; reconcile explicitly")
        if prior["state"] == "pending":
            with _index_lock(workspace) as locked:
                _apply_publication(workspace, row, path, prior, locked)
    if head is None or (
        prior and prior["state"] == "complete" and prior["head"] == head and prior["tree"] == tree
    ):
        return
    expected = prior["desired_index"] if prior else row.get("initial_index")
    with _index_lock(workspace) as locked:
        if _index_identity(workspace) != expected:
            raise CampaignError(
                "Canonical staging changed since native launch/publication; reconcile explicitly"
            )
        _, desired_identity = _desired_index(workspace, row, tree)
        pending = {
            "state": "pending",
            "workspace": str(workspace),
            "branch": row["branch"],
            "head": head,
            "before": before,
            "tree": tree,
            "prior_index": expected,
            "desired_index": desired_identity,
        }
        atomic_write_text(path, json.dumps(pending, sort_keys=True), follow_symlinks=False)
        _apply_publication(workspace, row, path, pending, locked)


def publish(workspace, baseline, *, directory=None, run_id=None, final=False):
    """Publish an exact sealed graph; leave live native refs/index untouched.

    Checkpoints give the controller an index at the published head, so dirty
    working bytes remain visible. At quiescence, seal the private index tree too
    and reconstruct canonical staging without copying untrusted index metadata.
    """
    if final and directory is None:
        raise CampaignError("Final native publication requires its exact launch directory")
    workspace = Path(workspace).resolve()
    common = Path(
        _text(workspace, "rev-parse", "--path-format=absolute", "--git-common-dir")
    ).resolve()
    if not (common / "bmad-loop-object-banks").exists():
        return _text(workspace, "rev-parse", "HEAD")
    from .campaign_object_promotion import promote

    with file_lock(common / "bmad-loop-native-publication.lock"):
        for row in _records(workspace, common, directory=directory, run_id=run_id):
            env = environment(row)
            branch = _text(workspace, "symbolic-ref", "--short", "HEAD", env=env)
            if (
                branch != row["branch"]
                or _text(workspace, "symbolic-ref", "--short", "HEAD") != branch
            ):
                raise CampaignError("Native Git branch identity changed")
            _finish_publication(workspace, row)
            candidate = _text(workspace, "rev-parse", "HEAD", env=env)
            if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", candidate):
                raise CampaignError("Native Git head is not an object identity")
            if _git(
                workspace,
                "merge-base",
                "--is-ancestor",
                row["baseline"],
                candidate,
                env=env,
                check=False,
            ).returncode:
                raise CampaignError("Native writer rewrote its launch ancestry")
            before = _text(workspace, "rev-parse", "HEAD")
            if final:
                _audit_index(workspace, env=env)
            tree = _text(workspace, "write-tree", env=env) if final else None
            promote(workspace, candidate, baseline, roots=(tree,) if tree else ())
            if _git(
                workspace, "merge-base", "--is-ancestor", before, candidate, check=False
            ).returncode:
                if (
                    not final
                    and not _git(
                        workspace, "merge-base", "--is-ancestor", candidate, before, check=False
                    ).returncode
                ):
                    continue  # An earlier session from this worker already published.
                raise CampaignError("Native candidate diverged from the published branch")
            if candidate != before or final:
                _finish_publication(
                    workspace,
                    row,
                    head=candidate,
                    before=before,
                    tree=tree or _text(workspace, "rev-parse", candidate + "^{tree}"),
                )
        return _text(workspace, "rev-parse", "HEAD")
