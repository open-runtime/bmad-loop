"""Import BMAD sources and isolate the source view of each campaign worker.

Canonical lifecycle changes always use the source's existing sole-writer API.
A worker can never use a shared ticket store, even when mark_done is disabled.
"""

from __future__ import annotations

import dataclasses
import json
import re
import shutil
import tomllib
from pathlib import Path
from typing import Any

import yaml

from . import bmadconfig, sprintstatus, stories, ticketstore, verify
from .campaign_model import CampaignError, CampaignSpec, SourceSelector, UnitSpec, validate_graph
from .journal import Journal
from .platform_util import atomic_write_text, is_link_like, safe_segment
from .tickettree import TicketTree


@dataclasses.dataclass(frozen=True)
class ImportedGraph:
    units: tuple[UnitSpec, ...]
    observations: dict[str, dict[str, Any]]


def qualified(source: SourceSelector, local: str) -> str:
    return f"{source.id or source.kind}:{source.initiative or 'project'}:{local}"


def source_paths(project: Path, source: SourceSelector) -> bmadconfig.ProjectPaths:
    paths = bmadconfig.load_paths(project, want_tickets=source.kind == "tickets")
    if source.initiative and source.initiative != paths.active_initiative:
        if paths.era != "v7":
            raise CampaignError("initiative selectors require a v7 ticket tree")
        paths = paths.with_initiative(source.initiative)
    return paths


