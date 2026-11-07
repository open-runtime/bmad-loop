"""Durable ownership of a worker's one private native ticket store.

The allocation journal, not an editable marker in the checkout, authorizes
reuse. A completed copy is never refreshed over an actor's private edits.
Prepared copies resume from their captured manifest before any actor launches.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path
from typing import Any

from .campaign_model import CampaignError
from .platform_util import atomic_write_text, file_lock, is_link_like, root_identity_record


def _pin(path: Path) -> list[int]:
    value = root_identity_record(path)
    if value is None:
        raise CampaignError("private source directory cannot establish its identity")
    return list(value)


def _confined(workspace: Path, path: Path) -> None:
    if not path.is_relative_to(workspace) or ".." in path.parts:
        raise CampaignError("private source destination escaped its workspace")
    for candidate in (path, *path.parents):
        if is_link_like(candidate):
            raise CampaignError("private source destination is redirected")
        if candidate == workspace:
            break


def _manifest(root: Path, *, source: bool = True) -> dict[str, Any]:
    from .campaign_closure import _read

    _pin(root)
    result: dict[str, Any] = {}
    total = 0
    for directory, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = sorted(
            name
            for name in dirs
            if not source or (name not in {".git", ".bmad-loop"} and not name.endswith(".lock"))
        )
        for name in (*dirs, *sorted(files)):
            if source and (name in {".git", ".bmad-loop"} or name.endswith(".lock")):
                continue
            path = Path(directory) / name
            if is_link_like(path):
                raise CampaignError("private source copy contains a redirected path")
            mode = path.lstat().st_mode
            if stat.S_ISDIR(mode):
                entry = {"kind": "directory"}
            else:
                data = _read(root, path)
                total += len(data)
                entry = {
                    "kind": "file",
                    "sha256": hashlib.sha256(data).hexdigest(),
                    "mode": stat.S_IMODE(mode),
                }
            result[path.relative_to(root).as_posix()] = entry
            if len(result) > 20000 or total > 64 * 1024 * 1024:
                raise CampaignError("private source copy exceeds its bounded manifest")
    return result


def _save(path: Path, value: dict[str, Any]) -> None:
    atomic_write_text(path, json.dumps(value, sort_keys=True), follow_symlinks=False)


def captured_manifest(
    authority_dir: Path,
    *,
    canonical: Path,
    workspace: Path,
    destination: Path,
) -> dict[str, Any]:
    """Return the completed copy baseline for binding planning's actual preimages."""
    from .campaign_closure import _read

    row = json.loads(_read(authority_dir, authority_dir / "ticket-store.json"))
    identity = row.get("identity", {})
    if (
        row.get("schema_version") != 1
        or row.get("state") != "complete"
        or identity.get("canonical") != str(canonical.resolve())
        or identity.get("canonical_identity") != _pin(canonical)
        or identity.get("workspace") != str(workspace.resolve())
        or identity.get("workspace_identity") != _pin(workspace)
        or identity.get("destination") != str(destination)
        or row.get("destination_identity") != _pin(destination)
    ):
        raise CampaignError("planning source differs from its completed private copy authority")
    _confined(workspace, destination)
    return row["manifest"]


def prepare(
    canonical: Path,
    workspace: Path,
    destination: Path,
    authority_dir: Path,
    *,
    initiative: str,
    store_remote: str,
    store_branch: str,
) -> str:
    """Return the stable default initiative of a proven private copy."""
    from .campaign_closure import _read

    canonical, workspace = canonical.resolve(), workspace.resolve()
    if authority_dir.resolve().is_relative_to(workspace):
        raise CampaignError("private source authority must be outside the editable workspace")
    authority_dir.mkdir(parents=True, exist_ok=True)
    if is_link_like(authority_dir):
        raise CampaignError("private source authority is redirected")
    _confined(workspace, destination)
    identity = {
        "canonical": str(canonical),
        "canonical_identity": _pin(canonical),
        "workspace": str(workspace),
        "workspace_identity": _pin(workspace),
        "destination": str(destination),
        "store_remote": store_remote,
        "store_branch": store_branch,
    }
    path = authority_dir / "ticket-store.json"
    with file_lock(authority_dir / "ticket-store.lock"):
        if path.exists():
            row = json.loads(_read(authority_dir, path).decode("utf-8"))
            if row.get("schema_version") != 1 or row.get("identity") != identity:
                raise CampaignError(
                    "private source origin or publication configuration differs from its binding"
                )
        else:
            if destination.exists():
                raise CampaignError("private source destination already exists without a binding")
            row = {
                "schema_version": 1,
                "identity": identity,
                "state": "prepared",
                "initiative": initiative,
                "manifest": _manifest(canonical),
            }
            _save(path, row)  # Authority precedes creation of any private bytes.
        if row["state"] == "complete":
            if _pin(destination) != row["destination_identity"]:
                raise CampaignError("private source destination identity changed")
            _confined(workspace, destination)
            return row["initiative"]
        if row["state"] != "prepared":
            raise CampaignError("private source copy has an unknown preparation state")
        staging = destination.with_name(destination.name + "-staging")
        _confined(workspace, staging)
        if destination.exists():
            # Atomic rename completed before its journal acknowledgement.
            if (
                _pin(destination) != row.get("staging_identity")
                or _manifest(destination, source=False) != row["manifest"]
            ):
                raise CampaignError("private source pending destination differs from captured copy")
        else:
            if _manifest(canonical) != row["manifest"]:
                raise CampaignError("canonical source changed during private copy preparation")
            destination.parent.mkdir(parents=True, exist_ok=True)
            if staging.exists():
                if _pin(staging) != row.get("staging_identity"):
                    raise CampaignError(
                        "private source staging directory has no matching binding; "
                        "preserve it for manual reconciliation before retrying allocation"
                    )
                existing = _manifest(staging, source=False)
                if any(row["manifest"].get(name) != entry for name, entry in existing.items()):
                    raise CampaignError("private source staging contains unowned changes")
            else:
                staging.mkdir()
                row["staging_identity"] = _pin(staging)
                _save(path, row)
            for relative, entry in sorted(row["manifest"].items()):
                target = staging / relative
                _confined(staging, target)
                if entry["kind"] == "directory":
                    target.mkdir(parents=True, exist_ok=True)
                elif not target.exists():
                    data = _read(canonical, canonical / relative)
                    if hashlib.sha256(data).hexdigest() != entry["sha256"]:
                        raise CampaignError(
                            "canonical source changed during private copy preparation"
                        )
                    target.parent.mkdir(parents=True, exist_ok=True)
                    from .platform_util import atomic_write_bytes_confined

                    atomic_write_bytes_confined(target, data, confine_root=staging)
                    target.chmod(entry["mode"])
            if (
                _manifest(staging, source=False) != row["manifest"]
                or _manifest(canonical) != row["manifest"]
            ):
                raise CampaignError("private source copy failed captured-manifest verification")
            _confined(workspace, destination)
            staging.rename(destination)
        row.update(state="complete", destination_identity=_pin(destination))
        _save(path, row)
        return row["initiative"]
