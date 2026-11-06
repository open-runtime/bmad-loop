"""Compact superseded observations, preserving mutation/ownership authority.

Only these completed observations have authoritative current projections in
bindings/evidence. Launches, checkpoints, GitHub intents and all other effects
remain hot because recovery and semantic synchronization consume their history.
This bounds repeated polling history, not the complete campaign authority.
"""

from __future__ import annotations

from typing import Any

OBSERVATION_KINDS = frozenset(
    {
        "inventory",
        "capacity",
        "source-observation",
        "observe-worker",
        "checkpoint-progress",
        "communication",
        "remote-wait",
    }
)


def split(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Keep the latest completed observation per unit/kind plus all other effects."""
    kept, archived, seen = [], [], set()
    for row in reversed(rows):
        pure = (
            row["state"] == "confirmed"
            and row["kind"] in OBSERVATION_KINDS
            and not row.get("payload", {}).get("intent")
            and not row.get("receipt", {}).get("intent")
            and not row.get("payload", {}).get("tracking_key")
        )
        key = (row["unit"], row["kind"])
        if pure and key in seen:
            archived.append(row)
        else:
            kept.append(row)
            if pure:
                seen.add(key)
    return list(reversed(kept)), list(reversed(archived))
