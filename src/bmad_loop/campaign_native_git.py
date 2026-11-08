"""Private native refs/index; publish only sealed objects to the shared clone.

An untrusted writer's commit must never expose a bank-only object through shared
refs: unrelated fetches traverse those refs concurrently. The native Git view
keeps refs and staging private, while controller publication seals object graphs,
checks append-only ancestry and compare-and-swaps the exact owned branch.
"""

from __future__ import annotations

import json
import os
import re
import shutil
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
        config = common / "config"
        if is_link_like(config):
            raise CampaignError("Canonical Git configuration is redirected")
        shutil.copyfile(config, private / "config")
        _git(workspace, "config", "--file", str(private / "config"), "core.bare", "false")
        _git(
            workspace, "config", "--file", str(private / "config"), "core.worktree", str(workspace)
        )
        hooks = Path(_text(workspace, "rev-parse", "--path-format=absolute", "--git-path", "hooks"))
        _git(workspace, "config", "--file", str(private / "config"), "core.hooksPath", str(hooks))
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
        record = {**identity, "baseline": baseline}
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


def _finish_publication(workspace, row, *, head=None, before=None, tree=None):
    """Recover the ref/index boundary without replaying unrelated staging."""
    path = Path(row["bank"]).with_suffix(".publication")
    if is_link_like(path):
        raise CampaignError("Native publication receipt is redirected")
    prior = json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
    if prior and prior.get("state") == "pending":
        if (
            prior.get("workspace") != str(workspace)
            or prior.get("branch") != row["branch"]
            or any(
                not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", str(prior.get(key, "")))
                for key in ("head", "before", "tree")
            )
        ):
            raise CampaignError("Native publication receipt identity differs")
        current = _text(workspace, "rev-parse", "HEAD")
        if current == prior["before"]:
            _git(
                workspace,
                "update-ref",
                "refs/heads/" + row["branch"],
                prior["head"],
                prior["before"],
            )
        elif current != prior["head"]:
            raise CampaignError("Unfinished native publication diverged from the owned branch")
        _git(workspace, "read-tree", prior["tree"])
        prior["state"] = "complete"
        atomic_write_text(path, json.dumps(prior, sort_keys=True), follow_symlinks=False)
    if head is not None:
        pending = {
            "state": "pending",
            "workspace": str(workspace),
            "branch": row["branch"],
            "head": head,
            "before": before,
            "tree": tree,
        }
        atomic_write_text(path, json.dumps(pending, sort_keys=True), follow_symlinks=False)
        _finish_publication(workspace, row)


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
