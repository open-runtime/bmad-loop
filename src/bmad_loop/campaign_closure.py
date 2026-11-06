"""Confine epic closure to its native epic and retrospective product files.

The closure writer receives a frozen private tree and exact canonical child
proof. Its result is local work only: verification, independent full review,
merge and canonical publication remain mandatory. Child plans and metadata are
immutable throughout this stage, including ignored private source files.
"""

from __future__ import annotations

import base64
import hashlib
import os
import stat
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import frontmatter
from . import platform_util as fs
from . import ticketlayout, verify
from .campaign_model import CampaignError, SourceSelector, UnitSpec
from .campaign_sources import source_paths
from .tickettree import TicketTree

if TYPE_CHECKING:
    from .campaign_source_publication import ArtifactChange


def _read(workspace: Path, path: Path) -> bytes:
    if not path.is_relative_to(workspace) or ".." in path.parts:
        raise CampaignError("closure source escaped its owned checkout")
    parent = fs.open_dir_confined(workspace, path.parent, root_identity=os.stat(workspace))
    if parent is None:
        raise CampaignError("closure source parent is redirected or unavailable")
    try:
        descriptor = fs.open_at(parent, path.name, os.O_RDONLY | fs.AT_NOFOLLOW | fs.AT_NONBLOCK)
        try:
            before = os.fstat(descriptor)
            if not stat.S_ISREG(before.st_mode) or before.st_size > 8 * 1024 * 1024:
                raise CampaignError("closure artifact is not a bounded regular document")
            chunks, size = [], 0
            while chunk := os.read(descriptor, 65536):
                size += len(chunk)
                if size > 8 * 1024 * 1024:
                    raise CampaignError("closure source exceeds its read bound")
                chunks.append(chunk)
            after = os.fstat(descriptor)

            def stable(value):
                return (
                    value.st_dev,
                    value.st_ino,
                    value.st_size,
                    value.st_mtime_ns,
                    value.st_ctime_ns,
                )

            if stable(before) != stable(after):
                raise CampaignError("closure source changed during observation")
            return b"".join(chunks)
        finally:
            os.close(descriptor)
    except OSError as exc:
        raise CampaignError("closure source could not be read without redirects") from exc
    finally:
        os.close(parent)


def _tree(workspace: Path, root: Path) -> dict[str, str]:
    if not root.is_relative_to(workspace) or fs.is_link_like(root):
        raise CampaignError("closure private source root is unavailable or redirected")
    files: dict[str, str] = {}
    size = 0
    for directory, directories, names in os.walk(root, followlinks=False):
        for name in (*directories, *names):
            if fs.is_link_like(Path(directory) / name):
                raise CampaignError("closure source contains a redirected path")
        for name in names:
            path = Path(directory) / name
            data = _read(workspace, path)
            size += len(data)
            if len(files) >= 10000 or size > 64 * 1024 * 1024:
                raise CampaignError("closure private source exceeds file/byte bounds")
            files[path.relative_to(workspace).as_posix()] = hashlib.sha256(data).hexdigest()
    return files


def children_terminal(workspace: Path, source: SourceSelector, unit: UnitSpec) -> None:
    """Check every native child, irrespective of the campaign selection."""
    paths = source_paths(workspace, source)
    tree = TicketTree(workspace, folder=paths.initiative_folder)
    board = tree.status()
    if board.problems:
        raise CampaignError("closure source tree has unresolved native problems")
    children = [row for row in board.tickets if row.epic == unit.selector]
    if not children:
        raise CampaignError("closure has no native child identities")
    for row in children:
        if row.state != "dropped" and row.status != "done":
            raise CampaignError(
                "closure has an unfinished selected or unselected child: " + (row.ref or row.title)
            )


def prepare(
    workspace: Path,
    source: SourceSelector,
    unit: UnitSpec,
    *,
    canonical: dict[str, Any],
    canonical_head: str,
) -> dict[str, Any]:
    """Capture authority before the closure actor starts; performs no writes."""
    workspace = workspace.resolve()
    if source.kind != "tickets" or unit.kind != "closure":
        raise CampaignError("native closure requires an exact ticket epic selector")
    if not verify.worktree_clean(workspace):
        raise CampaignError("closure requires a clean owned draft candidate")
    children_terminal(workspace, source, unit)
    paths = source_paths(workspace, source)
    if paths.initiative_folder is None:
        raise CampaignError("closure initiative identity is missing")
    epic = ticketlayout.epic_file(paths.initiative_folder, unit.selector)
    retrospective = ticketlayout.retrospective_doc(paths.initiative_folder, unit.selector)
    if not epic.is_relative_to(workspace) or not retrospective.is_relative_to(workspace):
        raise CampaignError("closure products are outside their private source view")
    canonical_epic = Path(canonical["path"])
    canonical_retro = canonical_epic.parent / retrospective.name
    # Its canonical preimage is later captured by the publisher from the same
    # fresh main/store observation, never selected by the model.
    return {
        "schema_version": 1,
        "unit": unit.id,
        "selector": unit.selector,
        "workspace": str(workspace),
        "head": verify.rev_parse_head(workspace),
        "source": asdict(source),
        "source_root": str(paths.initiative_folder),
        "epic": epic.relative_to(workspace).as_posix(),
        "retrospective": retrospective.relative_to(workspace).as_posix(),
        "files": _tree(workspace, paths.initiative_folder),
        "canonical_epic": str(canonical_epic),
        "canonical_retrospective": str(canonical_retro),
        "canonical_head": canonical_head,
    }


