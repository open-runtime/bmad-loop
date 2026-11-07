"""Explicit operator authority for acceptance reductions in delivered plans.

Plan delivery changes the product definition but cannot waive prior obligations.
The trusted local control inbox carries exact scope and revision, never an LLM's
claim of approval. Existing owner holds and required GitHub reviews remain intact.
"""

from .campaign_acceptance import authorize_amendment, digest, ensure
from .campaign_model import CampaignError, UnitState
from .campaign_store import add_event


def proposal(snapshot, unit_id, revision, reason):
    unit = snapshot.units[unit_id]
    ledger = ensure(unit)
    planning_id = unit.bindings.get("planning_unit")
    planning = snapshot.units.get(planning_id)
    if (
        not planning
        or planning.state != UnitState.DELIVERED
        or not planning.bindings.get("pr")
        or not planning.evidence.get("planning_flow", {}).get("canonical_revision")
    ):
        raise CampaignError("Scope authorization requires a delivered revised planning PR")
    if unit.state == UnitState.DELIVERED or (unit.claim and not unit.claim.quiescent):
        raise CampaignError("Scope authorization requires unfinished quiescent work")
    if any(
        action.unit == unit_id and action.state in {"prepared", "sent_unknown"}
        for action in snapshot.outbox
    ):
        raise CampaignError("Scope authorization waits for the unit's effects to reconcile")
    if type(revision) is not int or revision != ledger["revision"]:
        raise CampaignError("Acceptance revision changed; inspect current scope before authorizing")
    if not isinstance(reason, str) or not reason.strip():
        raise CampaignError("Scope authorization requires an explicit operator reason")
    before = [row["text"] for row in ledger["criteria"] if row["required"]]
    after = list(unit.spec.acceptance)
    if before == after:
        raise CampaignError("The current plan has no pending acceptance amendment")
    return {
        "schema_version": 1,
        "unit": unit_id,
        "revision": revision,
        "before": before,
        "after": after,
        "reason": reason,
        "planning_unit": planning_id,
        "canonical_revision": planning.evidence["planning_flow"]["canonical_revision"],
        "graph_revision": snapshot.graph_revision,
    }


def apply(snapshot, command):
    payload = command.get("payload", {}).get("scope")
    if not isinstance(payload, dict) or not command.get("id"):
        raise CampaignError("Scope authorization requires an exact durable operator request")
    current = proposal(snapshot, command["unit"], payload.get("revision"), payload.get("reason"))
    if payload != current:
        raise CampaignError("Accepted scope changed after the operator authorization")
    authorize_amendment(
        snapshot.units[command["unit"]],
        expected_revision=current["revision"],
        before=tuple(current["before"]),
        after=tuple(current["after"]),
        authority={
            "kind": "owner-decision",
            "id": command["id"],
            "source": "campaign-command:" + command["id"],
            "sha256": digest(current),
            "actor": command.get("actor", "operator"),
            "reason": current["reason"],
        },
    )
    add_event(
        snapshot,
        "acceptance-scope-authorized",
        unit=command["unit"],
        command=command["id"],
        before=current["before"],
        after=current["after"],
        reason=current["reason"],
    )
