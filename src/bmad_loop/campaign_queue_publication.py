"""Recheck publication authority before a successful G check can release a queue.

The check POST can itself cause a merge. Its durable preparation is therefore
not permission to ignore a later hold, review request or changed candidate.
Lost POST acknowledgments still use read-only check reconciliation.
"""

from dataclasses import asdict

from .campaign_github import GithubError
from .campaign_model import CampaignError
from .campaign_publication import PublicationController, PublicationRequirements
from .campaign_queue import observe_group
from .campaign_supervisor import Observation


def guard_success(runtime, snapshot, action, intent):
    coordinates = intent.coordinates or {}
    if (
        intent.kind != "queue_check"
        or intent.payload.get("conclusion") != "success"
        or coordinates.get("candidate_kind") == "pr-head"
    ):
        return None
    try:
        return _guard_success(runtime, snapshot, action, intent)
    except (CampaignError, GithubError, OSError, ValueError) as exc:
        # These are observations only: the check POST has not begun. Do not
        # turn a temporary read failure into an uncertain create that can never
        # be replayed. Re-arm through fresh queue observation instead.
        return Observation(
            "confirmed",
            {
                "queue_success_blocked": True,
                "group_digest": coordinates.get("group_digest"),
                "needs_dequeue": False,
                "gate_reasons": [f"queue publication observation unavailable: {exc}"],
            },
        )


def _guard_success(runtime, snapshot, action, intent):
    coordinates = intent.coordinates or {}
    current = runtime.store.load(snapshot.id)
    unit = current.units[action.unit]
    candidate = runtime._candidate(current, unit)
    reasons = []
    if current.phase != "active":
        reasons.append("campaign is no longer admitting publication")
    if coordinates.get("candidate") != asdict(candidate):
        reasons.append("accepted candidate changed before queue check submission")
    gateway = runtime.gateway(current)
    group = observe_group(gateway, candidate, clock=runtime.clock)
    same_group = group is not None and group.digest == coordinates.get("group_digest")
    if not same_group:
        reasons.append("queue candidate changed before check submission")
    controller = PublicationController(
        gateway,
        clock=runtime.clock,
        requirements=PublicationRequirements.from_spec(current.spec.github),
    )
    observed = controller.inspect(candidate.pr_number)
    gate = controller.evaluate(
        candidate,
        runtime._publication_evidence(current, unit),
        observed,
        merge_mode="queue",
        independent_families=current.spec.github.require_independent_review,
        queue_evidence_context=current.spec.github.candidate_check_context,
        queued_base=observed.base_head,
    )
    reasons.extend(gate.reasons)
    if coordinates.get("rules_fingerprint") != observed.rules.fingerprint:
        reasons.append("publication rules changed before queue check submission")
    if not reasons:
        return None
    return Observation(
        "confirmed",
        {
            "queue_success_blocked": True,
            "group_digest": coordinates.get("group_digest"),
            "needs_dequeue": same_group,
            "gate_reasons": reasons,
        },
    )


def apply_blocked(snapshot, action, data):
    unit = snapshot.units[action.unit]
    state = unit.evidence.setdefault("queue", {})
    if state.get("group_digest") != data.get("group_digest"):
        return
    state.pop("check_submitted", None)
    state["observed_at"] = 0
    if data.get("needs_dequeue"):
        state.update(needs_dequeue=True, gate_reasons=data["gate_reasons"])


def release_suppressed(unit, action):
    """A confirmed no-write effect must not retain an in-flight projection flag."""
    purpose = action.payload.get("purpose")
    if action.kind != "github" or purpose not in {"queue-check", "queue-dequeue"}:
        return
    state = unit.evidence.get("queue", {})
    intent = action.payload.get("intent", {})
    coordinates = intent.get("coordinates") or {}
    if purpose == "queue-check" and coordinates.get("group_digest") != state.get("group_digest"):
        return
    state.pop("check_submitted" if purpose == "queue-check" else "dequeue_submitted", None)
    state["observed_at"] = 0
