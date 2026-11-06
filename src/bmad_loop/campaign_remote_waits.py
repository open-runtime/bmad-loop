"""Reobserve temporary remote vetoes without turning them into owner decisions.

Clearing a wait only returns the unit to full acceptance. This module never
approves a candidate, waives a check or submits a merge.
"""

from __future__ import annotations

from dataclasses import asdict

from .campaign_publication import (
    Candidate,
    PublicationController,
    PublicationRequirements,
    _check_reason,
)
from .campaign_remote_feedback import collect


def observe(runtime, snapshot, action):
    unit = snapshot.units[action.unit]
    hold = unit.hold
    result = {"hold_id": action.payload["hold_id"], "ready_to_reaccept": False}
    if not hold or hold.id != action.payload["hold_id"]:
        return result
    raw = hold.evidence.get("candidate") or hold.evidence.get("feedback", {}).get("candidate")
    if not raw:
        return {**result, "reasons": ["Remote wait has no bound candidate; inspect prior receipt"]}
    candidate = Candidate(**raw)
    requirements = PublicationRequirements.from_spec(snapshot.spec.github)
    controller = PublicationController(
        runtime.gateway(snapshot), requirements=requirements, clock=runtime.clock
    )
    observed = controller.inspect(candidate.pr_number)
    pr = observed.pr
    reasons = []
    if (
        observed.repository != candidate.repository
        or pr.get("number") != candidate.pr_number
        or pr.get("head", {}).get("sha") != candidate.head
        or pr.get("base", {}).get("ref") != candidate.target
        or pr.get("base", {}).get("repo", {}).get("full_name") != candidate.repository
        or pr.get("state") != "open"
        or pr.get("merged") is not False
        or not observed.rules.complete
        or not 0 <= controller.clock() - observed.observed_at <= controller.max_observation_age_s
    ):
        return {**result, "reasons": ["Remote identity/state/rules observation is not current"]}
    labels = {str(row.get("name", "")).lower() for row in observed.issue.get("labels", [])}
    if labels.intersection(label.lower() for label in requirements.hold_labels):
        reasons.append("Remote owner/review hold is active")
    feedback = collect(candidate, observed, now=controller.clock(), requirements=requirements)
    previous = hold.evidence.get("feedback", {}).get("digest")
    # A genuinely new defect gets a fresh full acceptance/repair attempt. An
    # unchanged attempted defect remains a wait; no repeated model diagnosis.
    changed_defect = feedback is not None and feedback.digest != previous
    if feedback is not None and not changed_defect:
        reasons.append("Remote defect revision was already attempted")
    if not changed_defect:
        for check in requirements.checks(observed.rules.checks):
            reason = _check_reason(check, observed.checks, observed.statuses, candidate.head)
            if reason:
                reasons.append(reason)
        if observed.requested_reviewers or observed.requested_teams:
            reasons.append("Outstanding requested GitHub reviews remain")
        latest = {}
        for row in sorted(observed.reviews, key=lambda row: row.get("id", 0)):
            if row.get("state") not in {"COMMENTED", "PENDING"}:
                latest[str(row.get("user", {}).get("login", ""))] = row
        approvers = {
            name
            for name, row in latest.items()
            if name
            and name != pr.get("user", {}).get("login")
            and row.get("state") == "APPROVED"
            and row.get("commit_id") == candidate.head
        }
        if len(approvers) < observed.rules.required_reviews:
            reasons.append("Required current-head GitHub approvals are missing")
        if (
            observed.rules.required_reviews or observed.rules.last_push_approval
        ) and observed.review_decision != "APPROVED":
            reasons.append("Eligible GitHub approval is pending")
        if any(row.get("state") == "CHANGES_REQUESTED" for row in latest.values()):
            reasons.append("A reviewer requests changes")
        if any(row.get("isResolved") is not True for row in observed.threads):
            reasons.append("Unresolved review threads remain")
    return {
        **result,
        "ready_to_reaccept": not reasons,
        "reasons": reasons,
        "observed_at": observed.observed_at,
        "candidate": asdict(candidate),
    }