def context(manifest: dict[str, Any]) -> dict[str, Any]:
    return {
        "closure": {
            "epic": manifest["epic"],
            "retrospective": manifest["retrospective"],
            "canonical_children_verified_at": manifest["canonical_head"],
            "allowed_artifacts": [manifest["epic"], manifest["retrospective"]],
            "instruction": "Author this native epic with status: done and a substantive native retrospective. Return exactly these two artifacts. Do not change any child plan, source metadata, implementation, policy or other epic. The supervisor still owes fresh verification, full independent review, merge and canonical source publication.",
        }
    }


def validate(
    workspace: Path,
    unit: UnitSpec,
    manifest: dict[str, Any],
    artifact: dict[str, Any],
    *,
    mirrors: dict[str, str] | None = None,
    committed_head: str = "",
) -> list[dict[str, Any]]:
    """Reject collateral edits before committing products and after worker exit."""
    workspace = workspace.resolve()
    if (
        manifest.get("schema_version") != 1
        or manifest.get("unit") != unit.id
        or manifest.get("selector") != unit.selector
        or manifest.get("workspace") != str(workspace)
    ):
        raise CampaignError("closure preimage authority differs from the selected unit")
    required = (manifest["epic"], manifest["retrospective"])
    if artifact.get("artifacts") != list(required) and set(artifact.get("artifacts", [])) != set(
        required
    ):
        raise CampaignError("closure requires exactly its native epic and retrospective")
    source = SourceSelector(**manifest["source"])
    children_terminal(workspace, source, unit)
    after = _tree(workspace, Path(manifest["source_root"]))
    changed = {
        key
        for key in set(after) | set(manifest["files"])
        if after.get(key) != manifest["files"].get(key)
    }
    if changed - set(required):
        raise CampaignError(
            "closure changed child plans or neighboring source metadata: "
            + ", ".join(sorted(changed - set(required)))
        )
    epic, retrospective = (_read(workspace, workspace / path) for path in required)
    text = epic.decode("utf-8-sig")
    fm = frontmatter.parse_frontmatter(text)
    if (
        frontmatter.frontmatter_fault(text)
        or frontmatter.status_of(fm) != "done"
        or fm.get("blocked_reason")
        or fm.get("blocked_at")
    ):
        raise CampaignError("closure epic must be done without an owner hold")
    if len(retrospective.strip()) < 40:
        raise CampaignError("closure requires a substantive native retrospective")
    allowed = set(required) | set((mirrors or {}).values())
    for args in (
        ("diff", "--name-only", "-z", manifest["head"], "--"),
        ("ls-files", "--others", "--exclude-standard", "-z"),
    ):
        result = verify.git_bytes(workspace, *args)
        if result.returncode:
            raise CampaignError("closure candidate change observation failed")
        paths = {value.decode("utf-8") for value in result.stdout.split(b"\0") if value}
        if paths - allowed:
            raise CampaignError("closure changed implementation or unauthorized product files")
    result = []
    for relative, data, kind, canonical_path in (
        (required[0], epic, "closure", manifest["canonical_epic"]),
        (required[1], retrospective, "closure-retrospective", manifest["canonical_retrospective"]),
    ):
        mirror = (mirrors or {}).get(relative, relative)
        if not isinstance(mirror, str) or Path(mirror).is_absolute() or ".." in Path(mirror).parts:
            raise CampaignError("closure review mirror is not a confined product path")
        if (
            committed_head
            and verify.file_bytes_at_revision(workspace, committed_head, mirror) != data
        ):
            raise CampaignError("closure artifact lacks its exact committed review mirror")
        result.append(
            {
                "path": relative,
                "review_path": mirror,
                "sha256": hashlib.sha256(data).hexdigest(),
                "kind": kind,
                "canonical_path": canonical_path,
            }
        )
    return result


def retrospective_change(unit: Any) -> ArtifactChange:
    """Recover the exact reviewed native product and captured per-path preimage."""
    from .campaign_source_publication import ArtifactChange

    manifest = unit.evidence.get("closure_manifest")
    artifact = next(
        (
            row
            for row in unit.evidence.get("source_artifacts", [])
            if row.get("kind") == "closure-retrospective"
        ),
        None,
    )
    if not isinstance(manifest, dict) or artifact is None:
        raise CampaignError("closure publication lacks its captured native retrospective")
    if artifact.get("canonical_path") != manifest.get("canonical_retrospective"):
        raise CampaignError("retrospective destination differs from captured native mapping")
    workspace = Path(unit.bindings["workspace"])
    data = _read(workspace, workspace / artifact["path"])
    if hashlib.sha256(data).hexdigest() != artifact.get("sha256"):
        raise CampaignError("reviewed retrospective changed before canonical publication")
    if "retrospective_preimage" not in manifest or not manifest.get("canonical_head"):
        raise CampaignError("retrospective publication lacks its captured canonical preimage")
    before = manifest["retrospective_preimage"]
    return ArtifactChange(
        manifest["canonical_retrospective"],
        data,
        base64.b64decode(before, validate=True) if before is not None else None,
        artifact["review_path"],
    )


def publish_retrospective(
    publisher: Any,
    source: SourceSelector,
    unit: Any,
    action_id: str,
    *,
    merge: dict[str, Any],
    acceptance: dict[str, Any],
    journal: Any,
) -> Any:
    """Publish the exact merged retrospective before making the epic done."""
    change = retrospective_change(unit)
    return publisher.publish_artifacts(
        action_id + ":retrospective",
        source,
        unit.spec,
        (change,),
        merge=merge,
        acceptance=acceptance,
        journal=journal,
        store_remote=source.store_remote,
        store_branch=source.store_branch,
    )
