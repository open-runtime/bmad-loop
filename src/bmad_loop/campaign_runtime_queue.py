"""Single-writer queue progression around bounded authenticated effects.

Queue waits hold no publication slot. Failed G is never edited. The mapped PR is
removed from the queue and that removal is read back before any H repair is
admitted. A replaced G invalidates local/hosted evidence for the earlier group.
"""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path

from .campaign_github import GithubError
from .campaign_merge_adapter import AdapterReceipt
from .campaign_model import Hold, UnitState
from .campaign_publication import PublicationController, PublicationRequirements, read_rules
from .campaign_queue import (
    QueueGroup,
    allocate_checkout,
    capability,
    membership,
    observe_check,
    observe_group,
    prepare_check,
    prepare_dequeue,
)
from .campaign_store import add_event, transition
from .campaign_supervisor import Observation, Proposal


def proposals(runtime, snapshot, unit, reserve) -> tuple[Proposal, ...]:
    state = unit.evidence.get("queue", {})
    now = runtime.clock()
    if not state or now - state.get("observed_at", 0) >= 10:
        return (Proposal("queue-observe", unit.id),)
    if state.get("membership_missing_count"):
        return ()
    if state.get("needs_dequeue"):
        if state.get("failure") and not state.get("failure_check_attempted"):
            return (Proposal("queue-publish", unit.id, {"failure": True}),)
        if not state.get("dequeue_submitted"):
            return (Proposal("queue-dequeue", unit.id),)
        return ()
    raw = state.get("group")
    if not raw:
        return ()
    QueueGroup(**raw)
    if not state.get("workspace"):
        return (Proposal("queue-checkout", unit.id, {"group": raw}),)
    if not state.get("verification"):
        if not reserve("verify"):
            return ()
        return (
            Proposal(
                "launch",
                unit.id,
                {
                    "kind": "queue_verify",
                    "queue_group": raw,
                    "queue_candidate": asdict(runtime._candidate(snapshot, unit)),
                    "verification_workspace": state["workspace"],
                    "commands": list(snapshot.spec.github.candidate_command),
                    "check_context": snapshot.spec.github.candidate_check_context,
                    "rules_fingerprint": state["rules_fingerprint"],
                },
            ),
        )
    if (
        snapshot.spec.github.candidate_publish == "native"
        and not state.get("check_published")
        and not state.get("check_submitted")
    ):
        return (Proposal("queue-publish", unit.id),)
    return ()


def _terminal_effect(runtime, snapshot, action, candidate, pr):
    """Reconcile terminal remote state through the ordinary landing proof."""
    if pr.get("merged") is not True and pr.get("state") == "open":
        return None
    owned = snapshot.units[action.unit].bindings["pr"]
    if (
        pr.get("number") != candidate.pr_number
        or (pr.get("head") or {}).get("sha") != candidate.head
        or (pr.get("base") or {}).get("ref") != candidate.target
        or (owned.get("node_id") and pr.get("node_id") != owned["node_id"])
    ):
        raise GithubError("terminal queued PR identity differs from the owned candidate")
    if pr.get("merged") is True:
        merged = runtime._perform_observe_merge(snapshot, action)
        return Observation(merged.state, {"merged": merged.data}, merged.reason, merged.retry_at)
    if pr.get("state") == "closed" and pr.get("merged") is False:
        return Observation(
            "confirmed",
            {
                "observed_at": runtime.clock(),
                "closed": True,
                "reason": "queued PR closed without merge",
            },
        )
    return Observation(
        "unknown", reason="queued PR terminal state is unconfirmed", retry_at=runtime.clock() + 5
    )


