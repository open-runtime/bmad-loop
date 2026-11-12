"""Fenced cross-harness continuation of an existing campaign unit.

This collaborator never changes acceptance or transfers a native session ID.
Old writers drain before immutable capture; bounded no-tools jobs reconstruct
context before the existing scheduler may acquire a replacement writer claim.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
from pathlib import Path

from . import verify
from .campaign_handoff_obligations import AUDIT_SCHEMA
from .campaign_headless import capability_admission, capability_for, read_json, validate_capability
from .campaign_jobs import JobLimits, JobRequest, prepare, request_from_dict
from .campaign_model import CampaignError, Hold, RoleCandidate, UnitState
from .campaign_store import add_event, transition
from .campaign_supervisor import Observation, Proposal
from .campaign_transcripts import (
    HistorySnapshot,
    blob,
    digest,
    encoded,
    load_events,
    merge_lineage_events,
    snapshot_history,
)

PREFIX = "handoff-"
ACTIVE = {
    "drain",
    "capture",
    "summarize",
    "audit",
    "assemble",
    "ready",
    "constraint_reduce",
    "constraint_audit",
    "recovery_extract",
    "recovery_audit",
    "recovery_wait",
}
EXPECTED_PHASE = {
    "handoff-capture": "drain",
    "handoff-plan": "capture",
    "handoff-replan": "summarize",
    "handoff-audit-plan": "summarize",
    "handoff-audit-replan": "audit",
    "handoff-final-repair": "audit",
    "handoff-assemble": "audit",
    "handoff-release": "assemble",
    "handoff-constraint-audit": "constraint_reduce",
    "handoff-recovery-plan": "capture",
    "handoff-recovery-audit": "recovery_extract",
    "handoff-recovery-finish": "recovery_audit",
    "handoff-recovery-observe": "recovery_wait",
}


class HandoffRouteWait(CampaignError):
    """No currently available qualified route; retain progress and retry admission."""


class ConstraintReductionNeeded(CampaignError):
    """Audited constraints need another bounded reduction before writer admission."""


def _candidate(raw):
    return RoleCandidate(**{**raw, "capacity_command": tuple(raw.get("capacity_command", ()))})


def _cap(snapshot, candidate, mode="workspace-tools"):
    cap = capability_for(
        snapshot.config_snapshot.get("reasoning_capabilities", {}),
        candidate.profile,
        candidate.model,
        mode,
        effort=candidate.effort,
    )
    validate_capability(cap, candidate, permissions=cap["permissions"])
    return cap


def _permission(value):
    """Recover the original stage boundary, including older saved handoffs."""
    from .campaign_route_readiness import permissions_for

    payload = value.get("original_payload", {})
    read_only = payload.get("read_only")
    if value.get("predecessor", {}).get("writes") is False:
        if read_only is False:
            raise CampaignError("Handoff cannot widen a read-only predecessor claim")
        read_only = True
    expected = permissions_for(
        value.get("route_role", value["role"]),
        stage=payload.get("stage", ""),
        read_only=read_only,
    )
    if value.get("permissions", expected) != expected:
        raise CampaignError("Handoff permissions differ from its frozen original stage")
    return expected


def _unit_admission(unit):
    """Use current writer provenance, including mixed-family predecessor sessions."""
    return {
        "developer_family": unit.evidence.get("developer_family", ""),
        "developer_families": tuple(unit.evidence.get("developer_families", ())),
    }


def _target_cap(snapshot, candidate, role, *, unit, permissions=None):
    """One frozen permission and author policy for selection through adoption."""
    from .campaign_headless import provider_family

    if candidate not in snapshot.spec.candidates(role):
        raise CampaignError("Handoff target is not in the frozen approved role pool")
    authors = _unit_admission(unit)
    family = candidate.provider_family or provider_family(candidate.model)
    if (
        role == "publication_review"
        and snapshot.spec.github.require_independent_review
        and (authors["developer_family"] or authors["developer_families"])
        and family in {authors["developer_family"], *authors["developer_families"]}
    ):
        raise CampaignError(
            "Publication review handoff requires a different provider family; "
            f"{candidate.profile}:{candidate.model} belongs to author family {family!r}"
        )
    from .campaign_route_readiness import permissions_for

    mode = permissions or permissions_for(role)
    if mode != "native-review-pair":
        return _cap(snapshot, candidate, mode)
    from .campaign_codex_review_pair import selected

    cap = _cap(snapshot, candidate, "review")
    if cap["permissions"] != "read-only-tools" or not selected(cap):
        raise CampaignError("Native review pair handoff requires a qualified pair route")
    return cap


def _fallback(snapshot, role, previous, requested=None, *, unit, permissions=None):
    candidates = snapshot.spec.candidates(role)
    if requested is not None:
        selectors = {
            key: requested[key] for key in ("profile", "model", "effort") if requested.get(key)
        }
        matching = [
            candidate
            for candidate in candidates
            if all(getattr(candidate, key) == value for key, value in selectors.items())
        ]
        if not selectors or not matching:
            raise CampaignError("Handoff target is not in the frozen approved role pool")
        if len(matching) != 1:
            raise CampaignError(
                "Handoff target is ambiguous; specify the exact approved profile/model/effort"
            )
        candidate = matching[0]
        try:
            _target_cap(snapshot, candidate, role, unit=unit, permissions=permissions)
        except CampaignError as exc:
            raise CampaignError(f"Requested handoff target is inadmissible: {exc}") from exc
        return candidate
    for candidate in candidates:
        if (candidate.profile, candidate.model) == previous:
            continue
        try:
            _target_cap(snapshot, candidate, role, unit=unit, permissions=permissions)
        except CampaignError:
            continue
        return candidate
    return None


def _available_alternative(runtime, snapshot, value):
    """Use fresh shared admission observations without changing an explicit choice."""
    if (
        value.get("requested_candidate")
        or not value.get("candidate")
        or not hasattr(runtime, "_role")
    ):
        return None
    role = value.get("route_role", value["role"])
    permissions = _permission(value)
    unit = snapshot.units[value["predecessor"]["unit"]]
    selected = _candidate(value["candidate"])
    try:
        _target_cap(snapshot, selected, role, unit=unit, permissions=permissions)
    except CampaignError:
        pass
    else:
        if (
            runtime._role(
                snapshot,
                role,
                permissions=permissions,
                allowed_candidates=(selected,),
                **_unit_admission(unit),
            )
            is not None
        ):
            return None
    allowed = []
    for candidate in snapshot.spec.candidates(role):
        if candidate == selected or (candidate.profile, candidate.model) == tuple(
            value["previous_route"]
        ):
            continue
        try:
            _target_cap(snapshot, candidate, role, unit=unit, permissions=permissions)
        except CampaignError:
            continue
        allowed.append(candidate)
    return (
        runtime._role(
            snapshot,
            role,
            permissions=permissions,
            allowed_candidates=tuple(allowed),
            **_unit_admission(unit),
        )
        if allowed
        else None
    )


def control(runtime, snapshot, command) -> None:
    unit_id = command.get("unit") or command.get("payload", {}).get("unit")
    if unit_id not in snapshot.units:
        raise CampaignError("Handoff requires an existing unit")
    unit = snapshot.units[unit_id]
    payload = command.get("payload", {})
    requested = payload.get("candidate")
    if requested is None and any(payload.get(key) for key in ("profile", "model", "effort")):
        requested = {
            key: payload[key] for key in ("profile", "model", "effort") if payload.get(key)
        }
    begin(
        runtime,
        snapshot,
        unit,
        requested=requested,
        reason=command.get("reason") or "operator requested continuation",
    )


def begin(runtime, snapshot, unit, *, requested=None, reason, receipt=None):
    old = unit.evidence.get("handoff")
    if old and old.get("phase") in ACTIVE:
        return
    claim = unit.claim
    if (
        not claim
        or claim.resource == "verify"
        or unit.state
        in {
            UnitState.MERGE_REQUESTED,
            UnitState.MERGED_PENDING_SYNC,
            UnitState.DELIVERED,
            UnitState.CANCELED,
        }
    ):
        raise CampaignError("Handoff requires an owned coding stage before publication")
    if unit.hold and unit.hold.kind not in {"capacity", "quota", "handoff"}:
        raise CampaignError("Handoff cannot clear a preexisting owner or approval hold")
    launch = next((row for row in snapshot.outbox if row.id == claim.launch_id), None)
    if launch is None:
        raise CampaignError("Handoff predecessor has no durable launch intent")
    role = claim.role
    native_role = ((receipt or {}).get("interruption") or {}).get("role", "")
    if not native_role and role == "execution":
        sessions = (receipt or {}).get("sessions", [])
        native_role = sessions[-1].get("role", "dev") if sessions else "dev"
    route_role = "execution_review" if role == "execution" and native_role == "review" else role
    original_payload = dict(launch.payload)
    # The prepared child resolves dynamic planning stages. Its authority lives
    # outside the product checkout; do not infer that final permission from a
    # generic planning launch or from the failed model's text.
    request_path = (
        runtime.store.directory(snapshot.id) / "workers" / claim.launch_id / "request.json"
    )
    if request_path.exists():
        frozen = read_json(request_path)
        expected = {
            "campaign_id": snapshot.id,
            "unit_id": unit.id,
            "launch_id": claim.launch_id,
            "generation": claim.generation,
            "attempt": claim.attempt,
        }
        if any(frozen.get(key) != value for key, value in expected.items()):
            raise CampaignError("Handoff predecessor request belongs to another launch")
        if frozen.get("kind") != original_payload.get("kind"):
            raise CampaignError("Handoff predecessor request changed its execution kind")
        for key in ("stage", "read_only"):
            if key in frozen:
                original_payload[key] = frozen[key]
    permission_value = {
        "role": role,
        "route_role": route_role,
        "original_payload": original_payload,
        "predecessor": dataclasses.asdict(claim),
    }
    permissions = _permission(permission_value)
    selected = (
        launch.payload.get("review_candidate" if route_role == "execution_review" else "candidate")
        or unit.bindings.get("execution_candidate")
        or {}
    )
    previous = (selected.get("profile", ""), selected.get("model", ""))
    if route_role == "execution_review" and route_role not in dict(snapshot.spec.roles):
        target = None
    else:
        target = _fallback(
            snapshot, route_role, previous, requested, unit=unit, permissions=permissions
        )
    identifier = (
        "handoff-" + digest([snapshot.id, unit.id, claim.launch_id, snapshot.generation])[:24]
    )
    value = {
        "schema_version": 1,
        "id": identifier,
        "phase": "drain",
        "role": role,
        "route_role": route_role,
        "native_role": native_role,
        "predecessor": dataclasses.asdict(claim),
        "original_payload": original_payload,
        "permissions": permissions,
        "candidate": dataclasses.asdict(target) if target else None,
        "previous_route": list(previous),
        "requested_candidate": requested,
        "reason": reason,
        "graph_revision": snapshot.graph_revision,
        "config_revision": snapshot.config_revision,
        "created_at": runtime.clock(),
        "jobs": [],
        "audits": [],
        "summaries": [],
        "receipt": receipt or {},
        "failure_ledger": (
            dict(old.get("failure_ledger", {}))
            if old and old.get("predecessor", {}).get("launch_id") == claim.launch_id
            else {}
        ),
    }
    if receipt and role == "execution":
        from .campaign_attribution import record

        record(snapshot, unit, receipt)
    if old:
        unit.evidence.setdefault("handoff_history", []).append(old)
    unit.evidence["handoff"] = value
    unit.hold = Hold(
        "handoff",
        reason if target else "No qualified fallback route is currently available",
        id=identifier,
        evidence={"phase": "drain", "role": role},
    )
    add_event(
        snapshot,
        "handoff-requested",
        unit=unit.id,
        predecessor=claim.launch_id,
        role=role,
        candidate=value["candidate"],
        reason=reason,
    )


def intercept_terminal(runtime, snapshot, unit, receipt) -> bool:
    """Called only after normal worker observation proves coding descendants stopped."""
    if (
        (receipt.get("interruption") or {}).get("kind") == "quota"
        and unit.claim
        and unit.claim.quiescent
    ):
        from .campaign_capacity import record_unavailable

        launch = next((row for row in snapshot.outbox if row.id == unit.claim.launch_id), None)
        native_role = receipt["interruption"].get("role")
        key = (
            "review_candidate"
            if unit.claim.role == "execution" and native_role == "review"
            else "candidate"
        )
        candidate = launch.payload.get(key, {}) if launch else {}
        if not candidate and receipt.get("sessions"):
            native = receipt["sessions"][-1]
            candidate = {"profile": native.get("adapter"), "model": native.get("model")}
        if candidate.get("profile") and candidate.get("model"):
            record_unavailable(
                snapshot,
                candidate["profile"],
                candidate["model"],
                now=runtime.clock(),
                reason="native quota interruption",
            )
    value = unit.evidence.get("handoff")
    if value and unit.claim and unit.claim.launch_id != value["predecessor"]["launch_id"]:
        value["phase"] = "continuing"
    if value and value.get("phase") in ACTIVE:
        if not unit.claim or not unit.claim.quiescent:
            raise CampaignError("Handoff cannot consume a live predecessor receipt")
        value["receipt"] = receipt
        return True
    kind = (receipt.get("interruption") or {}).get("kind")
    if kind not in {"quota", "transport"}:
        return False
    if not snapshot.spec.reasoning.enabled:
        return False
    if not unit.claim or not unit.claim.quiescent:
        raise CampaignError("Quota handoff requires independent predecessor quiescence")
    begin(runtime, snapshot, unit, reason="native " + kind + " interruption", receipt=receipt)
    return True


def _job(runtime, snapshot, key):
    """Read archived receipts without restoring immutable jobs to hot state."""
    if key in snapshot.jobs:
        return snapshot.jobs[key]
    from collections import OrderedDict

    cache = getattr(runtime, "_handoff_job_cache", None)
    if cache is None:
        cache = OrderedDict()
        runtime._handoff_job_cache = cache
    identity = (snapshot.id, key)
    if identity in cache:
        cache.move_to_end(identity)
        return cache[identity]
    rows = runtime.store.jobs(snapshot.id, job_id=key, limit=1)
    if not rows:
        raise CampaignError("Handoff reasoning job is unavailable: " + key)
    row = rows[0]
    if row["state"] in {"applied", "canceled", "stale"}:
        cache[identity] = row
        while len(cache) > 256:
            cache.popitem(last=False)
    return row


def _pending(snapshot, unit, kind=None):
    return any(
        row.unit == unit.id
        and (kind is None or row.kind == kind)
        and row.state in {"prepared", "sent_unknown"}
        for row in snapshot.outbox
    )


def _debt(snapshot, unit):
    return bool(
        unit.evidence.get("stage_sync_debt")
        or any(
            row.unit == unit.id
            and row.state in {"prepared", "sent_unknown"}
            and row.kind
            not in {"observe-worker", "reflection", "communication", "terminate-worker"}
            and not row.kind.startswith(PREFIX)
            for row in snapshot.outbox
        )
    )


def _requires_replan(row):
    return row["state"] in {"capacity_wait", "stale", "canceled"} or (
        row["state"] == "failed"
        and (row.get("receipt") or {}).get("outcome") in {"quota", "transport", "limit"}
    )


def _obsolete_summaries(runtime, snapshot, value):
    from .campaign_summary import (
        EXTRACT_PROMPT_VERSION,
        FINAL_PROMPT_VERSION,
        REDUCE_PROMPT_VERSION,
        summary_contract_current,
    )

    if value.get("phase") in {"ready", "assemble"} and not value.get("packet", {}).get(
        "mandatory_context_sha256"
    ):
        return True  # A quiescent legacy packet needs fresh authority readback binding.
    for key in value.get("jobs", ()):
        row = snapshot.jobs.get(key)
        if row is None and runtime is not None:
            row = _job(runtime, snapshot, key)
        if row is None:
            # Applied jobs can leave the hot snapshot. This controller-written
            # projection was captured from their immutable requests, never from
            # a new route's proof. Older snapshots without it refresh safely.
            if value.get("summary_contracts", {}).get(key) not in {
                EXTRACT_PROMPT_VERSION,
                REDUCE_PROMPT_VERSION,
                FINAL_PROMPT_VERSION,
            }:
                return True
        elif not summary_contract_current(row["request"]):
            return True
    return False


def _failure_keys(raw):
    coordinates = raw["coordinates"]
    if raw["purpose"] == "handoff_reduce" and coordinates.get("reduction_failure_keys"):
        return list(
            dict.fromkeys(
                key for keys in coordinates["reduction_failure_keys"].values() for key in keys
            )
        )
    summary = coordinates.get("summary_spec") or {}
    identities = summary.get("primary_ids") or [source["sha256"] for source in raw["inputs"]]
    return [digest([raw["purpose"], coordinates.get("contract", ""), key]) for key in identities]


def _remember_failures(runtime, snapshot, value):
    ledger = value.setdefault("failure_ledger", {})
    for field in (
        "jobs",
        "audits",
        "constraint_jobs",
        "constraint_audits",
        "recovery_jobs",
        "recovery_audits",
    ):
        for identifier in value.get(field, []):
            row = _job(runtime, snapshot, identifier)
            failures = row.get("failure_attempts", 0)
            if failures:
                for key in _failure_keys(row["request"]):
                    ledger[key] = max(ledger.get(key, 0), failures)


def _prepare_with_failures(snapshot, request, value):
    family = request.coordinates.get("audit_family")
    if family:
        if value.get("audit_family", family) != family:
            raise CampaignError("Summary job changed the reserved independent audit family")
        value["audit_family"] = family
    row = prepare(snapshot, request)
    failures = max(
        (value.get("failure_ledger", {}).get(key, 0) for key in _failure_keys(row["request"])),
        default=0,
    )
    if failures:
        row["failure_attempts"] = max(row.get("failure_attempts", 0), failures)
        if row["failure_attempts"] >= snapshot.spec.reasoning.max_attempts:
            row.update(
                state="failed", reason="Continuation input exhausted its lifetime attempt budget"
            )
    return row


def _retry_context(runtime, value, rows):
    """Charge one cooldown to each exact attempt, including a deferred replan.

    A route with no alternate must become eligible after its first cooldown.
    Recording only successful plans would restart that cooldown on every poll.
    """
    value = dict(value)
    value["plan_epoch"] = value.get("plan_epoch", 0) + 1
    events = dict(value.get("route_cooldown_events", {}))
    cooldowns = dict(value.get("route_cooldowns", {}))
    for row in rows:
        selected = row.get("selection")
        if not selected or not _requires_replan(row):
            continue
        claim = row.get("claim") or {}
        identity = digest(
            [
                row["id"],
                row.get("attempt", 0),
                row.get("failure_attempts", 0),
                claim.get("launch_id"),
                claim.get("generation"),
                selected,
            ]
        )
        route = selected["profile"] + ":" + selected["model"]
        if identity not in events:
            # Receipt acceptance timestamps the attempt in controller time.
            # Older snapshots without that field anchor at their first replan.
            started = row.get("updated_at", runtime.clock())
            events[identity] = {"route": route, "until": started + 60}
        cooldowns[route] = max(cooldowns.get(route, 0), events[identity]["until"])
    value["route_cooldowns"] = cooldowns
    value["route_cooldown_events"] = events
    return value


@capability_admission
def proposals(runtime, snapshot):
    result = []
    for unit in snapshot.units.values():
        value = unit.evidence.get("handoff") or {}
        phase = value.get("phase")
        if phase not in ACTIVE or snapshot.phase != "active":
            continue
        claim = unit.claim
        if claim and not claim.quiescent:
            # The old role slot must be released before replacement admission.
            # A foreign hold still owns controls for this unit.
            if not unit.hold or unit.hold.id != value["id"]:
                continue
            if claim.launch_id != value["predecessor"]["launch_id"]:
                raise CampaignError("Another writer replaced the fenced predecessor")
            if (
                not _pending(snapshot, unit, "terminate-worker")
                and unit.bindings.get("stop_requested_for") != claim.launch_id
            ):
                result.append(Proposal("terminate-worker", unit.id, {"launch_id": claim.launch_id}))
            continue
        if phase not in {"drain", "capture"} and _obsolete_summaries(runtime, snapshot, value):
            # A version transition retains all native history/attempt debt but
            # cannot release an already assembled packet as fresh evidence.
            live_jobs = any(
                row.get("claim") and not row["claim"].get("quiescent")
                for row in snapshot.jobs.values()
                if row["request"].get("coordinates", {}).get("handoff_id") == value["id"]
            )
            if (
                not live_jobs
                and (not unit.hold or unit.hold.id == value["id"])
                and not _pending(snapshot, unit, "handoff-refresh")
            ):
                result.append(Proposal("handoff-refresh", unit.id, {"handoff_id": value["id"]}))
            continue
        alternate = _available_alternative(runtime, snapshot, value)
        if alternate is not None and (not unit.hold or unit.hold.id == value["id"]):
            if not _pending(snapshot, unit, "handoff-route"):
                result.append(
                    Proposal(
                        "handoff-route",
                        unit.id,
                        {"handoff_id": value["id"], "candidate": dataclasses.asdict(alternate)},
                    )
                )
            continue
        if value.get("candidate"):
            try:
                _target_cap(
                    snapshot,
                    _candidate(value["candidate"]),
                    value.get("route_role", value["role"]),
                    unit=unit,
                    permissions=_permission(value),
                )
            except CampaignError:
                needs_route = True
            else:
                needs_route = bool(value.get("route_wait"))
            if needs_route:
                if (
                    (not unit.hold or unit.hold.id == value["id"])
                    and value.get("retry_at", 0) <= runtime.clock()
                    and not _pending(snapshot, unit, "handoff-route")
                ):
                    result.append(
                        Proposal(
                            "handoff-route",
                            unit.id,
                            {
                                "handoff_id": value["id"],
                                "candidate": value["candidate"],
                            },
                        )
                    )
                continue
        if phase == "ready":
            if unit.hold:
                continue
            if value["capture"].get("audit_context") != _audit_context(snapshot, unit) or value.get(
                "packet", {}
            ).get("read_only") != (_permission(value) != "workspace-tools"):
                if not _pending(snapshot, unit, "handoff-refresh"):
                    result.append(Proposal("handoff-refresh", unit.id, {"handoff_id": value["id"]}))
            continue
        if not unit.hold or unit.hold.id != value["id"]:
            continue  # an operator hold remains authoritative
        if not value.get("candidate"):
            target = _fallback(
                snapshot,
                value.get("route_role", value["role"]),
                tuple(value["previous_route"]),
                unit=unit,
                permissions=_permission(value),
            )
            if target and not _pending(snapshot, unit, "handoff-route"):
                result.append(
                    Proposal("handoff-route", unit.id, {"candidate": dataclasses.asdict(target)})
                )
            continue
        if _debt(snapshot, unit):
            continue
        if value.get("retry_at", 0) > runtime.clock():
            continue
        live_jobs = any(
            row.get("claim") and not row["claim"].get("quiescent")
            for row in snapshot.jobs.values()
            if row["request"].get("coordinates", {}).get("handoff_id") == value["id"]
        )
        if value.get("capture") and value["capture"].get("audit_context") != _audit_context(
            snapshot, unit
        ):
            if not live_jobs and not _pending(snapshot, unit, "handoff-refresh"):
                result.append(Proposal("handoff-refresh", unit.id, {"handoff_id": value["id"]}))
            continue
        if phase == "audit" and value.get("final_repair_pending"):
            # Observe issued siblings to quiescence; never ask the same audit
            # again whether its unchanged rejected final should now pass.
            if not live_jobs and not _pending(snapshot, unit, "handoff-final-repair"):
                result.append(
                    Proposal("handoff-final-repair", unit.id, {"handoff_id": value["id"]})
                )
            continue
        if not live_jobs:
            exhausted = [
                key
                for field in (
                    "jobs",
                    "audits",
                    "constraint_jobs",
                    "constraint_audits",
                    "recovery_jobs",
                    "recovery_audits",
                )
                for key in value.get(field, [])
                if (
                    _job(runtime, snapshot, key).get("state") == "failed"
                    and _job(runtime, snapshot, key).get("failure_attempts", 0)
                    >= snapshot.spec.reasoning.max_attempts
                )
            ]
            if exhausted:
                if not _pending(snapshot, unit, "handoff-failed"):
                    result.append(
                        Proposal(
                            "handoff-failed",
                            unit.id,
                            {"handoff_id": value["id"], "jobs": exhausted},
                        )
                    )
                continue
        if (
            phase in {"summarize", "audit"}
            and not live_jobs
            and any(
                _requires_replan(_job(runtime, snapshot, key))
                for key in value["jobs" if phase == "summarize" else "audits"]
            )
        ):
            retry_kind = "handoff-replan" if phase == "summarize" else "handoff-audit-replan"
            if not _pending(snapshot, unit, retry_kind):
                result.append(Proposal(retry_kind, unit.id, {"handoff_id": value["id"]}))
            continue
        phase_jobs = {
            "summarize": "jobs",
            "audit": "audits",
            "constraint_reduce": "constraint_jobs",
            "constraint_audit": "constraint_audits",
            "recovery_extract": "recovery_jobs",
            "recovery_audit": "recovery_audits",
        }
        field = phase_jobs.get(phase)
        job_ids = value.get(field, []) if field else []
        if (
            phase in {"constraint_reduce", "constraint_audit", "recovery_extract", "recovery_audit"}
            and not live_jobs
            and any(_requires_replan(_job(runtime, snapshot, key)) for key in job_ids)
        ):
            if not _pending(snapshot, unit, "handoff-contract-replan"):
                result.append(
                    Proposal("handoff-contract-replan", unit.id, {"handoff_id": value["id"]})
                )
            continue
        if phase == "recovery_wait":
            if not live_jobs and not _pending(snapshot, unit, "handoff-recovery-observe"):
                result.append(
                    Proposal("handoff-recovery-observe", unit.id, {"handoff_id": value["id"]})
                )
            continue
        action = {
            "drain": "handoff-capture",
            "capture": "handoff-plan",
            "summarize": "handoff-audit-plan",
            "audit": "handoff-assemble",
            "constraint_reduce": "handoff-constraint-audit",
            "constraint_audit": "handoff-assemble",
            "recovery_extract": "handoff-recovery-audit",
            "recovery_audit": "handoff-recovery-finish",
            "assemble": "handoff-release",
        }[phase]
        if phase == "capture" and value["capture"]["gaps"] and not value.get("recovery"):
            action = "handoff-recovery-plan"
        if any(_job(runtime, snapshot, key).get("state") != "applied" for key in job_ids):
            continue
        if not _pending(snapshot, unit, action):
            result.append(Proposal(action, unit.id, {"handoff_id": value["id"]}))
    return tuple(result)


def _root(runtime, snapshot, value):
    return runtime.store.directory(snapshot.id) / "handoffs" / value["id"]


def _capture(runtime, snapshot, unit, value):
    from .campaign_acceptance import criteria
    from .campaign_fingerprint import capture
    from .campaign_workers import git

    workspace = Path(unit.bindings["workspace"])
    root = _root(runtime, snapshot, value)
    from . import bmadconfig, runs
    from .journal import load_state

    original_run_dir = value.get("receipt", {}).get("run_dir")
    if not original_run_dir and value["predecessor"].get("run_id"):
        candidate_dir = runs.run_dir_for(workspace, value["predecessor"]["run_id"])
        if candidate_dir.exists():
            original_run_dir = str(candidate_dir)
    original_run = load_state(Path(original_run_dir)) if original_run_dir else None
    native_role = value.get("native_role", "")
    if original_run is not None and value["role"] == "execution":
        if any(
            task.phase.value in {"review-running", "review-verify"}
            for task in original_run.tasks.values()
        ):
            native_role = "review"
        else:
            sessions = [
                session for task in original_run.tasks.values() for session in task.sessions
            ]
            if sessions and sessions[-1].status != "completed":
                native_role = sessions[-1].role
    source_roots = tuple(Path(path) for path in unit.bindings.get("private_paths", []))
    if original_run is not None:
        from .campaign_finalization import _source_roots

        source_roots = tuple(
            dict.fromkeys(
                [
                    *source_roots,
                    *_source_roots(
                        workspace,
                        bmadconfig.load_paths(
                            workspace, want_tickets=original_run.source == "tickets"
                        ),
                        original_run,
                    ),
                ]
            )
        )
    fingerprint = capture(workspace, source_roots=source_roots).to_dict()
    sources, gaps, seen = [], [], set()
    history = [*unit.evidence.get("worker_history", []), value.get("receipt", {})]
    for receipt in history:
        rows = list(receipt.get("sessions", []))
        if receipt.get("session_id"):
            rows.append(receipt)
        for row in rows:
            binding = row.get("native_binding") or {}
            if binding.get("headless_request"):
                from .campaign_managed_history import snapshot_managed_history

                managed_key = (binding["headless_request"], "managed-headless")
                if managed_key in seen:
                    continue
                seen.add(managed_key)
                try:
                    sources.append(
                        dataclasses.asdict(
                            snapshot_managed_history(
                                binding["headless_request"],
                                binding,
                                root / "managed-history",
                                session_id=row.get("session_id") or binding.get("session_id") or "",
                                max_bytes=snapshot.spec.reasoning.history_max_bytes,
                            )
                        )
                    )
                except (CampaignError, OSError, ValueError) as exc:
                    gaps.append(f"Managed headless history unavailable: {exc}")
                continue
            path, identity = (
                row.get("transcript_path"),
                row.get("session_id") or binding.get("session_id"),
            )
            harness = row.get("adapter") or binding.get("profile")
            if not harness:
                harness = value["original_payload"].get("candidate", {}).get("profile")
            key = (path, identity)
            if key in seen:
                continue
            seen.add(key)
            if (
                not path
                or not identity
                or harness not in {"codex", "claude", "grok", "antigravity", "muse"}
            ):
                gaps.append(
                    "Native session lacks an exact supported transcript binding: "
                    + str(identity or "unknown")
                )
                continue
            try:
                sources.append(
                    dataclasses.asdict(
                        snapshot_history(
                            Path(path),
                            harness,
                            identity,
                            root / "history",
                            max_bytes=snapshot.spec.reasoning.history_max_bytes,
                        )
                    )
                )
            except (CampaignError, OSError, ValueError) as exc:
                gaps.append(f"Native history unavailable for {identity}: {exc}")
    artifacts = {}
    for label, args in {
        "tracked_patch": ("diff", "--binary", "HEAD"),
        "index_patch": ("diff", "--cached", "--binary", "HEAD"),
        "status": ("status", "--porcelain=v1", "-z"),
        "untracked_names": ("ls-files", "--others", "--exclude-standard", "-z"),
    }.items():
        observed = verify.git_bytes(workspace, *args)
        if observed.returncode:
            raise CampaignError("Handoff could not preserve " + label)
        artifacts[label] = str(blob(root / "working-state", observed.stdout))
    untracked = []
    for name in Path(artifacts["untracked_names"]).read_bytes().split(b"\0"):
        if not name:
            continue
        path = workspace / name.decode("utf-8")
        if (
            path.is_symlink()
            or not path.resolve().is_relative_to(workspace.resolve())
            or not path.is_file()
        ):
            raise CampaignError("Handoff untracked file is redirected or unsupported")
        data = path.read_bytes()
        untracked.append(
            {
                "path": str(path.relative_to(workspace)),
                "blob": str(blob(root / "working-state", data)),
                "sha256": hashlib.sha256(data).hexdigest(),
            }
        )
    obligations = (unit.evidence.get("acceptance") or {}).get("criteria") or criteria(
        unit.id, unit.spec.acceptance
    )
    original_state = original_run.to_dict() if original_run is not None else None
    predecessor_data = encoded(value.get("receipt", {}))
    predecessor_path = blob(root / "predecessor", predecessor_data)
    state_data = encoded(original_state)
    state_path = blob(root / "predecessor", state_data)
    current = {
        "original_run_dir": original_run_dir,
        "original_run_id": original_run.run_id if original_run else None,
        "source_roots": [str(path.relative_to(workspace)) for path in source_roots],
        "original_state_path": str(state_path),
        "original_state_sha256": hashlib.sha256(state_data).hexdigest(),
        "predecessor_receipt_path": str(predecessor_path),
        "predecessor_receipt_sha256": hashlib.sha256(predecessor_data).hexdigest(),
        "unit": dataclasses.asdict(unit.spec),
        "workspace": str(workspace),
        "branch": git(workspace, "symbolic-ref", "--short", "HEAD"),
        "head": fingerprint["head"],
        "pr": unit.bindings.get("pr"),
        "fingerprint": fingerprint,
        "artifacts": artifacts,
        "untracked": untracked,
        "stage": value["original_payload"],
        "role": value["role"],
        "owner_decisions": unit.evidence.get("decisions", []),
        "acceptance_revision": (unit.evidence.get("acceptance") or {}).get("revision", 0),
        "pending_actions": [
            dataclasses.asdict(row)
            for row in snapshot.outbox
            if row.unit == unit.id and row.state in {"prepared", "sent_unknown"}
        ],
        "predecessor": value["predecessor"],
        "coverage_gaps": gaps,
    }
    if capture(workspace, source_roots=source_roots).to_dict() != fingerprint:
        raise CampaignError("Working state changed while capturing the drained predecessor")
    return {
        "audit_context": _audit_context(snapshot, unit),
        "native_role": native_role,
        "sources": sources,
        "gaps": gaps,
        "current": current,
        "obligations": obligations,
        "capture_digest": digest([current, sources, obligations]),
    }


def _audit_context(snapshot, unit):
    from .campaign_fidelity import HISTORY_AUDIT_PROMPT_VERSION

    return {
        "history_audit_contract": {
            "prompt_version": HISTORY_AUDIT_PROMPT_VERSION,
            "schema_digest": digest(AUDIT_SCHEMA),
        },
        "graph_revision": snapshot.graph_revision,
        "config_revision": snapshot.config_revision,
        "decision_input_revision": unit.evidence.get("decision_input_revision", ""),
        "acceptance_revision": (unit.evidence.get("acceptance") or {}).get("revision", 0),
    }


def _coordinates(snapshot, unit, value):
    return {
        "head": "",
        "base": "",
        "graph_revision": snapshot.graph_revision,
        "config_revision": snapshot.config_revision,
        "handoff_id": value["id"],
        "capture_digest": value["capture"]["capture_digest"],
        "plan_epoch": value.get("plan_epoch", 0),
    }


def _summary_route(
    runtime,
    snapshot,
    role,
    value=None,
    excluded_families=(),
    *,
    min_payload=0,
    purpose="",
    contract="",
    stage="",
):
    from .campaign_headless import provider_family
    from .campaign_summary_routes import candidates, enabled, reserve_auditor, stage_for

    allowed, failures = [], []
    purpose = purpose or (
        ("handoff_final_audit" if enabled(snapshot.spec) else "handoff_audit")
        if role == "handoff_audit"
        else "handoff_map"
    )
    stage = (stage or stage_for(purpose, contract)) if enabled(snapshot.spec) else ""
    authors = _summary_authors(runtime, snapshot, value) if stage else set()
    reserved = (value or {}).get("audit_family", "")
    excluded = {
        route
        for route, until in (value or {}).get("route_cooldowns", {}).items()
        if until > runtime.clock()
    }
    for candidate in candidates(snapshot.spec, role, stage):
        if (
            candidate.profile + ":" + candidate.model in excluded
            or provider_family(candidate.model) in excluded_families
            or (
                stage
                and role == "handoff_audit"
                and reserved
                and provider_family(candidate.model) != reserved
            )
        ):
            continue
        try:
            cap = _cap(snapshot, candidate, "reasoning")
            validate_fidelity_qualification(
                cap, candidate=candidate, purpose=purpose, contract=contract
            )
            if stage and role == "summarization":
                reserve_auditor(
                    runtime,
                    snapshot,
                    authors | {provider_family(candidate.model)},
                    reserved=reserved,
                )
            if min_payload and _budget(candidate, cap).payload_tokens < min_payload:
                continue
        except CampaignError as exc:
            failures.append(str(exc))
            continue
        allowed.append(candidate)
    selected = (
        runtime._role(
            snapshot,
            role,
            allowed_candidates=tuple(allowed),
            **(
                {"summary_stage": stage, "permissions": "reasoning", "require_headless": True}
                if stage
                else {}
            ),
        )
        if allowed and hasattr(runtime, "_role")
        else allowed[0] if allowed else None
    )
    if selected is None:
        raise HandoffRouteWait(
            "No currently available qualified headless route for "
            + role
            + (": " + "; ".join(dict.fromkeys(failures)) if failures else "")
        )
    return selected, _cap(snapshot, selected, "reasoning")


def _observed_families(row):
    """Use native attribution, including auxiliaries, never selected-model guesses."""
    receipts = [row.get("receipt") or {}]
    retained = (row.get("applied_result") or {}).get("receipt")
    if retained and retained != receipts[0]:
        receipts.append(retained)
    return set().union(*(_receipt_families(receipt) for receipt in receipts))


def _receipt_families(receipt):
    from .campaign_headless import provider_family

    family = receipt.get("provider_family")
    families = set()
    if family:
        if family not in {"openai", "anthropic", "google", "xai", "meta"}:
            raise HandoffRouteWait("Summary contributor family is unknown")
        families.add(family)
    models = receipt.get("observed_models", [])
    if not isinstance(models, list):
        raise HandoffRouteWait("Summary contributor models are malformed")
    models = [*models, *([receipt["observed_model"]] if receipt.get("observed_model") else [])]
    for model in models:
        observed = provider_family(model)
        if not observed:
            raise HandoffRouteWait("Summary contributor model family is unknown")
        families.add(observed)
    if not families:
        raise HandoffRouteWait("Applied summary author family is unknown")
    return families


def _summary_authors(runtime, snapshot, value):
    """Retain every observed contributor to artifacts used by the summary tree."""
    result = set()
    for field in ("all_jobs", "constraint_jobs", "recovery_jobs"):
        for identifier in (value or {}).get(field, ()):
            row = _job(runtime, snapshot, identifier)
            if row.get("state") != "applied" and not row.get("applied_result"):
                continue
            result.update(_observed_families(row))
    return result


def _audit_limits(snapshot, family):
    """Plan against every qualified fallback in the reserved auditor family."""
    from .campaign_headless import provider_family
    from .campaign_summary_routes import qualified_candidates

    options, _ = qualified_candidates(
        snapshot.spec, snapshot.config_snapshot.get("reasoning_capabilities", {}), "audit"
    )
    capacities = []
    for candidate in options:
        if provider_family(candidate.model) != family:
            continue
        cap = _cap(snapshot, candidate, "reasoning")
        capacities.append(
            min(
                cap["input_tokens"],
                cap["context_tokens"] - cap["output_tokens"] - cap["reserved_tokens"],
            )
        )
    if not capacities:
        raise HandoffRouteWait("Reserved audit family has no qualified input envelope")
    bound = min(capacities)
    if bound < 16384:
        raise HandoffRouteWait("Reserved audit route has insufficient compatible summary space")
    return bound // 8, bound // 16, bound // 8


def _summary_budget(runtime, snapshot, value, candidate, cap, stage="leaf"):
    from .campaign_summary_routes import enabled

    budget = _budget(candidate, cap)
    if not enabled(snapshot.spec):
        return budget, 0
    family = _stage_coordinates(runtime, snapshot, value, candidate, stage)["audit_family"]
    source_limit, leaf_limit, final_limit = _audit_limits(snapshot, family)
    if stage == "leaf":
        budget = dataclasses.replace(
            budget,
            context_tokens=min(
                budget.context_tokens,
                source_limit
                + budget.output_tokens
                + budget.prompt_tokens
                + budget.thinking_tokens
                + budget.working_tokens,
            ),
        )
    return budget, final_limit if stage == "final" else leaf_limit


def _stage_coordinates(runtime, snapshot, value, candidate, stage):
    from .campaign_headless import provider_family
    from .campaign_summary_routes import STAGES, enabled, reserve_auditor

    if not enabled(snapshot.spec):
        return {}
    reserved = (value or {}).get("audit_family", "")
    if STAGES[stage][0] == "summarization":
        reserved = reserve_auditor(
            runtime,
            snapshot,
            _summary_authors(runtime, snapshot, value) | {provider_family(candidate.model)},
            reserved=reserved,
        )
    return {"summary_stage": stage, "audit_family": reserved}


def _budget(candidate, cap):
    from .campaign_summary import ContextBudget

    # Input delivery and total context are independent ceilings. JSON wrapping
    # can double source text; output reserves context, not the input allowance.
    # Admission still checks the complete rendered prompt/schema/manifest.
    output_tokens = min(cap["output_tokens"], 32768)
    return ContextBudget(
        candidate.profile
        + ":"
        + candidate.model
        + "@"
        + candidate.effort
        + ":"
        + cap["qualification"]["sha256"],
        min(
            cap["input_tokens"] // 2 + output_tokens,
            cap["context_tokens"] - cap["reserved_tokens"],
        ),
        output_tokens,
        prompt_tokens=8192,
        working_tokens=8192,
        qualified=True,
    )


def _maps(runtime, snapshot, unit, value):
    from .campaign_summary import (
        SummaryJobSpec,
        plan_remaining_maps,
        summary_contract_current,
        summary_prompt,
        summary_schema,
    )

    histories = tuple(HistorySnapshot(**row) for row in value["capture"]["sources"])
    events = merge_lineage_events(histories)
    completed = tuple(
        (
            SummaryJobSpec(
                **_job(runtime, snapshot, key)["request"]["coordinates"]["summary_spec"]
            ),
            _applied(_job(runtime, snapshot, key)),
        )
        for key in value.get("all_jobs", [])
        if _job(runtime, snapshot, key)["state"] == "applied"
        and _job(runtime, snapshot, key)["request"]["purpose"] == "handoff_map"
        and _job(runtime, snapshot, key)["request"]["coordinates"]["config_revision"]
        == snapshot.config_revision
        and summary_contract_current(_job(runtime, snapshot, key)["request"])
    )
    from .campaign_history_warmup import reusable

    warmed, invalidated = reusable(
        runtime,
        snapshot,
        unit,
        value["capture"]["sources"],
    )
    known = {job.id for job, _ in completed}
    completed = (*completed, *((job, result) for job, result in warmed if job.id not in known))
    value = {
        **value,
        "all_jobs": list(
            dict.fromkeys([*value.get("all_jobs", []), *(job.id for job, _ in completed)])
        ),
    }
    candidate, cap = _summary_route(runtime, snapshot, "summarization", value)
    budget, artifact_limit = _summary_budget(runtime, snapshot, value, candidate, cap)
    planned = plan_remaining_maps(
        events, completed, budget, _root(runtime, snapshot, value) / "maps"
    )
    jobs = []
    for planned_spec in planned:
        spec = dataclasses.replace(
            planned_spec,
            id=digest(
                [value["id"], planned_spec.id, value.get("plan_epoch", 0), snapshot.config_revision]
                + ([artifact_limit] if artifact_limit else [])
            ),
            artifact_bytes=artifact_limit,
        )
        coordinates = {
            **_coordinates(snapshot, unit, value),
            **_stage_coordinates(runtime, snapshot, value, candidate, "leaf"),
            "summary_spec": dataclasses.asdict(spec),
            "history_snapshots": value["capture"]["sources"],
        }
        jobs.append(
            dataclasses.asdict(
                JobRequest(
                    spec.id,
                    "handoff_map",
                    "summarization",
                    coordinates,
                    (
                        {
                            "source_id": spec.id,
                            "path": spec.input_path,
                            "sha256": spec.input_sha256,
                            "tokens": spec.input_tokens,
                        },
                    ),
                    summary_prompt(spec),
                    summary_schema(spec),
                    JobLimits(
                        cap["input_tokens"],
                        spec.output_tokens,
                        1,
                        snapshot.spec.reasoning.timeout_s,
                    ),
                    unit=unit.id,
                    candidates=(candidate,),
                    created_at=runtime.clock(),
                )
            )
        )
    return {
        "requests": jobs,
        "reuse_jobs": [job.id for job, _ in completed],
        "invalidated_warmup_jobs": invalidated,
    }


def _reduce(runtime, snapshot, unit, value):
    from .campaign_summary import (
        FINAL_PROMPT_VERSION,
        SummaryJobSpec,
        owner_quote_references,
        plan_reduction,
        summary_prompt,
        summary_schema,
    )
    from .campaign_summary_quotes import resolve_owner_quotes
    from .campaign_summary_routes import enabled

    if any(_job(runtime, snapshot, key)["state"] != "applied" for key in value["jobs"]):
        raise CampaignError("History compaction requires a complete applied frontier")
    children = tuple(
        (
            SummaryJobSpec(
                **_job(runtime, snapshot, key)["request"]["coordinates"]["summary_spec"]
            ),
            _applied(_job(runtime, snapshot, key)),
        )
        for key in value["jobs"]
    )
    target = _candidate(value["candidate"])
    target_cap = _target_cap(
        snapshot,
        target,
        value.get("route_role", value["role"]),
        unit=unit,
        permissions=_permission(value),
    )
    ceiling = min(
        snapshot.spec.reasoning.packet_token_limit, _budget(target, target_cap).payload_tokens
    )
    if any(child.get("structural_coverage") is not True for _, child in children):
        raise CampaignError("History compaction requires complete structural coverage")
    frontier = {"bytes": sum(len(encoded(child)) for _, child in children), "nodes": len(children)}
    staged = enabled(snapshot.spec)
    if (
        staged
        and all(
            _job(runtime, snapshot, key)["request"]["coordinates"].get("summary_stage") == "final"
            for key in value["jobs"]
        )
        and len(children) == 1
    ):
        return None
    if not staged and (
        len(children) <= 1 or (not value.get("reduction_level", 0) and frontier["bytes"] <= ceiling)
    ):
        return None
    previous = value.get("reduction_frontier")
    limited = (
        "eight-round-limit"
        if value.get("reduction_level", 0) >= 8
        else (
            "no-observed-shrink"
            if previous and all(frontier[key] >= previous[key] for key in ("bytes", "nodes"))
            else ""
        )
    )
    if limited:
        if staged:
            raise HandoffRouteWait("Final summary synthesis remains incomplete: " + limited)
        return {
            "history_compaction": {
                "status": "compaction-limited",
                "reason": limited,
                "frontier": frontier,
                "previous_frontier": previous,
                "rounds": value.get("reduction_level", 0),
                "retained_jobs": list(value["jobs"]),
            }
        }
    # Resolve once for this frontier, before either the final-fit decision or
    # intermediate partitioning. Every candidate group pays for its exact spans.
    owner_quotes = resolve_owner_quotes(
        tuple(HistorySnapshot(**row) for row in value["capture"]["sources"]),
        owner_quote_references([result for _, result in children]),
    )
    stage = "intermediate"
    if staged:
        candidate, cap = _summary_route(
            runtime, snapshot, "summarization", value, purpose="handoff_final", stage="final"
        )
        final_plan = plan_reduction(
            children,
            _budget(candidate, cap),
            _root(runtime, snapshot, value) / "reductions",
            artifact_bytes=0,
            project_native_events=True,
            owner_quotes=owner_quotes,
        )
        if len(final_plan) == 1:
            stage = "final"
    if stage != "final":
        candidate, cap = _summary_route(
            runtime, snapshot, "summarization", value, purpose="handoff_reduce", contract=""
        )
    budget, artifact_limit = _summary_budget(runtime, snapshot, value, candidate, cap, stage)
    # Stored native/structural evidence is independent of the initial view.
    # Complete artifacts remain indexed; assembly budgets selected context only
    # after mandatory current authority has been independently audited.
    planned = plan_reduction(
        children,
        budget,
        _root(runtime, snapshot, value) / "reductions",
        artifact_bytes=artifact_limit,
        project_native_events=True,
        owner_quotes=owner_quotes,
    )
    jobs = []
    for planned_spec in planned:
        spec = dataclasses.replace(
            planned_spec,
            id=digest(
                [value["id"], planned_spec.id, value.get("plan_epoch", 0), snapshot.config_revision]
                + ([stage] if staged else [])
            ),
            **({"prompt_version": FINAL_PROMPT_VERSION} if stage == "final" else {}),
        )
        coordinates = {
            **_coordinates(snapshot, unit, value),
            **_stage_coordinates(runtime, snapshot, value, candidate, stage),
            "summary_spec": dataclasses.asdict(spec),
            "history_snapshots": value["capture"]["sources"],
            "reduction_failure_keys": _reduction_failure_keys(runtime, snapshot, spec),
        }
        jobs.append(
            dataclasses.asdict(
                JobRequest(
                    spec.id,
                    "handoff_reduce",
                    "summarization",
                    coordinates,
                    (
                        {
                            "source_id": spec.id,
                            "path": spec.input_path,
                            "sha256": spec.input_sha256,
                            "tokens": spec.input_tokens,
                        },
                    ),
                    summary_prompt(spec),
                    summary_schema(spec),
                    JobLimits(
                        cap["input_tokens"],
                        spec.output_tokens,
                        1,
                        snapshot.spec.reasoning.timeout_s,
                    ),
                    unit=unit.id,
                    dependencies=spec.dependencies,
                    candidates=(candidate,),
                    created_at=runtime.clock(),
                )
            )
        )
    return {"requests": jobs, "reduction": True, "reduction_frontier": frontier}


def _reduction_failure_keys(runtime, snapshot, spec):
    """Keep attempt debt attached to original children through splits and combines."""
    result = {}
    for key in spec.primary_ids:
        child = _job(runtime, snapshot, key)["request"]
        result[key] = (
            _failure_keys(child)
            if child["purpose"] == "handoff_reduce"
            else [digest(["handoff_reduce", "", key])]
        )
    return result


def _summary_replan(runtime, snapshot, unit, value):
    """Retry the current reduction frontier, never discard it to re-extract maps."""
    from .campaign_summary import (
        FINAL_PROMPT_VERSION,
        REDUCE_PROMPT_VERSION,
        SummaryJobSpec,
        load_input,
        split_reduction_input,
        summary_prompt,
        summary_schema,
    )

    rows = [_job(runtime, snapshot, key) for key in value["jobs"]]
    if not any(row["request"]["purpose"] == "handoff_reduce" for row in rows):
        return _maps(runtime, snapshot, unit, value)
    if any(row["request"]["purpose"] != "handoff_reduce" for row in rows):
        raise CampaignError("Continuation reduction frontier mixes maps and reducers")
    reuse, requests, split_jobs = [], [], []
    for row in rows:
        if not _requires_replan(row):
            reuse.append(row["id"])
            continue
        claim, receipt = row.get("claim") or {}, row.get("receipt") or {}
        if claim and not claim.get("quiescent"):
            raise CampaignError("Cannot replace a reducer with unknown native quiescence")
        old = SummaryJobSpec(**row["request"]["coordinates"]["summary_spec"])
        stage = row["request"]["coordinates"].get("summary_stage", "")
        split = (
            row["state"] == "failed"
            and claim.get("quiescent") is True
            and receipt.get("quiescent") is True
            and receipt.get("outcome") == "limit"
            and receipt.get("limit_kind") == "native-output-tokens"
            and not old.correction_of
            and len(load_input(old).get("children", [])) > 1
        )
        if split and value.get("reduction_level", 0) >= 8:
            raise CampaignError(
                "History reduction failed to converge within eight qualified rounds"
            )
        specs = (
            split_reduction_input(old, _root(runtime, snapshot, value) / "reductions")
            if split
            else (old,)
        )
        if split:
            split_jobs.append(row["id"])
        for planned in specs:
            candidate, cap = _summary_route(
                runtime,
                snapshot,
                "summarization",
                value,
                min_payload=planned.input_tokens
                + sum(source["tokens"] for source in row["request"]["inputs"][1:]),
                purpose="handoff_final" if stage == "final" else "handoff_reduce",
                stage=stage,
            )
            budget, artifact_limit = _summary_budget(
                runtime, snapshot, value, candidate, cap, stage or "intermediate"
            )
            spec = dataclasses.replace(
                planned,
                id=digest([value["id"], planned.id, value["plan_epoch"], snapshot.config_revision]),
                route=budget.route,
                output_tokens=min(planned.output_tokens, cap["output_tokens"]),
                artifact_bytes=(
                    min(planned.artifact_bytes, artifact_limit)
                    if planned.artifact_bytes and artifact_limit
                    else artifact_limit or planned.artifact_bytes
                ),
                prompt_version=FINAL_PROMPT_VERSION if stage == "final" else REDUCE_PROMPT_VERSION,
            )
            coordinates = {
                **_coordinates(snapshot, unit, value),
                **_stage_coordinates(runtime, snapshot, value, candidate, stage or "intermediate"),
                "summary_spec": dataclasses.asdict(spec),
                "history_snapshots": value["capture"]["sources"],
                "reduction_failure_keys": _reduction_failure_keys(runtime, snapshot, spec),
                **(
                    {"final_repair": row["request"]["coordinates"]["final_repair"]}
                    if "final_repair" in row["request"]["coordinates"]
                    else {}
                ),
            }
            requests.append(
                dataclasses.asdict(
                    JobRequest(
                        spec.id,
                        "handoff_reduce",
                        "summarization",
                        coordinates,
                        (
                            {
                                "source_id": spec.id,
                                "path": spec.input_path,
                                "sha256": spec.input_sha256,
                                "tokens": spec.input_tokens,
                            },
                            *tuple(row["request"]["inputs"][1:]),
                        ),
                        summary_prompt(spec),
                        summary_schema(spec),
                        JobLimits(
                            cap["input_tokens"],
                            spec.output_tokens,
                            1,
                            min(
                                row["request"]["limits"]["timeout_s"],
                                snapshot.spec.reasoning.timeout_s,
                            ),
                        ),
                        unit=unit.id,
                        dependencies=spec.dependencies,
                        candidates=(candidate,),
                        created_at=runtime.clock(),
                    )
                )
            )
            from .campaign_headless import render_input

            try:
                render_input(request_from_dict(requests[-1]), cap)
            except CampaignError as exc:
                raise HandoffRouteWait(
                    "Complete replanned summary evidence cannot fit: " + str(exc)
                ) from exc
    return {
        "requests": requests,
        "reuse_jobs": reuse,
        "reduction_replan": True,
        "reduction": bool(split_jobs),
        "split_jobs": split_jobs,
    }


def validate_fidelity_qualification(cap, *, candidate=None, purpose="", contract=""):
    """Require an independent proof for the exact operation, effort and transport."""
    from .campaign_fidelity import validate

    purpose = purpose or {
        "constraints-v1": "constraint_audit",
        "recovery-v1": "recovery_audit",
    }.get(contract, "handoff_audit")
    try:
        return validate(cap, AUDIT_SCHEMA, candidate=candidate, purpose=purpose, contract=contract)
    except CampaignError as exc:
        raise HandoffRouteWait(str(exc)) from exc


def _audit_plan(runtime, snapshot, unit, value):
    from .campaign_fidelity import FINAL_AUDIT_PROMPT_VERSION, HISTORY_AUDIT_PROMPT_VERSION
    from .campaign_summary import SummaryJobSpec, load_input
    from .campaign_summary_routes import enabled

    reduced = _reduce(runtime, snapshot, unit, value)
    if reduced is not None and "requests" in reduced:
        return reduced
    map_families = _summary_authors(
        runtime, snapshot, {**value, "all_jobs": value.get("all_jobs", value["jobs"])}
    )
    candidate, cap = _summary_route(runtime, snapshot, "handoff_audit", value, map_families)
    from .campaign_headless import provider_family

    if provider_family(candidate.model) in map_families:
        raise CampaignError("Handoff constraint audit requires an independent model family")
    histories = tuple(HistorySnapshot(**row) for row in value["capture"]["sources"])
    events = {event.id: event for history in histories for event in load_events(history)}
    jobs = []
    for key in value.get("map_jobs", value["jobs"]) or [None]:
        summary = _job(runtime, snapshot, key) if key else None
        source = (
            load_input(SummaryJobSpec(**summary["request"]["coordinates"]["summary_spec"]))
            if summary
            else {"primary": []}
        )
        owner_ids = sorted(
            {
                fragment["event_id"]
                for fragment in source["primary"]
                if events[fragment["event_id"]].record.owner_input
            }
        )
        from .campaign_handoff_obligations import PROMPT, batches, bound_schema, owner_spans

        spans = owner_spans(events[key] for key in owner_ids)
        obligations = value["capture"]["obligations"]
        content_base = {
            "history": source,
            "summary": _applied(summary) if summary else None,
            "canonical_obligations": obligations,
            "current_state": value["capture"]["current"],
            "exclusion_evidence": [],
        }
        if enabled(snapshot.spec):
            content_base["final_frontier"] = [
                {"job_id": identifier, "summary": _applied(_job(runtime, snapshot, identifier))}
                for identifier in value["jobs"]
            ]
        try:
            planned_batches = batches(
                spans,
                content=content_base,
                input_bound=min(
                    cap["input_tokens"],
                    cap["context_tokens"] - cap["output_tokens"] - cap["reserved_tokens"],
                )
                // 2,
                output_bound=cap["output_tokens"],
            )
        except CampaignError as exc:
            raise HandoffRouteWait(
                "Complete history audit context cannot fit: " + str(exc)
            ) from exc
        for batch in planned_batches:
            owner_ids = sorted({row["source_id"] for row in batch})
            content = {**content_base, "owner_spans": batch}
            data = encoded(content)
            path = blob(_root(runtime, snapshot, value) / "audits", data)
            coordinates = {
                **_coordinates(snapshot, unit, value),
                **_stage_coordinates(runtime, snapshot, value, candidate, "audit"),
                "owner_ids": owner_ids,
                "owner_spans": batch,
                "obligation_ids": [row["id"] for row in obligations],
                "history_snapshots": value["capture"]["sources"],
                "audit_prompt_version": (
                    FINAL_AUDIT_PROMPT_VERSION
                    if enabled(snapshot.spec)
                    else HISTORY_AUDIT_PROMPT_VERSION
                ),
                **({"final_frontier": list(value["jobs"])} if enabled(snapshot.spec) else {}),
            }
            prompt = PROMPT
            if enabled(snapshot.spec):
                prompt += " Independently compare the final_frontier synthesis against this exact leaf history, leaf summary and canonical obligations. Report any dropped, contradicted, invented or hidden material obligation in ambiguities; correct leaf extraction alone cannot approve an incorrect final synthesis."
            prompt += json.dumps(
                {
                    "handoff_id": value["id"],
                    "capture_digest": coordinates["capture_digest"],
                    "covered_owner_ids": owner_ids,
                    "covered_span_ids": [row["span_id"] for row in batch],
                    "obligation_ids": coordinates["obligation_ids"],
                }
            )
            identifier = "audit-" + digest([coordinates, data.decode()])[:32]
            jobs.append(
                dataclasses.asdict(
                    JobRequest(
                        identifier,
                        "handoff_audit",
                        "handoff_audit",
                        coordinates,
                        (
                            {
                                "source_id": identifier,
                                "path": str(path),
                                "sha256": hashlib.sha256(data).hexdigest(),
                                "tokens": len(data),
                            },
                        ),
                        prompt,
                        bound_schema(coordinates),
                        JobLimits(
                            cap["input_tokens"],
                            cap["output_tokens"],
                            1,
                            snapshot.spec.reasoning.timeout_s,
                        ),
                        unit=unit.id,
                        candidates=(candidate,),
                        created_at=runtime.clock(),
                    )
                )
            )
    from .campaign_headless import render_input

    for job in jobs:
        try:
            render_input(request_from_dict(job), cap)
        except CampaignError as exc:
            raise HandoffRouteWait(
                "Complete history audit envelope cannot fit: " + str(exc)
            ) from exc
    return {"requests": jobs, **(reduced or {})}


def apply_audit(runtime, snapshot, row):
    unit = snapshot.units[row["request"]["unit"]]
    value = unit.evidence.get("handoff") or {}
    coordinates = row["request"]["coordinates"]
    result = row["result"]
    from .campaign_summary import measured_output_tokens

    measured = measured_output_tokens(row.get("receipt"))
    output_count = len(encoded(result)) if measured is None else measured
    if (
        value.get("id") != coordinates["handoff_id"]
        or value["capture"]["capture_digest"] != coordinates["capture_digest"]
    ):
        raise CampaignError("Handoff audit coordinates changed")
    contract = coordinates.get("contract", "")
    if not contract or contract.endswith("audit-v1"):
        observed_families = _observed_families(row)
        reviewed = (
            row["request"]["dependencies"]
            if contract
            else [
                key
                for key in value.get("all_jobs", value["jobs"])
                if _job(runtime, snapshot, key)["state"] == "applied"
            ]
        )
        writers = set().union(
            *(_observed_families(_job(runtime, snapshot, key)) for key in reviewed)
        )
        if observed_families & writers:
            raise CampaignError("Actual handoff audit family is missing or not independent")
    if contract in {
        "constraint-reduce-v1",
        "constraint-audit-v1",
        "recovery-extract-v1",
        "recovery-audit-v1",
    }:
        if contract.startswith("constraint-"):
            from .campaign_handoff_constraints import validate
        else:
            from .campaign_handoff_recovery import validate

        source = row["request"]["inputs"][0]
        content = Path(source["path"]).read_bytes()
        if hashlib.sha256(content).hexdigest() != source["sha256"]:
            raise CampaignError("Constraint job input changed")
        if output_count > row["request"]["limits"]["output_tokens"]:
            raise CampaignError("Continuation context result exceeds its qualified output bound")
        validated = validate(result, json.loads(content), audit=contract.endswith("audit-v1"))
        data = encoded(validated)
        path = blob(_root(runtime, snapshot, value) / "constraint-results", data)
        row["applied_result"] = {
            "artifact_path": str(path),
            "artifact_sha256": hashlib.sha256(data).hexdigest(),
        }
        return
    if output_count > row["request"]["limits"]["output_tokens"]:
        raise CampaignError("Handoff audit result exceeds its qualified output bound")
    if (
        result.get("handoff_id") != value["id"]
        or result.get("capture_digest") != coordinates["capture_digest"]
    ):
        raise CampaignError("Handoff audit returned stale identity")
    for key in ("covered_owner_ids", "obligation_ids"):
        expected = coordinates["owner_ids" if key == "covered_owner_ids" else key]
        if (
            not isinstance(result.get(key), list)
            or len(set(result[key])) != len(result[key])
            or set(result[key]) != set(expected)
        ):
            raise CampaignError("Handoff audit omitted or invented required coverage")
    events = {
        event.id: event
        for source in coordinates["history_snapshots"]
        for event in load_events(HistorySnapshot(**source))
    }
    from .campaign_handoff_obligations import validate as validate_owner_spans

    result = validate_owner_spans(result, coordinates, events.values())
    if value["capture"]["gaps"] and not value.get("recovery", {}).get("ready"):
        raise CampaignError(
            "Missing native history must be recovered before automatic writer continuation"
        )
    if result.get("ambiguities"):
        _record_final_rejection(runtime, snapshot, unit, value, row, result)
        raise CampaignError(
            "Handoff still has unresolved current constraints: " + "; ".join(result["ambiguities"])
        )
    data = encoded({**result, "authority": "context-only", "validated": True})
    path = blob(_root(runtime, snapshot, value) / "audited-constraints", data)
    row["applied_result"] = {
        "artifact_path": str(path),
        "artifact_sha256": hashlib.sha256(data).hexdigest(),
    }


def _repair_sources(value):
    """A bounded rewrite cannot repair missing/corrupt original evidence."""
    from .campaign_transcripts import stream_hash

    if value["capture"].get("gaps"):
        raise CampaignError("Missing original history cannot be repaired by summary synthesis")
    histories = tuple(HistorySnapshot(**raw) for raw in value["capture"]["sources"])
    for history in histories:
        if stream_hash(Path(history.raw_path)) != history.raw_sha256:
            raise CampaignError("Final repair original native history changed")
        load_events(history)
    return histories


def _record_final_rejection(runtime, snapshot, unit, value, row, normalized):
    """Preserve a validated ambiguity; this never classifies it as resolvable."""
    from .campaign_summary_routes import enabled

    coordinates = row["request"]["coordinates"]
    frontier = value.get("jobs", [])
    if not enabled(snapshot.spec) or not coordinates.get("final_frontier"):
        return  # Legacy audit behavior remains unchanged.
    if (
        value.get("phase") != "audit"
        or coordinates["final_frontier"] != frontier
        or len(frontier) != 1
        or row["id"] not in value.get("audits", [])
        or coordinates.get("plan_epoch", 0) != value.get("plan_epoch", 0)
        or value["capture"].get("audit_context") != _audit_context(snapshot, unit)
    ):
        raise CampaignError("Rejected final audit no longer binds the current frontier")
    final = _job(runtime, snapshot, frontier[0])
    if (
        final["state"] != "applied"
        or final["request"]["coordinates"].get("summary_stage") != "final"
    ):
        raise CampaignError("Final repair requires an applied final synthesis")
    claim, receipt = row.get("claim") or {}, row.get("receipt") or {}
    expected = {
        "job_id": row["id"],
        "launch_id": claim.get("launch_id"),
        "generation": claim.get("generation"),
        "request_digest": row["request_digest"],
        "outcome": "completed",
        "quiescent": True,
        "input_complete": True,
        "processes_quiescent": True,
    }
    if (
        not claim.get("launch_id")
        or claim.get("quiescent") is not True
        or any(receipt.get(key) != val for key, val in expected.items())
        or receipt.get("result") != row["result"]
        or not unit.claim
        or not unit.claim.quiescent
    ):
        raise CampaignError("Final repair requires exact quiescent native audit and predecessor")
    # This immutable finding is context only. Full archive authentication runs
    # in the repair effect, outside the coordinator's result-application path.
    data = encoded(
        {
            "request": row["request"],
            "receipt": receipt,
            "result": row["result"],
            "normalized": normalized,
            "authority": "context-only; unresolved, not permission",
        }
    )
    path = blob(_root(runtime, snapshot, value) / "final-rejections", data)
    record = {
        "audit_job": row["id"],
        "artifact_path": str(path),
        "artifact_sha256": hashlib.sha256(data).hexdigest(),
        "request_digest": row["request_digest"],
        "receipt_digest": digest(receipt),
    }
    pending = value.setdefault(
        "final_repair_pending",
        {
            "frontier": list(frontier),
            "capture_digest": value["capture"]["capture_digest"],
            "findings": [],
        },
    )
    if (
        pending["frontier"] != frontier
        or pending["capture_digest"] != value["capture"]["capture_digest"]
    ):
        raise CampaignError("Final rejection belongs to a different repair frontier")
    prior = next((item for item in pending["findings"] if item["audit_job"] == row["id"]), None)
    if prior is not None and prior != record:
        raise CampaignError("Final audit already has different immutable rejection evidence")
    if prior is None:
        pending["findings"].append(record)
        add_event(
            snapshot,
            "handoff-final-rejected",
            unit=unit.id,
            handoff_id=value["id"],
            final_job=frontier[0],
            audit_job=row["id"],
            evidence=record,
        )


def _final_repair(runtime, snapshot, unit, value):
    """Create a contextual correction, with a lifetime cap and fresh audit debt."""
    from .campaign_headless import render_input
    from .campaign_summary import (
        SummaryJobSpec,
        corrective_spec,
        load_input,
        owner_quote_references,
        summary_prompt,
        summary_schema,
    )
    from .campaign_summary_quotes import resolve_owner_quotes

    pending = value.get("final_repair_pending")
    if (
        not pending
        or pending["frontier"] != value["jobs"]
        or pending["capture_digest"] != value["capture"]["capture_digest"]
    ):
        raise CampaignError("Final repair lost its exact rejected frontier")
    if any(
        row.get("claim") and not row["claim"].get("quiescent")
        for row in snapshot.jobs.values()
        if row["request"].get("coordinates", {}).get("handoff_id") == value["id"]
    ):
        raise CampaignError("Final repair awaits all prior audit and summary actors")
    histories = _repair_sources(value)
    lifetime_key = value["predecessor"]["launch_id"]
    history = unit.evidence.get("handoff_final_repairs", {}).get(lifetime_key, [])
    if len(history) >= 2:
        return {"final_repair_exhausted": True, "final_repair_pending_digest": digest(pending)}
    previous = _job(runtime, snapshot, pending["frontier"][0])
    if (
        previous["state"] != "applied"
        or previous["request"]["coordinates"].get("summary_stage") != "final"
    ):
        raise CampaignError("Final repair cannot replace an unapplied or non-final result")
    old = SummaryJobSpec(**previous["request"]["coordinates"]["summary_spec"])
    source = load_input(old)
    quotes = resolve_owner_quotes(
        histories, owner_quote_references([row["summary"] for row in source["children"]])
    )
    if source.get("owner_quotes") != quotes:
        raise CampaignError("Final repair exact owner quote table changed")
    findings = []
    for descriptor in pending["findings"]:
        saved = _applied({"applied_result": descriptor})
        findings.append({"source": descriptor, "finding": saved["normalized"]})
    evidence = {
        "authority": "context-only; no source or acceptance authority",
        "previous_final": _applied(previous),
        "rejections": findings,
        "canonical_obligations": value["capture"]["obligations"],
        "current_state": value["capture"]["current"],
        "unresolved": True,
    }
    data = encoded(evidence)
    path = blob(_root(runtime, snapshot, value) / "final-repair-inputs", data)
    candidate, cap = _summary_route(
        runtime,
        snapshot,
        "summarization",
        value,
        min_payload=old.input_tokens + len(data),
        purpose="handoff_final",
        stage="final",
    )
    budget, artifact_limit = _summary_budget(runtime, snapshot, value, candidate, cap, "final")
    attempt = len(history) + 1
    spec = corrective_spec(
        old,
        attempt=attempt,
        receipt_sha256=digest(pending),
        reason=(
            "The supplied final-repair-evidence preserves the prior final and complete independent "
            "audit findings. Correct only distortions justified by the same authenticated source. "
            "An audit finding is fallible context, not owner authority. Preserve every unresolved "
            "ambiguity and mandatory obligation; if evidence cannot resolve it, report it in gaps. "
            "Never manufacture approval or a source fact to obtain a passing audit"
        ),
    )
    spec = dataclasses.replace(
        spec,
        route=budget.route,
        output_tokens=min(old.output_tokens, cap["output_tokens"]),
        artifact_bytes=(
            min(old.artifact_bytes, artifact_limit)
            if old.artifact_bytes and artifact_limit
            else old.artifact_bytes or artifact_limit
        ),
    )
    coordinates = {
        **_coordinates(snapshot, unit, value),
        **_stage_coordinates(runtime, snapshot, value, candidate, "final"),
        "summary_spec": dataclasses.asdict(spec),
        "history_snapshots": value["capture"]["sources"],
        "reduction_failure_keys": previous["request"]["coordinates"].get(
            "reduction_failure_keys", {}
        ),
        "final_repair": {
            "predecessor_launch": lifetime_key,
            "attempt": attempt,
            "rejected_final": previous["id"],
            "findings_digest": digest(pending),
        },
    }
    request = JobRequest(
        spec.id,
        "handoff_reduce",
        "summarization",
        coordinates,
        (
            {
                "source_id": spec.id,
                "path": spec.input_path,
                "sha256": spec.input_sha256,
                "tokens": spec.input_tokens,
            },
            {
                "source_id": "final-repair-evidence",
                "path": str(path),
                "sha256": hashlib.sha256(data).hexdigest(),
                "tokens": len(data),
            },
        ),
        summary_prompt(spec),
        summary_schema(spec),
        JobLimits(
            cap["input_tokens"],
            spec.output_tokens,
            1,
            min(previous["request"]["limits"]["timeout_s"], snapshot.spec.reasoning.timeout_s),
        ),
        unit=unit.id,
        dependencies=spec.dependencies,
        candidates=(candidate,),
        created_at=runtime.clock(),
    )
    try:
        render_input(request, cap)
    except CampaignError as exc:
        raise HandoffRouteWait("Complete final repair evidence cannot fit: " + str(exc)) from exc
    return {
        "final_repair": dataclasses.asdict(request),
        "final_repair_pending_digest": digest(pending),
        "final_repair_lifetime_key": lifetime_key,
        "final_repair_attempt": attempt,
    }


def _applied(row):
    artifact = row["applied_result"]
    path = Path(artifact["artifact_path"])
    if hashlib.sha256(path.read_bytes()).hexdigest() != artifact["artifact_sha256"]:
        raise CampaignError("Validated handoff artifact changed")
    return read_json(path)


def _contract_job(
    runtime,
    snapshot,
    unit,
    value,
    *,
    candidate,
    cap,
    contract,
    content,
    schema,
    prompt,
    field,
    dependencies=(),
    output_tokens=None,
):
    from .campaign_handoff_constraints import bound_schema

    data = encoded(content)
    path = blob(_root(runtime, snapshot, value) / "contract-inputs", data)
    coordinates = {
        **_coordinates(snapshot, unit, value),
        **_stage_coordinates(
            runtime,
            snapshot,
            value,
            candidate,
            {
                "constraint-reduce-v1": "constraint",
                "constraint-audit-v1": "constraint_audit",
                "recovery-extract-v1": "recovery",
                "recovery-audit-v1": "recovery_audit",
            }[contract],
        ),
        "contract": contract,
        "handoff_job_field": field,
    }
    identifier = (
        "context-"
        + digest(
            [
                coordinates,
                data.decode(),
                candidate.profile,
                candidate.model,
                candidate.effort,
                cap["qualification"]["sha256"],
            ]
        )[:32]
    )
    request = JobRequest(
        identifier,
        "handoff_audit",
        (
            "summarization"
            if contract in {"constraint-reduce-v1", "recovery-extract-v1"}
            else "handoff_audit"
        ),
        coordinates,
        (
            {
                "source_id": identifier,
                "path": str(path),
                "sha256": hashlib.sha256(data).hexdigest(),
                "tokens": len(data),
            },
        ),
        prompt,
        bound_schema(schema, content),
        JobLimits(
            cap["input_tokens"],
            output_tokens or cap["output_tokens"],
            1,
            snapshot.spec.reasoning.timeout_s,
        ),
        unit=unit.id,
        dependencies=tuple(dependencies),
        candidates=(candidate,),
        created_at=runtime.clock(),
    )
    try:
        request.validate()
    except CampaignError as exc:
        raise HandoffRouteWait(
            "Qualified continuation route cannot fit the immutable context: " + str(exc)
        ) from exc
    return dataclasses.asdict(request)


def _constraint_plan(runtime, snapshot, unit, value):
    from .campaign_handoff_constraints import REDUCTION_SCHEMA, original_nodes, partition

    if value.get("constraint_level", 0) >= 8:
        raise HandoffRouteWait(
            "Current constraint reduction did not converge within its bounded rounds"
        )
    candidate, cap = _summary_route(
        runtime,
        snapshot,
        "summarization",
        value,
        purpose="constraint_reduce",
        contract="constraints-v1",
    )
    if value.get("constraint_jobs"):
        nodes = [
            node
            for key in value["constraint_jobs"]
            for node in _applied(_job(runtime, snapshot, key))["groups"]
        ]
        dependencies = value["constraint_audits"]
    else:
        nodes = original_nodes([{"constraints": _current_constraints(runtime, snapshot, value)}])
        dependencies = value["audits"]
    target = _candidate(value["candidate"])
    ceiling = min(
        snapshot.spec.reasoning.packet_token_limit,
        _budget(
            target,
            _target_cap(
                snapshot,
                target,
                value.get("route_role", value["role"]),
                unit=unit,
                permissions=_permission(value),
            ),
        ).payload_tokens,
    )
    output = min(cap["output_tokens"], max(512, ceiling // 16))
    jobs = []
    for batch in partition(
        nodes, _budget(candidate, cap).payload_tokens // 2, max_nodes=max(1, (output - 512) // 150)
    ):
        content = {
            "handoff_id": value["id"],
            "capture_digest": value["capture"]["capture_digest"],
            "nodes": batch,
        }
        jobs.append(
            _contract_job(
                runtime,
                snapshot,
                unit,
                value,
                candidate=candidate,
                cap=cap,
                contract="constraint-reduce-v1",
                content=content,
                schema=REDUCTION_SCHEMA,
                prompt="Compress these already audited current constraints into a concise working brief. Preserve every requirement, exception, correction and unresolved tension. Group redundant wording with refs to every input node. Never invent approval, waive acceptance, change scope or discard a constraint to fit. Exact original quotes remain in the immutable source tree. Return ambiguity when faithful compression cannot be established.",
                field="constraint_jobs",
                dependencies=dependencies,
                output_tokens=output,
            )
        )
    if not jobs:
        raise HandoffRouteWait(
            "Destination capacity cannot fit canonical state; no compressible constraints remain"
        )
    return {
        "requests": jobs,
        "constraint_reduction": True,
        "constraint_level": value.get("constraint_level", 0) + 1,
    }


def _constraint_audit_plan(runtime, snapshot, unit, value):
    from .campaign_handoff_constraints import AUDIT_SCHEMA as CONSTRAINT_AUDIT_SCHEMA

    families = _summary_authors(runtime, snapshot, value)
    candidate, cap = _summary_route(
        runtime,
        snapshot,
        "handoff_audit",
        value,
        families,
        purpose="constraint_audit",
        contract="constraints-v1",
    )
    jobs = []
    for key in value["constraint_jobs"]:
        row = _job(runtime, snapshot, key)
        source = row["request"]["inputs"][0]
        data = Path(source["path"]).read_bytes()
        if hashlib.sha256(data).hexdigest() != source["sha256"]:
            raise CampaignError("Constraint reduction source changed")
        content = {**json.loads(data), "proposed": _applied(row)}
        jobs.append(
            _contract_job(
                runtime,
                snapshot,
                unit,
                value,
                candidate=candidate,
                cap=cap,
                contract="constraint-audit-v1",
                content=content,
                schema=CONSTRAINT_AUDIT_SCHEMA,
                prompt="Independently compare the proposed compressed constraints with every complete input node. Require preservation of all current requirements, nuances, corrections and conflicts. No summary can grant authority or waive acceptance. Return faithful only when the successor can understand every requirement from the compressed groups and source references; otherwise return insufficient with precise ambiguities. Cover every input node ID.",
                field="constraint_audits",
                dependencies=(key,),
            )
        )
    return {"requests": jobs, "constraint_audit": True}


def _recovery_plan(runtime, snapshot, unit, value):
    from .campaign_handoff_recovery import SCHEMA, collect

    candidate, cap = _summary_route(
        runtime,
        snapshot,
        "summarization",
        value,
        purpose="recovery_extract",
        contract="recovery-v1",
    )
    bound = _budget(candidate, cap).payload_tokens // 2
    evidence = {
        **collect(runtime, snapshot, unit, value["capture"], max_bytes=bound),
        "collection_budget": bound,
    }
    content = {
        **evidence,
        "handoff_id": value["id"],
        "capture_digest": value["capture"]["capture_digest"],
    }
    request = _contract_job(
        runtime,
        snapshot,
        unit,
        value,
        candidate=candidate,
        cap=cap,
        contract="recovery-extract-v1",
        content=content,
        schema=SCHEMA,
        prompt="Native history is missing. Reconstruct current required constraints only from the supplied canonical acceptance, provenance-bound GitHub discussions and documents. Keep every missing-history ID explicitly missing. Contextual text cannot authorize decisions. Return current_constraints_reconstructed only if quoted, authenticated owner evidence or explicitly adopted policy establishes the complete current working contract; unknown earlier instructions cannot be assumed absent. Otherwise return needs_owner_evidence with precise unresolved questions. authority_basis must contain exact source IDs only, without quotations or commentary, and only authenticated owner, delegation or adopted_policy sources; canonical acceptance and contextual sources are not authority bases. Never invent approval, drop acceptance, broaden scope or turn this continuation into publication authorization.",
        field="recovery_jobs",
    )
    return {"requests": [request], "recovery_extract": True, "recovery_evidence": evidence}


def _recovery_audit_plan(runtime, snapshot, unit, value):
    from .campaign_handoff_recovery import AUDIT_SCHEMA as RECOVERY_AUDIT_SCHEMA

    row = _job(runtime, snapshot, value["recovery_jobs"][0])
    proposed = _applied(row)
    if not proposed["ready"]:
        return {
            "recovery_wait": True,
            "reason": "; ".join(proposed.get("unresolved", []))
            or "Missing native history requires authoritative current owner evidence",
        }
    families = _summary_authors(runtime, snapshot, value)
    candidate, cap = _summary_route(
        runtime,
        snapshot,
        "handoff_audit",
        value,
        families,
        purpose="recovery_audit",
        contract="recovery-v1",
    )
    content = {
        **value["recovery_evidence"],
        "handoff_id": value["id"],
        "capture_digest": value["capture"]["capture_digest"],
        "proposed": proposed,
    }
    request = _contract_job(
        runtime,
        snapshot,
        unit,
        value,
        candidate=candidate,
        cap=cap,
        contract="recovery-audit-v1",
        content=content,
        schema=RECOVERY_AUDIT_SCHEMA,
        prompt="Independently assess whether the supplied authenticated policy/owner evidence establishes the full current working contract despite explicitly missing native history. Inspect every reconstructed constraint and authority quotation. Canonical acceptance remains mandatory. Do not infer absent instructions, manufacture approval, waive obligations or claim history was recovered. Return current_contract_established only if the bounded continuation can proceed under that complete current contract; otherwise needs_owner_evidence with exact unresolved gaps. Cover all evidence, obligations and missing-history IDs.",
        field="recovery_audits",
        dependencies=(row["id"],),
    )
    return {"requests": [request], "recovery_audit": True}


def _contract_replan(runtime, snapshot, unit, value):
    fields = {
        "constraint_reduce": "constraint_jobs",
        "constraint_audit": "constraint_audits",
        "recovery_extract": "recovery_jobs",
        "recovery_audit": "recovery_audits",
    }
    field = fields[value["phase"]]
    rows = [_job(runtime, snapshot, key) for key in value[field]]
    requests, reuse = [], []
    for row in rows:
        if row["state"] == "applied":
            reuse.append(row["id"])
            continue
        raw = row["request"]
        excluded = []
        if raw["role"] == "handoff_audit":
            excluded = set().union(
                *(_observed_families(_job(runtime, snapshot, key)) for key in raw["dependencies"])
            )
        source = raw["inputs"][0]
        data = Path(source["path"]).read_bytes()
        if hashlib.sha256(data).hexdigest() != source["sha256"]:
            raise CampaignError("Retried context job source changed")
        contract = raw["coordinates"]["contract"]
        candidate, cap = _summary_route(
            runtime,
            snapshot,
            raw["role"],
            value,
            excluded,
            min_payload=len(data),
            purpose=contract.removesuffix("-v1").replace("-", "_"),
            contract="constraints-v1" if contract.startswith("constraint-") else "recovery-v1",
        )
        requests.append(
            _contract_job(
                runtime,
                snapshot,
                unit,
                value,
                candidate=candidate,
                cap=cap,
                contract=contract,
                content=json.loads(data),
                schema=raw["output_schema"],
                prompt=raw["prompt"],
                field=field,
                dependencies=raw["dependencies"],
                output_tokens=min(raw["limits"]["output_tokens"], cap["output_tokens"]),
            )
        )
    return {
        "requests": requests,
        "contract_replan": True,
        "field": field,
        "reuse_jobs": reuse,
        "plan_epoch": value["plan_epoch"],
        "route_cooldowns": value["route_cooldowns"],
    }


def _current_constraints(runtime, snapshot, value):
    """Unify audited history and reconstructed constraints without losing exact sources."""
    rows = [
        row
        for key in value["audits"]
        for row in _applied(_job(runtime, snapshot, key))["constraints"]
        if row["relevance"] in {"current", "context"}
    ]
    if value.get("recovery", {}).get("ready"):
        rows.extend(
            {**row, "relevance": "current", "origin": "current-contract-recovery"}
            for row in _applied(_job(runtime, snapshot, value["recovery_jobs"][0]))["constraints"]
        )
    return list({row.get("span_id") or digest(row): row for row in rows}.values())


def _assemble(runtime, snapshot, unit, value):
    from .campaign_handoff_context import (
        AuditedConstraintsTooLarge,
        assemble_packet,
        bind_authority_evidence,
        fit_audited_history,
        mandatory_context_digest,
    )
    from .campaign_summary import SummaryJobSpec

    if not value.get("audits") or any(
        _job(runtime, snapshot, key)["state"] != "applied" for key in value["audits"]
    ):
        raise CampaignError(
            "Current constraints require completed independent audits before packet release"
        )
    target = _candidate(value["candidate"])
    cap = _target_cap(
        snapshot,
        target,
        value.get("route_role", value["role"]),
        unit=unit,
        permissions=_permission(value),
    )
    summaries = tuple(_applied(_job(runtime, snapshot, key)) for key in value["jobs"])
    packet = assemble_packet(
        tuple(HistorySnapshot(**row) for row in value["capture"]["sources"]),
        tuple(value["capture"]["obligations"]),
        value["capture"]["current"],
        summaries,
        _budget(target, cap),
        _root(runtime, snapshot, value) / "packet",
        packet_ceiling=snapshot.spec.reasoning.packet_token_limit,
        summary_tree=tuple(
            (
                SummaryJobSpec(**row["request"]["coordinates"]["summary_spec"]),
                _applied(row),
            )
            for key in dict.fromkeys([*value.get("all_jobs", []), *value["jobs"]])
            if (row := _job(runtime, snapshot, key))["state"] == "applied"
            and row["request"]["purpose"] in {"handoff_map", "handoff_reduce"}
        ),
    )
    payload = read_json(Path(packet.path))
    if value.get("recovery", {}).get("ready"):
        payload["missing_native_history"] = value["capture"]["gaps"]
        payload["current_contract_recovery"] = value["recovery"]
        payload["recovered_constraints"] = _applied(
            _job(runtime, snapshot, value["recovery_jobs"][0])
        )["constraints"]
    payload["current_constraint_audits"] = [
        {
            "path": _job(runtime, snapshot, key)["applied_result"]["artifact_path"],
            "sha256": _job(runtime, snapshot, key)["applied_result"]["artifact_sha256"],
        }
        for key in value["audits"]
    ]
    payload["resolved_constraints"] = _current_constraints(runtime, snapshot, value)
    from .campaign_handoff_obligations import owner_spans

    complete_spans = owner_spans(
        merge_lineage_events(
            tuple(HistorySnapshot(**source) for source in value["capture"]["sources"])
        )
    )
    retained_spans = {row.get("span_id") for row in payload["resolved_constraints"]}
    if not {row["span_id"] for row in complete_spans} <= retained_spans:
        raise CampaignError("Handoff packet is missing complete native-owner span coverage")
    if value.get("constraint_jobs"):
        if not value.get("constraint_audits") or any(
            _job(runtime, snapshot, key)["state"] != "applied" for key in value["constraint_audits"]
        ):
            raise CampaignError("Reduced current constraints need independent fidelity audits")
        original = encoded(payload["resolved_constraints"])
        original_path = blob(_root(runtime, snapshot, value) / "constraint-index", original)
        payload["exact_current_constraint_index"] = {
            "path": str(original_path),
            "sha256": hashlib.sha256(original).hexdigest(),
        }
        payload["resolved_constraints"] = [
            node
            for key in value["constraint_jobs"]
            for node in _applied(_job(runtime, snapshot, key))["groups"]
        ]
        if "recovered_constraints" in payload:
            # Their complete quotations remain in the same exact index. Keeping
            # a second uncompressed copy defeats bounded packet assembly.
            payload["recovered_constraints"] = []
            payload["recovered_constraints_index"] = payload["exact_current_constraint_index"]
        tree = encoded(
            [
                _job(runtime, snapshot, key)["applied_result"]
                for key in value.get("constraint_all_jobs", [])
            ]
        )
        tree_path = blob(_root(runtime, snapshot, value) / "constraint-index", tree)
        payload["constraint_tree"] = {
            "path": str(tree_path),
            "sha256": hashlib.sha256(tree).hexdigest(),
        }
        payload["constraint_audit_receipts"] = value["constraint_audits"]
    payload["read_only"] = _permission(value) != "workspace-tools"
    payload["history_omissions_resolved_by"] = value["audits"]
    # Establish every mandatory field before allocating optional history bytes.
    # The generic read-only packet builder may omit obligations; audited writer
    # continuation cannot inherit that omission and simply clear read_only.
    payload["obligations"] = list(value["capture"]["obligations"])
    payload["latest_owner_direction"] = []
    payload["summaries"] = []
    payload["omission_index"] = str(
        blob(
            _root(runtime, snapshot, value) / "packet",
            encoded(
                {
                    "owner_ids": list(dict.fromkeys(row["source_id"] for row in complete_spans)),
                    "obligation_ids": [],
                }
            ),
        )
    )
    if value.get("history_compaction"):
        payload["history_compaction"] = {
            key: content
            for key, content in value["history_compaction"].items()
            if key != "retained_jobs"
        }
        payload["history_compaction"]["retained_job_count"] = len(
            value["history_compaction"].get("retained_jobs", [])
        )
        payload["history_compaction"]["retained_summary_index"] = payload["summary_index"]
    ceiling = min(snapshot.spec.reasoning.packet_token_limit, _budget(target, cap).payload_tokens)
    for field in (
        "current_constraint_audits",
        "constraint_audit_receipts",
        "history_omissions_resolved_by",
    ):
        if field in payload and len(encoded(payload[field])) > ceiling // 8:
            index = encoded(payload[field])
            index_path = blob(_root(runtime, snapshot, value) / "audit-index", index)
            payload[field + "_index"] = {
                "path": str(index_path),
                "sha256": hashlib.sha256(index).hexdigest(),
                "count": len(payload[field]),
            }
            payload[field] = []
    references = []
    for key in dict.fromkeys(
        [
            *value["audits"],
            *value.get("constraint_all_jobs", []),
            *value.get("recovery_jobs", []),
            *value.get("recovery_audits", []),
        ]
    ):
        row = _job(runtime, snapshot, key)
        if row["state"] != "applied":
            continue  # Failed attempts confer no authority and remain in the job ledger.
        artifact = row["applied_result"]
        references.append(
            {"path": artifact["artifact_path"], "sha256": artifact["artifact_sha256"]}
        )
        references.extend(
            {"path": source["path"], "sha256": source["sha256"]}
            for source in row["request"]["inputs"]
        )
    for field in (
        "owner_index",
        "obligation_index",
        "exact_current_constraint_index",
        "recovered_constraints_index",
        "constraint_tree",
        "current_constraint_audits_index",
        "constraint_audit_receipts_index",
        "history_omissions_resolved_by_index",
    ):
        if field in payload:
            references.append({"path": payload[field]["path"], "sha256": payload[field]["sha256"]})
    bind_authority_evidence(payload, _root(runtime, snapshot, value) / "packet", references)
    canonical_only = {
        key: content
        for key, content in payload.items()
        if key not in {"resolved_constraints", "recovered_constraints"}
    }
    if len(encoded(canonical_only)) > ceiling:
        raise CampaignError(
            "Canonical obligations and state exceed successor context; recovery required"
        )
    if len(encoded(payload)) > ceiling:
        raise ConstraintReductionNeeded(
            "Resolved current constraints exceed successor context; further bounded reduction required"
        )
    try:
        fit_audited_history(payload, _root(runtime, snapshot, value) / "packet", ceiling=ceiling)
    except AuditedConstraintsTooLarge as exc:
        raise ConstraintReductionNeeded(str(exc)) from exc
    data = encoded(payload)
    path = blob(_root(runtime, snapshot, value) / "packet", data)
    return {
        "packet": {
            "path": str(path),
            "sha256": hashlib.sha256(data).hexdigest(),
            "tokens": len(data),
            "read_only": payload["read_only"],
            "history_view_version": 1,
            "mandatory_context_sha256": mandatory_context_digest(payload),
        },
        "audits": value["audits"],
    }


def _perform(runtime, snapshot, action, *, reconcile=False):
    del reconcile
    unit = snapshot.units[action.unit]
    value = unit.evidence["handoff"]
    if action.payload.get("handoff_id", value["id"]) != value["id"]:
        raise CampaignError("Handoff action no longer owns its continuation")
    if (
        action.kind != "handoff-refresh"
        and value["phase"] not in {"drain", "capture"}
        and _obsolete_summaries(runtime, snapshot, value)
    ):
        return Observation("confirmed", {"superseded": True})
    expected_phase = EXPECTED_PHASE.get(action.kind)
    if (
        expected_phase
        and value["phase"] != expected_phase
        and not (action.kind == "handoff-assemble" and value["phase"] == "constraint_audit")
    ):
        return Observation("confirmed", {"superseded": True})
    if action.kind == "handoff-route":
        candidate = _candidate(action.payload["candidate"])
        try:
            cap = _target_cap(
                snapshot,
                candidate,
                value.get("route_role", value["role"]),
                unit=unit,
                permissions=_permission(value),
            )
        except CampaignError as exc:
            label = "Requested" if value.get("requested_candidate") else "Selected"
            raise HandoffRouteWait(f"{label} handoff target is inadmissible: {exc}") from exc
        packet = value.get("packet")
        fits = not packet or packet["tokens"] <= min(
            snapshot.spec.reasoning.packet_token_limit, _budget(candidate, cap).payload_tokens
        )
        return Observation(
            "confirmed",
            {
                "candidate": action.payload["candidate"],
                "packet_fits": fits,
                "effect_phase": value["phase"],
                "effect_context": _audit_context(snapshot, unit),
            },
        )
    if unit.claim and not unit.claim.quiescent or _debt(snapshot, unit):
        return Observation("unknown", reason="Predecessor or external writes still draining")
    if action.kind == "handoff-refresh":
        if any(
            row.get("claim") and not row["claim"].get("quiescent")
            for row in snapshot.jobs.values()
            if row["request"].get("coordinates", {}).get("handoff_id") == value["id"]
        ):
            return Observation("unknown", reason="Prior reasoning attempts still draining")
        data = _capture(runtime, snapshot, unit, value)
    elif action.kind == "handoff-final-repair":
        data = _final_repair(runtime, snapshot, unit, value)
    elif action.kind == "handoff-failed":
        data = {"exhausted_jobs": action.payload["jobs"]}
    elif action.kind in {"handoff-replan", "handoff-audit-replan", "handoff-contract-replan"}:
        field = {
            "summarize": "jobs",
            "audit": "audits",
            "constraint_reduce": "constraint_jobs",
            "constraint_audit": "constraint_audits",
            "recovery_extract": "recovery_jobs",
            "recovery_audit": "recovery_audits",
        }[value["phase"]]
        rows = [_job(runtime, snapshot, key) for key in value[field]]
        if any(row.get("claim") and not row["claim"].get("quiescent") for row in rows):
            return Observation("unknown", reason="Prior reasoning attempts still draining")
        prior_epoch = value.get("plan_epoch", 0)
        value = _retry_context(runtime, value, rows)
        planner = {
            "handoff-replan": _summary_replan,
            "handoff-audit-replan": _audit_plan,
            "handoff-contract-replan": _contract_replan,
        }[action.kind]
        try:
            data = {
                **planner(runtime, snapshot, unit, value),
                "plan_epoch": value["plan_epoch"],
                "route_cooldowns": value["route_cooldowns"],
            }
        except HandoffRouteWait as exc:
            data = {"wait_reason": str(exc), "retry_at": runtime.clock() + 30}
        data["retry_bookkeeping"] = {
            "route_cooldowns": value["route_cooldowns"],
            "route_cooldown_events": value["route_cooldown_events"],
        }
        data["retry_from_epoch"] = prior_epoch
    elif action.kind == "handoff-capture":
        data = _capture(runtime, snapshot, unit, value)
    elif action.kind == "handoff-plan":
        data = _maps(runtime, snapshot, unit, value)
    elif action.kind == "handoff-audit-plan":
        data = _audit_plan(runtime, snapshot, unit, value)
    elif action.kind == "handoff-assemble":
        try:
            data = _assemble(runtime, snapshot, unit, value)
        except ConstraintReductionNeeded:
            data = _constraint_plan(runtime, snapshot, unit, value)
    elif action.kind == "handoff-constraint-audit":
        data = _constraint_audit_plan(runtime, snapshot, unit, value)
    elif action.kind == "handoff-recovery-plan":
        data = _recovery_plan(runtime, snapshot, unit, value)
    elif action.kind == "handoff-recovery-observe":
        from .campaign_handoff_recovery import collect

        prior = value["recovery_evidence"]
        current = collect(
            runtime, snapshot, unit, value["capture"], max_bytes=prior["collection_budget"]
        )
        data = (
            {"recovery_refresh": True}
            if digest({**current, "collection_budget": prior["collection_budget"]}) != digest(prior)
            else {"recovery_unchanged": True, "retry_at": runtime.clock() + 30}
        )
    elif action.kind == "handoff-recovery-audit":
        data = _recovery_audit_plan(runtime, snapshot, unit, value)
    elif action.kind == "handoff-recovery-finish":
        row = _job(runtime, snapshot, value["recovery_audits"][0])
        result = _applied(row)
        data = {
            "recovery_finish": True,
            "ready": result["ready"],
            "artifact": row["applied_result"],
            "reason": "; ".join(result.get("unresolved", [])),
        }
    elif action.kind == "handoff-release":
        from .campaign_fingerprint import capture

        if (
            capture(
                Path(unit.bindings["workspace"]),
                source_roots=tuple(
                    Path(unit.bindings["workspace"]) / path
                    for path in value["capture"]["current"]["source_roots"]
                ),
            ).to_dict()
            != value["capture"]["current"]["fingerprint"]
        ):
            raise CampaignError("Working state changed after handoff capture")
        _target_cap(
            snapshot,
            _candidate(value["candidate"]),
            value.get("route_role", value["role"]),
            unit=unit,
            permissions=_permission(value),
        )
        data = {"ready": True}
        if value.get("recovery", {}).get("ready"):
            from .campaign_handoff_recovery import collect

            prior = value["recovery_evidence"]
            current = collect(
                runtime, snapshot, unit, value["capture"], max_bytes=prior["collection_budget"]
            )
            if digest({**current, "collection_budget": prior["collection_budget"]}) != digest(
                prior
            ):
                data = {"recovery_refresh": True}
    else:
        raise CampaignError("Unsupported handoff effect")
    return Observation(
        "confirmed",
        {**data, "effect_context": _audit_context(snapshot, unit), "effect_phase": value["phase"]},
    )


def perform(runtime, snapshot, action, *, reconcile=False):
    try:
        return _perform(runtime, snapshot, action, reconcile=reconcile)
    except HandoffRouteWait as exc:
        return Observation("confirmed", {"wait_reason": str(exc), "retry_at": runtime.clock() + 30})


def apply(runtime, snapshot, action, observation):
    from .campaign_jobs import request_from_dict

    unit = snapshot.units[action.unit]
    value = unit.evidence["handoff"]
    if action.payload.get("handoff_id", value["id"]) != value["id"]:
        add_event(
            snapshot,
            "handoff-stale-result",
            unit=unit.id,
            action=action.id,
            reason="continuation changed",
        )
        return
    data = observation.data
    if (
        data.get("superseded")
        or (
            action.kind != "handoff-refresh"
            and value["phase"] not in {"drain", "capture"}
            and _obsolete_summaries(runtime, snapshot, value)
        )
        or (
            data.get("final_repair_pending_digest") is not None
            and data["final_repair_pending_digest"] != digest(value.get("final_repair_pending"))
        )
        or data.get("retry_from_epoch", value.get("plan_epoch", 0)) != value.get("plan_epoch", 0)
        or data.get("effect_phase", value["phase"]) != value["phase"]
        or data.get("effect_context", _audit_context(snapshot, unit))
        != _audit_context(snapshot, unit)
    ):
        add_event(
            snapshot, "handoff-stale-result", unit=unit.id, action=action.id, reason="phase changed"
        )
        return
    if (
        action.kind in {"handoff-refresh", "handoff-route"}
        and value["phase"] == "ready"
        and not unit.hold
    ):
        unit.hold = Hold(
            "handoff",
            "Current constraints changed; refreshing continuation evidence",
            id=value["id"],
        )
    if not unit.hold or unit.hold.kind != "handoff" or unit.hold.id != value["id"]:
        add_event(
            snapshot,
            "handoff-stale-result",
            unit=unit.id,
            action=action.id,
            reason="another authority replaced the hold",
        )
        return
    _remember_failures(runtime, snapshot, value)
    if data.get("retry_bookkeeping"):
        for field in ("route_cooldowns", "route_cooldown_events"):
            value[field] = {**value.get(field, {}), **data["retry_bookkeeping"][field]}
    if data.get("wait_reason"):
        if action.kind == "handoff-route":
            value["route_wait"] = data["wait_reason"]
        value["retry_at"] = data["retry_at"]
        unit.hold.reason = data["wait_reason"]
        add_event(snapshot, "handoff-capacity-wait", unit=unit.id, reason=data["wait_reason"])
        return
    value.pop("retry_at", None)
    if data.get("final_repair_exhausted"):
        value["phase"] = "failed"
        unit.hold.reason = (
            "Final synthesis remains unresolved after two lifetime semantic corrections"
        )
    elif data.get("final_repair"):
        if unit.claim and not unit.claim.quiescent or _debt(snapshot, unit):
            raise CampaignError("Cannot apply final repair while original writes are uncertain")
        if any(
            row.get("claim") and not row["claim"].get("quiescent")
            for row in snapshot.jobs.values()
            if row["request"].get("coordinates", {}).get("handoff_id") == value["id"]
        ):
            raise CampaignError("Cannot replace a final while an audit or summary actor is live")
        ledger = unit.evidence.setdefault("handoff_final_repairs", {}).setdefault(
            data["final_repair_lifetime_key"], []
        )
        if data["final_repair_attempt"] != len(ledger) + 1 or len(ledger) >= 2:
            raise CampaignError("Final repair lifetime budget changed")
        request = request_from_dict(data["final_repair"])
        _prepare_with_failures(snapshot, request, value)
        ledger.append(
            {
                "attempt": data["final_repair_attempt"],
                "job_id": request.id,
                "capture_digest": value["capture"]["capture_digest"],
                "findings": value["final_repair_pending"],
                "config_revision": snapshot.config_revision,
            }
        )
        value.setdefault("audit_history", []).append(
            {"frontier": list(value["jobs"]), "audits": list(value["audits"])}
        )
        for key in value["audits"]:
            row = snapshot.jobs.get(key)
            if row is not None and row["state"] not in {"applied", "failed"}:
                row["state"] = "canceled"
        value["jobs"], value["audits"], value["phase"] = [request.id], [], "summarize"
        value["all_jobs"] = list(dict.fromkeys([*value.get("all_jobs", []), request.id]))
        value["summary_contracts"] = {
            request.id: request.coordinates["summary_spec"]["prompt_version"]
        }
        value.pop("final_repair_pending", None)
        value.pop("packet", None)
        unit.bindings.pop("handoff_ready", None)
        add_event(
            snapshot,
            "handoff-final-repair-planned",
            unit=unit.id,
            job_id=request.id,
            attempt=data["final_repair_attempt"],
            predecessor=data["final_repair_lifetime_key"],
        )
    elif data.get("exhausted_jobs"):
        value["phase"] = "failed"
        value["exhausted_jobs"] = data["exhausted_jobs"]
        unit.hold.reason = (
            "Continuation reasoning exhausted its lifetime attempt budget: "
            + ", ".join(data["exhausted_jobs"])
        )
    elif data.get("recovery_wait"):
        value["phase"] = "recovery_wait"
        value["retry_at"] = runtime.clock() + 30
        unit.hold.reason = data["reason"]
    elif data.get("recovery_unchanged"):
        value["retry_at"] = data["retry_at"]
    elif data.get("recovery_refresh"):
        value["phase"] = "capture"
        value.pop("recovery", None)
        value["plan_epoch"] = value.get("plan_epoch", 0) + 1
        unit.hold.reason = "Current recovery evidence changed; rechecking its authority"
    elif data.get("recovery_finish"):
        value["recovery"] = {
            "ready": data["ready"],
            "audit": data["artifact"],
            "capture_digest": value["capture"]["capture_digest"],
            "missing_history_retained": True,
        }
        value["phase"] = "capture" if data["ready"] else "recovery_wait"
        if not data["ready"]:
            value["retry_at"] = runtime.clock() + 30
            unit.hold.reason = data["reason"] or "Current owner constraints remain unresolved"
    elif data.get("contract_replan"):
        field = data["field"]
        for key in value[field]:
            row = snapshot.jobs.get(key)
            if row is not None and row["state"] != "applied":
                if row.get("claim") and not row["claim"].get("quiescent"):
                    raise CampaignError("Cannot replan a live continuation context job")
                row["state"] = "canceled"
        ids = list(data["reuse_jobs"])
        for raw in data["requests"]:
            request = request_from_dict(raw)
            _prepare_with_failures(snapshot, request, value)
            ids.append(request.id)
        value[field] = ids
        value["plan_epoch"] = data["plan_epoch"]
        value["route_cooldowns"] = data["route_cooldowns"]
        if field.startswith("constraint_"):
            value["constraint_all_jobs"] = list(
                dict.fromkeys([*value.get("constraint_all_jobs", []), *ids])
            )
    elif (
        data.get("constraint_reduction")
        or data.get("constraint_audit")
        or data.get("recovery_extract")
        or data.get("recovery_audit")
    ):
        ids = []
        for raw in data["requests"]:
            request = request_from_dict(raw)
            _prepare_with_failures(snapshot, request, value)
            ids.append(request.id)
        if data.get("recovery_extract"):
            value["recovery_jobs"], value["phase"] = ids, "recovery_extract"
            value["recovery_evidence"] = data["recovery_evidence"]
        elif data.get("recovery_audit"):
            value["recovery_audits"], value["phase"] = ids, "recovery_audit"
        elif data.get("constraint_reduction"):
            value["constraint_level"] = data["constraint_level"]
            value["constraint_jobs"], value["phase"] = ids, "constraint_reduce"
            value["constraint_audits"] = []
        else:
            value["constraint_audits"], value["phase"] = ids, "constraint_audit"
        if data.get("constraint_reduction") or data.get("constraint_audit"):
            value["constraint_all_jobs"] = list(
                dict.fromkeys([*value.get("constraint_all_jobs", []), *ids])
            )
    elif action.kind == "handoff-route":
        value.pop("route_wait", None)
        value["candidate"] = data["candidate"]
        if value["phase"] in {"ready", "assemble"}:
            if data.get("packet_fits", True):
                if value["phase"] == "ready":
                    unit.hold = None
            else:
                value["phase"] = "audit"
                value.pop("packet", None)
                unit.bindings.pop("handoff_ready", None)
    elif action.kind in {"handoff-capture", "handoff-refresh"}:
        if action.kind == "handoff-refresh":
            if value.get("final_repair_pending"):
                value.setdefault("invalidated_final_rejections", []).append(
                    value.pop("final_repair_pending")
                )
            value["plan_epoch"] = value.get("plan_epoch", 0) + 1
            for key in [
                *value["audits"],
                *value.get("constraint_jobs", []),
                *value.get("constraint_audits", []),
                *value.get("recovery_jobs", []),
                *value.get("recovery_audits", []),
            ]:
                row = snapshot.jobs.get(key)
                if row is None:
                    continue
                if row.get("claim") and not row["claim"].get("quiescent"):
                    raise CampaignError("Cannot refresh a live handoff audit")
                if row["state"] != "applied":
                    row["state"] = "canceled"
            for field in (
                "history_compaction",
                "reduction_frontier",
                "constraint_jobs",
                "constraint_audits",
                "constraint_all_jobs",
                "constraint_level",
                "recovery_jobs",
                "recovery_audits",
                "recovery",
                "recovery_evidence",
            ):
                value.pop(field, None)
        if value["role"] == "execution" and data.get("native_role") != value.get("native_role"):
            value["native_role"] = data["native_role"]
            value["route_role"] = (
                "execution_review" if data["native_role"] == "review" else "execution"
            )
            previous = value["original_payload"].get(
                "review_candidate" if data["native_role"] == "review" else "candidate", {}
            )
            value["previous_route"] = [previous.get("profile", ""), previous.get("model", "")]
            target = (
                _fallback(
                    snapshot,
                    value["route_role"],
                    tuple(value["previous_route"]),
                    value.get("requested_candidate"),
                    unit=unit,
                    permissions=_permission(value),
                )
                if value["route_role"] in dict(snapshot.spec.roles)
                else None
            )
            value["candidate"] = dataclasses.asdict(target) if target else None
        value["capture"], value["phase"] = data, "capture"
    elif action.kind in {
        "handoff-plan",
        "handoff-audit-plan",
        "handoff-replan",
        "handoff-audit-replan",
    }:
        if data.get("invalidated_warmup_jobs"):
            add_event(
                snapshot,
                "handoff-warmup-invalidated",
                unit=unit.id,
                jobs=data["invalidated_warmup_jobs"],
            )
        ids = list(data.get("reuse_jobs", []))
        if action.kind in {"handoff-replan", "handoff-audit-replan"}:
            value["plan_epoch"] = data["plan_epoch"]
            value["route_cooldowns"] = data.get("route_cooldowns", {})
            for key in value["jobs" if action.kind == "handoff-replan" else "audits"]:
                if key in ids:
                    continue
                row = snapshot.jobs.get(key)
                if row is None:
                    continue
                if row.get("claim") and not row["claim"].get("quiescent"):
                    raise CampaignError("Cannot replan a live historical extraction")
                if row["state"] != "applied":
                    row["state"] = "canceled"
        for raw in data["requests"]:
            request = request_from_dict(raw)
            _prepare_with_failures(snapshot, request, value)
            ids.append(request.id)
        field, phase = (
            ("jobs", "summarize")
            if action.kind in {"handoff-plan", "handoff-replan"} or data.get("reduction")
            else ("audits", "audit")
        )
        if action.kind in {"handoff-plan", "handoff-replan"} and not data.get("reduction_replan"):
            value["map_jobs"] = ids
        if field == "jobs":
            value["all_jobs"] = list(dict.fromkeys([*value.get("all_jobs", []), *ids]))
            value["summary_contracts"] = {
                key: _job(runtime, snapshot, key)["request"]["coordinates"]["summary_spec"][
                    "prompt_version"
                ]
                for key in ids
            }
        if data.get("reduction"):
            value["reduction_level"] = value.get("reduction_level", 0) + 1
            if data.get("reduction_frontier"):
                value["reduction_frontier"] = data["reduction_frontier"]
            value.pop("history_compaction", None)
        if data.get("history_compaction"):
            value["history_compaction"] = data["history_compaction"]
            add_event(
                snapshot, "handoff-compaction-limited", unit=unit.id, **data["history_compaction"]
            )
        value[field], value["phase"] = ids, phase
    elif action.kind == "handoff-assemble":
        value["packet"], value["phase"] = data["packet"], "assemble"
    elif action.kind == "handoff-release":
        if not unit.claim or not unit.claim.quiescent:
            raise CampaignError("Cannot release handoff while predecessor quiescence is unknown")
        value["phase"] = "ready"
        if unit.state in {UnitState.CLAIMED, UnitState.RUNNING}:
            transition(snapshot, unit.id, UnitState.READY)
        unit.hold = None
        unit.bindings["handoff_ready"] = value["id"]
    add_event(
        snapshot, "handoff-progress", unit=unit.id, handoff_id=value["id"], phase=value["phase"]
    )


def resume_proposal(snapshot, unit, runtime=None):
    value = unit.evidence.get("handoff") or {}
    if (
        _obsolete_summaries(runtime, snapshot, value)
        or value.get("phase") != "ready"
        or value["capture"].get("audit_context") != _audit_context(snapshot, unit)
        or value.get("packet", {}).get("read_only") != (_permission(value) != "workspace-tools")
        or (unit.claim and unit.claim.launch_id != value["predecessor"]["launch_id"])
    ):
        return None
    if _pending(snapshot, unit, "handoff-route") or (
        runtime is not None and _available_alternative(runtime, snapshot, value) is not None
    ):
        return None
    try:
        _target_cap(
            snapshot,
            _candidate(value["candidate"]),
            value.get("route_role", value["role"]),
            unit=unit,
            permissions=_permission(value),
        )
    except CampaignError:
        return None  # The durable route action reports the wait and can recover it.
    payload = dict(value["original_payload"])
    payload["read_only"] = _permission(value) != "workspace-tools"
    target_key = (
        "review_candidate" if value.get("route_role") == "execution_review" else "candidate"
    )
    payload[target_key] = value["candidate"]
    payload["role"] = value["role"]
    payload["handoff_id"] = value["id"]
    return Proposal("launch", unit.id, payload)


def decorate_launch(snapshot, unit, request):
    value = unit.evidence.get("handoff") or {}
    if value.get("phase") not in {"ready", "continuing"}:
        return
    if value["phase"] == "ready" and _obsolete_summaries(None, snapshot, value):
        raise CampaignError("Continuation summary contract changed before launch")
    if value["phase"] == "continuing" and not request.get("handoff_id"):
        return  # later verification/review/publication stages use ordinary fresh input
    if value["phase"] == "continuing" and request.get("launch_id") != value.get(
        "successor", {}
    ).get("launch_id"):
        raise CampaignError("Only the adopted successor may consume the continuation packet")
    if value["capture"].get("audit_context") != _audit_context(snapshot, unit):
        raise CampaignError("Current continuation constraints changed before launch")
    if request.get("handoff_id") != value["id"]:
        raise CampaignError("Replacement launch must explicitly claim its ready handoff")
    if request.get("role", value["role"]) != value["role"]:
        raise CampaignError("Handoff cannot turn review or planning into development")
    _target_cap(
        snapshot,
        _candidate(value["candidate"]),
        value.get("route_role", value["role"]),
        unit=unit,
        permissions=_permission(value),
    )
    read_only = _permission(value) != "workspace-tools"
    if request.get("read_only", read_only) != read_only:
        raise CampaignError("Handoff successor changed the frozen permission boundary")
    if value["packet"].get("read_only") != read_only:
        raise CampaignError("Handoff packet changed the frozen permission boundary")
    request["read_only"] = read_only
    if (
        unit.claim
        and not unit.claim.quiescent
        and unit.claim.launch_id == value["predecessor"]["launch_id"]
    ):
        raise CampaignError("Predecessor still owns the writer lease")
    request["handoff_packet"] = value["packet"]
    packet_path = Path(value["packet"]["path"])
    root = packet_path.parent.parent
    from .platform_util import is_link_like

    if root.name != value["id"] or is_link_like(root) or is_link_like(packet_path):
        raise CampaignError("Handoff retrieval root is not bound to the owned continuation")
    if hashlib.sha256(packet_path.read_bytes()).hexdigest() != value["packet"]["sha256"]:
        raise CampaignError("Handoff packet changed before launch")
    packet = read_json(packet_path)
    expected_view = value["packet"].get("history_view_version")
    if expected_view is not None and (
        type(expected_view) is not int
        or expected_view != 1
        or type(packet.get("history_view_version")) is not int
        or packet.get("history_view_version") != expected_view
    ):
        raise CampaignError("Handoff history view version differs from its durable packet")
    if packet.get("read_only") != read_only:
        raise CampaignError("Handoff packet content changed the frozen permission boundary")
    if packet.get("obligations") != list(value["capture"]["obligations"]):
        raise CampaignError("Handoff packet omitted or changed canonical obligations")
    from .campaign_handoff_context import validate_authority_evidence, validate_history_view

    validate_history_view(packet)
    validate_authority_evidence(packet, value["packet"].get("mandatory_context_sha256"))
    grants = request.get("headless_grants")
    if grants is not None:
        grants["read_only_paths"] = [
            *grants.get("read_only_paths", []),
            {
                "path": str(root.resolve()),
                "capture_digest": value["capture"]["capture_digest"],
                "packet_sha256": value["packet"]["sha256"],
            },
        ]
    request["native_predecessor"] = value["predecessor"]
    target_key = (
        "review_candidate" if value.get("route_role") == "execution_review" else "candidate"
    )
    request[target_key] = value["candidate"]
    current = value["capture"]["current"]
    request["handoff_state"] = {
        "capture_digest": value["capture"]["capture_digest"],
        "fingerprint": current["fingerprint"],
        "native_role": value.get("native_role", ""),
        **{
            key: current.get(key)
            for key in (
                "original_run_dir",
                "original_run_id",
                "source_roots",
                "original_state_path",
                "original_state_sha256",
                "predecessor_receipt_path",
                "predecessor_receipt_sha256",
            )
        },
    }
    request["headless_capabilities"] = snapshot.config_snapshot.get("reasoning_capabilities", {})
    # Claim adoption marks continuation after the durable replacement launch,
    # not while merely constructing a launch request.


def adopt_launch(snapshot, unit, payload, claim):
    """Record the replacement only in the same transaction that saves its claim."""
    value = unit.evidence.get("handoff") or {}
    if not payload.get("handoff_id"):
        return
    if value.get("phase") != "ready" or payload["handoff_id"] != value["id"]:
        raise CampaignError("Replacement launch does not own a ready continuation")
    if value["capture"].get("audit_context") != _audit_context(snapshot, unit):
        raise CampaignError("Current continuation constraints changed before claim adoption")
    target_key = (
        "review_candidate" if value.get("route_role") == "execution_review" else "candidate"
    )
    if not payload.get(target_key) or _candidate(payload[target_key]) != _candidate(
        value["candidate"]
    ):
        raise CampaignError("Replacement candidate differs from approved handoff route")
    if claim.launch_id == value["predecessor"]["launch_id"] or claim.role != value["role"]:
        raise CampaignError("Replacement must own a fresh launch with the original role")
    _target_cap(
        snapshot,
        _candidate(value["candidate"]),
        value.get("route_role", value["role"]),
        unit=unit,
        permissions=_permission(value),
    )
    read_only = _permission(value) != "workspace-tools"
    if payload.get("read_only", read_only) != read_only or claim.writes == read_only:
        raise CampaignError("Replacement claim changed the frozen permission boundary")
    value["phase"] = "continuing"
    value["successor"] = dataclasses.asdict(claim)
    unit.bindings.pop("handoff_ready", None)
    unit.evidence.setdefault("session_lineage", []).append(
        {
            "handoff_id": value["id"],
            "predecessor": value["predecessor"]["launch_id"],
            "successor": claim.launch_id,
            "candidate": value["candidate"],
            "packet": value["packet"],
            "native_session_imported": False,
        }
    )
    add_event(
        snapshot,
        "handoff-continuing",
        unit=unit.id,
        predecessor=value["predecessor"]["launch_id"],
        successor=claim.launch_id,
    )


def job_launch_allowed(snapshot, row):
    """Avoid relaunching obsolete geometry during quota replan/current-context refresh."""
    coordinates = row["request"].get("coordinates", {})
    if not coordinates.get("handoff_id"):
        return True
    unit = snapshot.units.get(row["request"]["unit"])
    value = unit.evidence.get("handoff", {}) if unit else {}
    if (
        value.get("id") != coordinates["handoff_id"]
        or _requires_replan(row)
        or value.get("final_repair_pending")
    ):
        return False
    if value.get("capture", {}).get("audit_context") != _audit_context(snapshot, unit):
        return False
    field = coordinates.get("handoff_job_field") or (
        "audits" if row["request"]["purpose"] == "handoff_audit" else "jobs"
    )
    phase = {
        "constraint_jobs": "constraint_reduce",
        "constraint_audits": "constraint_audit",
        "recovery_jobs": "recovery_extract",
        "recovery_audits": "recovery_audit",
    }.get(field, "audit" if field == "audits" else "summarize")
    return row["id"] in value.get(field, ()) and value.get("phase") == (phase)
