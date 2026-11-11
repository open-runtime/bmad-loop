"""Bounded navigation labels; never use presentation text as resource identity.

Groups, workspaces and tasks identify durable work. Sessions identify their own
native actors and separately named controller bridges. Legacy compact navigation
helpers remain available for old projections; they never define resource identity.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable


def _text(value: str, limit: int) -> str:
    # Native terminal titles discard replacement characters and Braille spinner
    # frames. Normalize before recording the expected title for exact readback.
    value = re.sub(r"[\ufffd\u2800-\u28ff]", "", value)
    clean = " ".join(
        "".join(char if not unicodedata.category(char).startswith("C") else " " for char in value)
        .replace(",", "/")
        .split()
    )
    return clean if len(clean) <= limit else clean[: limit - 1] + "…"


def _values(values: Iterable[str], *, limit: int = 30) -> str:
    values = sorted({clean for value in values if (clean := _text(value, 128))})
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


def navigation_tag(
    *,
    names: Iterable[str] = (),
    epics: Iterable[str] = (),
    issues: Iterable[str] = (),
    prs: Iterable[str] = (),
    limit: int = 35,
) -> str:
    """Fit all navigation fields in the host's remaining UTF-16 tag budget.

    The caller reserves the immutable ownership suffix separately. Normalize
    before measuring, as the native host lowercases tags and counts JavaScript
    string units. An ellipsis marks shortened values; ``?`` means not observed.
    Full titles and group metadata retain the longer labels.
    """
    names, epics, issues, prs = tuple(names), tuple(epics), tuple(issues), tuple(prs)

    def size(value: str) -> int:
        return sum(2 if ord(char) > 0xFFFF else 1 for char in value)

    full = navigation_title(names=names, epics=epics, issues=issues, prs=prs).lower()
    if size(full) <= limit:
        return full
    actors = ["@" + name for name in names if re.fullmatch(r"[a-z][a-z0-9_-]{2,31}", name)]
    values = [
        _values(actors).lower() if actors else "hcom:?",
        *(_values(items).lower() if items else "?" for items in (epics, issues, prs)),
    ]
    labels = ("", "e:", "i:", "pr:")
    budgets = [size(value) for value in values]
    minima = [2 if actors else len("hcom:?"), 1, 1, 1]
    overhead = len(" · ") * 3 + sum(map(len, labels))
    if limit < overhead + sum(minima):
        raise ValueError("Navigation tag budget cannot retain all four fields")
    while overhead + sum(budgets) > limit:
        index = max((i for i in range(4) if budgets[i] > minima[i]), key=budgets.__getitem__)
        budgets[index] -= 1

    def shorten(value: str, budget: int) -> str:
        if size(value) <= budget:
            return value
        prefix = ""
        for char in value:
            if size(prefix + char) > budget - 1:
                break
            prefix += char
        return prefix + "…"

    return " · ".join(
        label + shorten(value, budget) for label, value, budget in zip(labels, values, budgets)
    )


def _number(binding: object, url: str = "") -> tuple[str, ...]:
    if isinstance(binding, dict):
        number = binding.get("number")
        if type(number) is int and number > 0:
            return ("#" + str(number),)
        url = binding.get("url") or binding.get("html_url") or url
    match = re.search(r"/(?:issues|pull)/(\d+)(?:[?#].*)?$", str(url))
    return ("#" + match[1],) if match else ()


def issue_reference(unit, *, snapshot=None) -> tuple[dict, str]:
    """Prefer unit identity, then its exact applied shared hierarchy binding.

    Hierarchy reconciliation can confirm a native story before all links are
    ready. That is sufficient for navigation, but never completion or ownership.
    Source hints and prepared intents are not native issue identities.
    """
    bound = unit.bindings.get("issue")
    url = unit.bindings.get("issue_url", "")
    if bound or url or snapshot is None:
        return (bound if isinstance(bound, dict) else {}), url
    shared = snapshot.bindings.get("github_hierarchy", {}).get("story:" + unit.id)
    if not isinstance(shared, dict):
        control = unit.bindings.get("intake_control", {})
        records = snapshot.bindings.get("intake_inbox", {}).get("records", {})
        observed = []
        for version in control.get("input_versions", ()):
            record = records.get(version, {})
            row = record.get("observation", {})
            number = row.get("number")
            if (
                record.get("control_unit") == unit.id
                and row.get("kind") == "issue"
                and isinstance(row.get("repository"), str)
                and row.get("repository", "").lower() == snapshot.repository.lower()
                and type(number) is int
                and number > 0
                and isinstance(row.get("url"), str)
                and row.get("url", "").lower()
                == f"https://github.com/{snapshot.repository}/issues/{number}".lower()
            ):
                observed.append(row)
        if observed and len({row["number"] for row in observed}) == 1:
            return observed[0], observed[0]["url"]
        return {}, ""
    number, native_id, shared_url = (
        shared.get("number"),
        shared.get("database_id"),
        shared.get("url"),
    )
    if (
        shared.get("kind") != "issue"
        or type(number) is not int
        or number < 1
        or type(native_id) is not int
        or native_id < 1
        or not isinstance(shared_url, str)
        or shared_url.lower() != f"https://github.com/{snapshot.repository}/issues/{number}".lower()
    ):
        return {}, ""
    return shared, shared["url"]


def unit_navigation(unit, projection: dict | None = None, *, snapshot=None) -> dict:
    """Use only this launch's observed hooks, never inherited names or CLI brands."""
    stage = (
        projection
        if projection is not None
        else unit.bindings.get("reflection", {}).get("stage", {})
    )
    names = set()
    bridge = unit.bindings.get("managed_communication") or {}
    target = bridge.get("target", {})
    managed = (
        unit.claim
        and bridge.get("kind") == "bridge"
        and (target.get("unit_id"), target.get("launch_id"), target.get("generation"))
        == (unit.id, unit.claim.launch_id, unit.claim.generation)
    )
    if managed and isinstance(bridge.get("name"), str):
        names.add(bridge["name"])
    elif unit.claim and stage.get("launch_id") == unit.claim.launch_id:
        for window in stage.get("windows", ()):
            if not unit.claim.quiescent and window.get("state") not in {"active", "launching"}:
                continue
            name = (window.get("hook") or {}).get("hcom_name")
            if isinstance(name, str) and re.fullmatch(r"[a-z][a-z0-9_-]{2,31}", name):
                names.add(name)
    issue, issue_url = issue_reference(unit, snapshot=snapshot)
    return {
        "names": tuple(sorted(names)),
        "epics": (unit.spec.epic,) if unit.spec.epic else (),
        "issues": _number(issue, issue_url),
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
        values = unit_navigation(
            member, projection if member.id == unit.id else None, snapshot=snapshot
        )
        for name, items in values.items():
            fields[name].update(items)
    return {name: tuple(sorted(values)) for name, values in fields.items()}


def initiative_identity(snapshot, unit) -> str:
    """Use frozen source authority; never consult current project configuration."""
    observation = snapshot.bindings.get("source_observations", {}).get(unit.id, {})
    source = unit.bindings.get("effective_source", {})
    values = [unit.spec.initiative, observation.get("effective_initiative", "")]
    if source.get("kind") == "tickets" and source.get("id") == unit.spec.source:
        values.append(source.get("initiative", ""))
    values = {value for value in values if isinstance(value, str) and value}
    if len(values) > 1:
        raise ValueError("Frozen initiative identities disagree")
    return next(iter(values), "")


def group_key(snapshot, unit) -> tuple[str, str, str]:
    initiative = initiative_identity(snapshot, unit)
    mode = snapshot.spec.paradev.group_by
    if mode == "initiative":
        if not initiative and unit.spec.source != "campaign-intake":
            raise ValueError("Initiative grouping requires frozen initiative identity")
        label = initiative or "intake"
    else:
        label = unit.spec.epic if mode == "epic" else unit.spec.parent or unit.id
    return unit.spec.source, initiative, label


def group_title(snapshot, unit) -> str:
    """Business hierarchy only; sessions and rotating PR lists are not groups."""
    source, initiative, label = group_key(snapshot, unit)
    prefix = initiative or source
    if snapshot.spec.paradev.group_by == "initiative":
        parts = [prefix]
    elif snapshot.spec.paradev.group_by == "story":
        parts = [prefix, "Story " + (unit.spec.parent or unit.spec.selector or label)]
    else:
        parts = [prefix, "Intake" if source == "campaign-intake" else "Epic " + label]
    return folder_title(" · ".join(parts))


def folder_title(value: str) -> str:
    """Native folder displayName uses a separate 200 UTF-16-unit limit."""
    value = _text(value, 200)
    if len(value.encode("utf-16-le")) // 2 <= 200:
        return value
    result = ""
    for char in value:
        if len((result + char).encode("utf-16-le")) // 2 > 199:
            break
        result += char
    return result + "…"


def work_title(unit, *, snapshot=None) -> str:
    """Stable work labels omit absent coordinates and all actor identities."""
    issue, url = issue_reference(unit, snapshot=snapshot)
    parts = list(_number(issue, url))
    if unit.spec.selector:
        parts.append(unit.spec.selector)
    parts.append(unit.spec.title)
    prs = _number(unit.bindings.get("pr"))
    suffix = " · PR " + "/".join(prs) if prs else ""
    return _text(" · ".join(parts), 180 - len(suffix)) + suffix


def session_title(
    *,
    role="",
    model="",
    attempt=None,
    native_name="",
    bridge=None,
    description="",
    historical=False,
    limit=180,
) -> str:
    """Native actors and controller addresses are different, generation-bound facts."""
    parts = [str(value) for value in (role, model) if value]
    if re.fullmatch(r"[a-z][a-z0-9_-]{2,31}", native_name):
        parts.append("native @" + native_name)
    if isinstance(bridge, dict) and re.fullmatch(r"[a-z][a-z0-9_-]{2,31}", bridge.get("name", "")):
        closed = bridge.get("state") == "closed"
        label = "closed control @" if closed else "past control @" if historical else "control @"
        parts.append(label + bridge["name"])
    if attempt is not None:
        parts.append("attempt " + str(attempt))
    if historical:
        parts.append("history")
    if description:
        parts.append(description)
    return _text(" · ".join(parts) or "Session", limit)


def unit_session_title(unit, window, *, snapshot=None, historical=False, limit=150) -> str:
    claim = unit.claim
    bridge = unit.bindings.get("managed_communication", {})
    target = bridge.get("target", {})
    if not claim or (target.get("unit_id"), target.get("launch_id"), target.get("generation")) != (
        unit.id,
        claim.launch_id,
        claim.generation,
    ):
        bridge = None
    return session_title(
        role=window.get("phase", "session"),
        model=(window.get("native") or {}).get("definitionId", ""),
        attempt=claim.attempt if claim else unit.attempt,
        native_name=(window.get("hook") or {}).get("hcom_name", ""),
        bridge=bridge,
        description=work_title(unit, snapshot=snapshot),
        historical=historical,
        limit=limit,
    )
