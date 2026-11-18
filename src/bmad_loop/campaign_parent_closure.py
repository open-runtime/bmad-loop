"""Bounded independent parent closure, with individual durable GitHub writes.

Each scope observes and updates only its own hierarchy identity. Epics can close
concurrently; initiatives wait for their epic confirmations. An aggregate pass
still owns final campaign closure after every independent write has settled.
"""

from .campaign_action_history import TrackingReceipts
from .campaign_completion_scope import parent_keys, parent_members, parent_wait_reason
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


def dependencies(snapshot, key):
    bindings = snapshot.bindings.get("github_hierarchy", {})
    prefixes = tuple(
        "epic:" + alias.removeprefix("initiative:") + ":"
        for alias in parent_keys(snapshot, key)
        if alias.startswith("initiative:")
    )
    return {name for name in bindings if prefixes and name.startswith(prefixes)}


def proposals(snapshot):
    from .campaign_runtime_intake import optional_canceled, settled
    from .campaign_tracking_recovery import pending_parents

    if not settled(snapshot):
        return ()
    bindings = snapshot.bindings.get("github_hierarchy", {})
    deferred = _deferred(snapshot)
    pending = pending_parents(snapshot) | {
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
    selected = []
    for key in sorted(remaining):
        if (
            key in pending
            or parent_wait_reason(snapshot, key)
            or any(
                not bindings[dependency].get("closed_confirmed") and dependency not in deferred
                for dependency in dependencies(snapshot, key)
            )
        ):
            continue
        unit = parent_members(snapshot, key)[0]
        selected.append(Proposal("hierarchy-close", unit, {"parent_key": key}))
        if len(selected) >= max(0, LIMIT - len(pending)):
            break
    if (
        not remaining
        and not pending
        and snapshot.units
        and all(
            unit.state == "delivered" or optional_canceled(unit) for unit in snapshot.units.values()
        )
    ):
        selected.append(Proposal("hierarchy-close", next(iter(snapshot.units))))
    return tuple(selected)


def perform(runtime, snapshot, action):
    key = action.payload["parent_key"]
    bindings = snapshot.bindings.get("github_hierarchy", {})
    if key not in bindings or parent_wait_reason(snapshot, key):
        return Observation("confirmed", {"stale_scope": True})
    from .campaign_runtime_intake import settled

    if not settled(snapshot):
        return Observation("confirmed", {"stale_scope": True})
    receipts = TrackingReceipts(snapshot)
    step = HierarchyCoordinator(runtime.gateway(snapshot)).close_parents(
        snapshot,
        receipts=receipts,
        only_key=key,
    )
    data = observation(
        snapshot,
        step,
        read_keys={*parent_keys(snapshot, key), *dependencies(snapshot, key)},
    )
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
    from .campaign_completion_scope import retained_parent_reason

    keys = {action.payload["parent_key"]} if action.payload.get("parent_key") else set()
    # Old aggregate-close intents have no parent_key. Resolve their immutable
    # remote identity too, including aliases bound by another source selector.
    kind = "issue" if intent.kind == "close_issue" else "milestone"
    number = intent.path.rsplit("/", 1)[-1] or (intent.receipt or {}).get("number")
    native_id = (intent.coordinates or {}).get("id") or (intent.receipt or {}).get("id")
    matched = {
        key
        for key, binding in current.bindings.get("github_hierarchy", {}).items()
        if key.startswith(("epic:", "initiative:"))
        and binding.get("kind") == kind
        and str(binding.get("number")) == str(number)
        and binding.get("database_id") == native_id
    }
    if not matched or keys - matched:
        return Observation(
            "confirmed",
            {
                "intent": intent.to_dict(),
                "canceled_before_write": True,
                "reason": "Parent tracking native identity changed or is unproven",
            },
        )
    keys.update(matched)
    reason = next((value for key in keys if (value := retained_parent_reason(current, key))), "")
    if reason:
        return Observation(
            "confirmed",
            {"intent": intent.to_dict(), "canceled_before_write": True, "reason": reason},
        )
    if action.payload.get("scope_revision") != current.graph_revision:
        return Observation("confirmed", {"intent": intent.to_dict(), "canceled_before_write": True})
    from .campaign_runtime_intake import settled

    waiting = next(
        (reason for key in sorted(keys) if (reason := parent_wait_reason(current, key))), ""
    )
    if waiting or not settled(current):
        return Observation(
            "waiting",
            {"intent": intent.to_dict()},
            waiting or "Parent tracking waits for current intake to settle",
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
