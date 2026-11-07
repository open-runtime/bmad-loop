"""Bounded independent parent closure, with individual durable GitHub writes.

Each scope observes and updates only its own hierarchy identity. Epics can close
concurrently; initiatives wait for their epic confirmations. An aggregate pass
still owns final campaign closure after every independent write has settled.
"""

from .campaign_action_history import TrackingReceipts
from .campaign_store import add_event
from .campaign_supervisor import Observation, Proposal
from .campaign_tracking import HierarchyCoordinator
from .campaign_tracking_delta import observation

LIMIT = 16


def _deferred(snapshot):
    value = snapshot.bindings.get("hierarchy_deferred", {})
    return (
        value.get("parents", {}) if value.get("graph_revision") == snapshot.graph_revision else {}
    )


def dependencies(bindings, key):
    if key.startswith("initiative:"):
        prefix = "epic:" + key.removeprefix("initiative:") + ":"
        return {name for name in bindings if name.startswith(prefix)}
    return set()


def proposals(snapshot):
    bindings = snapshot.bindings.get("github_hierarchy", {})
    deferred = _deferred(snapshot)
    pending = {
        action.payload.get("parent_key", "")
        for action in snapshot.outbox
        if action.state in {"prepared", "sent_unknown"}
        and (action.kind == "hierarchy-close" or action.payload.get("purpose") == "parent-tracking")
    }
    if len(pending) >= LIMIT:
        return ()
    remaining = [
        key
        for key, row in bindings.items()
        if key.startswith(("epic:", "initiative:"))
        and not row.get("closed_confirmed")
        and key not in deferred
    ]
    unit = next(reversed(snapshot.units))
    selected = []
    for key in sorted(remaining):
        if key in pending or any(
            not bindings[dependency].get("closed_confirmed") and dependency not in deferred
            for dependency in dependencies(bindings, key)
        ):
            continue
        selected.append(Proposal("hierarchy-close", unit, {"parent_key": key}))
        if len(selected) >= max(0, LIMIT - len(pending)):
            break
    if not remaining and not pending:
        selected.append(Proposal("hierarchy-close", unit))
    return tuple(selected)


def perform(runtime, snapshot, action):
    key = action.payload["parent_key"]
    bindings = snapshot.bindings.get("github_hierarchy", {})
    if key not in bindings:
        return Observation("confirmed", {"stale_scope": True})
    receipts = TrackingReceipts(snapshot)
    step = HierarchyCoordinator(runtime.gateway(snapshot)).close_parents(
        snapshot,
        receipts=receipts,
        only_key=key,
    )
    data = observation(snapshot, step, read_keys={key, *dependencies(bindings, key)})
    data["parent_key"] = key
    prior = receipts.get(step.action_key) if step.action_key else None
    data["pending_parent"] = bool(prior and prior.state != "confirmed")
    return Observation("confirmed", data)


def apply(runtime, snapshot, unit, action, data):
    key = data["parent_key"]
    if data.get("intent"):
        runtime._queue_intent(
            snapshot,
            unit,
            {"intent": data["intent"], "purpose": "parent-tracking", "parent_key": key},
            tracking_key=data["action_key"],
        )
    elif data.get("reason") and not data.get("pending_parent"):
        state = snapshot.bindings.get("hierarchy_deferred")
        if not state or state["graph_revision"] != snapshot.graph_revision:
            state = {"graph_revision": snapshot.graph_revision, "parents": {}}
            snapshot.bindings["hierarchy_deferred"] = state
        state["parents"][key] = data["reason"]
        add_event(snapshot, "parent-tracking-deferred", parent=key, reason=data["reason"])


def guard(runtime, snapshot, action, intent):
    """Fresh scope and native-child checks immediately before a write or retry."""
    current = runtime.store.load(snapshot.id)
    if action.payload.get("scope_revision") != current.graph_revision:
        return Observation("confirmed", {"intent": intent.to_dict(), "canceled_before_write": True})
    if not runtime._units_settled(current):
        return Observation(
            "waiting",
            {"intent": intent.to_dict()},
            "Parent tracking waits for current intake and required delivery to settle",
            runtime.clock() + 5,
        )
    if intent.state == "confirmed":
        return None
    gateway = runtime.gateway(current)
    if intent.kind == "close_issue":
        members = gateway.pages(intent.path + "/sub_issues")
    else:
        number = intent.path.rsplit("/", 1)[-1]
        members = gateway.pages(f"{gateway.root}/issues?state=open&milestone={number}")
    if any(row.get("state") != "closed" for row in members):
        return Observation(
            "waiting",
            {"intent": intent.to_dict()},
            "Parent tracking waits for newly observed open native dependencies",
            runtime.clock() + 5,
        )
    return None
