"""Observed campaign timings, independent of admission and completion authority.

Admission intervals use deterministic scheduler reasons. Effect waits and real
worker start/finish receipts measure separate clocks: API retry debt never looks
like occupied coding capacity. These records cannot release a claim or a gate.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from .campaign_model import CampaignSnapshot, OutboxAction, UnitState
from .campaign_scheduler import admit, resource_available
from .campaign_store import add_event


def observe_admission(snapshot: CampaignSnapshot, *, now: float) -> bool:
    """Persist reason changes, rather than one sample per coordinator tick."""
    from .campaign_intake_baseline import admission_waits

    admission = (
        admit(snapshot, now=now, blocked=admission_waits(snapshot))
        if snapshot.phase == "active"
        else None
    )
    waits = dict(admission.waits) if admission else {}
    selected = set(admission.units) if admission else set()
    if admission:
        pending_units = {
            action.unit
            for action in snapshot.outbox
            if action.state in {"prepared", "sent_unknown"}
        }
        for unit in snapshot.units.values():
            if unit.hold or (unit.claim and not unit.claim.quiescent) or unit.id in pending_units:
                continue
            resource = ""
            if unit.state == UnitState.REVIEWING and not unit.evidence.get("review"):
                resource = "review"
            elif (
                unit.state == UnitState.SYNCING
                and unit.evidence.get("integrated")
                and unit.evidence.get("pushed")
            ):
                resource = "verify"
            elif unit.state == UnitState.LOCALLY_COMPLETE or (
                unit.state == UnitState.SYNCING and not unit.evidence.get("integrated")
            ):
                resource = "execution"
            if resource and not resource_available(snapshot, resource):
                waits[unit.id] = f"{resource} capacity"
    changed = False
    for unit in snapshot.units.values():
        reason = waits.get(unit.id, "")
        previous = unit.evidence.get("admission_wait")
        if previous and previous["reason"] != reason:
            elapsed = max(0.0, now - previous["since"])
            timing = unit.evidence.setdefault("timing", {})
            totals = timing.setdefault("admission_wait_s", {})
            totals[previous["reason"]] = totals.get(previous["reason"], 0.0) + elapsed
            add_event(
                snapshot,
                "admission-wait-ended",
                unit=unit.id,
                reason=previous["reason"],
                duration_s=elapsed,
                observed_at=now,
            )
            unit.evidence.pop("admission_wait")
            changed = True
        if reason and not unit.evidence.get("admission_wait"):
            unit.evidence["admission_wait"] = {"reason": reason, "since": now}
            add_event(
                snapshot, "admission-wait-started", unit=unit.id, reason=reason, observed_at=now
            )
            changed = True
        if unit.id in selected and "first_admitted_at" not in unit.evidence.get("timing", {}):
            timing = unit.evidence.setdefault("timing", {})
            timing["first_admitted_at"] = now
            timing["admission_delay_s"] = max(0.0, now - snapshot.created_at)
            add_event(snapshot, "unit-admitted", unit=unit.id, observed_at=now)
            changed = True
    return changed


def observe_effect(
    snapshot: CampaignSnapshot,
    action: OutboxAction,
    *,
    state: str,
    data: dict[str, Any],
    now: float,
) -> None:
    """Account readback waits and independently observed terminal worker receipts."""
    waits = snapshot.bindings.setdefault("effect_waits", {})
    if state in {"waiting", "unknown"}:
        waits.setdefault(action.id, {"since": now, "kind": action.kind, "unit": action.unit})
    elif action.id in waits:
        waiting = waits.pop(action.id)
        elapsed = max(0.0, now - waiting["since"])
        totals = snapshot.bindings.setdefault("effect_wait_s", {})
        totals[action.kind] = totals.get(action.kind, 0.0) + elapsed
        add_event(
            snapshot,
            "effect-wait-ended",
            unit=action.unit,
            action=action.id,
            effect=action.kind,
            duration_s=elapsed,
            observed_at=now,
        )
    if action.kind != "observe-worker" or state != "confirmed":
        return
    if data.get("state") != "finished":
        return
    unit = snapshot.units.get(action.unit)
    receipt = data.get("receipt", {})
    start, finish = receipt.get("started_at"), receipt.get("finished_at")
    if unit is None or not isinstance(start, (int, float)) or not isinstance(finish, (int, float)):
        return  # Historical receipts have no precise worker duration.
    stages = unit.evidence.setdefault("timing", {}).setdefault("stages", [])
    launch = action.payload.get("launch_id", "")
    if any(stage["launch_id"] == launch for stage in stages):
        return
    role = unit.claim.role if unit.claim and unit.claim.launch_id == launch else ""
    stage = {
        "launch_id": launch,
        "role": role,
        "stage": receipt.get("stage", ""),
        "started_at": start,
        "finished_at": finish,
        "duration_s": max(0.0, finish - start),
    }
    stages.append(stage)
    add_event(snapshot, "worker-finished", unit=unit.id, observed_at=now, **stage)


def document(snapshot: CampaignSnapshot, *, now: float) -> dict[str, Any]:
    """Project measured intervals, keeping unavailable historical timings unknown."""
    units = {}
    admission_totals: Counter[str] = Counter()
    for key, unit in snapshot.units.items():
        timing = unit.evidence.get("timing", {})
        totals = dict(timing.get("admission_wait_s", {}))
        current = unit.evidence.get("admission_wait")
        if current:
            reason = current["reason"]
            totals[reason] = totals.get(reason, 0.0) + max(0.0, now - current["since"])
        admission_totals.update(totals)
        units[key] = {
            "admission_delay_s": timing.get("admission_delay_s"),
            "admission_wait_s": totals,
            "current_wait": current,
            "stages": timing.get("stages", []),
        }
    effect_totals = dict(snapshot.bindings.get("effect_wait_s", {}))
    for waiting in snapshot.bindings.get("effect_waits", {}).values():
        kind = waiting["kind"]
        effect_totals[kind] = effect_totals.get(kind, 0.0) + max(0.0, now - waiting["since"])
    used = Counter(
        unit.claim.resource
        for unit in snapshot.units.values()
        if unit.claim and not unit.claim.quiescent
    )
    limits = {
        "execution": snapshot.spec.max_workers,
        "review": snapshot.spec.max_review_sessions,
        "verify": snapshot.spec.resources.verify_slots,
        "resolution": snapshot.spec.resolution.max_sessions,
        "sessions": snapshot.spec.resources.max_sessions,
    }
    return {
        "observed_at": now,
        "admission_wait_s": dict(admission_totals),
        "effect_wait_s": effect_totals,
        "units": units,
        "resources": {"occupied": dict(used), "limits": limits},
    }
