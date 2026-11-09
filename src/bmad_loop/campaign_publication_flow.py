"""Coordinator seams for reviewer recruitment, H checks and project executors.

All remote writes are durable GitHub-kind outbox actions. H verification runs in
its own disposable worker and consumes verification capacity before queue entry.
"""

from __future__ import annotations

from dataclasses import asdict, replace
from pathlib import Path

from .campaign_merge_adapter import AdapterReceipt, execute_project_merge, reconcile_project_merge
from .campaign_model import Hold, UnitState
from .campaign_publication import PublicationController, PublicationRequirements
from .campaign_queue import allocate_checkout, capability, head_candidate, prepare_pr_check
from .campaign_review_requests import execute_requests, prepare_requests, reconcile_requests
from .campaign_store import add_event
from .campaign_supervisor import Observation, Proposal


def record_stage_writer(unit, receipt):
    """A repair, planning or closure writer does not erase earlier authors."""
    from .campaign_attribution import record_stage_writer as record

    record(unit, receipt)


def controller(runtime, snapshot):
    return PublicationController(
        runtime.gateway(snapshot),
        clock=runtime.clock,
        requirements=PublicationRequirements.from_spec(snapshot.spec.github),
    )


def proposals(runtime, snapshot, unit, reserve):
    spec = snapshot.spec.github
    if not (spec.review_users or spec.review_teams) and (
        spec.merge_mode != "queue" or spec.candidate_publish != "native"
    ):
        return (Proposal("prepare-merge", unit.id),)
    candidate = runtime._candidate(snapshot, unit)
    if (spec.review_users or spec.review_teams) and unit.bindings.get(
        "recruited_head"
    ) != candidate.head:
        return (Proposal("prepare-review-requests", unit.id),)
    if spec.merge_mode != "queue" or spec.candidate_publish != "native":
        return (Proposal("prepare-merge", unit.id),)
    state = unit.evidence.get("pr_head_check", {})
    if state.get("candidate") != asdict(candidate) or not state.get("workspace"):
        return (Proposal("pr-head-checkout", unit.id),)
    if state.get("failure"):
        return ()
    if not state.get("verification"):
        if not reserve("verify"):
            return ()
        return (
            Proposal(
                "launch",
                unit.id,
                {
                    "kind": "queue_verify",
                    "candidate_kind": "pr-head",
                    "queue_group": state["group"],
                    "queue_candidate": asdict(candidate),
                    "verification_workspace": state["workspace"],
                    "commands": list(spec.candidate_command),
                    "check_context": spec.candidate_check_context,
                    "rules_fingerprint": state["rules_fingerprint"],
                },
            ),
        )
    if not state.get("published"):
        return (Proposal("pr-head-publish", unit.id),)
    return (Proposal("prepare-merge", unit.id),)


def prepare_review_effect(runtime, snapshot, action):
    intent = prepare_requests(
        controller(runtime, snapshot),
        runtime._candidate(snapshot, snapshot.units[action.unit]),
        users=snapshot.spec.github.review_users,
        teams=snapshot.spec.github.review_teams,
        action_id=action.id + "-reviews",
        generation=snapshot.generation,
    )
    return Observation("confirmed", {"intent": intent.to_dict(), "purpose": "review-requests"})


def head_checkout_effect(runtime, snapshot, action):
    unit = snapshot.units[action.unit]
    candidate = runtime._candidate(snapshot, unit)
    remote = controller(runtime, snapshot).inspect(candidate.pr_number)
    if remote.pr.get("head", {}).get("sha") != candidate.head:
        from .campaign_remote_head import checked as observe

        return Observation("confirmed", observe(runtime, snapshot, unit, pr=remote.pr))
    if remote.base_head != candidate.base:
        return Observation("confirmed", {"needs_integration": True, "base": remote.base_head})
    group = head_candidate(candidate, observed_at=runtime.clock())
    root = Path(
        snapshot.spec.paradev.worktree_root
        if snapshot.spec.paradev.reflect
        else snapshot.spec.worktree_root
    )
    workspace = allocate_checkout(
        runtime.project,
        root,
        runtime.store.directory(snapshot.id) / "head-allocations",
        group,
        remote=snapshot.spec.remote,
    )
    return Observation(
        "confirmed",
        {
            "candidate": asdict(candidate),
            "group": asdict(group),
            "workspace": str(workspace),
            "rules_fingerprint": remote.rules.fingerprint,
        },
    )