def import_sources(
    project: Path, spec: CampaignSpec, *, _expand_terminal_dependencies: bool = True
) -> ImportedGraph:
    units = list(spec.units)
    observations: dict[str, dict[str, Any]] = {}
    for source in spec.sources:
        paths = source_paths(project, source)
        selected: list[UnitSpec] = []
        if source.kind == "stories":
            folder = stories.resolve_spec_folder(project, source.path)
            board = stories.load_stories(folder)
            previous = ""
            for index, entry in enumerate(board.entries):
                unit_id = qualified(source, entry.id)
                if source.units and entry.id not in source.units:
                    continue
                after = () if source.independent or not previous else (previous,)
                state = stories.resolve_story_spec(folder, entry.id)
                selected.append(
                    UnitSpec(
                        unit_id,
                        source.id,
                        source.path,
                        entry.title,
                        entry.id,
                        after=after,
                        acceptance=(entry.description,),
                        order=index,
                        initiative=source.initiative,
                    )
                )
                observations[unit_id] = {
                    "kind": "stories",
                    "path": str(folder),
                    "status": state.status if hasattr(state, "status") else "",
                    "plan_checkpoint": entry.spec_checkpoint,
                    "done_checkpoint": entry.done_checkpoint,
                }
                previous = unit_id
        elif source.kind == "sprint-status":
            board = sprintstatus.load(paths.sprint_status)
            previous = ""
            for index, row in enumerate(board.stories):
                unit_id = qualified(source, row.key)
                if (source.epics and str(row.epic) not in source.epics) or (
                    source.units and row.key not in source.units
                ):
                    continue
                selected.append(
                    UnitSpec(
                        unit_id,
                        source.id,
                        str(row.epic),
                        row.slug.replace("-", " "),
                        row.key,
                        after=() if source.independent or not previous else (previous,),
                        order=index,
                        initiative=source.initiative,
                    )
                )
                observations[unit_id] = {
                    "kind": "sprint-status",
                    "path": str(paths.sprint_status),
                    "status": row.status,
                }
                previous = unit_id
        elif source.kind == "tickets":
            from .campaign_source_graph import inspect_ticket_graph

            tree = TicketTree(project, folder=paths.initiative_folder)
            board = tree.status()
            if board.problems:
                raise CampaignError("ticket source problems: " + "; ".join(board.problems))
            metadata = inspect_ticket_graph(project, source, paths, board, spec.sources)
            rows = list(board.tickets)
            if board.backlog:
                rows.extend(board.backlog.tickets)
            for index, row in enumerate(rows):
                if not row.ref:
                    raise CampaignError("ticket has no stable ref")
                if (source.epics and row.epic not in source.epics) or (
                    source.units and row.ref not in source.units
                ):
                    continue
                unit_id = qualified(source, row.ref)
                after = metadata.dependencies.get(row.ref, ())
                selected.append(
                    UnitSpec(
                        unit_id,
                        source.id,
                        row.epic,
                        row.title,
                        row.ref,
                        after=tuple(dict.fromkeys(after)),
                        scopes=metadata.scopes.get(row.ref, ()),
                        order=index,
                        initiative=source.initiative,
                    )
                )
                observations[unit_id] = {
                    "kind": "tickets",
                    "path": row.file or "",
                    "status": row.status,
                    "state": row.state,
                    "tracker_id": row.tracker_id,
                    "hitl": row.hitl,
                    "plan_checkpoint": row.plan_checkpoint,
                    "done_checkpoint": row.done_checkpoint,
                    "blocked_reason": row.blocked_reason,
                    "waiting_on": list(row.waiting_on),
                    "risk": row.risk,
                    "folder": str(paths.initiative_folder),
                    "scheduler_holds": metadata.holds.get(row.ref, ()),
                    "source_metadata_revisions": metadata.revisions,
                }
                external_holds = [
                    fact
                    for fact in metadata.holds.get(row.ref, ())
                    if fact["active"] and not fact["dependency"]
                ]
                if external_holds:
                    observations[unit_id]["blocked_reason"] = "; ".join(
                        f"scheduler hold until {fact['until']}: {fact['why']}"
                        for fact in external_holds
                    )
            for epic in board.epics:
                children = tuple(u.id for u in selected if u.epic == epic.slug)
                if not children:
                    continue
                closure = qualified(source, f"closure/{epic.slug}")
                selected.append(
                    UnitSpec(
                        closure,
                        source.id,
                        epic.slug,
                        f"Close {epic.slug}",
                        epic.slug,
                        after=children,
                        kind="closure",
                        initiative=source.initiative,
                        order=len(rows) + len(selected),
                        acceptance=(
                            "All selected and unselected epic children delivered; retrospective and source closure recorded.",
                        ),
                    )
                )
                observations[closure] = {
                    "kind": "closure",
                    "status": epic.status,
                    "folder": epic.folder,
                    "all_children_selected": all(
                        qualified(source, r.ref or "") in children
                        for r in rows
                        if r.epic == epic.slug
                    ),
                }
        else:
            raise CampaignError(f"unsupported source: {source.kind}")
        units.extend(selected)
    ids = [u.id for u in units]
    if len(ids) != len(set(ids)):
        raise CampaignError("source selectors overlap: duplicate qualified unit IDs")
    known = set(ids)
    missing = {dependency for unit in units for dependency in unit.after if dependency not in known}
    if missing and _expand_terminal_dependencies:
        from .campaign_existing import dependency_reconciliation

        all_sources = tuple(
            dataclasses.replace(source, epics=(), units=()) for source in spec.sources
        )
        complete = import_sources(
            project,
            dataclasses.replace(spec, sources=all_sources),
            _expand_terminal_dependencies=False,
        )
        units = dependency_reconciliation(
            units, observations, complete.units, complete.observations
        )
        known = {unit.id for unit in units}
    if not _expand_terminal_dependencies:
        return ImportedGraph(tuple(units), observations)
    for unit in units:
        missing = set(unit.after) - known
        if missing:
            raise CampaignError(
                f"{unit.id}: dependencies outside selected graph: {sorted(missing)}"
            )
    validate_graph(units)
    return ImportedGraph(tuple(units), observations)


