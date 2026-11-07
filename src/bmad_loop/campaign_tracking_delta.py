"""Apply hierarchy observations without replacing another unit's newer facts.

Each I/O effect records only changed fields and their preimages. Confirmation
flags from disjoint concurrent observations merge; conflicting remote state or
identity requires a fresh observation instead of a last-writer-wins overwrite.
"""

from __future__ import annotations

import copy
from dataclasses import asdict

from .campaign_model import CampaignError

_MISSING = object()


def _changes(before, after):
    return {
        key: {"before": copy.deepcopy(before.get(key)), "after": copy.deepcopy(value)}
        for key, value in after.items()
        if before.get(key) != value
    }


def observation(snapshot, result, unit_id="", *, read_keys=None):
    value = asdict(result)
    before = snapshot.bindings.get("github_hierarchy", {})
    value["binding_changes"] = _changes(before, value.pop("bindings"))
    unit_before = snapshot.units[unit_id].bindings if unit_id else {}
    unit_after = value.pop("unit_bindings")
    value["unit_changes"] = _changes(unit_before, unit_after)
    value["graph_revision"] = snapshot.graph_revision
    value["projection_version"] = 1
    # State/identity read dependencies are local to this unit's hierarchy.
    keys = set(value["binding_changes"])
    if unit_id:
        unit = snapshot.units[unit_id]
        namespace = unit.spec.source + ":" + (unit.spec.initiative or snapshot.spec.name)
        keys.update(("initiative:" + namespace, "epic:" + namespace + ":" + unit.spec.epic))
        while unit:
            keys.add("story:" + unit.id)
            unit = snapshot.units.get(unit.spec.parent)
    elif read_keys is not None:
        keys.update(read_keys)
    else:
        keys.update(result.bindings)
    value["read_fence"] = {
        key: {name: binding[name] for name in ("number", "database_id", "state") if name in binding}
        for key in keys
        if (binding := result.bindings.get(key)) is not None
    }
    return value


def merge(current, changes):
    result = copy.deepcopy(current)
    for key, change in changes.items():
        before, after = change["before"], change["after"]
        existing = result.get(key)
        if isinstance(after, dict):
            if existing is not None and not isinstance(existing, dict):
                raise CampaignError("Hierarchy binding changed type")
            for name in ("number", "database_id"):
                if (
                    existing
                    and name in existing
                    and name in after
                    and existing[name] != after[name]
                ):
                    raise CampaignError(
                        "GitHub hierarchy identity changed during concurrent projection"
                    )
            old = before if isinstance(before, dict) else {}
            merged = dict(existing or {})
            for name in set(old) | set(after):
                prior, following = old.get(name, _MISSING), after.get(name, _MISSING)
                if prior == following:
                    continue
                latest = merged.get(name, _MISSING)
                if latest != prior and latest != following:
                    return None
                if following is _MISSING:
                    merged.pop(name, None)
                else:
                    merged[name] = copy.deepcopy(following)
            result[key] = merged
        else:
            if existing != before and existing != after:
                return None
            result[key] = copy.deepcopy(after)
    return result


def apply(snapshot, data, unit_id="") -> bool:
    if data.get("projection_version") != 1 or data.get("graph_revision") != snapshot.graph_revision:
        return False
    current = snapshot.bindings.get("github_hierarchy", {})
    bindings = merge(current, data["binding_changes"])
    if bindings is None:
        return False
    for key, fence in data["read_fence"].items():
        if any(
            bindings.get(key, {}).get(name, _MISSING) != expected
            for name, expected in fence.items()
        ):
            return False
    unit_bindings = merge(snapshot.units[unit_id].bindings, data["unit_changes"]) if unit_id else {}
    if unit_bindings is None:
        return False
    snapshot.bindings["github_hierarchy"] = bindings
    if unit_id:
        snapshot.units[unit_id].bindings = unit_bindings
    return True
