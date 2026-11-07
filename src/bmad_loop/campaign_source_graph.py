"""Read structured ticket dependency, ownership and scheduler-hold declarations.

No project scheduler module is imported or executed. The qualified ticket API
supplies state/identity; bounded TOML reads supply project-specific admission
metadata that the stock ticket API deliberately omits. Unknown ownership remains
global/conservative rather than being inferred from a story number or prose.
"""

from __future__ import annotations

import hashlib
import json
import re
import tomllib
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .bmadconfig import ProjectPaths
from .campaign_model import CampaignError, SourceSelector, normalize_scope, scopes_overlap
from .ticketlayout import BREAKDOWN, breakdown_dir, epic_folder
from .ticketmodel import StatusView
from .tickettree import TicketsError, TicketTree


@dataclass(frozen=True)
class TicketGraphMetadata:
    dependencies: dict[str, tuple[str, ...]]
    scopes: dict[str, tuple[str, ...]]
    holds: dict[str, tuple[dict[str, Any], ...]]
    revisions: tuple[dict[str, str], ...]


def _qualified(source: SourceSelector, local: str) -> str:
    return f"{source.id or source.kind}:{source.initiative or 'project'}:{local}"


def _toml(path: Path, *, root: Path, revisions: list[dict[str, str]]) -> dict[str, Any]:
    if not path.is_relative_to(root) or ".." in path.relative_to(root).parts or path.is_symlink():
        raise CampaignError("ticket graph metadata is redirected outside its selected source")
    relative = path.relative_to(root)
    if any((root / ancestor).is_symlink() for ancestor in relative.parents) or root.is_symlink():
        raise CampaignError("ticket graph metadata parent is symlinked")
    try:
        with path.open("rb") as stream:
            raw = stream.read(8 * 1024 * 1024 + 1)
        if len(raw) > 8 * 1024 * 1024:
            raise CampaignError("ticket graph metadata exceeds byte bound")
        value = tomllib.loads(raw.decode("utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise CampaignError(
            f"ticket graph metadata unavailable: {path.name}: {type(exc).__name__}"
        ) from exc
    revisions.append({"path": str(path), "sha256": hashlib.sha256(raw).hexdigest()})
    return value


def _rows(value: dict[str, Any], name: str, key: str) -> dict[str, dict[str, Any]]:
    rows = value.get(name, [])
    if not isinstance(rows, list) or any(
        not isinstance(row, dict) or key not in row for row in rows
    ):
        raise CampaignError(f"ticket graph metadata requires structured [[{name}]] identities")
    result = {str(row[key]): row for row in rows}
    if len(result) != len(rows):
        raise CampaignError(f"ticket graph metadata has duplicate {name} identities")
    return result


def _touches(row: dict[str, Any], *, context: str) -> tuple[str, ...] | None:
    if "touches" not in row:
        return None
    values = row["touches"]
    if not isinstance(values, list) or any(not isinstance(value, str) for value in values):
        raise CampaignError(f"{context}: touches must be repository-relative paths")
    normalized = tuple(dict.fromkeys(normalize_scope(value) for value in values))
    if any(
        value.startswith("resource:")
        or any(mark in value for mark in ("?", "[", "]"))
        or ("*" in value and value != "*")
        for value in normalized
    ):
        raise CampaignError(
            f"{context}: touches paths cannot contain glob patterns or resource aliases"
        )
    return normalized


def inspect_ticket_graph(
    project: Path,
    source: SourceSelector,
    paths: ProjectPaths,
    board: StatusView,
    sources: tuple[SourceSelector, ...],
) -> TicketGraphMetadata:
    revisions: list[dict[str, str]] = []
    root = paths.initiative_folder
    if root is None:
        raise CampaignError("ticket graph requires a selected initiative folder")
    if not source.scheduler_config and (project / "tools/bmad-factory/scheduler.toml").exists():
        raise CampaignError(
            "configure source.scheduler_config to account for this project's scheduler holds"
        )
    declarations = _rows(
        _toml(breakdown_dir(root) / BREAKDOWN, root=root, revisions=revisions), "epic", "slug"
    )
    epic_ids = {str(epic.id): epic.slug for epic in board.epics if epic.id is not None}
    epics = {epic.slug: epic for epic in board.epics}
    all_tickets = (*board.tickets, *(board.backlog.tickets if board.backlog else ()))
    local_refs = {
        (row.epic, str(row.id)): row.ref for row in all_tickets if row.ref and row.id is not None
    }
    dependencies: dict[str, tuple[str, ...]] = {}
    scopes: dict[str, tuple[str, ...]] = {}
    holds: dict[str, tuple[dict[str, Any], ...]] = {}
    epic_scopes: dict[str, tuple[str, ...]] = {}

    def reference(epic: str, value: str) -> str:
        local = local_refs.get((epic, value), value)
        if local in epics:
            return "closure/" + local
        return local

    for epic in board.epics:
        row = declarations.get(epic.slug, {})
        ready_on = row.get("ready_on", "epics")
        if ready_on not in {"epics", "tickets"}:
            raise CampaignError(f"{epic.slug}: ready_on must be epics or tickets")
        if row.get("lease_scope", "declared") != "declared":
            raise CampaignError(f"{epic.slug}: unsupported lease_scope")
        declared_scope = _touches(row, context=epic.slug)
        entries: dict[str, dict[str, Any]] = {}
        if epic.folder:
            folder = epic_folder(root, epic.slug)
            entries = _rows(
                _toml(breakdown_dir(folder) / BREAKDOWN, root=root, revisions=revisions),
                "entry",
                "id",
            )
        children = [ticket for ticket in all_tickets if ticket.epic == epic.slug]
        ticket_scopes = {
            str(ticket.id): _touches(
                entries.get(str(ticket.id), {}), context=f"{epic.slug}/{ticket.id}"
            )
            for ticket in children
        }
        broad = (
            declared_scope
            if declared_scope is not None
            else tuple(
                dict.fromkeys(path for values in ticket_scopes.values() for path in values or ())
            )
        )
        # An incomplete derived footprint remains unknown. Explicit broad epic
        # declarations can safely reserve the same area for all legacy children.
        if declared_scope is None and any(value is None for value in ticket_scopes.values()):
            broad = ()
        epic_scopes[epic.slug] = broad
        pinned: dict[str, set[str]] = {}
        for ticket in children:
            for edge in ticket.after:
                target = (
                    epic_ids.get(edge.partition(".")[0])
                    if re.fullmatch(r"[0-9]+\.[0-9]+", edge)
                    else edge if edge in epics else None
                )
                if target and target != epic.slug:
                    pinned.setdefault(target, set()).add(reference(epic.slug, edge))
        inherited: set[str] = set()
        hard_gates = {str(gate) for gate in epic.gated_by}
        for declared in epic.after:
            target = epic_ids.get(declared.epic, declared.epic)
            if ready_on == "tickets" and target not in hard_gates and pinned.get(target):
                inherited.update(pinned[target])
            else:
                inherited.add("closure/" + target)
        inherited.update("closure/" + epic_ids.get(gate, gate) for gate in hard_gates)
        for ticket in children:
            if not ticket.ref:
                raise CampaignError("ticket graph metadata requires stable ticket references")
            after = set(inherited)
            after.update(reference(epic.slug, edge) for edge in ticket.after)
            after.update("closure/" + epic_ids.get(gate, gate) for gate in ticket.gated_by)
            dependencies[ticket.ref] = tuple(sorted(_qualified(source, ref) for ref in after))
            own = ticket_scopes[str(ticket.id)] if row.get("lease_scope") == "declared" else broad
            # Missing per-ticket declarations never inherit narrow sibling scope.
            scopes[ticket.ref] = own if own else ()

    for ticket in all_tickets:
        if ticket.epic in epics or not ticket.ref:
            continue
        dependencies[ticket.ref] = tuple(
            _qualified(source, reference(ticket.epic, edge)) for edge in ticket.after
        )
        scopes[ticket.ref] = ()

    if source.scheduler_config:
        config = Path(source.scheduler_config).expanduser()
        if not config.is_absolute():
            config = project / config
        config_root = project if config.is_relative_to(project) else config.parent
        raw = _toml(config, root=config_root, revisions=revisions)
        scheduler = raw.get("scheduler", {})
        if not isinstance(scheduler, dict) or not isinstance(scheduler.get("holds", []), list):
            raise CampaignError("scheduler metadata requires [[scheduler.holds]] tables")
        for hold in scheduler.get("holds", []):
            if not isinstance(hold, dict) or set(hold) - {
                "initiative",
                "epic",
                "touching",
                "until",
                "why",
            }:
                raise CampaignError("scheduler hold contains unknown fields")
            home = hold.get("initiative")
            if not isinstance(home, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", home):
                raise CampaignError("scheduler hold initiative identity invalid")
            if home != (source.initiative or paths.active_initiative):
                continue
            until = hold.get("until", "")
            match = (
                re.fullmatch(r"([A-Za-z0-9][A-Za-z0-9._-]*)/([0-9]+\.[0-9]+)", until)
                if isinstance(until, str)
                else None
            )
            if not match or not isinstance(hold.get("why", ""), str):
                raise CampaignError(
                    "scheduler hold until must name initiative/ticket-ref and textual reason"
                )
            until_home, until_ref = match.groups()
            target_epic = hold.get("epic")
            if target_epic is not None:
                if not isinstance(target_epic, str) or not target_epic or "touching" in hold:
                    raise CampaignError("scheduler epic hold must name one epic without touching")
                affected = {target_epic}
            else:
                touching = _touches({"touches": hold.get("touching")}, context="scheduler hold")
                if not touching:
                    raise CampaignError("scheduler touching hold must name nonempty path prefixes")
                affected = {
                    slug for slug, values in epic_scopes.items() if scopes_overlap(values, touching)
                }
            state, unavailable, source_revision = None, "", ""
            found = None
            try:
                other = (
                    board
                    if until_home == (source.initiative or paths.active_initiative)
                    else TicketTree(
                        project, folder=paths.with_initiative(until_home).initiative_folder
                    ).status()
                )
                if other.problems:
                    raise CampaignError("hold source reports problems")
                found = next((ticket for ticket in other.tickets if ticket.ref == until_ref), None)
                state = found.state if found else None
                source_revision = hashlib.sha256(
                    json.dumps(asdict(other), sort_keys=True).encode()
                ).hexdigest()
            except (OSError, ValueError, RuntimeError, TicketsError) as exc:
                unavailable = type(exc).__name__
            mapped = next(
                (
                    candidate
                    for candidate in sources
                    if candidate.kind == "tickets"
                    and (candidate.initiative or paths.active_initiative) == until_home
                    and (not candidate.units or until_ref in candidate.units)
                    and (not candidate.epics or (found and found.epic in candidate.epics))
                ),
                None,
            )
            for ticket in all_tickets:
                if ticket.epic not in affected or not ticket.ref:
                    continue
                fact = {
                    "until": until,
                    "why": hold.get("why", ""),
                    "state": state,
                    "active": state != "done",
                    "unavailable": unavailable,
                    "source_revision": source_revision,
                    "dependency": _qualified(mapped, until_ref) if mapped else "",
                }
                holds[ticket.ref] = (*holds.get(ticket.ref, ()), fact)
                if mapped is not None:
                    dependencies[ticket.ref] = tuple(
                        dict.fromkeys((*dependencies[ticket.ref], _qualified(mapped, until_ref)))
                    )
    return TicketGraphMetadata(dependencies, scopes, holds, tuple(revisions))
