"""Indexed immutable effect history outside the frequently rewritten authority.

Creation sequence and archival revision are different coordinates. A completed
older effect can be archived after a newer one, while readers of a frozen
snapshot must never see receipts that became durable after that snapshot.
Prepared and uncertain effects remain in the hot authority. The latest terminal
record of each unit/kind and explicitly referenced claims remain there too.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Iterable

from .campaign_model import CampaignError


def schema(connection: sqlite3.Connection) -> None:
    connection.execute(
        "CREATE TABLE IF NOT EXISTS action_history ("
        "sequence INTEGER PRIMARY KEY, id TEXT UNIQUE NOT NULL, kind TEXT NOT NULL, "
        "unit TEXT NOT NULL, state TEXT NOT NULL, native_id TEXT NOT NULL, "
        "tracking_key TEXT NOT NULL, archived_revision INTEGER, payload TEXT)"
    )
    for name, columns in (
        ("action_history_kind", "kind,sequence"),
        ("action_history_unit", "unit,sequence"),
        ("action_history_native", "native_id,sequence"),
        ("action_history_tracking", "tracking_key,sequence"),
    ):
        # Both identifiers are fixed constants above, never caller input.
        connection.execute(
            f"CREATE INDEX IF NOT EXISTS {name} ON action_history({columns})"
        )  # nosec B608


def available(connection: sqlite3.Connection) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='action_history'"
        ).fetchone()
        is not None
    )


def split(
    rows: list[dict[str, Any]], *, protected: Iterable[str] = ()
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Bound repeated terminal payloads without evicting uncertain authority."""
    retained, archived, seen = [], [], set()
    protected = set(protected)
    for row in reversed(rows):
        terminal = row["state"] in {"confirmed", "rejected"}
        key = (row["unit"], row["kind"])
        if terminal and key in seen and row["id"] not in protected:
            archived.append(row)
        else:
            retained.append(row)
        if terminal:
            seen.add(key)
    return list(reversed(retained)), list(reversed(archived))


def _coordinates(row: dict[str, Any]) -> tuple[str, str]:
    intent = row.get("receipt", {}).get("intent") or row.get("payload", {}).get("intent") or {}
    native = (intent.get("receipt") or {}).get("id")
    if native is None:
        native = (intent.get("coordinates") or {}).get("id", "")
    return str(native) if native is not None else "", str(
        row.get("payload", {}).get("tracking_key", "")
    )


def archive(
    connection: sqlite3.Connection,
    rows: list[dict[str, Any]],
    *,
    revision: int,
    protected: Iterable[str] = (),
) -> tuple[list[dict[str, Any]], set[str]]:
    """Register order and move terminal payloads in the caller's authority transaction."""
    schema(connection)
    identities = [row["id"] for row in rows]
    if len(set(identities)) != len(identities):
        raise CampaignError("duplicate action history identities")
    supplied = {row["id"]: row for row in rows}
    for offset in range(0, len(identities), 256):
        batch = identities[offset : offset + 256]
        registered = connection.execute(
            "SELECT id,kind,unit,archived_revision FROM action_history WHERE id IN ("  # nosec B608: bound placeholders only.
            + ",".join("?" for _ in batch)
            + ")",
            batch,
        ).fetchall()
        for identity, kind, unit, archived_revision in registered:
            if archived_revision is not None:
                raise CampaignError("cannot reactivate an archived effect ID")
            if (supplied[identity]["kind"], supplied[identity]["unit"]) != (kind, unit):
                raise CampaignError("cannot redefine a recorded effect identity")
    connection.executemany(
        "INSERT INTO action_history(id,kind,unit,state,native_id,tracking_key) "
        "VALUES(?,?,?,?,?,?) ON CONFLICT(id) DO NOTHING",
        ((row["id"], row["kind"], row["unit"], row["state"], *_coordinates(row)) for row in rows),
    )
    retained, archived = split(rows, protected=protected)
    connection.executemany(
        "UPDATE action_history SET state=?,native_id=?,tracking_key=?,archived_revision=?,payload=? "
        "WHERE id=? AND archived_revision IS NULL",
        (
            (
                row["state"],
                *_coordinates(row),
                revision,
                json.dumps(row, sort_keys=True, ensure_ascii=False, separators=(",", ":")),
                row["id"],
            )
            for row in archived
        ),
    )
    return retained, {row["id"] for row in archived}


def positions(connection: sqlite3.Connection, identities: Iterable[str]) -> dict[str, int]:
    """Read creation order for hot records when merging them with indexed history."""
    if not available(connection):
        return {}
    keys, result = list(identities), {}
    for offset in range(0, len(keys), 256):
        batch = keys[offset : offset + 256]
        result.update(
            connection.execute(
                "SELECT id,sequence FROM action_history WHERE id IN ("  # nosec B608: bound placeholders only.
                + ",".join("?" for _ in batch)
                + ")",
                batch,
            ).fetchall()
        )
    return result


def read(
    connection: sqlite3.Connection,
    *,
    through: int,
    after: int = 0,
    limit: int = 1000,
    kinds: tuple[str, ...] = (),
    unit: str | None = None,
    native_id: str | None = None,
    tracking_key: str | None = None,
    action_id: str | None = None,
) -> list[dict[str, Any]]:
    """Return detached historical records visible to one saved revision."""
    if through < 0 or after < 0 or not 1 <= limit <= 1000:
        raise CampaignError("invalid action history pagination")
    if not available(connection):
        return []
    clauses = ["archived_revision <= ?", "sequence > ?"]
    values: list[Any] = [through, after]
    for column, value in (
        ("unit", unit),
        ("native_id", native_id),
        ("tracking_key", tracking_key),
        ("id", action_id),
    ):
        if value is not None:
            clauses.append(column + " = ?")
            values.append(value)
    if kinds:
        clauses.append("kind IN (" + ",".join("?" for _ in kinds) + ")")
        values.extend(kinds)
    rows = connection.execute(
        "SELECT sequence,archived_revision,payload FROM action_history WHERE "  # nosec B608: fixed predicates, bound values.
        + " AND ".join(clauses)
        + " ORDER BY sequence LIMIT ?",
        (*values, limit),
    ).fetchall()
    return [
        {"sequence": sequence, "archived_revision": revision, "action": json.loads(payload)}
        for sequence, revision, payload in rows
    ]


def counts(connection: sqlite3.Connection, *, through: int) -> dict[str, int]:
    if through < 0:
        raise CampaignError("invalid action history revision")
    if not available(connection):
        return {}
    return dict(
        connection.execute(
            "SELECT state,count(*) FROM action_history WHERE archived_revision <= ? GROUP BY state",
            (through,),
        ).fetchall()
    )
