"""Project GitHub fault evidence onto durable, clock-bound effect waits.

Deadlines from GitHub are UTC epoch values. The supervisor may use a different
clock in a scripted test; translate the remaining duration once at observation.
No projection of a fault authorizes resubmitting an uncertain mutation.
"""

import time

from .campaign_supervisor import Observation


def retry_at(fault, *, now, attempts=1, wall_clock=time.time):
    category = fault.get("category", "transient")
    floor = 300 if category in {"authentication", "permission", "refused"} else 60
    backoff = min(900, floor * 2 ** min(max(0, attempts - 1), 4))
    deadline = fault.get("retry_at")
    remaining = max(0, deadline - wall_clock()) if deadline is not None else 0
    duration = (fault.get("retry_after_s") or 0) if deadline is None else 0
    return now + max(backoff, remaining, duration)


def mutation_observation(intent, *, now):
    state = intent.state if intent.state in {"confirmed", "rejected"} else "unknown"
    data = {"intent": intent.to_dict()}
    deadline = None
    if intent.fault and state == "unknown":
        data["github_fault"] = intent.fault
        deadline = retry_at(intent.fault, now=now)
    return Observation(state, data, intent.error, deadline)


def _actions_with_held_faults(snapshot):
    from .campaign_action_history import action as recorded_action

    yield from snapshot.outbox
    hot_ids = {action.id for action in snapshot.outbox}
    for unit in getattr(snapshot, "units", {}).values():
        hold = unit.hold
        if hold and hold.kind == "effect_refused" and hold.id and hold.id not in hot_ids:
            recorded = recorded_action(snapshot, hold.id)
            if recorded and recorded.unit == unit.id and recorded.state == "rejected":
                yield recorded


def status_waits(snapshot, *, limit=200):
    """Expose bounded actionable faults without publishing mutation payloads."""
    from .sanitize import scrub_text

    rows = []
    total = 0
    for action in _actions_with_held_faults(snapshot):
        if action.state == "confirmed" or not (action.error or action.next_retry_at):
            continue
        total += 1
        if len(rows) >= limit:
            continue
        fault = action.receipt.get("github_fault") or (action.receipt.get("intent") or {}).get(
            "fault"
        )
        item = {
            "id": action.id,
            "unit": action.unit,
            "kind": action.kind,
            "state": action.state,
            "reason": (
                "Checkpoint observation unavailable; inspect the private campaign record."
                if action.kind in {"checkpoint", "checkpoint-progress"} and action.error
                else scrub_text(action.error)
            ),
            "next_retry_at": action.next_retry_at or None,
            "next_action": (
                "operator reconciliation required"
                if action.state == "rejected"
                else "reconcile existing effect after retry deadline"
            ),
        }
        if fault:
            item["github_fault"] = {
                key: fault.get(key)
                for key in (
                    "schema_version",
                    "category",
                    "certainty",
                    "status",
                    "retry_at",
                    "request_id",
                )
            }
        rows.append(item)
    return {"total": total, "items": rows, "truncated": total > len(rows)}
