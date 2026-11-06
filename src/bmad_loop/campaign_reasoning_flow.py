"""Campaign reasoning effects: short launches, observation and durable claims.

This collaborator shares global/role/provider admission with coding workers but
never borrows a unit's write claim. Every effect carries a distinct job identity.
"""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path

from .campaign_jobs import TERMINAL, accept_receipt, current, live, request_from_dict
from .campaign_model import CampaignError, RoleCandidate
from .campaign_store import add_event
from .campaign_supervisor import Observation, Proposal

PREFIX = "reasoning-"
WORKSPACE_WAIT = "Waiting for an owned Paradev workspace; unit allocation will resume this job"


def presentation(snapshot, request):
    """Bind read-only jobs to an existing owned workspace, never the coordinator's cwd."""
    if not snapshot.spec.paradev.reflect:
        return None
    from .campaign_reflection_flow import workspace_binding

    units = (
        [snapshot.units[request.unit]]
        if request.unit in snapshot.units
        else sorted(
            snapshot.units.values(), key=lambda unit: (unit.spec.kind != "planning", unit.id)
        )
    )
    for unit in units:
        if unit.bindings.get("native"):
            binding = workspace_binding(unit)
            return {"workspace": binding.path, "workspace_id": binding.workspace_id}
    return None


def pending(snapshot, job_id: str, kind: str) -> bool:
    return any(
        action.kind == kind
        and action.payload.get("job_id") == job_id
        and action.state in {"prepared", "sent_unknown"}
        for action in snapshot.outbox
    )


def outstanding(snapshot) -> bool:
    return any(
        live(row) or (row["request"]["required"] and row["state"] not in TERMINAL)
        for row in snapshot.jobs.values()
    )


def proposals(runtime, snapshot) -> tuple[Proposal, ...]:
    result: list[Proposal] = []
    semantic_fingerprints: dict[str, str] = {}
    active = sum(live(row) for row in snapshot.jobs.values())
    sessions = (
        sum(
            bool(unit.claim and not unit.claim.quiescent and unit.claim.resource != "verify")
            for unit in snapshot.units.values()
        )
        + active
    )
    selected: list[tuple[str, RoleCandidate]] = []
    for job_id, row in sorted(
        snapshot.jobs.items(), key=lambda item: (item[1]["request"]["created_at"], item[0])
    ):
        request = request_from_dict(row["request"])
        if live(row):
            kind = (
                "reasoning-terminate"
                if snapshot.bindings.get("terminate_requested")
                else "reasoning-observe"
            )
            if (
                not pending(snapshot, job_id, kind)
                and runtime.clock() - row.get("observed_at", 0) >= 1
            ):
                result.append(Proposal(kind, request.unit, {"job_id": job_id}))
            continue
        if row["state"] in TERMINAL or snapshot.phase != "active":
            continue
        # Canonical context can change without a graph edit (a hold, remote
        # answer, head or delivery). Retire obsolete advice before retry clocks
        # or exhausted budgets strand its payload in hot authority forever.
        if not current(snapshot, request, semantic_fingerprints=semantic_fingerprints):
            if not pending(snapshot, job_id, "reasoning-stale"):
                result.append(Proposal("reasoning-stale", request.unit, {"job_id": job_id}))
            continue
        if row["state"] == "validated":
            if not pending(snapshot, job_id, "reasoning-apply"):
                result.append(Proposal("reasoning-apply", request.unit, {"job_id": job_id}))
            continue
        if (
            row["state"] == "failed"
            and row.get("failure_attempts", 0) >= snapshot.spec.reasoning.max_attempts
        ):
            continue
        if row.get("retry_at") and row["retry_at"] > runtime.clock():
            continue
        if pending(snapshot, job_id, "reasoning-launch"):
            continue
        if snapshot.spec.paradev.reflect and presentation(snapshot, request) is None:
            # Preparing the first unit supplies a native workspace. Optional
            # portfolio jobs wait without consuming a failed-attempt budget.
            if row.get("reason") != WORKSPACE_WAIT and not pending(
                snapshot, job_id, "reasoning-wait"
            ):
                result.append(Proposal("reasoning-wait", request.unit, {"job_id": job_id}))
            continue
        if request.purpose.startswith("handoff_") and request.coordinates.get("handoff_id"):
            from .campaign_handoff_flow import job_launch_allowed

            if not job_launch_allowed(snapshot, row):
                continue
        dependencies = []
        for dependency in request.dependencies:
            known = snapshot.jobs.get(dependency)
            if known is None:
                saved = runtime.store.jobs(snapshot.id, job_id=dependency, limit=1)
                known = saved[0] if saved else None
            dependencies.append(known)
        if any(not dependency or dependency["state"] != "applied" for dependency in dependencies):
            continue
        if (
            active >= snapshot.spec.reasoning.max_sessions
            or sessions >= snapshot.spec.resources.max_sessions
        ):
            continue
        candidate = runtime._role(
            snapshot,
            request.role,
            provisional=tuple(selected),
            allowed_candidates=request.candidates,
        )
        if candidate is None or candidate not in request.candidates:
            continue
        result.append(
            Proposal(
                "reasoning-launch", request.unit, {"job_id": job_id, "candidate": asdict(candidate)}
            )
        )
        selected.append((request.role, candidate))
        sessions += 1
        active += 1
    return tuple(result)


