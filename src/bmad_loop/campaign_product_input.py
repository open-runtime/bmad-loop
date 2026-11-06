"""Bind an early draft specification to its selected native product intent.

Read only the exact selected private plan/spec, native ticket entry or Markdown
story section. Workflow status, dispatch prose and neighboring units are never
substitutes for a product requirement. Configuration and shared stores are not
embedded; worker source isolation must precede this bounded confined read.
"""

from __future__ import annotations

import hashlib
import json
import re
import tomllib
from pathlib import Path

from . import stories, ticketlayout
from .campaign_closure import _read
from .campaign_model import CampaignError, SourceSelector, UnitSpec
from .campaign_sources import source_paths
from .tickettree import TicketTree

MAX_BYTES = 256 * 1024


def _document(workspace: Path, path: Path) -> bytes:
    if not path.is_relative_to(workspace) or path.is_symlink() or not path.is_file():
        raise CampaignError(
            "selected product input is missing, redirected or outside its private checkout"
        )
    if path.stat().st_size > MAX_BYTES:
        raise CampaignError("selected product input exceeds its byte bound")
    data = _read(workspace, path)
    if len(data) > MAX_BYTES:
        raise CampaignError("selected product input exceeds its byte bound")
    return data


def _product(
    workspace: Path, path: Path, data: bytes, *, text: str | None = None, fragment: str = ""
) -> dict[str, str]:
    product = data.decode("utf-8-sig") if text is None else text
    if not product.strip():
        raise CampaignError("selected native product input is empty")
    return {
        "path": path.relative_to(workspace).as_posix() + fragment,
        "sha256": hashlib.sha256(data).hexdigest(),
        "text": product,
    }


def collect(workspace: Path, source: SourceSelector, unit: UnitSpec) -> tuple[dict[str, str], ...]:
    """Capture genuine native intent, preserving exact source preimage hashes."""
    workspace = workspace.resolve()
    if unit.kind in {"planning", "tracking"}:
        return ()  # Their approved brief/intake/source debt is appended separately.
    if source.kind not in {"tickets", "stories", "sprint-status"}:
        return ()
    paths = source_paths(workspace, source)
    if source.kind == "tickets":
        if paths.initiative_folder is None:
            raise CampaignError("native product input requires the selected initiative")
        if unit.kind == "closure":
            path = ticketlayout.epic_file(paths.initiative_folder, unit.selector)
            return (_product(workspace, path, _document(workspace, path)),)
        found = TicketTree(workspace, folder=paths.initiative_folder).find(unit.selector)
        plan = Path(found.plan)
        if plan.is_file():
            return (_product(workspace, plan, _document(workspace, plan)),)
        if found.story_file:
            leaf = Path(found.story_file)
            return (_product(workspace, leaf, _document(workspace, leaf)),)
        if not found.epic_file:
            raise CampaignError("unplanned native ticket has no exact product source")
        manifest = ticketlayout.breakdown_dir(Path(found.epic_file).parent) / ticketlayout.BREAKDOWN
        data = _document(workspace, manifest)
        rows = tomllib.loads(data.decode("utf-8-sig")).get("entry", [])
        selected = [row for row in rows if str(row.get("id")) == str(found.row.id)]
        if len(selected) != 1 or selected[0].get("title") != found.row.title:
            raise CampaignError("selected native ticket entry changed during product capture")
        # Known product fields only; never copy neighboring entries or arbitrary
        # configuration keys from an unplanned ticket's shared container.
        row = {
            key: selected[0][key]
            for key in ("id", "type", "title", "description", "verify", "references", "notes")
            if key in selected[0]
        }
        if not str(row.get("title", "")).strip():
            raise CampaignError("selected native ticket has no product intent")
        return (
            _product(
                workspace,
                manifest,
                data,
                text=json.dumps(row, ensure_ascii=False, indent=2),
                fragment="#entry:" + str(found.row.id),
            ),
        )
    if source.kind == "stories":
        folder = stories.resolve_spec_folder(workspace, source.path)
        selected = stories.resolve_story_spec(folder, unit.selector)
        if selected.path is not None and selected.kind == stories.KIND_PRESENT:
            return (_product(workspace, selected.path, _document(workspace, selected.path)),)
        if selected.kind != stories.KIND_PENDING:
            raise CampaignError("selected story product input is ambiguous")
        manifest = folder / stories.STORIES_FILENAME
        data = _document(workspace, manifest)
        entry = stories.find_entry(stories.load_stories(folder), unit.selector)
        text = json.dumps(
            {"id": entry.id, "title": entry.title, "description": entry.description},
            ensure_ascii=False,
            indent=2,
        )
        if _document(workspace, manifest) != data:
            raise CampaignError("selected stories product input changed during capture")
        return (_product(workspace, manifest, data, text=text, fragment="#story:" + entry.id),)
    if not re.fullmatch(r"\d+-\d+[a-z]?-[A-Za-z0-9_-]+", unit.selector):
        raise CampaignError("sprint product input requires an exact selected story key")
    matches = [
        path
        for path in (
            paths.implementation_artifacts / ("spec-" + unit.selector + ".md"),
            paths.implementation_artifacts / (unit.selector + ".md"),
        )
        if path.exists()
    ]
    if len(matches) > 1:
        raise CampaignError("selected sprint product spec is ambiguous")
    if matches:
        return (_product(workspace, matches[0], _document(workspace, matches[0])),)
    # An unplanned sprint story can use its exact native Markdown story section;
    # never publish the other stories from a shared epics document.
    manifest = paths.planning_artifacts / "epics.md"
    if not manifest.is_file():
        return ()
    data = _document(workspace, manifest)
    text = data.decode("utf-8-sig")
    epic, number, _ = unit.selector.split("-", 2)
    pattern = re.compile(
        r"^(#{1,6})\s+Story\s+" + re.escape(epic + "." + number) + r"\s*:\s+.+$",
        re.MULTILINE | re.IGNORECASE,
    )
    headings = list(pattern.finditer(text))
    if len(headings) != 1:
        return ()
    start = headings[0]
    end = re.search(
        r"^#{1," + str(len(start.group(1))) + r"}\s+", text[start.end() :], re.MULTILINE
    )
    section = text[start.start() : start.end() + end.start() if end else len(text)]
    return (
        _product(workspace, manifest, data, text=section, fragment="#Story-" + epic + "." + number),
    )
