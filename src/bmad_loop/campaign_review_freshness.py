"""Refresh stale publication reviews without abandoning uncertain merge writes.

Graph and source changes can arrive after a successful review. A remote wait is
not a recovery path for obsolete local evidence: return to verification/review.
Already submitted and delivered receipts remain reconciliation and history.
"""

from .campaign_model import UnitState
from .campaign_store import add_event, transition


def current(snapshot, unit) -> bool:
    review = unit.evidence.get("review", {})
    integrated = unit.evidence.get("integrated", {})
    coordinates = review.get("validated", {}).get("coordinates", {})
    return bool(
        integrated.get("head")
        and review.get("head") == integrated["head"]
        and all(
            coordinates.get(key) == value
            for key, value in {
                "head": integrated["head"],
                "base": integrated.get("base"),
                "graph_revision": snapshot.graph_revision,
                "source_revision": unit.bindings.get("source_revision", ""),
            }.items()
        )
        and str(coordinates.get("config_revision")) == str(snapshot.config_revision)
    )


def stale(snapshot, unit) -> bool:
    from .campaign_native_review_flow import current as pair_current
    from .campaign_native_review_flow import required

    return bool(
        (unit.evidence.get("review") and not current(snapshot, unit))
        or (
            required(snapshot)
            and unit.evidence.get("native_review_pair")
            and not pair_current(snapshot, unit)
        )
    )


def preserve(snapshot, unit) -> bool:
    return unit.state in {
        UnitState.MERGE_REQUESTED,
        UnitState.MERGED_PENDING_SYNC,
        UnitState.DELIVERED,
        UnitState.CANCELED,
    } or any(
        action.unit == unit.id
        and action.kind == "github"
        and action.payload.get("purpose") == "merge"
        and action.state in {"prepared", "sent_unknown"}
        for action in snapshot.outbox
    )


def invalidate(snapshot, unit) -> bool:
    if preserve(snapshot, unit):
        return False
    for key in ("native_review_pair", "review"):
        unit.evidence.pop(key, None)
    if unit.state in {UnitState.REVIEWING, UnitState.READY_TO_PUBLISH}:
        transition(snapshot, unit.id, UnitState.SYNCING)
    add_event(snapshot, "publication-review-invalidated", unit=unit.id)
    return True