def _prepare_ticket_source(
    project: Path, workspace: Path, source: SourceSelector
) -> bmadconfig.ProjectPaths:
    """Copy shared stores and redirect only the newly owned worker checkout."""
    canonical = source_paths(project, source)
    if source.kind != "tickets":
        return canonical.rebased(workspace)
    topology = ticketstore.assess_store(canonical, "worktree")
    if isinstance(topology, ticketstore.StoreRefusal):
        raise CampaignError(f"cannot isolate ticket source: {topology.code}")
    shared = topology is not None and not topology.per_worktree()
    if (
        shared
        or canonical.shared_store is not None
        or not canonical.output_folder.is_relative_to(project)
    ):
        private = workspace / ".bmad-loop" / "campaign-source"
        if private.exists():
            raise CampaignError("private source destination already exists without a binding")
        from .install import _worktree_local_exclude

        refusal = _worktree_local_exclude(
            workspace, ["/.bmad-loop/campaign-source/", "/_bmad/custom/config.user.toml"]
        )
        if refusal:
            raise CampaignError(f"cannot shield private ticket source: {refusal}")
        _check_source_tree(canonical.output_folder)
        shutil.copytree(
            canonical.output_folder,
            private,
            ignore=shutil.ignore_patterns(".git", ".bmad-loop", "*.lock"),
            symlinks=False,
        )
        ticketstore.redirect_mount_config(
            workspace,
            main_store=private,
            initiative=source.initiative or canonical.active_initiative,
        )
    elif (
        source.initiative
        and source.initiative
        != bmadconfig.load_paths(workspace, want_tickets=True).active_initiative
    ):
        ticketstore.redirect_mount_config(workspace, main_store=None, initiative=source.initiative)
    paths = bmadconfig.load_paths(workspace, want_tickets=True)
    if not paths.output_folder.resolve().is_relative_to(workspace.resolve()):
        raise CampaignError("worker source still resolves outside its private checkout")
    return paths


@dataclasses.dataclass(frozen=True)
class PreparedSource:
    paths: bmadconfig.ProjectPaths
    selector: SourceSelector
    private_paths: tuple[Path, ...] = ()


def _check_source_tree(root: Path) -> None:
    """Reject implicit redirects before copying an explicitly selected source."""
    if not root.is_dir() or is_link_like(root):
        raise CampaignError(f"source copy root is missing or link-like: {root}")
    for path in root.rglob("*"):
        if is_link_like(path):
            raise CampaignError(f"source copy contains a link-like path: {path}")


def _shield(workspace: Path, paths: tuple[Path, ...]) -> None:
    from .install import _worktree_local_exclude

    patterns = [
        "/" + path.relative_to(workspace).as_posix() + ("/" if path.is_dir() else "")
        for path in paths
    ]
    refusal = _worktree_local_exclude(workspace, patterns)
    if refusal:
        raise CampaignError(f"cannot shield private source: {refusal}")


def _copy_missing(source: Path, target: Path, *, tracked_project: Path | None = None) -> None:
    """Copy missing private content without overwriting checkout-owned files."""
    _check_source_tree(source)
    target.mkdir(parents=True, exist_ok=True)
    if is_link_like(target):
        raise CampaignError("private source destination is redirected")
    for child in source.iterdir():
        if child.name in {".git", ".bmad-loop"} or child.name.endswith(".lock"):
            continue
        destination = target / child.name
        if child.is_dir():
            _copy_missing(child, destination, tracked_project=tracked_project)
        elif destination.exists():
            if is_link_like(destination) or not destination.is_file():
                raise CampaignError("private source destination conflicts with an existing path")
        else:
            if (
                tracked_project is not None
                and child.is_relative_to(tracked_project)
                and verify.path_tracked(
                    tracked_project, child.relative_to(tracked_project).as_posix()
                )
            ):
                raise CampaignError(
                    "tracked native source is absent from fresh worker main; update the project base instead of restoring deleted planning files"
                )
            shutil.copyfile(child, destination)


