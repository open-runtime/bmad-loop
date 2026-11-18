"""Selection delivery does not grant authority to close a native parent scope."""

from .campaign_model import CampaignSnapshot, UnitState


def parent_keys(snapshot: CampaignSnapshot, key: str) -> tuple[str, ...]:
    """Join aliases only through a complete, exact native parent identity."""
    bindings = snapshot.bindings.get("github_hierarchy", {})
    bound = bindings.get(key, {})
    identity = tuple(bound.get(field) for field in ("kind", "database_id", "number"))
    if not all(identity):
        return (key,)
    return tuple(
        sorted(
            {
                key,
                *(
                    name
                    for name, row in bindings.items()
                    if name.startswith(("epic:", "initiative:"))
                    and tuple(row.get(field) for field in ("kind", "database_id", "number"))
                    == identity
                ),
            }
        )
    )


def parent_members(snapshot: CampaignSnapshot, key: str) -> tuple[str, ...]:
    """Include every known alias and declared descendant of a parent scope."""
    keys = set(parent_keys(snapshot, key))
    members = {
        unit.id
        for unit in snapshot.units.values()
        if (
            f"epic:{unit.spec.source}:{unit.spec.initiative or snapshot.spec.name}:{unit.spec.epic}"
            in keys
            or f"initiative:{unit.spec.source}:{unit.spec.initiative or snapshot.spec.name}" in keys
        )
    }
    while descendants := {
        unit.id
        for unit in snapshot.units.values()
        if unit.spec.parent in members and unit.id not in members
    }:
        members.update(descendants)
    return tuple(sorted(members))


def parent_wait_reason(snapshot: CampaignSnapshot, key: str) -> str:
    """A delivered selection may settle its own scope, never a foreign one."""
    from .campaign_runtime_intake import optional_canceled
    from .campaign_tracking_recovery import acceptance_wait

    members = parent_members(snapshot, key)
    if not members:
        return "Parent tracking has no proven campaign member scope"
    for name in members:
        unit = snapshot.units[name]
        if unit.hold:
            return f"Parent tracking retains member hold: {name}"
        if any(
            unit.evidence.get(field)
            for field in (
                "unresolved_approval_requirements",
                "pending_failure_hold",
                "pending_failure_holds",
                "operator_prior_hold",
            )
        ):
            return f"Parent tracking retains pending member obligations: {name}"
        if unit.claim and not unit.claim.quiescent:
            return f"Parent tracking waits for member quiescence: {name}"
        if reason := acceptance_wait(unit):
            return f"Parent tracking retains member acceptance: {name}: {reason}"
        if unit.state != UnitState.DELIVERED and not optional_canceled(unit):
            return f"Parent tracking waits for required member delivery: {name}"
    return ""


def retained_parent_reason(snapshot: CampaignSnapshot, key: str) -> str:
    """Keep selected-source epics/milestones open, even with no remote children.

    Match durable source namespaces rather than mutable parent titles. A sparse
    GitHub projection is not evidence that the rest of a native epic is done.
    """
    keys = parent_keys(snapshot, key)
    for source in snapshot.spec.sources:
        if source.completion_scope != "selected-units":
            continue
        namespace = f"{source.id or source.kind}:{source.initiative or snapshot.spec.name}"
        if any(
            item == "initiative:" + namespace or item.startswith("epic:" + namespace + ":")
            for item in keys
        ):
            return f"Source {source.id or source.kind} delivers selected units; parent scope remains open"
    return ""
