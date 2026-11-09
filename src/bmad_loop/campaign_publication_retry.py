"""Bound repeated current-base acceptance without weakening publication gates.

The counter belongs to the authoritative unit snapshot and survives restarts.
An exhausted candidate requests an operator retry; release still performs the
ordinary quiescence/ownership checks and cannot reuse invalidated acceptance.
"""

from .campaign_model import CampaignSnapshot, Hold, UnitState
from .campaign_store import add_event, transition


def invalidate(snapshot: CampaignSnapshot, unit_id: str, action_id: str, base: str) -> Hold | None:
    unit = snapshot.units[unit_id]
    previous = unit.evidence.get("integrated", {}).get("base", "")
    for key in ("integrated", "verification", "review", "native_review_pair", "pushed"):
        unit.evidence.pop(key, None)
    if unit.state != UnitState.SYNCING:
        transition(snapshot, unit_id, UnitState.SYNCING)
    if not base or base == previous:
        return None
    attempts = unit.evidence.setdefault("base_refreshes", [])
    if any(row["action_id"] == action_id for row in attempts):
        return None
    attempts.append({"action_id": action_id, "previous_base": previous, "observed_base": base})
    limit = snapshot.spec.github.max_base_refreshes
    exhausted = len(attempts) >= limit
    add_event(
        snapshot,
        "publication-starved" if exhausted else "publication-base-moved",
        unit=unit_id,
        count=len(attempts),
        limit=limit,
        previous_base=previous,
        observed_base=base,
    )
    if exhausted:
        return Hold(
            "operator",
            f"main moved during acceptance {len(attempts)} times (limit {limit}); "
            "publication is starved; release explicitly to retry against fresh main",
            id="publication-starved:" + action_id,
            actor="campaign",
            evidence={"base_refreshes": len(attempts), "limit": limit, "base": base},
        )
    return None
