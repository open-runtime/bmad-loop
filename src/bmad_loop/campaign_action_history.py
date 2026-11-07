"""Explicit historical reads combine frozen cold receipts with current hot effects.

The scheduler scans only the hot outbox. Historical readers request indexed
records by native object, unit, kind or stable action ID. Cold values are detached
copies, never mutable authority. Creation order remains stable across archival;
later commits cannot become visible to an earlier worker snapshot.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from typing import Any

from .campaign_database import EventJournal
from .campaign_model import CampaignSnapshot, OutboxAction


def _matches(row: OutboxAction, query: dict[str, Any]) -> bool:
    intent = row.receipt.get("intent") or row.payload.get("intent") or {}
    native = (intent.get("receipt") or {}).get("id")
    if native is None:
        native = (intent.get("coordinates") or {}).get("id", "")
    return (
        (not query.get("kinds") or row.kind in query["kinds"])
        and (query.get("unit") is None or row.unit == query["unit"])
        and (query.get("action_id") is None or row.id == query["action_id"])
        and (query.get("native_id") is None or str(native) == query["native_id"])
        and (
            query.get("tracking_key") is None
            or row.payload.get("tracking_key", "") == query["tracking_key"]
        )
    )


def actions(
    snapshot: CampaignSnapshot,
    *,
    kinds: tuple[str, ...] = (),
    unit: str | None = None,
    native_id: str | None = None,
    tracking_key: str | None = None,
    action_id: str | None = None,
    reverse: bool = False,
) -> list[OutboxAction]:
    query = dict(
        kinds=kinds, unit=unit, native_id=native_id, tracking_key=tracking_key, action_id=action_id
    )
    hot = [row for row in snapshot.outbox if _matches(row, query)]
    if not isinstance(snapshot.events, EventJournal):
        return list(reversed(hot)) if reverse else hot
    database = snapshot.events.database
    indexed = {}
    after = 0
    while rows := database.actions(through=snapshot.revision, after=after, **query):
        for item in rows:
            row = OutboxAction(**item["action"])
            indexed[row.id] = (item["sequence"], row)
        after = rows[-1]["sequence"]
    positions = database.action_positions([row.id for row in hot])
    for index, row in enumerate(hot):
        # New, uncommitted actions follow every persisted creation sequence.
        indexed[row.id] = (positions.get(row.id, 2**63 + index), row)
    return [row for _, row in sorted(indexed.values(), key=lambda item: item[0], reverse=reverse)]


def action(snapshot: CampaignSnapshot, identity: str) -> OutboxAction | None:
    hot = next((row for row in snapshot.outbox if row.id == identity), None)
    if hot is not None:
        return hot
    if isinstance(snapshot.events, EventJournal):
        rows = snapshot.events.database.actions(
            through=snapshot.revision, action_id=identity, limit=1
        )
        if rows:
            return OutboxAction(**rows[0]["action"])
    return None


def confirmation_revision(snapshot: CampaignSnapshot, identity: str) -> int:
    events = snapshot.events
    if isinstance(events, EventJournal):
        confirmed = events.database.confirmation_revision(identity, through=events.base_count)
        pending = events.pending
    else:
        confirmed, pending = -1, events
    return max(
        [
            confirmed,
            *(
                row.get("revision", -1)
                for row in pending
                if row.get("kind") == "effect-confirmed" and row.get("action") == identity
            ),
        ]
    )


def counts(snapshot: CampaignSnapshot) -> dict[str, int]:
    result = {state: 0 for state in ("prepared", "sent_unknown", "confirmed", "rejected")}
    if isinstance(snapshot.events, EventJournal):
        result.update(snapshot.events.database.action_counts(through=snapshot.revision))
    for row in snapshot.outbox:
        result[row.state] += 1
    return result


class TrackingReceipts(Mapping[str, Any]):
    """Resolve hierarchy's stable keys lazily instead of loading unrelated history."""

    def __init__(self, snapshot: CampaignSnapshot):
        self.snapshot = snapshot

    def __getitem__(self, key: str) -> Any:
        from .campaign_github import MutationIntent

        for row in actions(self.snapshot, tracking_key=key, reverse=True):
            if row.receipt.get("intent"):
                return MutationIntent.from_dict(row.receipt["intent"])
        raise KeyError(key)

    def __iter__(self) -> Iterator[str]:
        return iter(
            dict.fromkeys(
                row.payload["tracking_key"]
                for row in actions(self.snapshot)
                if row.payload.get("tracking_key") and row.receipt.get("intent")
            )
        )

    def __len__(self) -> int:
        return sum(1 for _ in self)

    def __bool__(self) -> bool:
        # TrackingCoordinator uses ``receipts or {}``; avoid materializing all
        # historical keys merely to choose this already-valid lookup provider.
        return True
