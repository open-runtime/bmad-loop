"""Wake decision review from an authenticated reply to its exact owner question."""

import dataclasses

from .campaign_decision_sources import configured_locations, controller_comment, github_provider
from .campaign_model import CampaignError
from .campaign_store import add_event


def question(snapshot, unit_id, decision_id):
    from .campaign_resolution_flow import _coordinates

    unit = snapshot.units.get(unit_id)
    receipt = snapshot.bindings.get("decision_receipts", {}).get(decision_id, {})
    coordinates = dataclasses.asdict(_coordinates(snapshot, unit_id)) if unit else None
    if (
        unit is None
        or not unit.hold
        or unit.hold.id != receipt.get("hold_id")
        or receipt.get("clear_hold") is not False
        or receipt.get("disposition") not in {"needs_new_owner_decision", "conflicting_evidence"}
        or receipt.get("coordinates") != coordinates
        or unit.evidence.get("decision_advice", {}).get("receipt", decision_id) != decision_id
        or not any(
            row.get("receipt") == decision_id
            for row in snapshot.bindings.get("decision_requests", {}).values()
        )
    ):
        raise CampaignError("answer requires an outstanding decision on this exact unit and head")
    return receipt


def observe(runtime, snapshot, unit_id, decision_id, number, comment_id):
    receipt = question(snapshot, unit_id, decision_id)
    unit = snapshot.units[unit_id]
    numbers = {
        (unit.bindings.get(key) or {}).get("number") for key in ("issue", "pr", "epic_issue")
    }
    if (
        not snapshot.spec.github.enabled
        or any(type(i) is not int or i <= 0 for i in (number, comment_id))
        or number not in numbers
    ):
        raise CampaignError(
            "answer must reference a comment on this unit's mapped GitHub issue or PR"
        )
    identity = f"gh:comment:{number}:{comment_id}"
    configured = configured_locations(snapshot.spec.decisions.sources, scope=(unit_id,))
    pins = {
        loc.expected_author
        for loc in configured
        if loc.kind == "github"
        and loc.expected_author
        and loc.location.partition("@")[0] in {identity, f"gh:issue:{number}", f"gh:pull:{number}"}
    }
    if len(pins) > 1:
        raise CampaignError("conflicting owner identities for decision reply")
    if pins:
        identity += "@" + next(iter(pins))
    (location,) = configured_locations((identity,), scope=(unit_id,))
    source = github_provider(runtime.gateway(snapshot))(location)
    if (
        not source.origin_verified
        or source.authority != "owner"
        or controller_comment(
            snapshot, source, prior_projections=runtime._decision_projections(snapshot)
        )
        or not source.content.strip().startswith(f"Decision {decision_id}\n")
        or not source.content.strip()[len(f"Decision {decision_id}\n") :].strip()
    ):
        raise CampaignError(
            "answer requires an authenticated owner reply beginning with the exact decision ID"
        )
    return {
        "decision_id": decision_id,
        "hold_id": receipt["hold_id"],
        "source": source.identity,
        "revision": source.revision,
        "actor": source.author_id,
        "url": source.url,
    }


def apply(snapshot, unit_id, evidence):
    receipt = question(snapshot, unit_id, evidence["decision_id"])
    if evidence["hold_id"] != receipt["hold_id"]:
        raise CampaignError("owner reply belongs to an obsolete hold")
    unit = snapshot.units[unit_id]
    records = unit.evidence.setdefault("owner_answers", {})
    if evidence["revision"] in records:
        return
    records[evidence["revision"]] = dict(evidence)
    # The ordinary decision worker fetches the mapped comment again and must
    # satisfy the original hold-specific rules. This receipt only wakes review.
    unit.evidence["decision_input_revision"] = evidence["revision"]
    add_event(
        snapshot,
        "owner-answer-observed",
        unit=unit_id,
        decision=evidence["decision_id"],
        actor=evidence["actor"],
    )
