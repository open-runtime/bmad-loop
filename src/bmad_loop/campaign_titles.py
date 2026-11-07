"""Bounded navigation labels; never use presentation text as resource identity.

Hcom identities come from hook observations bound to the current worker launch.
Unknown actors and uncreated GitHub objects stay explicit. Group labels aggregate
their members rather than attributing the whole group to one arbitrary worker.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable


def _text(value: str, limit: int) -> str:
    clean = " ".join(
        "".join(char if not unicodedata.category(char).startswith("C") else " " for char in value)
        .replace(",", "/")
        .split()
    )
    return clean if len(clean) <= limit else clean[: limit - 1] + "…"


def _values(values: Iterable[str], *, limit: int = 30) -> str:
    values = sorted({_text(value, 128) for value in values if value.strip()})
    if not values:
        return "pending"
    shown = values[:2]
    suffix = f" +{len(values) - len(shown)}" if len(values) > len(shown) else ""
    return _text("/".join(shown), limit - len(suffix)) + suffix


def navigation_title(
    *,
    names: Iterable[str] = (),
    epics: Iterable[str] = (),
    issues: Iterable[str] = (),
    prs: Iterable[str] = (),
    description: str = "",
    limit: int = 180,
) -> str:
    """Reserve space for every navigation field before shortening description."""
    actors = ["@" + name for name in names if re.fullmatch(r"[a-z][a-z0-9_-]{2,31}", name)]
    prefix = " · ".join(
        (
            _values(actors) if actors else "hcom:pending",
            "E:" + _values(epics),
            "I:" + _values(issues, limit=26),
            "PR:" + _values(prs, limit=26),
        )
    )
    remaining = limit - len(prefix) - 3
    detail = _text(description, remaining) if description and remaining > 1 else ""
    return prefix + (" · " + detail if detail else "")


def _number(binding: object, url: str = "") -> tuple[str, ...]:
    if isinstance(binding, dict):
        number = binding.get("number")
        if type(number) is int and number > 0:
            return ("#" + str(number),)
        url = binding.get("url") or binding.get("html_url") or url
    match = re.search(r"/(?:issues|pull)/(\d+)(?:[?#].*)?$", str(url))
    return ("#" + match[1],) if match else ()


def unit_navigation(unit, projection: dict | None = None) -> dict:
    """Use only this launch's observed hooks, never inherited names or CLI brands."""
    stage = (
        projection
        if projection is not None
        else unit.bindings.get("reflection", {}).get("stage", {})
    )
    names = set()
    if unit.claim and stage.get("launch_id") == unit.claim.launch_id:
        for window in stage.get("windows", ()):
            if not unit.claim.quiescent and window.get("state") not in {"active", "launching"}:
                continue
            name = (window.get("hook") or {}).get("hcom_name")
            if isinstance(name, str) and re.fullmatch(r"[a-z][a-z0-9_-]{2,31}", name):
                names.add(name)
    return {
        "names": tuple(sorted(names)),
        "epics": (unit.spec.epic,) if unit.spec.epic else (),
        "issues": _number(unit.bindings.get("issue"), unit.bindings.get("issue_url", "")),
        "prs": _number(unit.bindings.get("pr")),
    }


def group_navigation(snapshot, unit, *, projection: dict | None = None) -> dict:
    """Aggregate exactly the same source/initiative/story-or-epic group members."""
    by_epic = snapshot.spec.paradev.group_by == "epic"

    def key(member):
        spec = member.spec
        return (spec.source, spec.initiative, spec.epic if by_epic else spec.parent or spec.id)

    fields: dict[str, set[str]] = {name: set() for name in ("names", "epics", "issues", "prs")}
    for member in snapshot.units.values():
        if key(member) != key(unit) or not member.bindings.get("native"):
            continue
        values = unit_navigation(member, projection if member.id == unit.id else None)
        for name, items in values.items():
            fields[name].update(items)
    return {name: tuple(sorted(values)) for name, values in fields.items()}
