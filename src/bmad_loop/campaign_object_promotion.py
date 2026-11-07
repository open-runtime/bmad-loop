"""Seal native writer objects into controller-owned Git storage before acceptance.

Per-launch object banks remain writable to their native process, including through
open descriptors. A checkpoint cannot depend on those bytes. Repack the exact
new reachable graph without reusing compressed object data, independently check
the pack, and install fresh controller-owned inodes. Verification deliberately
uses a canonical-only object view, never the repository's bank alternates.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path

from . import verify
from .campaign_model import CampaignError
from .platform_util import file_lock, is_link_like


def _git(repo, *args, env=None, input_data=None, check=True):
    result = verify.git_bytes(repo, "--no-replace-objects", *args, env=env, input_data=input_data)
    if check and result.returncode:
        raise CampaignError(
            "Git object promotion failed: "
            + result.stderr.decode("utf-8", errors="replace")[-2000:]
        )
    return result


def _environment(objects):
    return {
        **{key: value for key, value in os.environ.items() if not key.startswith("GIT_")},
        "GIT_OBJECT_DIRECTORY": str(objects),
        "GIT_ALTERNATE_OBJECT_DIRECTORIES": "",
        "GIT_NO_REPLACE_OBJECTS": "1",
    }


def _banks(parent: Path, workspace: Path) -> tuple[Path, ...]:
    """Only controller-registered banks belonging to this exact workspace."""
    result = []
    for path in sorted(parent.glob("*.json")):
        if is_link_like(path):
            raise CampaignError("Git object bank manifest is redirected")
        row = json.loads(path.read_text(encoding="utf-8"))
        if row.get("workspace") != str(workspace.resolve()):
            continue
        bank = parent / path.stem
        if (
            row.get("schema_version") != 1
            or row.get("path") != str(bank)
            or not re.fullmatch(r"[0-9a-f]{64}", path.stem)
            or is_link_like(bank)
            or not bank.is_dir()
        ):
            raise CampaignError("Git object bank manifest does not bind its storage")
        result.append(bank)
    return tuple(result)


def promote(workspace: Path, head: str, baseline: str) -> dict[str, str]:
    """Return only after the exact head's new graph is independent of live banks.

    Legacy workers with no registered banks retain their existing Git path. A
    bank-only baseline is refused: excluding its graph would silently leave an
    earlier writer's mutable objects in an otherwise accepted checkpoint.
    """
    common = Path(
        _git(workspace, "rev-parse", "--path-format=absolute", "--git-common-dir")
        .stdout.decode()
        .strip()
    ).resolve()
    banks = common / "bmad-loop-object-banks"
    if not banks.exists():
        return {"state": "not_required", "head": head}
    from .campaign_execution_sandbox import reject_legacy_bank_alternates

    reject_legacy_bank_alternates(common)
    if not all(
        isinstance(value, str) and re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", value)
        for value in (head, baseline)
    ):
        raise CampaignError("Git object promotion requires exact head and baseline object IDs")
    objects = common / "objects"
    if any(is_link_like(path) for path in (banks, objects, objects / "pack")):
        raise CampaignError("Git object promotion storage is redirected")
    with file_lock(common / "bmad-loop-object-promotion.lock"):
        (objects / "pack").mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="bmad-object-promotion-", dir=common) as name:
            directory = Path(name)
            view = directory / "canonical"
            view.mkdir()
            # No info/alternates: explicit environment settings alone do not
            # suppress Git's on-disk alternates file.
            for entry in objects.iterdir():
                if entry.name == "pack" or re.fullmatch(r"[0-9a-f]{2}", entry.name):
                    if is_link_like(entry):
                        raise CampaignError("Git canonical object storage is redirected")
                    (view / entry.name).symlink_to(entry, target_is_directory=True)
            canonical_env = _environment(view)
            if _git(
                workspace, "cat-file", "-e", baseline + "^{commit}", env=canonical_env, check=False
            ).returncode:
                raise CampaignError(
                    "Git object promotion baseline is not sealed in canonical storage"
                )
            complete = ("rev-list", "--objects", "--missing=error", head, "^" + baseline)
            if not _git(workspace, *complete, env=canonical_env, check=False).returncode:
                return {"state": "sealed", "head": head, "baseline": baseline}
            source_env = _environment(view)
            source_env["GIT_ALTERNATE_OBJECT_DIRECTORIES"] = os.pathsep.join(
                json.dumps(str(path), ensure_ascii=False) for path in _banks(banks, workspace)
            )
            digest = (
                _git(
                    workspace,
                    "pack-objects",
                    "--revs",
                    "--no-reuse-object",
                    "--no-reuse-delta",
                    str(directory / "pack"),
                    env=source_env,
                    input_data=(head + "\n^" + baseline + "\n").encode("ascii"),
                )
                .stdout.decode("ascii")
                .strip()
            )
            if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", digest):
                raise CampaignError("Git object promotion returned an invalid pack identity")
            pack = directory / ("pack-" + digest + ".pack")
            _git(workspace, "index-pack", "--strict", "--verify", str(pack), env=canonical_env)
            for suffix in (".pack", ".idx"):
                source = pack.with_suffix(suffix)
                target = objects / "pack" / source.name
                # A matching canonical pack is already controller owned. Never
                # replace its bytes or import hardlinks from a writable bank.
                if is_link_like(target):
                    raise CampaignError("Git object promotion target is redirected")
                if not target.exists():
                    with source.open("rb") as stream:
                        os.fsync(stream.fileno())
                    os.replace(source, target)
            if os.name != "nt":
                descriptor = os.open(objects / "pack", os.O_RDONLY)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
            _git(workspace, *complete, env=canonical_env)
            return {"state": "sealed", "head": head, "baseline": baseline, "pack": digest}
