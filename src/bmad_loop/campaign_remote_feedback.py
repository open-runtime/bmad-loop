"""Turn fresh authenticated remote defects into bounded same-PR repair holds.

Pending CI or missing approval waits. Concrete failed required checks, current
changes-requested reviews and current unresolved threads can request diagnosis;
they cannot approve, resolve threads, waive checks or expand ownership. The
receipt binds the exact H and rules revision and is revalidated before repair.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Any

from .campaign_model import CampaignError, CampaignSnapshot, Hold, UnitState
from .campaign_publication import Candidate, PublicationObservation
from .campaign_store import add_event, transition


@dataclass(frozen=True)
class RemoteFeedback:
    candidate: Candidate
    rules_fingerprint: str
    defects: tuple[dict[str, Any], ...]
    digest: str
    observed_at: float


def collect(
    candidate: Candidate,
    observation: PublicationObservation,
    *,
    now: float,
    hold_labels: tuple[str, ...],
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
    if labels.intersection(label.lower() for label in hold_labels):
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
    for required in observation.rules.checks:
        checks = [
            row
            for row in observation.checks
            if row.get("name") == required.context
            and row.get("head_sha") == candidate.head
            and (required.app_id is None or (row.get("app") or {}).get("id") == required.app_id)
        ]
        if checks:
            newest = max(checks, key=lambda row: int(row.get("id", 0)))
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
                row for row in observation.statuses if row.get("context") == required.context
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
    if observation.rules.required_reviews:
        latest: dict[str, dict[str, Any]] = {}
        for review in sorted(observation.reviews, key=lambda row: int(row.get("id", 0))):
            actor = (review.get("user") or {}).get("login")
            if actor:
                latest[actor] = review
        for actor, review in latest.items():
            if (
                review.get("id")
                and review.get("state") == "CHANGES_REQUESTED"
                and review.get("commit_id") == candidate.head
            ):
                defects.append(
                    {
                        "kind": "changes_requested",
                        "id": str(review["id"]),
                        "author": actor,
                        "head": candidate.head,
                        "revision": review.get("submitted_at") or str(review["id"]),
                        "body": review.get("body", ""),
                        "url": review.get("html_url", ""),
                    }
                )
    if observation.rules.conversation_resolution:
        for thread in observation.threads:
            if (
                thread.get("id")
                and thread.get("isResolved") is False
                and thread.get("isOutdated") is False
            ):
                defects.append(
                    {
                        "kind": "review_thread",
                        "id": str(thread["id"]),
                        "head": candidate.head,
                        "path": thread.get("path", ""),
                        "line": thread.get("line"),
                        "comments": thread.get("comments"),
                    }
                )
    if not defects:
        return None
    body = {
        "candidate": asdict(candidate),
        "rules": observation.rules.fingerprint,
        "defects": defects,
    }
    digest = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
    return RemoteFeedback(
        candidate, observation.rules.fingerprint, tuple(defects), digest, observation.observed_at
    )


def apply(snapshot: CampaignSnapshot, unit_id: str, feedback: RemoteFeedback) -> None:
    unit = snapshot.units[unit_id]
    candidate = feedback.candidate
    integrated = unit.evidence.get("integrated", {})
    if (
        unit.state != UnitState.READY_TO_PUBLISH
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
    for name in ("integrated", "pushed", "verification", "review"):
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


def repair_current(
    gateway, feedback: dict[str, Any], *, now: float, hold_labels: tuple[str, ...]
) -> bool:
    """Refresh actual remote authority before the bounded writer starts."""
    from .campaign_publication import PublicationController

    candidate = Candidate(**feedback["candidate"])
    observation = PublicationController(gateway).inspect(candidate.pr_number)
    fresh = collect(candidate, observation, now=now, hold_labels=hold_labels)
    return fresh is not None and fresh.digest == feedback["digest"]