def _overlay_table(text: str, table: str, fields: dict[str, str]) -> str:
    """Patch only worker-local native TOML fields; preserve unrelated bytes."""
    newline = "\r\n" if "\r\n" in text else "\n"
    additions = "".join(f"{key} = {json.dumps(value)}{newline}" for key, value in fields.items())
    replaced = re.compile(r"^\s*(?:" + "|".join(map(re.escape, fields)) + r")\s*=")
    section = ""
    inserted = False
    out = []
    for line in text.splitlines(keepends=True):
        match = re.match(r"^\s*\[([^\]]+)\]\s*(?:#.*)?$", line.strip())
        if match:
            section = match.group(1).strip()
            out.append(line if line.endswith(("\n", "\r")) else line + newline)
            if section == table:
                out.append(additions)
                inserted = True
        elif section == table and replaced.match(line):
            continue
        else:
            out.append(line)
    if not inserted:
        if out and not out[-1].endswith(("\n", "\r")):
            out.append(newline)
        out.append(f"{newline}[{table}]{newline}{additions}")
    result = "".join(out)
    try:
        parsed = tomllib.loads(result)
        value: Any = parsed
        for component in table.split("."):
            value = value[component]
        if any(value.get(key) != expected for key, expected in fields.items()):
            raise ValueError("worker artifact override failed read-back")
    except (tomllib.TOMLDecodeError, KeyError, TypeError, ValueError) as exc:
        raise CampaignError(
            f"worker source config cannot take a safe native override: {exc}"
        ) from exc
    return result


def prepare_source_view(project: Path, workspace: Path, source: SourceSelector) -> PreparedSource:
    """Return effective worker selectors and paths with no shared source writer.

    Relative tracked source stays on the branch. Ignored source is copied to the
    same relative path. External v6 artifact paths use a private native user
    overlay; the immutable tracked project config is never rewritten for a PR.
    """
    project, workspace = project.resolve(), workspace.resolve()
    canonical = source_paths(project, source)
    if source.kind == "tickets":
        paths = _prepare_ticket_source(project, workspace, source)
        return PreparedSource(paths, source, (paths.output_folder,))
    if source.kind not in {"sprint-status", "stories"}:
        raise CampaignError(f"unsupported campaign source: {source.kind}")
    values = {
        name: getattr(canonical, name)
        for name in ("output_folder", "implementation_artifacts", "planning_artifacts")
    }
    private_root = workspace / ".bmad-loop" / "campaign-source" / "v6"
    mapped: dict[str, Path] = {}
    copied: list[Path] = []
    external = any(not value.is_relative_to(project) for value in values.values())
    if external:
        central = any((project / relative).is_file() for relative in bmadconfig.CENTRAL_LAYERS_REL)
        legacy = (bmadconfig.LEGACY_CONFIG_REL).as_posix()
        if not central and verify.path_tracked(workspace, legacy):
            raise CampaignError(
                "external artifacts with tracked legacy YAML require a qualified native central-config overlay before campaign isolation"
            )
        for name, value in values.items():
            if value.is_relative_to(project):
                mapped[name] = workspace / value.relative_to(project)
            else:
                mapped[name] = private_root / name
                if value.exists():
                    _copy_missing(value, mapped[name])
                else:
                    mapped[name].mkdir(parents=True, exist_ok=True)
                copied.append(mapped[name])
        overlay = workspace / bmadconfig.CENTRAL_LAYERS_REL[-1]
        relative = overlay.relative_to(workspace).as_posix()
        if verify.path_tracked(workspace, relative):
            raise CampaignError("private source overrides require an untracked native user config")
        original = overlay.read_text(encoding="utf-8") if overlay.exists() else ""
        text = _overlay_table(original, "core", {"output_folder": str(mapped["output_folder"])})
        text = _overlay_table(
            text,
            "modules.bmm",
            {key: str(mapped[key]) for key in ("implementation_artifacts", "planning_artifacts")},
        )
        overlay.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(overlay, text, follow_symlinks=False)
        _shield(workspace, (*copied, overlay))
        legacy_path = workspace / bmadconfig.LEGACY_CONFIG_REL
        if not central and legacy_path.is_file():
            # A seeded legacy config is worker-only and can point old skills at
            # the private view too. Tracked legacy config was refused above.
            legacy_doc = yaml.safe_load(legacy_path.read_text(encoding="utf-8"))
            if not isinstance(legacy_doc, dict):
                raise CampaignError("legacy worker source config must be a mapping")
            legacy_doc.update({key: str(value) for key, value in mapped.items()})
            atomic_write_text(
                legacy_path, yaml.safe_dump(legacy_doc, sort_keys=False), follow_symlinks=False
            )
            _shield(workspace, (legacy_path,))
    else:
        mapped = {name: workspace / value.relative_to(project) for name, value in values.items()}
    # Existing tracked files remain from the selected base; ignored source data
    # omitted by git worktree add is copied into this checkout only.
    for name, origin in values.items():
        destination = mapped[name]
        if origin.is_dir() and origin != destination:
            _copy_missing(origin, destination, tracked_project=project)
            if not verify.path_tracked(workspace, destination.relative_to(workspace).as_posix()):
                _shield(workspace, (destination,))
                copied.append(destination)
    effective = source
    if source.kind == "stories":
        original = stories.resolve_spec_folder(project, source.path).resolve()
        destination = (
            workspace / original.relative_to(project)
            if original.is_relative_to(project)
            else mapped["output_folder"] / "campaign-stories" / safe_segment(source.id or "stories")
        )
        if original != destination:
            _copy_missing(original, destination, tracked_project=project)
            if not verify.path_tracked(workspace, destination.relative_to(workspace).as_posix()):
                _shield(workspace, (destination,))
                copied.append(destination)
        effective = dataclasses.replace(source, path=destination.relative_to(workspace).as_posix())
    paths = bmadconfig.load_paths(workspace)
    if not paths.repo_root.resolve().is_relative_to(workspace):
        raise CampaignError(
            "worker source configuration points Git operations outside its checkout"
        )
    if any(not getattr(paths, name).resolve().is_relative_to(workspace) for name in values):
        raise CampaignError("worker artifact configuration still reaches a shared source")
    return PreparedSource(paths, effective, tuple(dict.fromkeys(copied)))


