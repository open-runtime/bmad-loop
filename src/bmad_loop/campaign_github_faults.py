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
    duration = fault.get("retry_after_s") or 0
    return now + max(backoff, remaining, duration)


def mutation_observation(intent, *, now):
    state = intent.state if intent.state in {"confirmed", "rejected"} else "unknown"
    data = {"intent": intent.to_dict()}
    deadline = None
    if intent.fault and state == "unknown":
        data["github_fault"] = intent.fault
        deadline = retry_at(intent.fault, now=now)
    return Observation(state, data, intent.error, deadline)