def observe_effect(runtime, snapshot, action) -> Observation:
    unit = snapshot.units[action.unit]
    gateway = runtime.gateway(snapshot)
    candidate = runtime._candidate(snapshot, unit)
    pr = gateway.pull(candidate.pr_number)
    terminal = _terminal_effect(runtime, snapshot, action, candidate, pr)
    if terminal is not None:
        return terminal
    prior = unit.evidence.get("queue", {})
    member = membership(gateway, candidate)
    expected_node = unit.bindings["pr"].get("node_id")
    if not member.get("id") or (expected_node and member["id"] != expected_node):
        raise GithubError("queue membership PR native identity changed")
    if member.get("merged") is True:
        merged = runtime._perform_observe_merge(snapshot, action)
        return Observation(merged.state, {"merged": merged.data}, merged.reason, merged.retry_at)
    if member.get("merged") is not False or member.get("state") != "OPEN":
        return Observation(
            "unknown",
            reason="queue removal lacks current open/unmerged PR proof",
            retry_at=runtime.clock() + 5,
        )
    if member.get("mergeQueueEntry") is None and prior.get("needs_dequeue"):
        return Observation(
            "confirmed",
            {
                "observed_at": runtime.clock(),
                "dequeued": True,
                "candidate": asdict(candidate),
                "acceptance_changed": bool(
                    prior.get("candidate") and prior["candidate"] != asdict(candidate)
                ),
            },
        )
    if prior.get("candidate") and prior["candidate"] != asdict(candidate):
        return Observation(
            "confirmed", {"observed_at": runtime.clock(), "acceptance_changed": True}
        )
    if member.get("mergeQueueEntry") is None and prior.get("group"):
        old_group = QueueGroup(**prior["group"])
        expected_app = snapshot.spec.github.candidate_check_app_id or prior.get("app_id", 0)
        check = (
            observe_check(
                gateway, old_group, snapshot.spec.github.candidate_check_context, expected_app
            )
            if expected_app
            else None
        )
        if (
            check
            and check.get("status") == "completed"
            and check.get("conclusion")
            in {"failure", "timed_out", "cancelled", "action_required", "startup_failure", "stale"}
        ):
            return Observation(
                "confirmed",
                {
                    "observed_at": runtime.clock(),
                    "dequeued": True,
                    "failure": {
                        "reason": "required App check failed on combined candidate G",
                        "check": check,
                        "group": prior["group"],
                    },
                },
            )
        if not prior.get("membership_missing_count"):
            return Observation(
                "confirmed", {"observed_at": runtime.clock(), "membership_missing": True}
            )
        return Observation(
            "confirmed",
            {
                "observed_at": runtime.clock(),
                "closed": True,
                "reason": "queue entry removed without merge; external intent must be reconciled before re-enqueue",
            },
        )
    group = observe_group(gateway, candidate, clock=runtime.clock)
    controller = PublicationController(
        gateway,
        clock=runtime.clock,
        requirements=PublicationRequirements.from_spec(snapshot.spec.github),
    )
    observed = controller.inspect(candidate.pr_number)
    # Queue publication can land between the initial PR/membership reads and
    # this inspection. Terminal state is reconciliation, never a repair gate.
    terminal = _terminal_effect(runtime, snapshot, action, candidate, observed.pr)
    if terminal is not None:
        return terminal
    gate = controller.evaluate(
        candidate,
        runtime._publication_evidence(snapshot, unit),
        observed,
        merge_mode="queue",
        independent_families=snapshot.spec.github.require_independent_review,
        queue_evidence_context=snapshot.spec.github.candidate_check_context,
        queued_base=observed.base_head,
    )
    from .campaign_remote_feedback import collect as collect_remote_feedback

    remote_feedback = (
        collect_remote_feedback(
            candidate, observed, now=runtime.clock(), requirements=controller.requirements
        )
        if not prior.get("failure")
        else None
    )
    if remote_feedback is not None and not any(
        row["kind"] in {"required_check", "required_status"} for row in remote_feedback.defects
    ):
        remote_feedback = None
    if group is None:
        if remote_feedback is not None:
            return Observation(
                "confirmed",
                {
                    "observed_at": runtime.clock(),
                    "group": None,
                    "gate_reasons": list(gate.reasons),
                    "candidate": asdict(candidate),
                    "remote_feedback": asdict(remote_feedback),
                },
            )
        if (member.get("mergeQueueEntry") or {}).get("state") == "UNMERGEABLE":
            return Observation(
                "confirmed",
                {
                    "observed_at": runtime.clock(),
                    "unmergeable": True,
                    "reason": "GitHub could not construct a mergeable combined candidate; H repair requires confirmed queue removal",
                },
            )
        return Observation(
            "confirmed",
            {
                "observed_at": runtime.clock(),
                "group": None,
                "gate_reasons": list(gate.reasons),
                "candidate": asdict(candidate),
                "reason": "waiting for authenticated synthetic merge-group candidate",
            },
        )
    # After enqueue the explicit queue contract verifies current B/G over the
    # retained accepted B0. Every other current remote hold remains a veto.
    reasons = gate.reasons
    rules = observed.rules
    check_app = snapshot.spec.github.candidate_check_app_id or next(
        (
            check.app_id
            for check in rules.checks
            if check.context == snapshot.spec.github.candidate_check_context
        ),
        0,
    )
    check = (
        observe_check(gateway, group, snapshot.spec.github.candidate_check_context, check_app)
        if check_app
        else None
    )
    hosted = (
        check
        if check and check.get("status") == "completed" and check.get("conclusion") == "success"
        else None
    )
    failed_check = (
        check
        if check
        and check.get("status") == "completed"
        and check.get("conclusion")
        in {"failure", "timed_out", "cancelled", "action_required", "startup_failure", "stale"}
        else None
    )
    return Observation(
        "confirmed",
        {
            "observed_at": runtime.clock(),
            "group": asdict(group),
            "group_digest": group.digest,
            "rules_fingerprint": rules.fingerprint,
            "hosted_check": hosted,
            "gate_reasons": list(reasons),
            "app_id": check_app,
            "failed_check": failed_check,
            "candidate": asdict(candidate),
            "remote_feedback": asdict(remote_feedback) if remote_feedback is not None else None,
        },
    )


