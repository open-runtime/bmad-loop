"""Selection delivery does not grant authority to close a native parent scope."""

from .campaign_model import CampaignSnapshot


def retained_parent_reason(snapshot: CampaignSnapshot, key: str) -> str:
    """Keep selected-source epics/milestones open, even with no remote children.

    Match durable source namespaces rather than mutable parent titles. A sparse
    GitHub projection is not evidence that the rest of a native epic is done.
    """
    keys = {key}
    bindings = snapshot.bindings.get("github_hierarchy", {})
    bound = bindings.get(key)
    if bound:
        keys.update(
            name
            for name, binding in bindings.items()
            if binding.get("kind") == bound.get("kind")
            and binding.get("number") == bound.get("number")
        )
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
