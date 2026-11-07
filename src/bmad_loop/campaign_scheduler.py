"""Pure deterministic campaign admission and role-capacity decisions.

The repository coordinator lock is not a worker semaphore. Independent units
can run within the same epic; only explicit dependencies, ownership scopes and
configured capacities constrain them. Uncertain actors retain their resources.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Mapping

from .campaign_model import (
    CampaignSnapshot,
    CampaignSpec,
    CampaignUnit,
    RoleCandidate,
    UnitState,
    scopes_overlap,
)

_IN_FLIGHT = frozenset(
    {
        UnitState.CLAIMED,
        UnitState.RUNNING,
        UnitState.LOCALLY_COMPLETE,
        UnitState.SYNCING,
        UnitState.VERIFYING,
        UnitState.REVIEWING,
        UnitState.READY_TO_PUBLISH,
        UnitState.MERGE_REQUESTED,
        UnitState.MERGED_PENDING_SYNC,
    }
)


@dataclass(frozen=True)
class Admission:
    units: tuple[str, ...]
    waits: dict[str, str]
    next_epic: str = ""


@dataclass(frozen=True)
class CapacityObservation:
    """An explicit observed route, not a binary-on-PATH inference.

    Unknown route availability blocks admission. Unknown concurrency falls back
    to campaign limits. Usage/budget accounting remains campaign-owned and cannot
    be reset by a provider switch.
    """

    available: bool
    slots: int = 0
    active: int = 0
    reason: str = ""
    model_access: bool = False
    budget_remaining: float | None = None
    known: bool = True
    concurrency_known: bool = True
    observed_at: float | None = None
    expires_at: float | None = None


def route_unavailable(observation: CapacityObservation | None, *, now: float | None = None) -> str:
    """One route gate for startup and admission; missing slot telemetry is not zero."""
    if observation is None or not observation.known:
        return "capacity unknown"
    if now is not None and (
        observation.observed_at is None
        or observation.expires_at is None
        or not observation.observed_at <= now <= observation.expires_at
    ):
        return "capacity observation expired or unversioned"
    if (
        not observation.available
        or not observation.model_access
        or (observation.concurrency_known and observation.active >= observation.slots)
    ):
        return observation.reason or "route/model/quota unavailable"
    if observation.budget_remaining is not None and observation.budget_remaining <= 0:
        return "account budget exhausted"
    return ""


def epic_key(source: str, initiative: str, epic: str) -> str:
    return f"{source}:{initiative}:{epic}"


def delegates_tracking_slot(snapshot: CampaignSnapshot, original: CampaignUnit) -> bool:
    """A merged original hands its active slot to its actual source repair child."""
    return original.state == UnitState.MERGED_PENDING_SYNC and any(
        key in snapshot.units
        and snapshot.units[key].spec.kind == "tracking"
        and snapshot.units[key].spec.parent == original.id
        and snapshot.units[key].state not in (UnitState.DELIVERED, UnitState.CANCELED)
        for key in original.bindings.get("tracking_units", ())
    )


def is_writer(unit: CampaignUnit) -> bool:
    claim = unit.claim
    return bool(claim and not claim.quiescent and (claim.resource == "execution" or claim.writes))


def writer_available(
    snapshot: CampaignSnapshot, unit_id: str, *, provisional: tuple[str, ...] = ()
) -> bool:
    selected = snapshot.units[unit_id]
    for key, unit in snapshot.units.items():
        if key == unit_id:
            continue
        uncertain = unit.state in {UnitState.CLAIMED, UnitState.RUNNING} and unit.claim is None
        if (is_writer(unit) or uncertain or key in provisional) and scopes_overlap(
            selected.spec.scopes, unit.spec.scopes
        ):
            return False
    return True


def admit(snapshot: CampaignSnapshot, *, now: float = 0) -> Admission:
    waits: dict[str, str] = {}
    if snapshot.phase != "active":
        return Admission(
            (),
            {
                key: f"campaign {snapshot.phase}"
                for key, unit in snapshot.units.items()
                if unit.state == UnitState.READY
            },
            snapshot.last_epic,
        )
    spec = snapshot.spec
    active = [unit for unit in snapshot.units.values() if unit.claim and not unit.claim.quiescent]
    # A claimed/running record without a receipt is an uncertain launch, never a
    # free slot merely because its PID has not been observed yet.
    uncertain = [
        unit
        for unit in snapshot.units.values()
        if unit.state in {UnitState.CLAIMED, UnitState.RUNNING} and not unit.claim
    ]
    writers = [unit for unit in active if is_writer(unit)] + uncertain
    execution = len(writers)
    sessions = sum(unit.claim.resource != "verify" for unit in active if unit.claim) + len(
        uncertain
    )
    sessions += sum(
        bool(job.get("claim") and not job["claim"]["quiescent"]) for job in snapshot.jobs.values()
    )
    # Code already merged; the real source-repair child inherits its original's
    # slot. Counting both would deadlock a campaign whose configured limit is 1.
    inflight = [
        unit
        for unit in snapshot.units.values()
        if unit.state in _IN_FLIGHT and not delegates_tracking_slot(snapshot, unit)
    ]
    active_epics = {
        epic_key(unit.spec.source, unit.spec.initiative, unit.spec.epic) for unit in inflight
    }
    epic_workers = Counter(
        epic_key(unit.spec.source, unit.spec.initiative, unit.spec.epic) for unit in writers
    )
    candidates: dict[str, list[str]] = defaultdict(list)
    for key, unit in sorted(snapshot.units.items(), key=lambda item: (item[1].spec.order, item[0])):
        if unit.state != UnitState.READY:
            continue
        if unit.hold:
            waits[key] = f"hold:{unit.hold.kind}"
        elif unit.retry_at is not None and now < unit.retry_at:
            waits[key] = "retry-wait"
        elif unit.claim and not unit.claim.quiescent:
            waits[key] = "unreconciled claim"
        elif any(
            snapshot.units[dependency].state != UnitState.DELIVERED
            for dependency in unit.spec.after
        ):
            waits[key] = "dependency not delivered"
        else:
            candidates[epic_key(unit.spec.source, unit.spec.initiative, unit.spec.epic)].append(key)
    epics = list(candidates)
    if snapshot.last_epic in epics:
        split = epics.index(snapshot.last_epic) + 1
        epics = epics[split:] + epics[:split]
    selected: list[str] = []
    next_epic = snapshot.last_epic
    while epics:
        remaining_epics = []
        for epic in epics:
            if not candidates[epic]:
                continue
            key = candidates[epic].pop(0)
            unit = snapshot.units[key]
            reason = ""
            if execution >= spec.max_workers:
                reason = "execution capacity"
            elif sessions >= spec.resources.max_sessions:
                reason = "session capacity"
            elif len(inflight) + len(selected) >= spec.max_in_flight_units:
                reason = "in-flight capacity"
            elif epic not in active_epics and len(active_epics) >= spec.max_active_epics:
                reason = "active epic capacity"
            elif (
                spec.max_workers_per_epic is not None
                and epic_workers[epic] >= spec.max_workers_per_epic
            ):
                reason = "explicit epic worker capacity"
            elif any(scopes_overlap(unit.spec.scopes, other.spec.scopes) for other in writers):
                reason = "ownership conflict"
            if reason:
                waits[key] = reason
            else:
                selected.append(key)
                writers.append(unit)
                execution += 1
                sessions += 1
                epic_workers[epic] += 1
                active_epics.add(epic)
                next_epic = epic
            if candidates[epic]:
                remaining_epics.append(epic)
        epics = remaining_epics
    return Admission(tuple(selected), waits, next_epic)


def allocation_order(snapshot: CampaignSnapshot) -> tuple[str, ...]:
    """Round-robin epics before reserving workspaces, not only at dispatch."""
    grouped: dict[str, list[str]] = defaultdict(list)
    for key, unit in sorted(snapshot.units.items(), key=lambda item: (item[1].spec.order, item[0])):
        grouped[epic_key(unit.spec.source, unit.spec.initiative, unit.spec.epic)].append(key)
    epics = list(grouped)
    if snapshot.last_epic in epics:
        split = epics.index(snapshot.last_epic) + 1
        epics = epics[split:] + epics[:split]
    result: list[str] = []
    while epics:
        for epic in epics:
            result.append(grouped[epic].pop(0))
        epics = [epic for epic in epics if grouped[epic]]
    return tuple(result)


def active_sessions(snapshot: CampaignSnapshot) -> int:
    """One global count for unit workers, reasoning jobs and uncertain actors."""
    return sum(
        bool(unit.claim and not unit.claim.quiescent and unit.claim.resource != "verify")
        or (unit.state in {UnitState.CLAIMED, UnitState.RUNNING} and not unit.claim)
        for unit in snapshot.units.values()
    ) + sum(
        bool(row.get("claim") and not row["claim"]["quiescent"]) for row in snapshot.jobs.values()
    )


def resource_available(snapshot: CampaignSnapshot, resource: str) -> bool:
    """Separate phase limits; CI/merge/tracking waits use no session slot."""
    claims = [
        unit.claim for unit in snapshot.units.values() if unit.claim and not unit.claim.quiescent
    ]
    if resource in {"execution", "review", "resolution", "planning"}:
        if active_sessions(snapshot) >= snapshot.spec.resources.max_sessions:
            return False
    used = sum(claim.resource == resource for claim in claims)
    limit = {
        "execution": snapshot.spec.max_workers,
        "review": snapshot.spec.max_review_sessions,
        "verify": snapshot.spec.resources.verify_slots,
        "resolution": snapshot.spec.resolution.max_sessions,
        "planning": snapshot.spec.resources.max_sessions,
    }.get(resource)
    if limit is None:
        raise ValueError(f"unknown campaign resource {resource}")
    return used < limit


def select_role(
    spec: CampaignSpec,
    role: str,
    capacities: Mapping[tuple[str, str], CapacityObservation],
    *,
    selections: Mapping[tuple[str, str], int] | None = None,
    spent_usd: float | None = 0,
    allowed_candidates: tuple[RoleCandidate, ...] | None = None,
) -> tuple[RoleCandidate | None, str]:
    """Pick an eligible authorized model fairly, with configured stable ties."""
    role_spec = spec.role(role)
    if role_spec.budget_usd is not None and (
        spent_usd is None or spent_usd >= role_spec.budget_usd
    ):
        return None, "role budget unknown or exhausted"
    options = tuple(
        candidate
        for candidate in spec.candidates(role)
        if allowed_candidates is None or candidate in allowed_candidates
    )
    eligible = []
    reasons = []
    for index, candidate in enumerate(options):
        key = (candidate.profile, candidate.model)
        reason = route_unavailable(capacities.get(key))
        if reason:
            reasons.append(f"{candidate.profile}/{candidate.model}: {reason}")
        else:
            eligible.append(((selections or {}).get(key, 0), index, candidate))
    if not eligible:
        return None, "; ".join(reasons)
    return min(eligible, key=lambda item: item[:2])[2], ""
