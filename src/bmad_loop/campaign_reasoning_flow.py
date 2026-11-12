"""Campaign reasoning effects: short launches, observation and durable claims.

This collaborator shares global/role/provider admission with coding workers but
never borrows a unit's write claim. Every effect carries a distinct job identity.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path

from .campaign_jobs import TERMINAL, accept_receipt, current, live, request_from_dict
from .campaign_model import CampaignError, RoleCandidate
from .campaign_store import add_event
from .campaign_supervisor import Observation, Proposal
from .platform_util import is_link_like

PREFIX = "reasoning-"
WORKSPACE_WAIT = "Waiting for an owned Paradev workspace; unit allocation will resume this job"


def admission_reason(snapshot, unit_id):
    """Controls prevent new reasoning sessions, never observation of existing ones."""
    from .campaign_transfer import retired

    if retired(snapshot):
        return "campaign ownership was transferred"
    if snapshot.phase != "active":
        return "campaign is not active"
    unit = snapshot.units.get(unit_id)
    if unit and unit.hold and unit.hold.kind == "operator":
        return "unit is held by the operator"
    return ""


def _deferred(row, reason):
    return Observation(
        "confirmed", {"state": "deferred", "reason": reason, "effect_binding": _binding(row)}
    )


def _launch_guard(runtime, snapshot, row, action, path):
    """Recheck committed control authority and the immediate scoped hold channel.

    A native intent is already an uncertain external effect. Its real outcome
    must be observed even if a hold/stop arrived while preparation was running.
    """
    from . import campaign_hold

    if (path.parent / "launch-intent.json").exists():
        return None
    try:
        latest = runtime.store.load(snapshot.id)
    except (CampaignError, OSError, ValueError) as exc:
        return Observation("unknown", reason=str(exc), retry_at=runtime.clock() + 5)
    current_row = latest.jobs.get(row["id"])
    if current_row is None or _binding(current_row) != _binding(row):
        return Observation("confirmed", {"state": "settled", "effect_binding": _binding(row)})
    request = request_from_dict(current_row["request"])
    reason = (
        admission_reason(latest, request.unit)
        if current(latest, request)
        else "reasoning request is no longer current"
    )
    if not reason and request.coordinates.get("handoff_id"):
        from .campaign_handoff_flow import job_launch_allowed

        if not job_launch_allowed(latest, current_row):
            reason = "handoff no longer admits this reasoning frontier"
    if not reason and request.coordinates.get("history_warmup"):
        from .campaign_history_warmup import launch_allowed

        if not launch_allowed(latest, request):
            reason = "history warmup no longer admits new sessions"
    if not reason and campaign_hold.active(
        campaign_hold.channel(runtime.store.directory(snapshot.id), row["request"]["unit"]),
        campaign=snapshot.id,
        unit=row["request"]["unit"],
    ):
        reason = "unit is held by the operator channel"
    if not reason:
        owned = next((item for item in latest.outbox if item.id == action.id), None)
        if (
            owned is None
            or owned.state not in {"prepared", "sent_unknown"}
            or (current_row.get("claim") or {}).get("quiescent")
        ):
            reason = "reasoning launch no longer owns admission"
    return _deferred(row, reason) if reason else None


class _LaunchDeferred(Exception):
    def __init__(self, observation):
        self.observation = observation


@contextmanager
def _admission(runtime, snapshot, row, action, path):
    from .platform_util import file_lock

    # Store.save is the control acknowledgement boundary. No terminal RPC or
    # capability validation may run while this repository-wide lock is held.
    with file_lock(runtime.store.root / ".state.lock"):
        deferred = _launch_guard(runtime, snapshot, row, action, path)
        if deferred is not None:
            raise _LaunchDeferred(deferred)
        yield


def defer_never_submitted(runtime, snapshot, action):
    """Cancel only an exact durably prepared attempt, preserving resumability."""
    row = snapshot.jobs.get(action.payload.get("job_id"))
    claim = (row or {}).get("claim") or {}
    if (
        action.state != "prepared"
        or not row
        or row["state"] != "prepared"
        or claim.get("quiescent")
        or claim.get("status") != "prepared"
        or claim.get("launch_id") != action.id
        or _superseded(row, action)
        or (Path(claim["run_dir"]) / "launch-intent.json").exists()
    ):
        return False
    apply(runtime, snapshot, action, _deferred(row, "campaign drained before launch submission"))
    if not claim.get("quiescent"):
        return False
    action.state = "rejected"
    action.error = row["reason"]
    return True


def presentation(snapshot, request=None):
    """Bind read-only jobs to an existing owned workspace, never the coordinator's cwd."""
    if not snapshot.spec.paradev.reflect:
        return None
    from .campaign_reflection_flow import workspace_binding
    from .campaign_titles import work_title

    unit_id = request.unit if request is not None else ""
    units = (
        [snapshot.units[unit_id]]
        if unit_id in snapshot.units
        else sorted(
            snapshot.units.values(), key=lambda unit: (unit.spec.kind != "planning", unit.id)
        )
    )
    for unit in units:
        if unit.bindings.get("native"):
            binding = workspace_binding(unit)
            return {
                "workspace": binding.path,
                "workspace_id": binding.workspace_id,
                "navigation_prefix": (
                    work_title(unit, snapshot=snapshot)
                    if unit_id in snapshot.units
                    else "Portfolio"
                ),
            }
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
        snapshot.jobs.items(),
        key=lambda item: (
            not item[1]["request"]["required"],
            item[1]["request"]["created_at"],
            item[0],
        ),
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
        if row["state"] in TERMINAL or admission_reason(snapshot, request.unit):
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
        if request.coordinates.get("history_warmup"):
            from .campaign_history_warmup import launch_allowed

            if not launch_allowed(snapshot, request):
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
        from .campaign_summary_routes import validate_request

        try:
            stage = validate_request(snapshot, request)
        except CampaignError as exc:
            if row.get("reason") != str(exc) and not pending(snapshot, job_id, "reasoning-wait"):
                result.append(
                    Proposal("reasoning-wait", request.unit, {"job_id": job_id, "reason": str(exc)})
                )
            continue
        candidate = runtime._role(
            snapshot,
            request.role,
            provisional=tuple(selected),
            allowed_candidates=request.candidates,
            permissions="reasoning",
            require_headless=True,
            **({"summary_stage": stage} if stage else {}),
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
    reason = admission_reason(snapshot, request.unit)
    if reason:
        action.payload["job_binding"] = _binding(row)
        action.state = "confirmed"
        action.receipt = {"state": "deferred", "reason": reason, "effect_binding": _binding(row)}
        row["reason"] = reason
        add_event(
            snapshot,
            "reasoning-admission-deferred",
            unit=request.unit,
            job_id=row["id"],
            reason=reason,
        )
        return
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


def _confirmed_preparation_failure(snapshot, row):
    """Recover old quiescent claims that did not record not_launched status."""
    claim = row.get("claim") or {}
    return any(
        effect.id == claim.get("launch_id")
        and effect.generation == claim.get("generation")
        and effect.kind == "reasoning-launch"
        and effect.state == "confirmed"
        and effect.payload.get("job_id") == row["id"]
        and effect.payload.get("job_binding") == _binding(row)
        and effect.receipt.get("state") == "preparation_failed"
        for effect in snapshot.outbox
    )


def perform(runtime, snapshot, action, *, reconcile: bool = False) -> Observation:
    from .campaign_communication_runtime import RegistrationBusy
    from .campaign_headless import capability_for, readonly_grants

    row = _effect_row(runtime, snapshot, action)
    if row["state"] in TERMINAL or _superseded(row, action):
        return Observation("confirmed", {"state": "settled", "effect_binding": _binding(row)})
    request = request_from_dict(row["request"])
    if action.kind in {"reasoning-stale", "reasoning-apply", "reasoning-wait"}:
        return Observation("confirmed", {})
    claim = row.get("claim") or {}
    path = Path(claim["run_dir"]) / "request.json"
    if (
        claim.get("quiescent")
        and not (path.parent / "launch-intent.json").exists()
        and not is_link_like(path.parent / "launch-intent.json")
        and (claim.get("status") == "not_launched" or _confirmed_preparation_failure(snapshot, row))
    ):
        return Observation("confirmed", {"state": "settled", "effect_binding": _binding(row)})
    runner = _runner(runtime, snapshot)
    if action.kind == "reasoning-launch":
        deferred = _launch_guard(runtime, snapshot, row, action, path)
        if deferred is not None:
            return deferred
        if (path.parent / "launch-intent.json").exists() and not path.exists():
            return Observation(
                "unknown",
                reason="reasoning launch intent has no readable request",
                retry_at=runtime.clock() + 5,
            )
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
                    routes, candidate.profile, candidate.model, "reasoning", effort=candidate.effort
                )
                permission = capability["permissions"]
                grants = None
                if permission == "read-only-tools":
                    workspace = (claim.get("presentation") or {}).get(
                        "workspace", str(runtime.project)
                    )
                    grants = readonly_grants(
                        workspace, request.coordinates, capability, generation=claim["generation"]
                    )
                from .campaign_communication_runtime import prepare as prepare_communication
                from .campaign_headless import render_input
                from .campaign_messages import MessageLedger

                managed_binding = prepare_communication(runtime, snapshot, job=row)
                _, delivery = render_input(request, capability)
                room = (
                    min(
                        request.limits.input_tokens,
                        capability["input_tokens"],
                        capability["context_tokens"]
                        - request.limits.output_tokens
                        - capability["reserved_tokens"],
                    )
                    - delivery["token_upper_bound"]
                    - 512
                )
                mail = MessageLedger(runtime.store.directory(snapshot.id)).freeze(
                    request.unit or "@campaign",
                    claim["launch_id"],
                    job=row["id"],
                    max_bytes=min(32768, room),
                )
                view = claim.get("presentation")
                if managed_binding and view:
                    from .campaign_titles import work_title

                    view = {
                        **view,
                        "navigation_prefix": (
                            work_title(snapshot.units[request.unit], snapshot=snapshot)
                            if request.unit in snapshot.units
                            else "Portfolio"
                        ),
                    }
                deferred = _launch_guard(runtime, snapshot, row, action, path)
                if deferred is not None:
                    return deferred
                path = runner.prepare(
                    path.parent,
                    request,
                    candidate,
                    capability,
                    launch_id=claim["launch_id"],
                    generation=claim["generation"],
                    permissions=permission,
                    grants=grants,
                    communication_room=snapshot.spec.communication.room,
                    presentation=view,
                    **({"managed_binding": managed_binding} if managed_binding else {}),
                    **({"steering": mail} if mail else {}),
                )
            except RegistrationBusy as exc:
                if any(
                    (path.parent / name).exists() or is_link_like(path.parent / name)
                    for name in (
                        "request.json",
                        "launch-intent.json",
                        "startup.json",
                        "receipt.json",
                    )
                ):
                    return Observation("unknown", reason=str(exc), retry_at=runtime.clock() + 5)
                deferred = _launch_guard(runtime, snapshot, row, action, path)
                if deferred is not None:
                    return deferred
                # The native runner already journals dispatch; retrying this
                # exact pre-request job needs no second launch authority.
                return Observation(
                    "waiting",
                    {"registration_wait": {"phase": "before_native_request"}},
                    "Managed bridge registration busy before native reasoning dispatch",
                    runtime.clock() + 1,
                )
            except (CampaignError, OSError, ValueError) as exc:
                # No launch has been attempted; this is a typed preparation
                # failure, not evidence that a previously started process died.
                if (path.parent / "launch-intent.json").exists():
                    return Observation("unknown", reason=str(exc), retry_at=runtime.clock() + 5)
                return Observation("confirmed", {"state": "preparation_failed", "reason": str(exc)})
        deferred = _launch_guard(runtime, snapshot, row, action, path)
        if deferred is not None:
            return deferred
        if not (path.parent / "launch-intent.json").exists() and not is_link_like(
            path.parent / "launch-intent.json"
        ):
            from .campaign_summary_routes import validate_request

            try:
                validate_request(snapshot, request)
            except (CampaignError, OSError, ValueError) as exc:
                return Observation("confirmed", {"state": "preparation_failed", "reason": str(exc)})
        try:
            observed = (
                runner.observe(path)
                if (path.parent / "launch-intent.json").exists()
                else runner.launch(
                    path, admission=lambda: _admission(runtime, snapshot, row, action, path)
                )
            )
        except _LaunchDeferred as exc:
            return exc.observation
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
    if path.exists():
        from .campaign_headless import read_json

        envelope = read_json(path)
        if envelope.get("managed_binding"):
            observed["managed_communication"] = envelope["managed_binding"]
        if (
            observed["state"] == "finished"
            and observed.get("receipt", {}).get("outcome") == "completed"
            and envelope.get("steering")
        ):
            from .campaign_messages import MessageLedger

            MessageLedger(runtime.store.directory(snapshot.id)).applied(
                [message["id"] for message in envelope["steering"]],
                launch_id=claim["launch_id"],
                unit=request.unit or "@campaign",
                job=row["id"],
            )
    if observed["state"] in {"running", "finished"} and claim.get("presentation"):
        view = presentation(snapshot, request)
        if view is not None:
            if observed.get("managed_communication"):
                from .campaign_titles import work_title

                view["navigation_prefix"] = (
                    work_title(snapshot.units[request.unit], snapshot=snapshot)
                    if request.unit in snapshot.units
                    else "Portfolio"
                )
            observed["presentation_status"] = runner.refresh_presentation(
                path, view["navigation_prefix"], historical=observed["state"] == "finished"
            )
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
    if data.get("managed_communication") and row.get("claim"):
        row["claim"]["managed_communication"] = data["managed_communication"]
    if action.kind == "reasoning-wait":
        if not live(row):
            row["reason"] = action.payload.get("reason", WORKSPACE_WAIT)
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
    elif data.get("state") == "deferred":
        claim = row.get("claim")
        if claim and (Path(claim["run_dir"]) / "launch-intent.json").exists():
            claim["quiescent"] = False
            row["state"], row["reason"] = (
                "uncertain",
                "native launch intent requires reconciliation",
            )
        else:
            if claim:
                claim.update(quiescent=True, status="not_launched")
            row["state"], row["reason"], row["retry_at"] = "proposed", data["reason"], None
    elif data.get("state") == "preparation_failed":
        row["claim"].update(quiescent=True, status="not_launched")
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