def checkout_effect(runtime, snapshot, action) -> Observation:
    group = QueueGroup(**action.payload["group"])
    unit = snapshot.units[action.unit]
    current = observe_group(
        runtime.gateway(snapshot), runtime._candidate(snapshot, unit), clock=runtime.clock
    )
    if current is None or current.digest != group.digest:
        return Observation("confirmed", {"stale_group": True})
    root = Path(
        snapshot.spec.paradev.worktree_root
        if snapshot.spec.paradev.reflect
        else snapshot.spec.worktree_root
    )
    path = allocate_checkout(
        runtime.project,
        root,
        runtime.store.directory(snapshot.id) / "queue-allocations",
        group,
        remote=snapshot.spec.remote,
    )
    return Observation("confirmed", {"workspace": str(path), "group_digest": group.digest})


def publish_effect(runtime, snapshot, action) -> Observation:
    unit = snapshot.units[action.unit]
    state = unit.evidence["queue"]
    group = QueueGroup(**state["group"])
    candidate = runtime._candidate(snapshot, unit)
    gateway = runtime.gateway(snapshot)
    current = observe_group(gateway, candidate, clock=runtime.clock)
    if current is None or current.digest != group.digest:
        if action.payload.get("failure"):
            return Observation(
                "confirmed",
                {
                    "failure_check_attempted": True,
                    "external_wait": "failed queue candidate was replaced before failure publication",
                },
            )
        return Observation("confirmed", {"stale_group": True})
    failure = bool(action.payload.get("failure"))
    rules = read_rules(gateway, candidate.target)
    producer = capability(
        snapshot.spec.github, snapshot.repository, runtime.project, rules, clock=runtime.clock
    )
    if producer.mode == "external" or not producer.ready:
        if failure:
            return Observation(
                "confirmed",
                {
                    "failure_check_attempted": True,
                    "external_wait": producer.reason
                    or "failed G must be reported by its external check producer",
                },
            )
        return Observation(
            "waiting",
            {"producer": asdict(producer)},
            producer.reason or "waiting for required external producer",
            runtime.clock() + 15,
        )
    if not state.get("verification"):
        return Observation(
            "confirmed",
            {
                "failure_check_attempted": True,
                "external_wait": "no bound local verification receipt; required producer remains unconfirmed",
            },
        )
    receipt = AdapterReceipt.from_dict(state["verification"])
    intent = prepare_check(
        candidate,
        current,
        receipt,
        producer,
        gateway,
        action_id=action.id + "-check",
        generation=snapshot.generation,
        now=runtime.clock(),
        conclusion="failure" if failure else "success",
    )
    return Observation(
        "confirmed",
        {
            "intent": intent.to_dict(),
            "purpose": "queue-check",
            "group_digest": group.digest,
            "failure": failure,
        },
    )