def prepare_effect(runtime, snapshot, action) -> None:
    row = snapshot.jobs[action.payload["job_id"]]
    if action.kind != "reasoning-launch":
        action.payload["job_binding"] = _binding(row)
        return
    if live(row):
        raise CampaignError("reasoning job already has an unreconciled claim")
    request = request_from_dict(row["request"])
    candidate = RoleCandidate(
        **{
            **action.payload["candidate"],
            "capacity_command": tuple(action.payload["candidate"].get("capacity_command", ())),
        }
    )
    if candidate not in request.candidates:
        raise CampaignError("reasoning launch candidate is not authorized")
    row["attempt"] += 1
    directory = runtime.store.directory(snapshot.id) / "jobs" / row["id"] / str(row["attempt"])
    row["claim"] = {
        "launch_id": action.id,
        "attempt": row["attempt"],
        "generation": action.generation,
        "quiescent": False,
        "run_dir": str(directory),
        "started_at": runtime.clock(),
        "status": "prepared",
    }
    if snapshot.spec.paradev.reflect:
        view = presentation(snapshot, request)
        if view is None:
            raise CampaignError("Reasoning terminal awaits an owned native workspace")
        row["claim"]["presentation"] = view
    row["selection"] = action.payload["candidate"]
    row["receipt"] = None
    row["reason"] = ""
    row["state"] = "prepared"
    row["updated_at"] = runtime.clock()
    action.payload["job_binding"] = _binding(row)
    selections = snapshot.bindings.setdefault("role_selections", {})
    key = candidate.profile + "\0" + candidate.model
    selections[key] = selections.get(key, 0) + 1
    add_event(
        snapshot, "reasoning-prepared", unit=request.unit, job_id=row["id"], launch_id=action.id
    )


def _runner(runtime, snapshot):
    from .campaign_headless import HeadlessRunner

    factory = getattr(runtime, "reasoning_runner", None)
    if factory is not None:
        return factory
    return HeadlessRunner(
        mux=runtime._native_mux(snapshot) if snapshot.spec.paradev.reflect else None
    )


def _binding(row):
    claim = row.get("claim") or {}
    return {
        "request_digest": row["request_digest"],
        **{key: claim.get(key) for key in ("launch_id", "generation", "attempt")},
    }


def adopt_prepared_generation(snapshot, action):
    """Rebind only never-sent effects for the exact adopted launch claim."""
    row = snapshot.jobs.get(action.payload.get("job_id", ""))
    claim = (row or {}).get("claim") or {}
    if (
        action.kind != "reasoning-launch"
        or claim.get("status") != "prepared"
        or claim.get("launch_id") != action.id
    ):
        return
    prior = _binding(row)
    claim["generation"] = action.generation
    for pending_action in snapshot.outbox:
        if (
            pending_action.state == "prepared"
            and pending_action.payload.get("job_binding") == prior
        ):
            pending_action.payload["job_binding"] = _binding(row)


