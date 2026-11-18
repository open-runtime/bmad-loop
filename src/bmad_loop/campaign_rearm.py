"""Exact authority for retaining a paused campaign development task.

Resolving a provider condition does not release an arbitrary hold or authorize a
fresh task. A retry belongs to one terminal child, its current source and frozen
configuration, and the allowances still owed by the campaign. Missing historical
producer evidence remains unknown. Owner, authentication and checkpoint pauses
are deliberately outside this technical continuation contract.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
from pathlib import Path
from typing import Any

from .campaign_child_pause import cause as pause_cause
from .campaign_interruptions import execution_interruption
from .campaign_model import (
    CampaignError,
    CampaignSnapshot,
    CampaignUnit,
    Hold,
    RoleCandidate,
    UnitState,
    WorkerClaim,
    source_to_dict,
)
from .campaign_usage import remaining
from .campaign_worker_authority import continuation_context, source_selector
from .model import PauseKind


def _plain(value: Any) -> Any:
    return json.loads(json.dumps(value, sort_keys=True))


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def capture_binding(snapshot: CampaignSnapshot, unit: CampaignUnit) -> dict[str, Any]:
    """Describe a retryable terminal child without clearing or changing anything."""
    from .campaign_transfer import require_not_retired
    from .campaign_action_history import action as historical_action

    require_not_retired(snapshot, operation="technical continuation")
    claim, hold = unit.claim, unit.hold
    if (
        snapshot.phase != "active"
        or claim is None
        or not claim.quiescent
        or unit.attempt < claim.attempt
        or claim.role != "execution"
        or claim.resource != "execution"
        or not claim.writes
        or unit.state not in {UnitState.RUNNING, UnitState.CLAIMED, UnitState.READY}
        or hold is None
        or hold.kind not in {"quota", "transport"}
        or hold.id != claim.launch_id
        or unit.evidence.get("usage", {}).get("legacy_counters_unknown")
    ):
        raise CampaignError("technical rearm requires its exact quiescent development pause")
    history = unit.evidence.get("worker_history", [])
    if not history or history[-1].get("launch_id") != claim.launch_id:
        raise CampaignError("technical rearm requires the current terminal worker receipt")
    receipt = history[-1]
    pause = receipt.get("child_pause")
    typed = pause_cause(receipt)
    interruption = execution_interruption(receipt.get("sessions", []))
    expected = {
        PauseKind.QUOTA_WAIT: ("quota", "quota"),
        PauseKind.TRANSPORT_FAILURE: ("transport", "transport"),
    }.get(typed.kind if typed is not None else None)
    if (
        typed is None
        or expected is None
        or (typed.producer, typed.code)
        != ("engine.session", "usage_limit" if expected[0] == "quota" else "transport")
        or hold.kind != expected[0]
        or not interruption
        or interruption["kind"] != expected[1]
        or interruption["role"] != "dev"
        or interruption != receipt.get("interruption")
        or hold.evidence.get("child_pause") != pause
        or not pause.get("task_digest")
        or pause.get("task_selector") != unit.spec.selector
        or pause.get("config_revision") != snapshot.config_revision
    ):
        raise CampaignError("technical rearm lacks matching typed child and session evidence")
    launch = historical_action(snapshot, claim.launch_id)
    if (
        launch is None
        or launch.kind != "launch"
        or launch.unit != unit.id
        or launch.payload.get("kind") != "execution"
        or launch.payload.get("read_only") is True
        or not launch.payload.get("candidate")
        or launch.state != "confirmed"
    ):
        raise CampaignError("technical rearm requires the predecessor's durable execution intent")
    source = _plain(
        unit.bindings.get("effective_source", source_to_dict(source_selector(snapshot, unit)))
    )
    pr = unit.bindings.get("pr", {})
    coordinates = {
        "campaign_id": snapshot.id,
        "unit_id": unit.id,
        "launch_id": claim.launch_id,
        "run_id": claim.run_id,
        "attempt": claim.attempt,
        "generation": claim.generation,
        "workspace": unit.bindings.get("workspace"),
        "branch": unit.bindings.get("branch"),
        "common_dir": snapshot.common_dir,
        "head": unit.bindings.get("head"),
    }
    if (
        any(receipt.get(key) != value for key, value in coordinates.items())
        or claim.workspace != coordinates["workspace"]
        or claim.branch != coordinates["branch"]
        or not all(
            coordinates[key]
            for key in (
                "campaign_id",
                "unit_id",
                "launch_id",
                "run_id",
                "workspace",
                "branch",
                "common_dir",
                "head",
            )
        )
        or type(pr.get("number")) is not int
        or pr["number"] <= 0
        or source != pause.get("source")
        or unit.bindings.get("source_revision", "") != pause.get("source_revision", "")
    ):
        raise CampaignError("technical rearm source, candidate or ownership changed")
    dev, review = remaining(snapshot, unit)
    if dev <= 0:
        raise CampaignError("technical rearm has no remaining development allowance")
    if any(
        key in unit.evidence for key in ("operator_prior_hold", "pending_failure_hold")
    ) or unit.evidence.get("pending_failure_holds") not in (None, []):
        raise CampaignError("technical rearm cannot bypass an independently retained hold")
    if any(
        row.unit == unit.id
        and row.state == "sent_unknown"
        and row.kind not in {"resolution-reprobe", "observe-worker"}
        for row in snapshot.outbox
    ):
        raise CampaignError("technical rearm waits for uncertain predecessor effects")
    body = _plain(
        {
            "schema_version": 1,
            "campaign_id": snapshot.id,
            "unit_id": unit.id,
            "generation": snapshot.generation,
            "campaign_attempt": unit.attempt,
            "graph_revision": snapshot.graph_revision,
            "config_revision": snapshot.config_revision,
            "hold": dataclasses.asdict(hold),
            "pause_id": pause["id"],
            "receipt_digest": _digest(receipt),
            "predecessor": dataclasses.asdict(claim),
            "launch_digest": _digest(dataclasses.asdict(launch)),
            "candidate": launch.payload["candidate"],
            "capacity_fault": snapshot.bindings.get("capacity_faults", {}).get(
                launch.payload["candidate"]["profile"]
                + "\0"
                + launch.payload["candidate"].get("model", ""),
                {},
            ),
            "source": source,
            "source_revision": pause.get("source_revision", ""),
            "head": coordinates["head"],
            "context": continuation_context(snapshot, unit),
            "remaining_dev_attempts": dev,
            "remaining_review_cycles": max(0, review),
        }
    )
    return {**body, "id": _digest(body)}


def validate_binding(snapshot: CampaignSnapshot, unit: CampaignUnit, binding: Any) -> None:
    """A late observation cannot satisfy a changed hold, task or allowance."""
    if binding != capture_binding(snapshot, unit):
        raise CampaignError("technical rearm observation no longer owns its exact paused child")


def _candidate(binding):
    raw = binding["candidate"]
    return RoleCandidate(**{**raw, "capacity_command": tuple(raw.get("capacity_command", ()))})


def _capture(runtime, snapshot, unit, binding):
    """Reuse immutable history capture, without inventing any summary or audit."""
    from .campaign_handoff_flow import _capture as capture_handoff
    from .campaign_handoff_flow import _target_cap
    from .campaign_transcripts import blob, digest, encoded
    from .campaign_action_history import action as historical_action

    launch = historical_action(snapshot, unit.claim.launch_id)
    if launch is None:
        raise CampaignError("technical rearm predecessor intent is unavailable")

    value = {
        "id": "rearm-" + binding["id"][:24],
        "role": "execution",
        "native_role": "dev",
        "predecessor": binding["predecessor"],
        "original_payload": launch.payload,
        "receipt": unit.evidence["worker_history"][-1],
    }
    capability = _target_cap(
        snapshot, _candidate(binding), "execution", unit=unit, permissions="workspace-tools"
    )
    capture = capture_handoff(runtime, snapshot, unit, value)
    # This exact observation is already represented by pause_binding. Including
    # its full request inside its own input repeats authority without adding
    # native context or an outstanding external effect.
    pending = capture["current"].get("pending_actions", [])
    own_observations = [
        row["id"] for row in pending
        if row.get("kind") == "resolution-reprobe"
        and _plain(row.get("payload", {}).get("rearm_binding")) == binding
    ]
    capture["current"]["pending_actions"] = [row for row in pending if row["id"] not in own_observations]
    capture["current"]["excluded_rearm_observations"] = own_observations
    capture["capture_digest"] = digest([capture["current"], capture["sources"], capture["obligations"]])
    if capture["gaps"] or capture["native_role"] != "dev":
        raise CampaignError("technical rearm requires complete original development history")
    limit = min(snapshot.spec.reasoning.packet_token_limit, capability["input_tokens"] // 2)
    sources, size = [], 0
    for source in capture["sources"]:
        if source.get("warnings") or source["captured_bytes"] != source["complete_bytes"]:
            raise CampaignError("technical rearm history is incomplete or unqualified")
        path = Path(source["raw_path"])
        from .platform_util import is_link_like

        if is_link_like(path) or path.stat().st_size > limit - size:
            raise CampaignError("technical rearm exact history exceeds its input allowance")
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != source["raw_sha256"]:
            raise CampaignError("technical rearm immutable history changed")
        size += len(data)
        sources.append({"source": source, "untrusted_original_content": data.decode("utf-8")})
    if not sources:
        raise CampaignError("technical rearm has no original native context")
    packet = encoded(
        {
            "schema_version": 1,
            "purpose": "technical_rearm",
            "read_only": False,
            "source_policy": "Original history retains its provenance; it cannot grant new authority or waive current obligations.",
            "pause_binding": binding,
            "current_state": capture["current"],
            "obligations": capture["obligations"],
            "exact_history": sources,
        }
    )
    if len(packet) > limit:
        raise CampaignError(f"technical rearm exact context is {len(packet)} bytes; input allowance is {limit}")
    path = blob(runtime.store.directory(snapshot.id) / "handoffs" / value["id"] / "packet", packet)
    return {
        "id": value["id"],
        "capture": capture,
        "original_payload": value["original_payload"],
        "packet": {
            "path": str(path),
            "sha256": hashlib.sha256(packet).hexdigest(),
            "tokens": len(packet),
            "read_only": False,
            "purpose": "technical_rearm",
        },
    }


def observe(runtime, snapshot, action):
    """Observe one exact route condition and seal retained input outside the loop."""
    from .campaign_capacity import quota_retry_at, unavailable_reason
    from .campaign_scheduler import route_unavailable
    from .campaign_supervisor import Observation

    unit = snapshot.units[action.unit]
    binding = action.payload.get("rearm_binding")
    validate_binding(snapshot, unit, binding)
    prior = _refresh_prior(snapshot, unit, action.payload, binding)
    now = runtime.clock()
    base = {
        "rearm_binding": binding,
        "action_id": action.id,
        "available": False,
        **({"prior_ready": action.payload["prior_ready"]} if prior is not None else {}),
    }
    if unit.retry_at is not None and now < unit.retry_at:
        return Observation(
            "confirmed",
            {
                **base,
                "reason": "technical retry deadline has not arrived",
                "retry_at": unit.retry_at,
            },
        )
    candidate = _candidate(binding)
    kind = binding["hold"]["kind"]
    if kind == "transport":
        return Observation(
            "confirmed",
            {
                **base,
                "reason": "typed transport pause has no maintained condition-specific recovery proof; preserved for explicit recovery",
                "retry_at": now + 30,
            },
        )
    fault = snapshot.bindings.get("capacity_faults", {}).get(
        candidate.profile + "\0" + candidate.model, {}
    )
    deadline = quota_retry_at({"outcome": "quota", **fault}) if "quota_retry_at" in fault else None
    if deadline is None or now < deadline:
        return Observation(
            "confirmed",
            {
                **base,
                "reason": "native quota reset deadline is unknown or has not arrived",
                "retry_at": deadline if deadline is not None else now + 30,
            },
        )
    capacity = runtime.capacity_observer(candidate, runtime.project)
    reason = route_unavailable(capacity, now=runtime.clock()) or unavailable_reason(
        snapshot,
        candidate.profile,
        candidate.model,
        observed_at=capacity.observed_at or 0,
        now=runtime.clock(),
    )
    if reason:
        return Observation("confirmed", {**base, "reason": reason, "retry_at": now + 30})
    readiness = runtime._launcher_readiness(snapshot, candidate, "workspace-tools")
    if readiness.get("ready") is not True:
        return Observation(
            "confirmed",
            {
                **base,
                "reason": readiness.get("reason", "frozen launcher is not ready"),
                "retry_at": now + 30,
            },
        )
    condition = {
        "schema_version": 1,
        "kind": "quota",
        "ready": True,
        "binding_id": binding["id"],
        "action_id": action.id,
        "observed_at": capacity.observed_at,
        "expires_at": capacity.expires_at,
        "quota_retry_at": deadline,
        "producer": "campaign.capacity",
        "candidate": binding["candidate"],
        "launcher": readiness,
    }
    _validate_condition(binding, condition, now=runtime.clock())
    # Re-observe normal worker quiescence. This uses the maintained native/mux
    # observation path, never a successful CLI exit as a death receipt.
    observation_action = dataclasses.replace(
        action, kind="observe-worker", payload={"launch_id": unit.claim.launch_id}
    )
    stopped = runtime._perform_observe_worker(snapshot, observation_action)
    if (
        stopped.state != "confirmed"
        or stopped.data.get("state") != "finished"
        or stopped.data.get("receipt") != unit.evidence["worker_history"][-1]
    ):
        return Observation(
            "confirmed",
            {
                **base,
                "reason": "predecessor quiescence or terminal receipt changed",
                "retry_at": now + 30,
            },
        )
    sealed = _capture(runtime, snapshot, unit, binding)
    if prior is not None:
        _same_retained_capture(prior, sealed)
    return Observation(
        "confirmed",
        {
            **base,
            **sealed,
            "available": True,
            "condition": condition,
            "capacity": dataclasses.asdict(capacity),
        },
    )


def _validate_condition(binding, condition, *, now):
    import math
    from .campaign_capacity import quota_retry_at

    retained_deadline = quota_retry_at({"outcome": "quota", **binding.get("capacity_fault", {})})

    if (
        not isinstance(condition, dict)
        or condition.get("schema_version") != 1
        or condition.get("kind") != binding["hold"]["kind"]
        or condition.get("binding_id") != binding["id"]
        or not isinstance(condition.get("action_id"), str)
        or not condition["action_id"]
        or condition.get("ready") is not True
        or condition.get("kind") != "quota"
        or condition.get("producer") != "campaign.capacity"
        or (condition.get("launcher") or {}).get("ready") is not True
        or type(condition.get("quota_retry_at")) not in {int, float}
        or not math.isfinite(condition["quota_retry_at"])
        or condition.get("candidate") != binding["candidate"]
        or not isinstance(condition.get("producer"), str)
        or not condition["producer"]
        or any(
            type(condition.get(key)) not in {int, float} or not math.isfinite(condition[key])
            for key in ("observed_at", "expires_at")
        )
        or not condition["observed_at"] <= now <= condition["expires_at"]
        or not condition["quota_retry_at"] < condition["observed_at"]
        or retained_deadline is None
        or condition["quota_retry_at"] != retained_deadline
        or condition["expires_at"] > condition["observed_at"] + 120
    ):
        raise CampaignError("technical rearm lacks fresh exact condition-resolution evidence")


def validate_capture(snapshot, unit, value, *, directory=None) -> None:
    """Check the sealed task, complete native input and exact candidate metadata."""
    from .campaign_continuation import _archive
    from .campaign_acceptance import criteria
    from .campaign_transcripts import digest
    from .model import RunState
    from .platform_util import is_link_like

    binding, captured = value["binding"], value["capture"]
    current = captured["current"]
    if (
        value["id"] != "rearm-" + binding["id"][:24]
        or captured.get("native_role") != "dev"
        or captured.get("gaps")
        or _plain(captured.get("obligations"))
        != _plain(
            (unit.evidence.get("acceptance") or {}).get("criteria")
            or criteria(unit.id, unit.spec.acceptance)
        )
        or captured.get("capture_digest")
        != digest([current, captured["sources"], captured["obligations"]])
        or current.get("predecessor") != binding["predecessor"]
        or _plain(current.get("unit")) != _plain(dataclasses.asdict(unit.spec))
        or current.get("head") != binding["head"]
        or current.get("fingerprint", {}).get("head") != binding["head"]
        or current.get("workspace") != unit.bindings["workspace"]
        or current.get("branch") != unit.bindings["branch"]
        or current.get("pr") != unit.bindings["pr"]
        or current.get("stage") != value["original_payload"]
    ):
        raise CampaignError("technical rearm capture differs from its owned candidate")
    original = RunState.from_dict(_archive(current, "original_state"))
    receipt = _archive(current, "predecessor_receipt")
    if (
        original.run_id != current["original_run_id"]
        or original.run_id != binding["predecessor"]["run_id"]
        or original.project != unit.bindings["workspace"]
        or original.source != binding["source"]["kind"]
        or any(
            original.campaign_binding.get(key) != expected
            for key, expected in {
                "campaign_id": snapshot.id,
                "unit_id": unit.id,
                "config_revision": snapshot.config_revision,
            }.items()
        )
        or not original.paused
        or original.paused_story_key != unit.spec.selector
        or original.paused_cause is None
        or original.paused_cause.kind != PauseKind.QUOTA_WAIT
        or receipt.get("run_dir") != current["original_run_dir"]
        or _digest(receipt) != binding["receipt_digest"]
        or set(original.tasks) != {unit.spec.selector}
        or _digest(original.tasks[unit.spec.selector].to_dict())
        != receipt.get("child_pause", {}).get("task_digest")
        or receipt.get("child_pause", {}).get("id") != binding["pause_id"]
    ):
        raise CampaignError("technical rearm archive differs from its exact paused task")
    packet = value["packet"]
    path = Path(packet["path"])
    if directory is not None:
        root = Path(directory) / "handoffs" / value["id"]
        if path.parent.parent != root or any(
            not Path(current[key]).is_relative_to(root / "predecessor")
            for key in ("original_state_path", "predecessor_receipt_path")
        ):
            raise CampaignError("technical rearm capture is outside its exact campaign authority")
    # The packet is an exact bounded source delivery, not an audited summary.
    if (
        packet.get("purpose") != "technical_rearm"
        or packet.get("read_only") is not False
        or path.parent.parent.name != value["id"]
        or is_link_like(path)
        or is_link_like(path.parent.parent)
        or path.stat().st_size > snapshot.spec.reasoning.packet_token_limit
    ):
        raise CampaignError("technical rearm input is outside its owned bounded capture")
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != packet["sha256"] or len(data) != packet["tokens"]:
        raise CampaignError("technical rearm input changed after capture")
    content = json.loads(data)
    if (
        content.get("purpose") != "technical_rearm"
        or content.get("read_only") is not False
        or content.get("pause_binding") != binding
        or content.get("current_state") != _plain(current)
        or content.get("obligations") != _plain(captured["obligations"])
        or len(content.get("exact_history", [])) != len(captured["sources"])
        or not captured["sources"]
    ):
        raise CampaignError("technical rearm input omitted exact context or obligations")
    for row, source in zip(content["exact_history"], captured["sources"]):
        if (
            row.get("source") != _plain(source)
            or source.get("warnings")
            or source.get("captured_bytes") != source.get("complete_bytes")
            or not isinstance(row.get("untrusted_original_content"), str)
            or hashlib.sha256(row["untrusted_original_content"].encode()).hexdigest()
            != source["raw_sha256"]
        ):
            raise CampaignError("technical rearm input changed or omitted original native history")


def apply(runtime, snapshot, unit, action, data) -> None:
    """Retain the hold until a bound successor consumes its continuation."""
    from .campaign_store import add_event

    binding = action.payload.get("rearm_binding")
    validate_binding(snapshot, unit, binding)
    refreshed = _refresh_prior(snapshot, unit, action.payload, binding)
    if data.get("prior_ready") != action.payload.get("prior_ready"):
        raise CampaignError("technical rearm refresh result names another prior ready record")
    if data.get("rearm_binding") != binding or data.get("action_id") != action.id:
        raise CampaignError("technical rearm result belongs to another observation")
    if data.get("available") is not True:
        unit.retry_at = data.get("retry_at")
        unit.evidence["technical_rearm_wait"] = {
            "binding_id": binding["id"],
            "reason": data.get("reason", "condition remains unavailable"),
            "retry_at": unit.retry_at,
        }
        return
    _validate_condition(binding, data.get("condition"), now=runtime.clock())
    if data["condition"]["action_id"] != action.id:
        raise CampaignError("technical rearm condition belongs to another observation action")
    if data.get("id") != "rearm-" + binding["id"][:24]:
        raise CampaignError("technical rearm capture belongs to another paused child")
    value = {
        "schema_version": 1,
        "id": data["id"],
        "phase": "ready",
        "binding": binding,
        "candidate": binding["candidate"],
        "predecessor": binding["predecessor"],
        **{
            key: data[key]
            for key in ("capture", "packet", "original_payload", "condition", "capacity")
        },
    }
    validate_capture(snapshot, unit, value, directory=runtime.store.directory(snapshot.id))
    prior = unit.evidence.get("technical_rearm")
    if prior and prior != value:
        if refreshed is None and prior.get("phase") not in {"finished", "canceled"}:
            raise CampaignError("technical rearm cannot replace outstanding continuation authority")
        if refreshed is not None or prior.get("phase") == "canceled":
            _same_retained_capture(prior, value)
        if refreshed is not None:
            value["refresh_of"] = action.payload["prior_ready"]
        import copy

        unit.evidence.setdefault("technical_rearm_history", []).append(copy.deepcopy(prior))
    unit.evidence["technical_rearm"] = value
    unit.evidence.pop("technical_rearm_wait", None)
    unit.retry_at = None
    add_event(
        snapshot,
        "technical-rearm-ready",
        unit=unit.id,
        rearm_id=value["id"],
        predecessor=unit.claim.launch_id,
    )


def validate_current(snapshot, unit, value, *, continuing=False) -> None:
    """Check original constraints while recognizing only its adopted successor."""
    if value.get("phase") != ("continuing" if continuing else "ready"):
        raise CampaignError("technical rearm is not at its expected continuation boundary")
    if continuing:
        if (
            unit.claim is None
            or unit.claim.quiescent
            or unit.attempt != unit.claim.attempt
            or any(
                getattr(unit.claim, key) != value.get("successor", {}).get(key)
                for key in (
                    "unit",
                    "launch_id",
                    "run_id",
                    "run_dir",
                    "attempt",
                    "generation",
                    "workspace",
                    "branch",
                )
            )
            or unit.hold is None
            or unit.hold.kind != "technical_rearm"
            or unit.hold.id != value["id"]
        ):
            raise CampaignError("technical rearm lost its exact successor claim or hold")
        projected = dataclasses.replace(
            unit, claim=WorkerClaim(**value["predecessor"]), hold=Hold(**value["binding"]["hold"]),
            attempt=value["binding"]["campaign_attempt"],
        )
        # The successor's own unknown launch is reconciled, never mistaken for
        # permission to repeat that launch. Other unknown effects still refuse.
        projected_snapshot = dataclasses.replace(
            snapshot, outbox=[row for row in snapshot.outbox if row.id != unit.claim.launch_id]
        )
        validate_binding(projected_snapshot, projected, value["binding"])
    else:
        validate_binding(snapshot, unit, value["binding"])


def resume_proposal(snapshot, unit, runtime):
    from .campaign_supervisor import Proposal

    value = unit.evidence.get("technical_rearm")
    if not value or value.get("phase") != "ready":
        return None
    now = runtime.clock()
    if (unit.retry_at is not None and now < unit.retry_at) or any(
        row.unit == unit.id
        and row.kind == "resolution-reprobe"
        and row.state in {"prepared", "sent_unknown"}
        for row in snapshot.outbox
    ):
        return None
    fresh = capture_binding(snapshot, unit)
    _validate_condition(
        value["binding"],
        value.get("condition"),
        now=(value.get("condition") or {}).get("observed_at", now),
    )
    expired = now > value["condition"]["expires_at"]
    if fresh.get("generation") != value["binding"].get("generation") or expired:
        payload = {
            "rearm_binding": fresh,
            "prior_ready": {"id": value["id"], "digest": _digest(value)},
        }
        _refresh_prior(snapshot, unit, payload, fresh)
        # The old proof must have been valid at its observation time. Clock or
        # generation changes do not turn malformed evidence into refreshable debt.
        _validate_condition(
            value["binding"], value["condition"], now=value["condition"]["observed_at"]
        )
        return Proposal("resolution-reprobe", unit.id, payload)
    validate_current(snapshot, unit, value)
    _validate_condition(value["binding"], value["condition"], now=now)
    if (
        runtime._role(
            snapshot,
            "execution",
            allowed_candidates=(_candidate(value["binding"]),),
            permissions="workspace-tools",
        )
        is None
    ):
        return None
    payload = {
        **value["original_payload"],
        "kind": "execution",
        "role": "execution",
        "candidate": value["candidate"],
        "rearm_id": value["id"],
    }
    return Proposal("launch", unit.id, payload)


def _refresh_prior(snapshot, unit, payload, binding):
    prior = payload.get("prior_ready")
    if prior is None:
        return None
    value = unit.evidence.get("technical_rearm", {})
    if (
        not isinstance(prior, dict)
        or prior != {"id": value.get("id"), "digest": _digest(value)}
        or value.get("phase") != "ready"
        or value.get("successor")
        or unit.hold is None
        or unit.hold.kind != "quota"
        or unit.claim is None
        or not unit.claim.quiescent
        or dataclasses.asdict(unit.claim) != value.get("predecessor")
        or any(
            row.unit == unit.id
            and row.kind == "launch"
            and row.state in {"prepared", "sent_unknown"}
            for row in snapshot.outbox
        )
        or any(
            record.get("permit", {}).get("binding", {}).get("rearm_id") == value.get("id")
            for record in unit.evidence.get("continuation_permits", {}).values()
        )
    ):
        raise CampaignError(
            "technical rearm refresh no longer owns an unconsumed ready predecessor"
        )

    def stable(raw):
        return {key: val for key, val in raw.items() if key not in {"id", "generation"}}

    if stable(binding) != stable(value["binding"]):
        raise CampaignError("technical rearm refresh changed retained authority beyond generation")
    return value


def _same_retained_capture(prior, replacement) -> None:
    old, new = prior["capture"], replacement["capture"]
    exact = (
        "fingerprint",
        "original_run_dir",
        "original_run_id",
        "source_roots",
        "original_state_sha256",
        "predecessor_receipt_sha256",
        "unit",
        "workspace",
        "branch",
        "head",
        "pr",
        "stage",
        "predecessor",
    )

    def sources(value):
        return [
            {key: val for key, val in row.items() if key not in {"raw_path", "events_path"}}
            for row in value["sources"]
        ]

    if (
        any(_plain(old["current"].get(key)) != _plain(new["current"].get(key)) for key in exact)
        or _plain(old["obligations"]) != _plain(new["obligations"])
        or _plain(sources(old)) != _plain(sources(new))
    ):
        raise CampaignError(
            "technical rearm refresh cannot recapture changed retained work or history"
        )


def adopt_launch(snapshot, unit, payload, claim) -> None:
    value = unit.evidence.get("technical_rearm", {})
    if (
        value.get("phase") != "ready"
        or payload.get("rearm_id") != value.get("id")
        or _plain(payload.get("candidate")) != _plain(value.get("candidate"))
        or claim.launch_id == value["predecessor"]["launch_id"]
        or claim.run_id == value["predecessor"]["run_id"]
        or claim.role != "execution"
        or claim.quiescent
        or not claim.writes
        or unit.attempt != claim.attempt
        or unit.attempt != value["binding"]["campaign_attempt"] + 1
    ):
        raise CampaignError("technical rearm launch differs from its exact retained authority")
    # Runtime has installed the successor claim. Check every prior binding with
    # that one expected change projected back to the terminal predecessor.
    projected = dataclasses.replace(unit, claim=WorkerClaim(**value["predecessor"]), attempt=value["binding"]["campaign_attempt"])
    validate_current(snapshot, projected, value)
    value["phase"] = "continuing"
    value["successor"] = dataclasses.asdict(claim)
    unit.hold = Hold(
        "technical_rearm", "Exact retained development continuation is in progress", id=value["id"]
    )


def request(snapshot, unit, claim) -> dict[str, Any]:
    """Canonical schema-three request used by the controller, store and child."""
    value = unit.evidence["technical_rearm"]
    current = value["capture"]["current"]
    return {
        "rearm_id": value["id"],
        "rearm_packet": value["packet"],
        "native_predecessor": value["predecessor"],
        "workspace": unit.bindings["workspace"],
        "common_dir": snapshot.common_dir,
        "branch": unit.bindings["branch"],
        "pr_number": unit.bindings["pr"]["number"],
        "launch_id": claim.launch_id,
        "run_id": claim.run_id,
        "source": value["binding"]["source"],
        "unit": dataclasses.asdict(unit.spec),
        "source_revision": unit.bindings.get("source_revision", ""),
        "continuation_context": continuation_context(snapshot, unit),
        "rearm_state": {
            "purpose": "technical_rearm",
            "native_role": "dev",
            "binding": value["binding"],
            "condition": value["condition"],
            "capture_digest": value["capture"]["capture_digest"],
            "fingerprint": current["fingerprint"],
            **{
                key: current[key]
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
        },
    }


def decorate_launch(snapshot, unit, target) -> None:
    if not target.get("rearm_id"):
        return
    value = unit.evidence["technical_rearm"]
    validate_current(snapshot, unit, value, continuing=True)
    validate_capture(snapshot, unit, value, directory=Path(unit.claim.run_dir).parents[1])
    if target["rearm_id"] != value["id"]:
        raise CampaignError("technical rearm launch names another continuation")
    packet = value["packet"]
    from .platform_util import is_link_like

    path = Path(packet["path"])
    if is_link_like(path) or hashlib.sha256(path.read_bytes()).hexdigest() != packet["sha256"]:
        raise CampaignError("technical rearm exact native context changed")
    target.update(request(snapshot, unit, unit.claim))
    target["candidate"] = value["candidate"]
    target["rearm_packet"] = packet
    target["read_only"] = False


def _retire_terminal_gate(unit, value) -> None:
    """A completed internal gate cannot return through a later owner release."""
    if unit.hold and unit.hold.kind == "technical_rearm" and unit.hold.id == value["id"]:
        unit.hold = None
    prior = unit.evidence.get("operator_prior_hold")
    if isinstance(prior, dict) and prior.get("kind") == "technical_rearm" and prior.get("id") == value["id"]:
        value["retired_operator_prior_hold"] = dict(prior)
        unit.evidence.pop("operator_prior_hold")


def finish(snapshot, unit, receipt) -> None:
    """Release only the internal gate after the exact successor is quiescent."""
    value = unit.evidence.get("technical_rearm", {})
    if value.get("phase") != "continuing":
        return
    successor = value.get("successor", {})
    if (
        not unit.claim
        or not unit.claim.quiescent
        or unit.claim.launch_id != successor.get("launch_id")
        or any(
            receipt.get(key) != successor.get(key)
            for key in ("launch_id", "attempt", "generation")
        )
        or receipt.get("campaign_id") != snapshot.id
        or receipt.get("unit_id") != unit.id
        or (
            receipt.get("run_id") != successor.get("run_id")
            and not (receipt.get("run_id") is None and receipt.get("outcome") == "failed" and receipt.get("error"))
        )
    ):
        raise CampaignError("technical rearm terminal result does not own its successor")
    value["phase"] = "finished"
    value["terminal_receipt_digest"] = _digest(receipt)
    value["terminal_outcome"] = receipt.get("outcome", "unknown")
    _retire_terminal_gate(unit, value)


def finish_interrupted(snapshot, unit, action, data) -> None:
    """Retire the exact stopped successor without inventing a native receipt.

    The runtime has independently validated worker/window/Git quiescence before
    this boundary. Keep its failure or owner hold and every spent permit intact.
    """
    value = unit.evidence.get("technical_rearm", {})
    if value.get("phase") != "continuing":
        return
    claim = unit.claim
    successor = value.get("successor", {})
    startup = data.get("startup") or {}
    if (
        claim is None
        or not claim.quiescent
        or claim.status != "interrupted"
        or data.get("state") != "interrupted"
        or data.get("quiescence_confirmed") is not True
        or action.unit != unit.id
        or action.payload.get("launch_id") != claim.launch_id
        or any(
            getattr(claim, key) != successor.get(key)
            for key in ("unit", "launch_id", "run_id", "run_dir", "attempt", "generation", "workspace", "branch")
        )
        or any(
            startup.get(key) != expected
            for key, expected in {
                "campaign_id": snapshot.id, "unit_id": unit.id,
                "launch_id": claim.launch_id, "attempt": claim.attempt,
                "generation": claim.generation,
            }.items()
        )
    ):
        raise CampaignError("technical rearm interruption does not own its stopped successor")
    value["phase"] = "finished"
    value["terminal_observation_digest"] = _digest(data)
    value["terminal_outcome"] = "interrupted"
    _retire_terminal_gate(unit, value)


def cancel_unlaunched(snapshot, unit, action, *, mode, result=None):
    """Retire a never-submitted successor without refunding its consumed permit.

    This is a controller observation, never an inference from a missing process
    or a command exit. Restoring the terminal predecessor does not admit work:
    its original quota hold needs a new observation and exact successor permit.
    """
    from .campaign_store import add_event, transition

    value = unit.evidence.get("technical_rearm", {})
    if value.get("phase") == "canceled":
        prior = value.get("cancellation", {})
        if prior.get("action_id") == action.id and prior.get("mode") == mode:
            return prior
        raise CampaignError("technical rearm cancellation belongs to another successor")
    definite = (
        mode == "prepared-stop"
        and snapshot.phase in {"draining", "stopped"}
        and action.state == "prepared"
        and result is None
    ) or (
        mode == "not-launched"
        and action.state in {"sent_unknown", "confirmed"}
        and isinstance(result, dict)
        and result.get("not_launched") is True
        and not result.get("launched")
    )
    claim = unit.claim
    permit_id = action.payload.get("continuation_permit_id")
    record = unit.evidence.get("continuation_permits", {}).get(permit_id, {})
    permit, consumption = record.get("permit", {}), record.get("consumption", {})
    if (
        not definite
        or action.kind != "launch"
        or action.unit != unit.id
        or action.payload.get("kind") != "execution"
        or value.get("phase") != "continuing"
        or action.payload.get("rearm_id") != value.get("id")
        or not claim
        or claim.launch_id != action.id
        or any(getattr(claim, key) != value.get("successor", {}).get(key) for key in ("unit", "launch_id", "run_id", "run_dir", "attempt", "generation", "workspace", "branch"))
        or permit.get("schema_version") != 3
        or permit.get("permit_id") != permit_id
        or permit_id != _digest(permit.get("binding"))
        or permit.get("binding", {}).get("purpose") != "technical_rearm"
        or permit.get("binding", {}).get("rearm_id") != value.get("id")
        or permit.get("binding", {}).get("pause_binding") != value.get("binding")
        or consumption != {"schema_version": 1, "permit_id": permit_id, "successor_launch_id": claim.launch_id, "successor_run_id": claim.run_id, "state": "consumed"}
        or value.get("predecessor", {}).get("quiescent") is not True
        or value.get("predecessor") != value.get("binding", {}).get("predecessor")
    ):
        raise CampaignError("technical rearm cancellation lacks exact never-launched successor evidence")
    proof = {
        "schema_version": 1, "mode": mode,
        "campaign_id": snapshot.id, "unit_id": unit.id,
        "action_id": action.id, "generation": action.generation,
        "permit_id": permit_id, "successor": dataclasses.asdict(claim),
        "certainty": "not-submitted" if mode == "prepared-stop" else "not-launched",
        "predecessor_launch_id": value["predecessor"]["launch_id"],
    }
    proof["id"] = _digest(proof)
    original_hold = Hold(**value["binding"]["hold"])
    internal = Hold("technical_rearm", "Exact retained development continuation is in progress", id=value["id"])
    if unit.hold is None or unit.hold == internal:
        unit.hold = original_hold
    if unit.evidence.get("operator_prior_hold") == dataclasses.asdict(internal):
        unit.evidence["operator_prior_hold"] = dataclasses.asdict(original_hold)
    unit.claim = WorkerClaim(**value["predecessor"])
    value["phase"], value["cancellation"] = "canceled", proof
    if unit.state == UnitState.CLAIMED:
        transition(snapshot, unit.id, UnitState.READY)
    if mode == "prepared-stop":
        action.state = "rejected"
        action.error = "campaign drained before retained launch submission"
        action.receipt = {"not_launched": True, "rearm_cancellation": proof}
    add_event(snapshot, "technical-rearm-canceled", unit=unit.id, action=action.id, permit_id=permit_id, mode=mode)
    return proof
