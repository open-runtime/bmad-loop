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

from . import sprintstatus, stories, verify
from .campaign_model import CampaignError, SourceSelector
from .campaign_sources import source_paths
from .platform_util import atomic_write_text, is_link_like, safe_segment
from .tickettree import TicketsError, TicketTree

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
    source_authority: Path | None = None,
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
        copy_manifest = None
        if (
            source_authority is not None
            and canonical.kind == "tickets"
            and local == workspace.resolve() / ".bmad-loop" / "campaign-source"
        ):
            from .campaign_source_views import captured_manifest

            copy_manifest = captured_manifest(
                source_authority, canonical=root, workspace=workspace, destination=local
            )
        preimages = {}
        for directory, dirs, files in os.walk(root, followlinks=False):
            dirs[:] = sorted(
                name
                for name in dirs
                if name not in {".git", "node_modules", "runs"}
                and (copy_manifest is None or (name != ".bmad-loop" and not name.endswith(".lock")))
            )
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
        if copy_manifest is not None:
            expected = {
                name: entry["sha256"]
                for name, entry in copy_manifest.items()
                if entry["kind"] == "file"
                and Path(name).suffix in SUFFIXES
                and not ({"node_modules", "runs"} & set(Path(name).parts[:-1]))
            }
            captured = {
                name: hashlib.sha256(base64.b64decode(value, validate=True)).hexdigest()
                for name, value in preimages.items()
            }
            if captured != expected:
                raise CampaignError("canonical source changed since private copy capture")
        rows.append(
            {
                "source": canonical.id,
                "kind": canonical.kind,
                "root": str(root),
                "local_root": str(local),
                "local_initiative": (
                    str(source_paths(workspace, effective).initiative_folder)
                    if canonical.kind == "tickets"
                    else ""
                ),
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


def _artifact_sources(
    manifest: dict[str, Any],
    workspace: Path,
    artifacts: list[dict[str, Any]],
    source_bindings: dict[str, Any],
    units: dict[str, Any],
) -> dict[str, str]:
    """Route one canonical path once, using the reviewed graph's native identity."""
    owners: dict[str, str] = {}
    for artifact in artifacts:
        relative = artifact["path"]
        item = workspace / relative
        if Path(relative).is_absolute() or ".." in Path(relative).parts or is_link_like(item):
            raise CampaignError("planning artifact path is redirected or outside its checkout")
        candidates = [
            row
            for row in manifest["sources"]
            if item.resolve().is_relative_to(Path(row["local_root"]))
        ]
        if not candidates:
            continue  # Ordinary code-owned planning documents have no source publication.
        declared = {
            units[key]["source"]
            for key, binding in source_bindings.items()
            if key in units and binding["path"] == relative
        }
        if len(declared) > 1:
            raise CampaignError("planning artifact has conflicting source ownership")
        if declared:
            candidates = [row for row in candidates if row["source"] in declared]
            if len(candidates) != 1:
                raise CampaignError("planning artifact source differs from its captured authority")
            selected_source = candidates[0]
            if selected_source.get("kind") == "tickets" and (
                not selected_source.get("local_initiative")
                or not item.resolve().is_relative_to(Path(selected_source["local_initiative"]))
            ):
                raise CampaignError("planning ticket artifact belongs to another initiative")
        elif len(candidates) > 1:
            # Select a store's own initiative before the stable config-order
            # owner of store-wide metadata. Never send B's plan through A's tree.
            selected = [
                row
                for row in candidates
                if row.get("local_initiative")
                and item.resolve().is_relative_to(Path(row["local_initiative"]))
            ]
            if selected:
                candidates = selected
        destinations = {
            str(Path(row["root"]) / item.resolve().relative_to(Path(row["local_root"])))
            for row in candidates
        }
        if len(destinations) != 1:
            raise CampaignError("planning artifact maps to conflicting canonical destinations")
        owners[relative] = candidates[0]["source"]
    return owners


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
    owners = _artifact_sources(manifest, workspace, artifacts, source_bindings, units)
    local, root = Path(row["local_root"]), Path(row["root"])
    result = []
    selectors = {
        value["path"]: units[key].get("selector", "")
        for key, value in source_bindings.items()
        if key in units and units[key].get("source") == source_id
    }
    for artifact in artifacts:
        if owners.get(artifact["path"]) != source_id:
            continue
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


def drift(unit: Any) -> list[dict[str, str]]:
    workspace = Path(unit.bindings["workspace"])
    head = verify.rev_parse_head(workspace)
    latest = {}
    for stage, receipt in unit.evidence.get("planning_flow", {}).get("stages", {}).items():
        if stage == "planning_review":
            continue
        for artifact in receipt.get("validated", {}).get("artifacts", []):
            latest[artifact["path"]] = {**artifact, "stage": stage}
    changed = []
    for relative, artifact in latest.items():
        path = workspace / relative
        mirror = artifact.get("review_path", relative)
        data = verify.file_bytes_at_revision(workspace, head, mirror)
        if (
            is_link_like(path)
            or not path.is_file()
            or path.stat().st_size > 8 * 1024 * 1024
            or hashlib.sha256(path.read_bytes()).hexdigest() != artifact["sha256"]
            or data is None
            or hashlib.sha256(data).hexdigest() != artifact["sha256"]
        ):
            changed.append({"path": relative, "stage": artifact["stage"]})
    return changed


def canonical_drift(unit: Any) -> list[str]:
    manifest_path = Path(unit.bindings["planning_manifest"])
    if is_link_like(manifest_path) or manifest_path.stat().st_size > 128 * 1024 * 1024:
        raise CampaignError("planning canonical preimages are redirected or unbounded")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    workspace = Path(unit.bindings["workspace"])
    products = {}
    for receipt in unit.evidence.get("planning_flow", {}).get("stages", {}).values():
        for artifact in receipt.get("validated", {}).get("artifacts", []):
            products[artifact["path"]] = artifact
    changed = []
    for row in manifest["sources"]:
        local = Path(row["local_root"])
        for relative in products:
            path = (workspace / relative).resolve()
            if not path.is_relative_to(local):
                continue
            suffix = path.relative_to(local).as_posix()
            canonical = Path(row["root"]) / suffix
            encoded = row["preimages"].get(suffix)
            expected = base64.b64decode(encoded, validate=True) if encoded is not None else None
            if is_link_like(canonical) or (
                canonical.exists()
                and (not canonical.is_file() or canonical.stat().st_size > 8 * 1024 * 1024)
            ):
                raise CampaignError("canonical planning source is redirected or unbounded")
            current = canonical.read_bytes() if canonical.exists() else None
            if current not in (expected, path.read_bytes()):
                changed.append(relative)
    return changed


def validate_stage_products(
    workspace: Path, receipt: dict[str, Any], sources: tuple[SourceSelector, ...]
) -> None:
    """Planning writes product documents in its declared source view and docs."""
    roots = [workspace / "docs", *(source_root(workspace, source) for source in sources)]
    allowed = set()
    for artifact in receipt["validated"]["artifacts"]:
        path = workspace / artifact["path"]
        if path.suffix not in SUFFIXES or not any(
            path.resolve().is_relative_to(root.resolve()) for root in roots
        ):
            raise CampaignError("planning artifact escaped its product-document ownership")
        allowed.update((artifact["path"], artifact.get("review_path", artifact["path"])))
    authored = {
        (workspace / artifact["path"]).resolve() for artifact in receipt["validated"]["artifacts"]
    }
    for source in sources:
        if source.kind == "stories":
            folder = stories.resolve_spec_folder(workspace, source.path)
            if (folder / stories.STORIES_FILENAME).resolve() not in authored:
                continue
            try:
                stories.load_stories(folder)
            except stories.StoriesError as exc:
                raise CampaignError(
                    "planning authored an invalid native stories manifest: " + str(exc)
                ) from exc
        elif source.kind == "sprint-status":
            board = source_paths(workspace, source).sprint_status
            if board.resolve() not in authored:
                continue
            try:
                sprintstatus.planning_rows_in_bytes(board.read_bytes())
                sprintstatus.load(board)
            except sprintstatus.SprintStatusError as exc:
                raise CampaignError(
                    "planning authored an invalid native sprint board: " + str(exc)
                ) from exc
        elif source.kind == "tickets":
            folder = source_paths(workspace, source).initiative_folder
            if folder is None or not any(
                path.is_relative_to(folder.resolve()) for path in authored
            ):
                continue
            try:
                board = TicketTree(workspace, folder=folder).status()
            except TicketsError as exc:
                raise CampaignError(
                    "planning authored an invalid native ticket tree: " + str(exc)
                ) from exc
            if board.problems:
                raise CampaignError(
                    "planning authored an invalid native ticket tree: " + "; ".join(board.problems)
                )
    observed = verify.git_bytes(
        workspace, "diff", "--name-only", "--no-renames", "-z", receipt["base"], receipt["head"]
    )
    if observed.returncode:
        raise CampaignError("planning changed-path observation failed")
    actual = {raw.decode("utf-8") for raw in observed.stdout.split(b"\0") if raw}
    if actual - allowed:
        raise CampaignError("planning committed changes outside its declared product artifacts")


def invalidate(snapshot: Any, unit: Any, changed: list[dict[str, str]]) -> None:
    from .campaign_model import UnitState
    from .campaign_planning_flow import STAGES
    from .campaign_store import add_event, transition

    state = unit.evidence["planning_flow"]
    earliest = min((item["stage"] for item in changed), key=STAGES.index)
    for stage in STAGES[STAGES.index(earliest) :]:
        state["stages"].pop(stage, None)
    state.update(
        state="planning",
        proposal=None,
        repair=True,
        findings=[
            {
                "required": True,
                "stage": earliest,
                "reason": "Main integration or steering changed reviewed planning products",
                "artifacts": changed,
            }
        ],
    )
    for key in ("integrated", "verification", "review", "native_review_pair", "pushed"):
        unit.evidence.pop(key, None)
    transition(snapshot, unit.id, UnitState.READY)
    add_event(snapshot, "planning-evidence-invalidated", unit=unit.id, changed=changed)


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
    review_coordinates = review.get("validated", {}).get("coordinates", {}) or review.get(
        "artifact", {}
    ).get("coordinates", {})
    merge_coordinates = {
        "head": unit.evidence["integrated"]["head"],
        "merged_commit": merge["merge_commit"],
        "pr_number": unit.bindings["pr"]["number"],
    }
    acceptance = {
        "head": merge_coordinates["head"],
        "verdict": "approved",
        "review_id": review.get("session_id") or review.get("launch_id"),
        "graph_revision": review_coordinates.get("graph_revision", snapshot.graph_revision),
        "config_revision": review_coordinates.get("config_revision", snapshot.config_revision),
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
            tracking_control = None
            if published.state == "tracking_pr_required":
                from .campaign_runtime_tracking import prepare

                tracking_control = prepare(
                    snapshot,
                    unit,
                    published,
                    state_dir=runtime.store.directory(snapshot.id),
                    publisher_project=runtime.project,
                    source=source,
                    changes=authored,
                    merge=merge_coordinates,
                    acceptance=acceptance,
                )
            return {
                "state": published.state,
                "canonical_head": canonical,
                "publications": publications,
                "proposal": published.proposal,
                "detail": published.detail,
                "tracking_control": tracking_control,
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