def _effect_row(runtime, snapshot, action):
    identity = action.payload["job_id"]
    row = snapshot.jobs.get(identity)
    if row is not None:
        return row
    saved = runtime.store.jobs(snapshot.id, job_id=identity, limit=1)
    if saved and saved[0]["state"] in TERMINAL:
        return saved[0]  # Detached immutable history, never reinsert into hot authority.
    raise CampaignError("reasoning effect has no current or terminal job binding")


def _superseded(row, action, data=None):
    expected = action.payload.get("job_binding") or (data or {}).get("effect_binding")
    if expected is not None:
        return expected != _binding(row)
    # Older persisted launch effects still carry their exact native claim ID.
    return action.kind == "reasoning-launch" and (
        (row.get("claim") or {}).get("launch_id") != action.id
        or (row.get("claim") or {}).get("generation") != action.generation
    )


def perform(runtime, snapshot, action, *, reconcile: bool = False) -> Observation:
    from .campaign_headless import capability_for

    row = _effect_row(runtime, snapshot, action)
    if row["state"] in TERMINAL or _superseded(row, action):
        return Observation("confirmed", {"state": "settled", "effect_binding": _binding(row)})
    request = request_from_dict(row["request"])
    if action.kind in {"reasoning-stale", "reasoning-apply", "reasoning-wait"}:
        return Observation("confirmed", {})
    claim = row.get("claim") or {}
    path = Path(claim["run_dir"]) / "request.json"
    runner = _runner(runtime, snapshot)
    if action.kind == "reasoning-launch":
        if not path.exists():
            candidate = RoleCandidate(
                **{
                    **row["selection"],
                    "capacity_command": tuple(row["selection"].get("capacity_command", ())),
                }
            )
            routes = snapshot.config_snapshot.get("reasoning_capabilities", {})
            try:
                capability = capability_for(
                    routes, candidate.profile, candidate.model, "no-tools", effort=candidate.effort
                )
                path = runner.prepare(
                    path.parent,
                    request,
                    candidate,
                    capability,
                    launch_id=claim["launch_id"],
                    generation=claim["generation"],
                    presentation=claim.get("presentation"),
                )
            except (CampaignError, OSError, ValueError) as exc:
                # No launch has been attempted; this is a typed preparation
                # failure, not evidence that a previously started process died.
                if (path.parent / "launch-intent.json").exists():
                    return Observation("unknown", reason=str(exc), retry_at=runtime.clock() + 5)
                return Observation("confirmed", {"state": "preparation_failed", "reason": str(exc)})
        observed = (
            runner.observe(path)
            if reconcile and (path.parent / "launch-intent.json").exists()
            else runner.launch(path)
        )
    elif action.kind == "reasoning-terminate":
        if not path.exists():
            return Observation(
                "unknown",
                reason="reasoning launch has no owned request",
                retry_at=runtime.clock() + 5,
            )
        observed = runner.terminate(path)
    elif action.kind == "reasoning-observe":
        if not path.exists():
            return Observation(
                "unknown", reason="reasoning request is not yet bound", retry_at=runtime.clock() + 5
            )
        observed = runner.observe(path)
    else:
        raise CampaignError("unsupported reasoning effect")
    if observed["state"] == "unknown":
        return Observation(
            "unknown",
            observed,
            reason=observed.get("reason", "reasoning ownership uncertain"),
            retry_at=runtime.clock() + 5,
        )
    return Observation("confirmed", {**observed, "effect_binding": _binding(row)})


