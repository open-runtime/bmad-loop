"""OS write confinement for qualified headless execution routes.

The native CLI and its descendants inherit the sandbox. This is a filesystem
write boundary, not a no-tools mode, network policy, or proof of process death.
Native transport qualification and post-session ownership verification remain
required. Unsupported platforms fail rather than running an unsandboxed writer.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from . import verify
from .campaign_model import CampaignError
from .platform_util import is_link_like


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
        allowed.extend([git_dir, common / "objects"])
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


def macos_profile(paths):
    """Seatbelt applies write confinement to child tools as well as the native CLI."""
    clauses = []
    for path in paths:
        operator = "literal" if path.is_file() else "subpath"
        clauses.append(f"({operator} {json.dumps(str(path), ensure_ascii=True)})")
    return (
        '(version 1)\n(allow default)\n(deny file-write*)\n(allow file-write* (literal "/dev/null") '
        + " ".join(clauses)
        + ")\n"
    )


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
    profile = grants_path.parent / "execution.sb"
    from .platform_util import atomic_write_text

    atomic_write_text(profile, macos_profile(allowed), follow_symlinks=False)
    environment = dict(os.environ)
    environment.update(TMPDIR=str(scratch), TMP=str(scratch), TEMP=str(scratch))
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
