"""Bounded durable operator/peer mail, separate from hot campaign snapshots.

An acknowledged message is never an implicit authority grant. Application means
inclusion in the next owned stage's advisory context, not proof of compliance or
permission to change acceptance. Terminal product decisions retain their own
validated contracts.
"""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

from .campaign_model import CampaignError
from .platform_util import atomic_write_text, file_lock, is_link_like, safe_segment


class MessageLedger:
    def __init__(self, directory: Path, *, clock=time.time):
        self.directory = directory
        self.path = directory / "messages.sqlite3"
        self.clock = clock

    @contextmanager
    def _connection(self, *, write=False):
        if is_link_like(self.directory) or is_link_like(self.path):
            raise CampaignError("campaign message ledger is redirected")
        if write:
            self.directory.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(self.path, timeout=10)
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=FULL")
            connection.executescript(
                "CREATE TABLE IF NOT EXISTS messages ("
                "sequence INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL, "
                "unit TEXT NOT NULL, job TEXT NOT NULL, state TEXT NOT NULL, "
                "payload TEXT NOT NULL, result TEXT NOT NULL, created REAL NOT NULL);"
                "CREATE INDEX IF NOT EXISTS messages_target ON messages(unit, job, state, sequence);"
                "CREATE INDEX IF NOT EXISTS messages_queued_jobs ON messages(state, job, unit);"
                "CREATE TABLE IF NOT EXISTS closed_targets (unit TEXT NOT NULL, job TEXT NOT NULL, "
                "reason TEXT NOT NULL, PRIMARY KEY(unit,job));"
            )
        else:
            connection = sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True, timeout=10)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def accept(self, *, message_id: str, unit: str, text: str, job="", source=None):
        if not message_id or not unit or not isinstance(text, str) or not text.strip():
            raise CampaignError("steering requires an identity, unit and nonempty text")
        if len(text.encode()) > 16384:
            raise CampaignError("steering text exceeds 16384 bytes")
        payload = {
            "schema_version": 1,
            "id": message_id,
            "unit": unit,
            "job": job,
            "text": text,
            "source": source or {"kind": "operator-cli"},
            "authority": "advisory",
        }
        encoded = json.dumps(payload, sort_keys=True)
        if len(encoded.encode()) > 32768:
            raise CampaignError("steering envelope exceeds its byte bound")
        with self._connection(write=True) as db:
            existing = db.execute(
                "SELECT payload FROM messages WHERE id=?", (message_id,)
            ).fetchone()
            if existing is not None:
                if existing["payload"] != encoded:
                    raise CampaignError("message identity reused with different input")
            else:
                if db.execute(
                    "SELECT 1 FROM closed_targets WHERE unit=? AND job=?", (unit, job)
                ).fetchone():
                    raise CampaignError("message target has closed")
                db.execute(
                    "INSERT INTO messages(id,unit,job,state,payload,result,created) VALUES(?,?,?,?,?,?,?)",
                    (message_id, unit, job, "queued", encoded, "{}", self.clock()),
                )
        return payload

    @staticmethod
    def _document(row):
        return {
            **json.loads(row["payload"]),
            "sequence": row["sequence"],
            "state": row["state"],
            "result": json.loads(row["result"]),
            "created_at": row["created"],
        }

    def list(self, *, unit="", job="", after=0, limit=100, state=""):
        if type(after) is not int or after < 0 or not 1 <= limit <= 500:
            raise CampaignError("invalid message page bounds")
        if not self.path.exists():
            return []
        with self._connection() as db:
            rows = db.execute(
                "SELECT * FROM messages WHERE sequence > ? "
                "AND (? = '' OR unit = ?) AND (? = '' OR job = ?) "
                "AND (? = '' OR state = ?) ORDER BY sequence LIMIT ?",
                (after, unit, unit, job, job, state, state, limit),
            ).fetchall()
        return [self._document(row) for row in rows]

    def pending(self, unit: str, *, job="", max_bytes=32768):
        selected, size = [], 0
        for row in self.list(unit=unit, state="queued", limit=50):
            if row["job"] != job:
                continue
            item = {key: row[key] for key in ("id", "text", "source", "authority")}
            cost = len(json.dumps(item).encode())
            if size + cost > max_bytes:
                break
            selected.append(item)
            size += cost
        return selected

    def applied(self, ids: list[str], *, launch_id: str, unit: str, job=""):
        """Record context application only after observing the matching launch."""
        if not launch_id:
            raise CampaignError("message application needs an owned launch")
        with self._connection(write=True) as db:
            for identity in ids:
                row = db.execute("SELECT * FROM messages WHERE id=?", (identity,)).fetchone()
                if row is None or row["unit"] != unit or (row["job"] and row["job"] != job):
                    raise CampaignError("message application target differs from accepted input")
                result = {
                    "launch_id": launch_id,
                    "kind": "stage-context",
                    "compliance_verified": False,
                    "authority": "advisory",
                }
                if row["state"] == "applied":
                    if json.loads(row["result"]) != result:
                        raise CampaignError("message was already applied to another launch")
                    continue
                if row["state"] != "queued":
                    raise CampaignError("message is no longer queued")
                db.execute(
                    "UPDATE messages SET state='applied', result=? WHERE id=?",
                    (json.dumps(result, sort_keys=True), identity),
                )

    def freeze(self, unit: str, launch_id: str, *, job="", max_bytes=32768) -> list[dict]:
        """A retry of a prepared launch uses precisely the original mail context."""
        root = self.directory / "message-context"
        root.mkdir(parents=True, exist_ok=True)
        path = root / (safe_segment(launch_id) + ".json")
        with file_lock(root / ".lock"):
            if path.exists():
                if is_link_like(path) or path.stat().st_size > 65536:
                    raise CampaignError("message context is redirected or oversized")
                saved = json.loads(path.read_text(encoding="utf-8"))
                if (saved.get("unit"), saved.get("job"), saved.get("launch_id")) != (
                    unit,
                    job,
                    launch_id,
                ):
                    raise CampaignError("message context belongs to a different launch")
                return saved["messages"]
            messages = self.pending(unit, job=job, max_bytes=max(0, max_bytes))
            atomic_write_text(
                path,
                json.dumps(
                    {
                        "unit": unit,
                        "job": job,
                        "launch_id": launch_id,
                        "messages": messages,
                    },
                    sort_keys=True,
                ),
                follow_symlinks=False,
            )
        return messages

    def close_target(self, unit: str, *, job="", reason: str):
        with self._connection(write=True) as db:
            db.execute(
                "INSERT OR IGNORE INTO closed_targets(unit,job,reason) VALUES(?,?,?)",
                (unit, job, reason),
            )
            db.execute(
                "UPDATE messages SET state='closed', result=? WHERE unit=? AND job=? AND state='queued'",
                (json.dumps({"reason": reason}), unit, job),
            )


def context(messages: list[dict]) -> str:
    if not messages:
        return ""
    return (
        "\n\nQueued campaign steering (advisory; existing scope, holds, acceptance and "
        "ownership remain binding; report conflicts instead of changing authority):\n"
        + json.dumps(messages, sort_keys=True)
    )