def head_publish_effect(runtime, snapshot, action):
    unit = snapshot.units[action.unit]
    state = unit.evidence["pr_head_check"]
    candidate = runtime._candidate(snapshot, unit)
    remote = controller(runtime, snapshot).inspect(candidate.pr_number)
    if remote.pr.get("head", {}).get("sha") != candidate.head:
        from .campaign_remote_head import checked as observe

        return Observation("confirmed", observe(runtime, snapshot, unit, pr=remote.pr))
    if (
        state["candidate"] != asdict(candidate)
        or remote.pr.get("head", {}).get("sha") != candidate.head
        or remote.base_head != candidate.base
    ):
        return Observation("confirmed", {"needs_integration": True, "base": remote.base_head})
    if remote.rules.fingerprint != state["rules_fingerprint"]:
        return Observation("confirmed", {"invalidate_head_check": True})
    producer = capability(
        snapshot.spec.github,
        snapshot.repository,
        runtime.project,
        remote.rules,
        clock=runtime.clock,
    )
    if not producer.ready:
        return Observation(
            "waiting", {"producer": asdict(producer)}, producer.reason, runtime.clock() + 15
        )
    receipt = AdapterReceipt.from_dict(state["verification"])
    # Local immutable H/B proof remains valid; current producer/PR observation is
    # the freshness boundary, not how long a completed verification took.
    receipt = replace(receipt, observed_at=runtime.clock())
    intent = prepare_pr_check(
        candidate,
        receipt,
        producer,
        runtime.gateway(snapshot),
        action_id=action.id + "-check",
        generation=snapshot.generation,
        now=runtime.clock(),
    )
    return Observation("confirmed", {"intent": intent.to_dict(), "purpose": "pr-head-check"})


def apply_head_worker(runtime, snapshot, unit, receipt):
    state = unit.evidence.setdefault("pr_head_check", {})
    if receipt.get("outcome") != "queue_verified" or receipt.get("candidate_kind") != "pr-head":
        state["failure"] = receipt
        unit.evidence["failure"] = {
            "code": "verification_failed",
            "producer": "campaign.pr-head",
            "id": unit.claim.launch_id,
            "revision": unit.bindings["head"],
            "detail": receipt.get("error", "PR-head candidate verification failed"),
        }
        unit.resume_state = UnitState.SYNCING
        runtime._set_hold(
            unit,
            Hold(
                "technical_failure",
                "PR-head candidate verification failed",
                id=unit.claim.launch_id,
            ),
        )
    else:
        state["verification"] = receipt["verification"]
    add_event(
        snapshot, "pr-head-verification-observed", unit=unit.id, outcome=receipt.get("outcome")
    )


def execute_special(runtime, snapshot, action, intent, *, reconcile=False):
    pub = controller(runtime, snapshot)
    current = replace(intent, generation=snapshot.generation, state="sent_unknown")
    if intent.kind == "native_merge":
        from . import campaign_hold
        from .campaign_native_merge import execute

        def fresh_evidence():
            current_snapshot = runtime.store.load(snapshot.id)
            unit = current_snapshot.units[action.unit]
            evidence = runtime._publication_evidence(current_snapshot, unit)
            return replace(
                evidence,
                held=(
                    evidence.held
                    or current_snapshot.phase != "active"
                    or current_snapshot.generation != snapshot.generation
                    or campaign_hold.active(
                        campaign_hold.channel(runtime.store.directory(snapshot.id), action.unit),
                        campaign=snapshot.id,
                        unit=action.unit,
                    )
                ),
            )

        result = execute(
            current,
            runtime.project,
            runtime.store.directory(snapshot.id) / "native-merge-executions" / intent.action_id,
            pub,
            evidence=fresh_evidence(),
            evidence_provider=fresh_evidence,
            generation=snapshot.generation,
            reconcile=reconcile,
            independent_families=snapshot.spec.github.require_independent_review,
        )
        if (
            result.state == "rejected"
            and (result.receipt or {}).get("needs_integration")
            and result.receipt.get("submission") == "not-submitted"
            and result.receipt.get("definitive") is True
        ):
            remote = {}
            if result.receipt.get("remote_head_changed"):
                from .campaign_remote_head import checked as observe

                remote = observe(runtime, snapshot, snapshot.units[action.unit])
            return Observation(
                "confirmed",
                {
                    "canceled_before_write": True,
                    "needs_integration": True,
                    "base": result.receipt["base"],
                    "retired_native_intent": result.to_dict(),
                    **remote,
                },
            )
    elif intent.kind == "review_request":
        result = (
            reconcile_requests(pub, current)
            if reconcile
            else execute_requests(pub, current, generation=snapshot.generation)
        )
    else:
        arguments = (
            current,
            runtime.project,
            runtime.store.directory(snapshot.id) / "project-merge-executions" / intent.action_id,
            pub,
        )
        if reconcile:
            result = reconcile_project_merge(*arguments, generation=snapshot.generation)
        else:
            result = execute_project_merge(
                *arguments,
                evidence=runtime._publication_evidence(snapshot, snapshot.units[action.unit]),
                generation=snapshot.generation,
                independent_families=snapshot.spec.github.require_independent_review,
            )
    return Observation(
        (
            "confirmed"
            if result.state == "confirmed"
            else "rejected" if result.state == "rejected" else "unknown"
        ),
        {"intent": result.to_dict()},
        result.error,
    )
