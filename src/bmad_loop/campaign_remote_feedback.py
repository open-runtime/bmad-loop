"""Turn fresh authenticated remote defects into bounded same-PR repair holds.

Pending CI or missing approval waits. Concrete failed required checks,
changes-requested reviews and unresolved threads can request diagnosis;
they cannot approve, resolve threads, waive checks or expand ownership. The
receipt binds the exact H and rules revision and is revalidated before repair.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, replace
from typing import Any

from .campaign_model import CampaignError, CampaignSnapshot, CampaignUnit, Hold, UnitState
from .campaign_publication import Candidate, PublicationObservation, PublicationRequirements
from .campaign_store import add_event, transition


@dataclass(frozen=True)
class RemoteFeedback:
    candidate: Candidate
    rules_fingerprint: str
    defects: tuple[dict[str, Any], ...]
    digest: str
    observed_at: float


def _feedback_digest(candidate, rules, defects):
    body = {"candidate": candidate, "rules": rules, "defects": defects}
    return hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()


def collect(
    candidate: Candidate,
    observation: PublicationObservation,
    *,
    now: float,
    requirements: PublicationRequirements,
    max_age_s: float = 30,
) -> RemoteFeedback | None:
    pr = observation.pr
    if (
        observation.repository != candidate.repository
        or pr.get("number") != candidate.pr_number
        or pr.get("head", {}).get("sha") != candidate.head
        or pr.get("base", {}).get("ref") != candidate.target
        or pr.get("base", {}).get("repo", {}).get("full_name") != candidate.repository
        or pr.get("state") != "open"
        or pr.get("merged") is not False
        or not observation.rules.complete
        or not 0 <= now - observation.observed_at <= max_age_s
    ):
        return None
    labels = {str(row.get("name", "")).lower() for row in observation.issue.get("labels", [])}
    if labels.intersection(label.lower() for label in requirements.hold_labels):
        return None
    defects = []
    failed_conclusions = {
        "failure",
        "timed_out",
        "cancelled",
        "action_required",
        "startup_failure",
        "stale",
    }
    for required in requirements.checks(observation.rules.checks):
        checks = [
            row
            for row in observation.checks
            if row.get("name") == required.context
            and row.get("head_sha") == candidate.head
            and (required.app_id is None or (row.get("app") or {}).get("id") == required.app_id)
        ]
        suites = {}
        for row in checks:
            suite = ((row.get("app") or {}).get("id"), (row.get("check_suite") or {}).get("id"))
            if suite not in suites or int(row.get("id", 0)) > int(suites[suite].get("id", 0)):
                suites[suite] = row
        for newest in suites.values():
            if (
                newest.get("id")
                and newest.get("status") == "completed"
                and newest.get("conclusion") in failed_conclusions
            ):
                defects.append(
                    {
                        "kind": "required_check",
                        "id": str(newest["id"]),
                        "app_id": (newest.get("app") or {}).get("id"),
                        "context": required.context,
                        "head": candidate.head,
                        "conclusion": newest["conclusion"],
                        "revision": newest.get("completed_at")
                        or newest.get("updated_at")
                        or str(newest["id"]),
                        "url": newest.get("details_url", ""),
                        "output": newest.get("output"),
                    }
                )
        if required.app_id is None:
            statuses = [
                row
                for row in observation.statuses
                if row.get("context") == required.context
                and row.get("sha", candidate.head) == candidate.head
            ]
            if statuses:
                newest = max(statuses, key=lambda row: int(row.get("id", 0)))
                if newest.get("id") and newest.get("state") in {"failure", "error"}:
                    defects.append(
                        {
                            "kind": "required_status",
                            "id": str(newest["id"]),
                            "context": required.context,
                            "head": candidate.head,
                            "conclusion": newest["state"],
                            "revision": newest.get("updated_at") or str(newest["id"]),
                            "url": newest.get("target_url", ""),
                            "description": newest.get("description", ""),
                        }
                    )
    latest: dict[str, dict[str, Any]] = {}
    for review in sorted(observation.reviews, key=lambda row: int(row.get("id", 0))):
        actor = (review.get("user") or {}).get("login")
        if actor and review.get("state") not in {"COMMENTED", "PENDING"}:
            latest[actor] = review
    for actor, review in latest.items():
        if review.get("id") and review.get("state") == "CHANGES_REQUESTED":
            defects.append(
                {
                    "kind": "changes_requested",
                    "id": str(review["id"]),
                    "author": actor,
                    "head": candidate.head,
                    "review_head": review.get("commit_id", ""),
                    "revision": review.get("submitted_at") or str(review["id"]),
                    "body": review.get("body", ""),
                    "url": review.get("html_url", ""),
                }
            )
    # Publication protects every unresolved thread, including old-head human
    # objections and repositories without a server resolution rule. Diagnosis
    # must retain the same vetoes; it does not acquire thread-resolution power.
    for thread in observation.threads:
        if thread.get("id") and thread.get("isResolved") is False:
            defects.append(
                {
                    "kind": "review_thread",
                    "id": str(thread["id"]),
                    "head": candidate.head,
                    "is_outdated": thread.get("isOutdated"),
                    "path": thread.get("path", ""),
                    "line": thread.get("line"),
                    "comments": thread.get("comments"),
                }
            )
    if not defects:
        return None
    digest = _feedback_digest(asdict(candidate), observation.rules.fingerprint, defects)
    return RemoteFeedback(
        candidate, observation.rules.fingerprint, tuple(defects), digest, observation.observed_at
    )


def apply(snapshot: CampaignSnapshot, unit_id: str, feedback: RemoteFeedback) -> None:
    _apply(snapshot, unit_id, feedback, expected_state=UnitState.READY_TO_PUBLISH)


def apply_after_queue_removal(
    snapshot: CampaignSnapshot, unit_id: str, feedback: RemoteFeedback
) -> None:
    """Use the existing repair budget only after exact-H queue removal readback."""
    queue = snapshot.units[unit_id].evidence.get("queue", {})
    if queue.get("dequeue_confirmed") != asdict(feedback.candidate):
        raise CampaignError(
            "remote repair requires confirmed removal of its exact queued candidate"
        )
    _apply(snapshot, unit_id, feedback, expected_state=UnitState.MERGE_REQUESTED)


def _apply(
    snapshot: CampaignSnapshot, unit_id: str, feedback: RemoteFeedback, *, expected_state: UnitState
) -> None:
    unit = snapshot.units[unit_id]
    candidate = feedback.candidate
    integrated = unit.evidence.get("integrated", {})
    if (
        unit.state != expected_state
        or unit.hold
        or not unit.claim
        or not unit.claim.quiescent
        or candidate.repository != snapshot.repository
        or candidate.unit_id != unit.id
        or candidate.head != integrated.get("head")
        or candidate.base != integrated.get("base")
        or candidate.graph_revision != snapshot.graph_revision
        or candidate.config_revision != str(snapshot.config_revision)
        or candidate.source_revision != unit.bindings.get("source_revision", "")
        or not feedback.defects
    ):
        raise CampaignError(
            "remote defect evidence is stale or the accepted writer is not quiescent"
        )
    seen = unit.evidence.setdefault("remote_feedback_attempted", [])
    if feedback.digest in seen:
        if expected_state == UnitState.MERGE_REQUESTED:
            for name in ("integrated", "pushed", "verification", "review", "native_review_pair"):
                unit.evidence.pop(name, None)
            unit.resume_state = UnitState.SYNCING
            transition(snapshot, unit.id, UnitState.SYNCING)
        unit.hold = Hold(
            "external_wait",
            "remote feedback revision was already attempted; wait for actual check/reviewer/thread update",
            id=feedback.digest,
            actor="github",
            evidence={"feedback": asdict(feedback)},
        )
        return
    seen.append(feedback.digest)
    verification = any(
        row["kind"] in {"required_check", "required_status"} for row in feedback.defects
    )
    cause = "verification_failed" if verification else "review_findings"
    unit.evidence["remote_feedback"] = asdict(feedback)
    unit.evidence["failure"] = {
        "code": cause,
        "producer": "github.authenticated_publication_observation",
        "id": feedback.digest,
        "revision": feedback.digest,
        "detail": "Fresh required remote defects on accepted PR head",
        "paths": [row["path"] for row in feedback.defects if row.get("path")],
    }
    unit.evidence["required_findings"] = [
        row for row in feedback.defects if row["kind"] in {"changes_requested", "review_thread"}
    ]
    for name in ("integrated", "pushed", "verification", "review", "native_review_pair"):
        unit.evidence.pop(name, None)
    unit.resume_state = UnitState.SYNCING
    transition(snapshot, unit.id, UnitState.SYNCING)
    unit.hold = Hold(
        cause,
        "required remote feedback needs bounded same-PR diagnosis and repair",
        id=feedback.digest,
        actor="github",
        evidence={"feedback": asdict(feedback)},
    )
    add_event(
        snapshot,
        "remote-defect-repair-requested",
        unit=unit.id,
        revision=feedback.digest,
        defect_count=len(feedback.defects),
    )


def restore_inconclusive_check_wait(snapshot: CampaignSnapshot, unit: CampaignUnit) -> bool:
    """Keep a legacy check-only hold until ordinary GitHub readback permits reacceptance."""
    hold = unit.hold
    if (
        not hold
        or hold.kind != "verification_failed"
        or hold.actor != "github"
        or unit.state != UnitState.SYNCING
        or unit.resume_state != UnitState.SYNCING
        or not unit.claim
        or not unit.claim.quiescent
        or snapshot.spec.github.merge_mode != "direct-authorized"
        or not snapshot.spec.github.allow_admin_merge
        or snapshot.spec.github.repository != snapshot.repository
        or unit.evidence.get("required_findings")
        or unit.evidence.get("resolution_owner_review_pending")
        or unit.evidence.get("accepted_resolution")
    ):
        return False
    feedback = hold.evidence.get("feedback")
    failure = unit.evidence.get("failure")
    if (
        not isinstance(feedback, dict)
        or feedback != unit.evidence.get("remote_feedback")
        or not isinstance(failure, dict)
        or failure.get("producer") != "github.authenticated_publication_observation"
        or failure.get("code") != hold.kind
        or failure.get("id") != hold.id
        or failure.get("revision") != hold.id
        or hold.id not in unit.evidence.get("remote_feedback_attempted", ())
    ):
        return False
    try:
        candidate = Candidate(**feedback["candidate"])
    except (KeyError, TypeError):
        return False
    pr = unit.bindings.get("pr")
    defects = feedback.get("defects")
    rules = feedback.get("rules_fingerprint")
    if (
        candidate.repository != snapshot.repository
        or candidate.unit_id != unit.id
        or type(candidate.pr_number) is not int
        or candidate.pr_number <= 0
        or candidate.target != snapshot.spec.target
        or not candidate.base
        or not candidate.head
        or candidate.head != unit.bindings.get("head")
        or candidate.pr_number != (pr.get("number") if isinstance(pr, dict) else pr)
        or candidate.source_revision != unit.bindings.get("source_revision", "")
        or candidate.graph_revision != snapshot.graph_revision
        or candidate.config_revision != str(snapshot.config_revision)
        or not isinstance(rules, str)
        or not rules
        or not isinstance(defects, (list, tuple))
        or not defects
        or any(
            not isinstance(row, dict)
            or row.get("kind") != "required_check"
            or row.get("head") != candidate.head
            or row.get("conclusion") not in ("skipped", "neutral")
            or not isinstance(row.get("id"), str)
            or not row["id"].isdecimal()
            or not isinstance(row.get("context"), str)
            or not row["context"].strip()
            or not row.get("revision")
            for row in defects
        )
        or feedback.get("digest") != hold.id
        or _feedback_digest(asdict(candidate), rules, defects) != hold.id
    ):
        return False
    # The wait keeps its native evidence. Archive the superseded repair authority
    # so it cannot be consulted for a later, unrelated repair on the same unit.
    unit.hold = replace(
        hold, kind="external_wait", reason="Waiting for a successful required hosted check"
    )
    unit.evidence.setdefault("inconclusive_check_history", {})[hold.id] = {
        "hold": asdict(hold),
        "failure": failure,
    }
    del unit.evidence["remote_feedback"], unit.evidence["failure"]
    add_event(snapshot, "inconclusive-check-wait-restored", unit=unit.id, hold=hold.id)
    return True


def repair_current(
    gateway, feedback: dict[str, Any], *, now: float, requirements: PublicationRequirements
) -> bool:
    """Refresh actual remote authority before the bounded writer starts."""
    from .campaign_publication import PublicationController

    candidate = Candidate(**feedback["candidate"])
    observation = PublicationController(gateway, requirements=requirements).inspect(
        candidate.pr_number
    )
    fresh = collect(candidate, observation, now=now, requirements=requirements)
    return fresh is not None and fresh.digest == feedback["digest"]
