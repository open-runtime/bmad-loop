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
        if not current(snapshot, request):
            result.append(Proposal("reasoning-stale", request.unit, {"job_id": job_id}))
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
    row["selection"] = action.payload["candidate"]
    row["receipt"] = None
    row["state"] = "prepared"
    row["updated_at"] = runtime.clock()
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


def perform(runtime, snapshot, action, *, reconcile: bool = False) -> Observation:
    from .campaign_headless import capability_for

    row = snapshot.jobs[action.payload["job_id"]]
    request = request_from_dict(row["request"])
    if action.kind in {"reasoning-stale", "reasoning-apply"}:
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
                capability = capability_for(routes, candidate.profile, candidate.model, "no-tools")
                path = runner.prepare(
                    path.parent,
                    request,
                    candidate,
                    capability,
                    launch_id=claim["launch_id"],
                    generation=claim["generation"],
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
    return Observation("confirmed", observed)


def apply(runtime, snapshot, action, observation) -> None:
    row = snapshot.jobs[action.payload["job_id"]]
    request = request_from_dict(row["request"])
    data = observation.data
    if action.kind == "reasoning-stale":
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
        accept_receipt(snapshot, row["id"], data["receipt"], now=runtime.clock())
        if row["state"] == "failed":
            row["failure_attempts"] = row.get("failure_attempts", 0) + 1
            row["retry_at"] = runtime.clock() + 15
        elif row["state"] == "capacity_wait":
            row["retry_at"] = runtime.clock() + 30
            snapshot.bindings.setdefault("capacity", {})["observed_at"] = 0
    else:
        row["state"] = "running"
        row["claim"]["status"] = "running"
        row["claim"].update({key: data[key] for key in ("native_id", "run_dir") if key in data})
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
