"""Bind private planning products to reviewed mirrors and canonical preimages.

The manifest lives outside the editable checkout. Planning actors can author a
private source view; they cannot choose its canonical destination or preimage.
Publication still goes through SourcePublication's native sole-writer APIs.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
from typing import Any

from . import stories, verify
from .campaign_model import CampaignError, SourceSelector
from .campaign_sources import source_paths
from .platform_util import atomic_write_text, is_link_like, safe_segment

SUFFIXES = {".md", ".toml", ".yaml", ".yml", ".json"}


def source_root(project: Path, source: SourceSelector) -> Path:
    return (
        stories.resolve_spec_folder(project, source.path)
        if source.kind == "stories"
        else source_paths(project, source).output_folder
    ).resolve()


def capture(
    path: Path,
    *,
    project: Path,
    workspace: Path,
    sources: tuple[tuple[SourceSelector, SourceSelector], ...],
    max_files: int = 10000,
    max_bytes: int = 64 * 1024 * 1024,
) -> dict[str, Any]:
    """Capture once before planning, including absence proofs for new products."""
    if path.exists():
        if is_link_like(path) or path.stat().st_size > max_bytes * 2:
            raise CampaignError("planning preimage manifest is redirected or unbounded")
        return json.loads(path.read_text(encoding="utf-8"))
    rows = []
    size = count = 0
    for canonical, effective in sources:
        root, local = source_root(project, canonical), source_root(workspace, effective)
        if not local.is_relative_to(workspace.resolve()):
            raise CampaignError("planning source view is outside its owned checkout")
        preimages = {}
        for directory, dirs, files in os.walk(root, followlinks=False):
            dirs[:] = sorted(name for name in dirs if name not in {".git", "node_modules", "runs"})
            for name in dirs:
                if is_link_like(Path(directory) / name):
                    raise CampaignError("planning canonical source contains redirected directories")
            for name in sorted(files):
                item = Path(directory) / name
                if item.suffix not in SUFFIXES:
                    continue
                if is_link_like(item) or not item.is_file():
                    raise CampaignError("planning canonical product is redirected")
                length = item.stat().st_size
                size += length
                count += 1
                if count > max_files or size > max_bytes:
                    raise CampaignError("planning canonical preimages exceed configured bounds")
                preimages[item.relative_to(root).as_posix()] = base64.b64encode(
                    item.read_bytes()
                ).decode("ascii")
        rows.append(
            {
                "source": canonical.id,
                "root": str(root),
                "local_root": str(local),
                "preimages": preimages,
            }
        )
    result = {
        "schema_version": 1,
        "project": str(project.resolve()),
        "workspace": str(workspace.resolve()),
        "sources": rows,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, json.dumps(result, sort_keys=True), follow_symlinks=False)
    return result


def mirror_artifacts(
    workspace: Path, unit_id: str, artifacts: list[str]
) -> tuple[list[Path], dict[str, str]]:
    """Commit ignored product bytes as ordinary reviewable documents."""
    paths, mirrors = [], {}
    for relative in artifacts:
        item = workspace / relative
        if (
            Path(relative).is_absolute()
            or ".." in Path(relative).parts
            or is_link_like(item)
            or not item.resolve().is_relative_to(workspace.resolve())
            or not item.is_file()
            or item.stat().st_size > 8 * 1024 * 1024
        ):
            raise CampaignError(
                "stage artifact is missing, redirected, unbounded or outside its checkout"
            )
        ignored = verify.git_bytes(workspace, "check-ignore", "-q", "--", relative)
        if ignored.returncode not in {0, 1}:
            raise CampaignError("stage artifact ownership probe failed")
        if ignored.returncode == 0:
            if item.suffix not in SUFFIXES:
                raise CampaignError("private planning artifact must be a product document")
            key = hashlib.sha256(relative.encode()).hexdigest()[:20]
            destination = (
                workspace
                / "docs"
                / "bmad-campaign"
                / safe_segment(unit_id)
                / "planning"
                / key
                / item.name
            )
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(item.read_bytes())
        else:
            destination = item
        paths.append(destination)
        mirrors[relative] = destination.relative_to(workspace).as_posix()
    return paths, mirrors


def changes(
    manifest: dict[str, Any],
    workspace: Path,
    source_id: str,
    artifacts: list[dict[str, Any]],
    source_bindings: dict[str, Any],
    units: dict[str, Any],
) -> tuple[Any, ...]:
    from .campaign_source_publication import ArtifactChange

    row = next((value for value in manifest["sources"] if value["source"] == source_id), None)
    if row is None or manifest["workspace"] != str(workspace.resolve()):
        raise CampaignError("planning source has no captured canonical preimage authority")
    local, root = Path(row["local_root"]), Path(row["root"])
    result = []
    selectors = {
        value["path"]: units[key].get("selector", "")
        for key, value in source_bindings.items()
        if key in units and units[key].get("source") == source_id
    }
    for artifact in artifacts:
        item = workspace / artifact["path"]
        if not item.resolve().is_relative_to(local):
            continue
        data = item.read_bytes()
        if hashlib.sha256(data).hexdigest() != artifact["sha256"]:
            raise CampaignError("reviewed planning product changed before canonical publication")
        relative = item.resolve().relative_to(local).as_posix()
        encoded = row["preimages"].get(relative)
        result.append(
            ArtifactChange(
                str(root / relative),
                data,
                base64.b64decode(encoded, validate=True) if encoded is not None else None,
                artifact.get("review_path", artifact["path"]),
                selector=selectors.get(artifact["path"], ""),
                owned_rows=tuple(
                    value.get("selector", "")
                    for value in units.values()
                    if value.get("source") == source_id and value.get("selector")
                ),
            )
        )
    return tuple(result)


def bind_mirrors(workspace: Path, receipt: dict[str, Any]) -> None:
    """Bind each validated artifact to bytes actually committed in this PR."""
    mapping = receipt.get("artifact_mirrors", {})
    for artifact in receipt["validated"]["artifacts"]:
        relative = artifact["path"]
        mirror = mapping.get(relative, relative)
        if not isinstance(mirror, str) or Path(mirror).is_absolute() or ".." in Path(mirror).parts:
            raise CampaignError("planning review mirror is not a confined product path")
        data = verify.file_bytes_at_revision(workspace, receipt["head"], mirror)
        if data is None or hashlib.sha256(data).hexdigest() != artifact["sha256"]:
            raise CampaignError("planning product has no exact committed review mirror")
        artifact["review_path"] = mirror


def publish(runtime: Any, snapshot: Any, unit: Any, action: Any) -> dict[str, Any]:
    """Publish all approved source products before activating the reviewed graph."""
    flow = unit.evidence["planning_flow"]
    proposal = flow["proposal"]
    merge = unit.evidence["merge"]
    canonical = merge["canonical_head"]
    workspace = Path(unit.bindings["workspace"])
    path = Path(unit.bindings["planning_manifest"])
    if is_link_like(path) or path.stat().st_size > 128 * 1024 * 1024:
        raise CampaignError("planning publication manifest is redirected or unbounded")
    manifest = json.loads(path.read_text(encoding="utf-8"))
    products = {}
    for stage in flow["stages"].values():
        for artifact in stage.get("validated", {}).get("artifacts", []):
            products[artifact["path"]] = artifact
    units = {value["id"]: value for value in proposal["units"]}
    bindings, publications = {}, {}
    review = unit.evidence["review"]
    merge_coordinates = {
        "head": unit.evidence["integrated"]["head"],
        "merged_commit": merge["merge_commit"],
        "pr_number": unit.bindings["pr"]["number"],
    }
    acceptance = {
        "head": merge_coordinates["head"],
        "verdict": "approved",
        "review_id": review.get("session_id") or review.get("launch_id"),
        "graph_revision": snapshot.graph_revision,
        "config_revision": snapshot.config_revision,
    }
    for source in snapshot.spec.sources:
        authored = changes(
            manifest,
            workspace,
            source.id,
            list(products.values()),
            proposal["source_bindings"],
            units,
        )
        if not authored:
            continue
        published = runtime._source_publisher(snapshot).publish_artifacts(
            action.id + ":" + source.id,
            source,
            unit.spec,
            authored,
            merge=merge_coordinates,
            acceptance=acceptance,
            journal=runtime._operation_journal(snapshot, action),
            store_remote=source.store_remote,
            store_branch=source.store_branch,
        )
        publications[source.id] = {
            "state": published.state,
            "canonical_head": published.canonical_head,
            "source_revision": published.source_revision,
            "source_commit": published.source_commit,
            "source_remote_head": published.source_remote_head,
            "proposal": published.proposal,
            "detail": published.detail,
        }
        if published.state != "confirmed":
            return {
                "state": published.state,
                "canonical_head": canonical,
                "publications": publications,
                "proposal": published.proposal,
                "detail": published.detail,
            }
        by_path = {change.path: change for change in authored}
        row = next(value for value in manifest["sources"] if value["source"] == source.id)
        for key, expected in proposal["source_bindings"].items():
            if units[key]["source"] != source.id:
                continue
            local = (workspace / expected["path"]).resolve()
            if not local.is_relative_to(Path(row["local_root"])):
                raise CampaignError(
                    "proposed source artifact differs from its canonical source authority"
                )
            destination = str(Path(row["root"]) / local.relative_to(Path(row["local_root"])))
            if destination not in by_path:
                raise CampaignError(
                    "proposed source was not included in its reviewed publication manifest"
                )
            bindings[key] = {
                "path": destination,
                "artifact_path": expected["path"],
                "sha256": expected["sha256"],
                "kind": expected["kind"],
                "revision": published.source_remote_head or published.source_revision or canonical,
                "code_revision": canonical,
                "remote_publication_confirmed": True,
            }
    if set(bindings) != set(proposal["source_bindings"]):
        raise CampaignError(
            "reviewed graph contains a source without confirmed canonical publication"
        )
    return {
        "state": "confirmed",
        "canonical_head": canonical,
        "source_bindings": bindings,
        "publications": publications,
    }
