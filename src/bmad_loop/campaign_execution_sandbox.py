"""OS write confinement for qualified headless execution routes.

The native CLI and its descendants inherit the sandbox. This is a filesystem
write boundary, not a no-tools mode, network policy, or proof of process death.
Native transport qualification and post-session ownership verification remain
required. Unsupported platforms fail rather than running an unsandboxed writer.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path

from . import verify
from .campaign_model import CampaignError
from .platform_util import atomic_write_text, file_lock, is_link_like


def _git(workspace, *argv):
    result = verify.git_bytes(workspace, *argv)
    if result.returncode:
        raise CampaignError(
            "Cannot establish execution Git confinement: "
            + result.stderr.decode("utf-8", "replace")
        )
    return result.stdout.decode("utf-8").strip()


def _path(raw, *, root=None):
    if not isinstance(raw, str) or not raw or "\x00" in raw:
        raise CampaignError("Sandbox grant requires a nonempty path")
    value = Path(raw)
    if root is not None and not value.is_absolute():
        value = root / value
    if not value.is_absolute() or is_link_like(value):
        raise CampaignError("Sandbox paths must be absolute and not redirected")
    resolved = value.resolve()
    if root is not None and not resolved.is_relative_to(root):
        raise CampaignError("Product write grant escapes its owned workspace")
    if resolved == Path(resolved.anchor) or resolved == Path.home():
        raise CampaignError("Sandbox grant cannot expose a filesystem or home root")
    return resolved


def write_paths(grants, directory):
    """Expand exact frozen grants; never infer writable native home/config roots."""
    if grants.get("schema_version") != 1 or not grants.get("config_digest"):
        raise CampaignError("Execution sandbox requires frozen versioned grants")
    workspace = _path(grants.get("workspace"))
    if not workspace.is_dir() or not grants.get("scopes"):
        raise CampaignError("Execution sandbox requires a real workspace and scopes")
    product = []
    for item in [*grants["scopes"], *grants.get("write_paths", [])]:
        if any(char in str(item) for char in "*?["):
            raise CampaignError("Execution sandbox requires concrete paths, not glob grants")
        product.append(_path(item, root=workspace))
    scratch = directory / "sandbox-runtime"
    scratch.mkdir(mode=0o700, exist_ok=True)
    allowed = [*product, scratch.resolve()]
    allowed.extend(_path(item) for item in grants.get("runtime_write_paths", []))
    if grants.get("git_branch"):
        branch = _git(workspace, "symbolic-ref", "--short", "HEAD")
        if branch != grants["git_branch"] or branch in grants.get(
            "protected_branches", ["main", "master"]
        ):
            raise CampaignError("Sandbox Git grant is not bound to an owned non-target branch")
        git_dir = Path(_git(workspace, "rev-parse", "--absolute-git-dir")).resolve()
        common = Path(
            _git(workspace, "rev-parse", "--path-format=absolute", "--git-common-dir")
        ).resolve()
        if git_dir == common:
            raise CampaignError("Execution sandbox requires an isolated Git worktree")
        # Objects are shared by every worktree, including protected branches.
        # A writer gets a separate object bank below, never this common store.
        allowed.append(git_dir)
        for parent in (common / "refs" / "heads", common / "logs" / "refs" / "heads"):
            branch_path = _path(branch, root=parent)
            allowed.extend([branch_path, Path(str(branch_path) + ".lock")])
    readonly = [
        _path(row["path"] if isinstance(row, dict) else row)
        for row in grants.get("read_only_paths", [])
    ]
    if any(read == write or read.is_relative_to(write) for read in readonly for write in allowed):
        raise CampaignError("Read-only retrieval grant overlaps a writable grant")
    # A precise future file scope may need parent directories before the CLI
    # starts. Create only those verified workspace-local parents; do not grant
    # their other children permission to write.
    for path in product:
        if path != workspace:
            path.parent.mkdir(parents=True, exist_ok=True)
    return workspace, tuple(dict.fromkeys(allowed)), scratch


def reject_legacy_bank_alternates(common):
    """Refuse old global exposure without deleting unresolved work or user config."""
    alternates = common / "objects/info/alternates"
    if not alternates.exists():
        return
    if is_link_like(alternates):
        raise CampaignError("Git object alternates are redirected")
    parent = (common / "bmad-loop-object-banks").resolve()
    for line in alternates.read_text(encoding="utf-8").splitlines():
        if not line:
            continue
        # Earlier BMAD bank registrations always used literal absolute paths.
        # Preserve every unrelated alternate, including its exact quoting.
        path = Path(line)
        if not path.is_absolute():
            path = common / "objects" / path
        path = path.resolve()
        if path.parent == parent and re.fullmatch(r"[0-9a-f]{64}", path.name):
            raise CampaignError(
                "Legacy native object bank remains globally visible; migrate its objects "
                "and remove its owned alternate before resuming"
            )


def _object_bank(grants, directory, workspace):
    """Register private per-launch objects without exposing them to trusted Git.

    Only the promotion helper may read banks explicitly. Adding them to common
    alternates is unsafe: a forged loose object can shadow a canonical packed
    object. Banks outlive worktree/job cleanup; a collector must first prove
    reachable objects have been copied and verified in canonical storage.
    """
    if not grants.get("git_branch"):
        return None, None
    common = Path(
        _git(workspace, "rev-parse", "--path-format=absolute", "--git-common-dir")
    ).resolve()
    reject_legacy_bank_alternates(common)
    objects = common / "objects"
    parent = common / "bmad-loop-object-banks"
    identity = hashlib.sha256(
        json.dumps(
            [
                str(directory.resolve()),
                grants["config_digest"],
                str(workspace),
                grants["git_branch"],
            ]
        ).encode()
    ).hexdigest()
    bank = parent / identity
    for path in (parent, bank):
        if is_link_like(path):
            raise CampaignError("Git object-bank registration path is redirected")
        path.mkdir(parents=True, exist_ok=True)
    if any(char in str(bank) for char in '\n\r"'):
        raise CampaignError("Git object bank has an unsupported pathname")
    manifest = parent / (identity + ".json")
    record = {
        "schema_version": 1,
        "path": str(bank),
        "workspace": str(workspace),
        "git_branch": grants["git_branch"],
        "config_digest": grants["config_digest"],
        "launch_directory": str(directory.resolve()),
    }
    with file_lock(common / "bmad-loop-object-banks.lock"):
        if is_link_like(manifest):
            raise CampaignError("Git object bank manifest is redirected")
        if manifest.exists():
            if json.loads(manifest.read_text(encoding="utf-8")) != record:
                raise CampaignError("Git object bank manifest changed")
        else:
            atomic_write_text(manifest, json.dumps(record, sort_keys=True), follow_symlinks=False)
    return bank, objects


def macos_profile(paths, *, object_bank=None):
    """Seatbelt applies write confinement to child tools as well as the native CLI."""
    clauses = []
    for path in paths:
        operator = "literal" if path.is_file() else "subpath"
        clauses.append(f"({operator} {json.dumps(str(path), ensure_ascii=True)})")
    profile = (
        '(version 1)\n(allow default)\n(deny file-write* file-link)\n(allow file-write* (literal "/dev/null") '
        + " ".join(clauses)
        + ")\n"
    )
    if object_bank is not None:
        # Seatbelt permits initial data writes through a newly created file's
        # descriptor, but denies reopening an existing object for mutation.
        # Git closes its temporary object before renaming it to its final hash.
        # Do not permit hardlinks: a writable alias can damage read-only history.
        prefix = re.escape(str(object_bank))
        fanout = prefix + "/[0-9a-f][0-9a-f]"
        # Git's own bounded object-file pattern, not a temp directory.
        temporary = fanout + "/tmp_obj_[A-Za-z0-9]+"  # nosec B108

        def pattern(value):
            # Seatbelt's #"..." reader retains regex escapes verbatim; normal
            # JSON's doubled backslash would match a literal backslash instead.
            return json.dumps(value, ensure_ascii=False).replace("\\\\", "\\")

        profile += (
            "(allow file-write-create (regex #"
            + pattern("^" + fanout + "$")
            + ") (regex #"
            + pattern("^" + fanout + "/[0-9a-f]+$")
            + ") (regex #"
            + pattern("^" + temporary + "$")
            + "))\n(allow file-write-unlink (regex #"
            + pattern("^" + temporary + "$")
            + "))\n"
        )
    return profile


def command(grants_path, native, *, platform=None):
    grants_path = _path(str(grants_path))
    grants = json.loads(grants_path.read_text(encoding="utf-8"))
    workspace, allowed, scratch = write_paths(grants, grants_path.parent)
    if not native or not Path(native[0]).is_absolute():
        raise CampaignError("Qualified native command requires an absolute executable")
    if (platform or sys.platform) != "darwin":
        raise CampaignError("OS execution confinement is not qualified on this platform")
    sandbox = Path("/usr/bin/sandbox-exec")
    if not sandbox.is_file():
        raise CampaignError("Qualified macOS execution sandbox is unavailable")
    bank, objects = _object_bank(grants, grants_path.parent, workspace)
    profile = grants_path.parent / "execution.sb"

    atomic_write_text(profile, macos_profile(allowed, object_bank=bank), follow_symlinks=False)
    environment = dict(os.environ)
    environment.update(TMPDIR=str(scratch), TMP=str(scratch), TEMP=str(scratch))
    if bank is not None:
        environment["GIT_OBJECT_DIRECTORY"] = str(bank)
        environment["GIT_ALTERNATE_OBJECT_DIRECTORIES"] = json.dumps(
            str(objects), ensure_ascii=False
        )
        count = int(environment.get("GIT_CONFIG_COUNT", "0"))
        for key, value in (("core.createObject", "rename"), ("gc.auto", "0")):
            environment[f"GIT_CONFIG_KEY_{count}"] = key
            environment[f"GIT_CONFIG_VALUE_{count}"] = value
            count += 1
        environment["GIT_CONFIG_COUNT"] = str(count)
    return [str(sandbox), "-f", str(profile), *native], workspace, environment


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--grants", required=True, type=Path)
    parser.add_argument("native", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    native = args.native[1:] if args.native[:1] == ["--"] else args.native
    try:
        launch, workspace, environment = command(args.grants, native)
        os.chdir(workspace)
        os.execve(
            launch[0], launch, environment
        )  # nosec B606: fixed absolute sandbox executable; argv never passes through a shell.
    except (CampaignError, OSError, ValueError) as exc:
        print("Execution confinement refused: " + str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