def dequeue_effect(runtime, snapshot, action) -> Observation:
    # This preparation can survive a lost observation or race with queue landing.
    # Settle terminal/removal readback before considering another mutation.
    current = observe_effect(runtime, snapshot, action)
    if current.state != "confirmed" or any(
        key in current.data for key in ("merged", "closed", "dequeued")
    ):
        return current
    unit = snapshot.units[action.unit]
    intent = prepare_dequeue(
        runtime.gateway(snapshot),
        runtime._candidate(snapshot, unit),
        action_id=action.id + "-dequeue",
        generation=snapshot.generation,
    )
    return Observation("confirmed", {"intent": intent.to_dict(), "purpose": "queue-dequeue"})


def apply_effect(runtime, snapshot, unit, action, data) -> None:
    state = unit.evidence.setdefault("queue", {})
    if "merged" in data:
        unit.evidence["merge"] = data["merged"]
        if data["merged"].get("landing_acceptance"):
            unit.evidence["landing_acceptance"] = data["merged"]["landing_acceptance"]
        transition(snapshot, unit.id, UnitState.MERGED_PENDING_SYNC)
        return
    if data.get("closed"):
        unit.hold = Hold("external_wait", data["reason"], id=action.id, actor="github")
        return
    if data.get("membership_missing"):
        state.update(
            observed_at=data["observed_at"],
            membership_missing_count=state.get("membership_missing_count", 0) + 1,
        )
        return
    if data.get("acceptance_changed") and not data.get("dequeued"):
        state.update(
            observed_at=data["observed_at"],
            needs_dequeue=True,
            restart_acceptance=True,
            gate_reasons=[
                "queued source/graph/config acceptance changed; repeat full PR acceptance"
            ],
        )
        return
    if data.get("unmergeable"):
        state.update(
            observed_at=data["observed_at"],
            needs_dequeue=True,
            failure_check_attempted=True,
            failure={"reason": data["reason"], "group": state.get("group")},
            external_wait="server did not supply G; no synthetic failure check is invented",
        )
        return
    if data.get("dequeued"):
        readback_candidate = data.get("candidate")
        if readback_candidate:
            current_candidate = asdict(runtime._candidate(snapshot, unit))
            if any(
                readback_candidate[key] != current_candidate[key]
                for key in ("repository", "pr_number", "target", "head")
            ):
                state["observed_at"] = 0
                add_event(snapshot, "queue-removal-readback-stale", unit=unit.id)
                return
            if readback_candidate != current_candidate:
                state["restart_acceptance"] = True
        if data.get("acceptance_changed"):
            state["restart_acceptance"] = True
        if (
            state.get("remote_feedback")
            and not state.get("failure")
            and not state.get("restart_acceptance")
        ):
            from .campaign_publication import Candidate
            from .campaign_remote_feedback import RemoteFeedback, apply_after_queue_removal

            raw = state["remote_feedback"]
            feedback = RemoteFeedback(
                Candidate(**raw["candidate"]),
                raw["rules_fingerprint"],
                tuple(raw["defects"]),
                raw["digest"],
                raw["observed_at"],
            )
            # This branch is reached only from authenticated open/unmerged,
            # exact-head membership readback with no queue entry.
            state["dequeue_confirmed"] = data["candidate"]
            apply_after_queue_removal(snapshot, unit.id, feedback)
            unit.bindings.pop("merge_request_id", None)
            return
        candidate_before_removal = asdict(runtime._candidate(snapshot, unit))
        failure = data.get("failure") or state.get("failure")
        reasons = state.get("gate_reasons", [])
        unit.bindings.pop("merge_request_id", None)
        unit.evidence.pop("integrated", None)
        unit.evidence.pop("pushed", None)
        unit.evidence.pop("verification", None)
        unit.evidence.pop("review", None)
        unit.evidence.pop("native_review_pair", None)
        transition(snapshot, unit.id, UnitState.SYNCING)
        unit.resume_state = UnitState.SYNCING
        if state.get("restart_acceptance") and not failure:
            unit.hold = None
            add_event(snapshot, "queue-acceptance-restarted", unit=unit.id)
            return
        unit.hold = Hold(
            "verification_failed" if failure else "remote_wait",
            (
                failure.get("reason", "combined candidate verification failed")
                if failure
                else "; ".join(reasons)
            ),
            id=action.id,
            actor="campaign" if failure else "github",
            evidence={
                "queue": state,
                "dequeue_confirmed": True,
                "candidate": candidate_before_removal,
            },
        )
        add_event(
            snapshot,
            "queue-repair-authorized" if failure else "queue-publication-held",
            unit=unit.id,
            head=unit.bindings["head"],
        )
        return
    if data.get("stale_group"):
        state["observed_at"] = 0
        return
    if action.kind == "queue-observe":
        digest = data.get("group_digest")
        if digest and (
            digest != state.get("group_digest")
            or data.get("rules_fingerprint") != state.get("rules_fingerprint")
        ):
            if state.get("workspace"):
                unit.evidence.setdefault("queue_history", []).append(state)
            unit.evidence["queue"] = state = {}
            add_event(snapshot, "queue-candidate-changed", unit=unit.id, group=digest)
        if data.get("group") is None and not state.get("needs_dequeue"):
            if state.get("workspace"):
                unit.evidence.setdefault("queue_history", []).append(state)
            unit.evidence["queue"] = state = {"observed_at": data["observed_at"]}
        elif data.get("group") is not None:
            state["group"] = data["group"]
            state.pop("membership_missing_count", None)
        state.update(
            {
                key: data[key]
                for key in (
                    "observed_at",
                    "group_digest",
                    "rules_fingerprint",
                    "hosted_check",
                    "gate_reasons",
                    "app_id",
                    "candidate",
                )
                if key in data
            }
        )
        if data.get("gate_reasons"):
            state["needs_dequeue"] = True
        if data.get("remote_feedback"):
            state.update(remote_feedback=data["remote_feedback"], needs_dequeue=True)
        if data.get("failed_check"):
            state.update(
                needs_dequeue=True,
                failure_check_attempted=True,
                failure={
                    "reason": "required App check failed on combined candidate G",
                    "group": data.get("group"),
                    "check": data["failed_check"],
                },
            )
    elif action.kind == "queue-checkout":
        if data.get("group_digest") == state.get("group_digest"):
            state["workspace"] = data["workspace"]
    elif action.kind in {"queue-publish", "queue-dequeue"}:
        if data.get("intent"):
            runtime._queue_intent(snapshot, unit, data)
            state["dequeue_submitted" if action.kind == "queue-dequeue" else "check_submitted"] = (
                True
            )
            if data.get("failure"):
                state["failure_check_attempted"] = True
        if data.get("failure_check_attempted"):
            state["failure_check_attempted"] = True
            state["external_wait"] = data["external_wait"]


def apply_worker(runtime, snapshot, unit, receipt) -> None:
    state = unit.evidence.setdefault("queue", {})
    group = QueueGroup(**receipt.get("queue_group", state["group"]))
    if group.digest != state.get("group_digest"):
        state["observed_at"] = 0
        return
    if receipt["outcome"] == "queue_verified":
        state["verification"] = receipt["verification"]
    else:
        state["verification"] = receipt.get("verification")
        state["failure"] = {
            "reason": receipt.get("reason")
            or receipt.get("error")
            or "combined candidate verification failed",
            "group": asdict(group),
            "verification": receipt.get("verification"),
        }
        state["needs_dequeue"] = True