def apply(runtime, snapshot, action, observation) -> None:
    row = _effect_row(runtime, snapshot, action)
    superseded = _superseded(row, action, observation.data)
    if (
        not superseded
        and observation.data.get("state") == "finished"
        and row.get("receipt") is not None
        and observation.data.get("receipt") != row["receipt"]
    ):
        raise CampaignError("reasoning attempt already has a different terminal receipt")
    if (
        row["state"] in TERMINAL
        or superseded
        or observation.data.get("state") == "settled"
        or (
            row.get("receipt") is not None
            and action.kind in {"reasoning-launch", "reasoning-observe", "reasoning-terminate"}
        )
    ):
        # Another observer may have already completed the exact native claim,
        # applied its result and archived the job while launch's ACK was in flight.
        # Acknowledge only the effect; never rewrite or resurrect that job.
        add_event(
            snapshot,
            "reasoning-observation-retired",
            unit=row["request"].get("unit", ""),
            job_id=row["id"],
            action=action.id,
            reason="job is terminal or its native claim changed",
        )
        return
    request = request_from_dict(row["request"])
    data = observation.data
    if action.kind == "reasoning-wait":
        if not live(row):
            row["reason"] = WORKSPACE_WAIT
    elif action.kind == "reasoning-stale":
        if live(row):
            raise CampaignError("cannot invalidate a live reasoning claim")
        row["state"], row["validation"], row["reason"] = (
            "stale",
            "stale",
            "reasoning inputs changed",
        )
    elif action.kind == "reasoning-apply":
        if row["state"] != "validated" or not current(snapshot, request):
            row["state"], row["validation"] = "stale", "stale"
        else:
            from .campaign_reasoning_results import apply_result

            try:
                apply_result(runtime, snapshot, row)
            except CampaignError as exc:
                row["application"], row["state"], row["reason"] = "rejected", "failed", str(exc)
                row["failure_attempts"] = row.get("failure_attempts", 0) + 1
                row["retry_at"] = runtime.clock() + 15
            else:
                row["application"], row["state"] = "applied", "applied"
    elif data.get("state") == "preparation_failed":
        row["claim"]["quiescent"] = True
        row["state"], row["reason"] = "failed", data["reason"]
        row["failure_attempts"] = snapshot.spec.reasoning.max_attempts
    elif data.get("state") == "finished":
        first_receipt = row.get("receipt") is None
        accept_receipt(snapshot, row["id"], data["receipt"], now=runtime.clock())
        if first_receipt:
            usage = data["receipt"].get("usage") or {}
            ledger = snapshot.bindings.setdefault(
                "reasoning_usage", {"totals": {}, "complete_cost": True}
            )
            for name, value in usage.items():
                ledger["totals"][name] = ledger["totals"].get(name, 0) + value
            cost = usage.get("cost_usd")
            role_spend = snapshot.bindings.setdefault("role_spend", {})
            if cost is None:
                ledger["complete_cost"] = False
                snapshot.bindings["spend_usd"] = None
                role_spend[request.role] = None
            else:
                # Unknown earlier currency usage remains unknown. A later
                # priced receipt cannot reconstruct another harness's cost.
                spent = snapshot.bindings.get("spend_usd", 0)
                role_total = role_spend.get(request.role, 0)
                snapshot.bindings["spend_usd"] = None if spent is None else spent + cost
                role_spend[request.role] = None if role_total is None else role_total + cost
        if row["state"] == "failed":
            row["failure_attempts"] = row.get("failure_attempts", 0) + 1
            row["retry_at"] = runtime.clock() + 15
        elif row["state"] == "capacity_wait":
            row["retry_at"] = runtime.clock() + 30
            from .campaign_capacity import record_unavailable

            selected = row.get("selection", {})
            record_unavailable(
                snapshot,
                selected.get("profile", ""),
                selected.get("model", ""),
                now=runtime.clock(),
                reason="native quota exhausted; waiting for newer provider capacity",
            )
    else:
        row["state"] = "running"
        row["claim"]["status"] = "running"
        row["claim"].update(
            {
                key: data[key]
                for key in ("native_id", "run_dir", "presentation_status")
                if key in data
            }
        )
    row["observed_at"] = runtime.clock()
    row["updated_at"] = runtime.clock()
    add_event(
        snapshot,
        "reasoning-observed",
        unit=request.unit,
        job_id=row["id"],
        state=row["state"],
        reason=row.get("reason", ""),
    )