def prepare_private_source(
    project: Path, workspace: Path, source: SourceSelector
) -> bmadconfig.ProjectPaths:
    """Compatibility wrapper; new campaign callers persist the effective selector."""
    return prepare_source_view(project, workspace, source).paths


def publish_status(
    project: Path,
    source: SourceSelector,
    unit: UnitSpec,
    *,
    journal: Journal,
    expected_status: str,
    merged: bool,
) -> None:
    """Apply one terminal semantic change after confirmed remote delivery."""
    if not merged:
        raise CampaignError("canonical publication requires confirmed remote merge")
    paths = source_paths(project, source)
    if source.kind == "sprint-status":
        current = sprintstatus.story_status(paths.sprint_status, unit.selector)
        if current == "done":
            return
        if current != expected_status:
            raise CampaignError("canonical story changed during execution")
        sprintstatus.advance(paths.sprint_status, unit.selector, "done")
    elif source.kind == "tickets":
        tree = TicketTree(project, folder=paths.initiative_folder)
        result = tree.find(unit.selector)
        plan = Path(result.plan) if result.plan else None
        if plan is None:
            raise CampaignError("canonical ticket has no plan")
        if result.row.status == "done":
            return
        if result.row.status != expected_status:
            raise CampaignError("canonical ticket changed during execution")
        tree.mark(unit.selector, "done", reason="accept", expected_plan=plan, journal=journal)
    elif source.kind == "stories":
        # Tracked spec lifecycle is already in the merged code commit. Never
        # rewrite a whole stories.yaml; it intentionally contains no statuses.
        state = stories.resolve_story_spec(
            stories.resolve_spec_folder(project, source.path), unit.selector
        )
        if state.status != "done":
            raise CampaignError("merged story spec is not done")


def source_receipt(graph: ImportedGraph) -> str:
    return json.dumps({"schema_version": 1, "observations": graph.observations}, sort_keys=True)
