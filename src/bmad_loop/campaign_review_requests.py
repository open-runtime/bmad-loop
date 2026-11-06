"""Exact-head reviewer recruitment with observable, non-replayed requests.

The coordinator persists the returned intent before execution and retains its
confirmation for the candidate. Removing a request never authorizes a second
POST. GitHub review approval remains a separate publication requirement.
"""

from __future__ import annotations

import re
from dataclasses import asdict, replace

from .campaign_github import GithubError, MutationIntent
from .campaign_publication import Candidate, PublicationController, PublicationObservation


def _observe(controller: PublicationController, candidate: Candidate) -> PublicationObservation:
    observation = controller.inspect(candidate.pr_number)
    pr = observation.pr
    if (
        observation.repository != candidate.repository
        or controller.gateway.repository != candidate.repository
        or pr.get("head", {}).get("sha") != candidate.head
        or pr.get("base", {}).get("ref") != candidate.target
        or pr.get("base", {}).get("repo", {}).get("full_name") != candidate.repository
        or pr.get("state") != "open"
        or pr.get("draft") is not False
        or pr.get("merged") is not False
        or not 0 <= controller.clock() - observation.observed_at <= controller.max_observation_age_s
    ):
        raise GithubError("review request requires the current owned, ready PR head")
    return observation


def _answered(observation: PublicationObservation, head: str) -> set[str]:
    return {
        str(row.get("user", {}).get("login", "")).casefold()
        for row in observation.reviews
        if row.get("commit_id") == head
        and row.get("state") in {"APPROVED", "CHANGES_REQUESTED", "COMMENTED"}
    }


def prepare_requests(
    controller: PublicationController,
    candidate: Candidate,
    *,
    users: tuple[str, ...] = (),
    teams: tuple[str, ...] = (),
    action_id: str,
    generation: int,
) -> MutationIntent:
    """Request only absent, unanswered configured reviewers; preserve human requests."""
    if any(not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", value) for value in (*users, *teams)):
        raise GithubError("reviewers must be GitHub logins and teams must be organization slugs")
    observation = _observe(controller, candidate)
    author = str(observation.pr.get("user", {}).get("login", "")).casefold()
    if author in {user.casefold() for user in users}:
        raise GithubError("PR author cannot be recruited as its approving reviewer")
    existing = {name.casefold() for name in observation.requested_reviewers}
    existing |= _answered(observation, candidate.head)
    existing_teams = {name.casefold() for name in observation.requested_teams}
    wanted_users = sorted({name for name in users if name.casefold() not in existing})
    wanted_teams = sorted({name for name in teams if name.casefold() not in existing_teams})
    intent = MutationIntent(
        action_id,
        "review_request",
        "POST",
        f"{controller.gateway.root}/pulls/{candidate.pr_number}/requested_reviewers",
        {"reviewers": wanted_users, "team_reviewers": wanted_teams},
        generation=generation,
        coordinates={"candidate": asdict(candidate)},
    )
    if not wanted_users and not wanted_teams:
        return replace(intent, state="confirmed", receipt={"already_requested_or_answered": True})
    return intent


def reconcile_requests(controller: PublicationController, intent: MutationIntent) -> MutationIntent:
    """Read requests or current-head answers back; absence never permits blind POST."""
    if intent.kind != "review_request":
        raise GithubError("not a reviewer recruitment intent")
    candidate = Candidate(**(intent.coordinates or {})["candidate"])
    observation = _observe(controller, candidate)
    users = {name.casefold() for name in observation.requested_reviewers}
    users |= _answered(observation, candidate.head)
    teams = {name.casefold() for name in observation.requested_teams}
    if {name.casefold() for name in intent.payload["reviewers"]} <= users and {
        name.casefold() for name in intent.payload["team_reviewers"]
    } <= teams:
        return replace(
            intent,
            state="confirmed",
            receipt={"head": candidate.head, "users": sorted(users), "teams": sorted(teams)},
            error="",
        )
    return replace(
        intent,
        state="sent_unknown",
        error="review recruitment remains uncertain; no blind duplicate request",
    )


def execute_requests(
    controller: PublicationController, intent: MutationIntent, *, generation: int
) -> MutationIntent:
    """One authorized send, followed by independent readback even after a lost acknowledgement."""
    candidate = Candidate(**(intent.coordinates or {})["candidate"])
    _observe(controller, candidate)
    if intent.kind != "review_request" or intent.path != (
        f"{controller.gateway.root}/pulls/{candidate.pr_number}/requested_reviewers"
    ):
        raise GithubError("review request coordinates differ from its outbound operation")
    result = controller.gateway.execute(intent, generation=generation)
    if result.state == "rejected":
        return result
    return reconcile_requests(controller, result)
