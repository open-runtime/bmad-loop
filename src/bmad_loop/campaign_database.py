"""Transactional local campaign authority and an append-only lazy event journal.

The JSON marker is the atomic authority switch, not a second snapshot. SQLite
contains hot state, immutable events and job records; payload files remain outside
the database. Connections are short lived and never cross worker threads.
"""

from __future__ import annotations

import copy
import json
import os
import sqlite3
import stat
import uuid
from collections.abc import Iterator, MutableSequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any, overload

from .campaign_model import CampaignError

STORAGE_SCHEMA_VERSION = 3
MARKER = {"schema_version": STORAGE_SCHEMA_VERSION, "storage": "sqlite"}
LEGACY_MARKER = {"schema_version": 2, "storage": "sqlite"}


def encode(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


class CampaignDatabase:
    """One database per campaign; caller owns the repository/state locks."""

    def __init__(self, directory: Path):
        self.directory = directory
        self.path = directory / "campaign.sqlite"

    def _check_paths(self, *, missing: bool = False) -> None:
        if self.directory.is_symlink() or not self.directory.is_dir():
            raise CampaignError("campaign database directory is missing or symlinked")
        for path in (self.path, Path(str(self.path) + "-wal"), Path(str(self.path) + "-shm")):
            try:
                mode = path.lstat().st_mode
            except FileNotFoundError:
                if path == self.path and not missing:
                    raise CampaignError("campaign database is missing") from None
                continue
            if not stat.S_ISREG(mode):
                raise CampaignError("campaign database or sidecar is not a regular file")

    @contextmanager
    def connect(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        self._check_paths()
        uri = self.path.as_uri() + ("?mode=rw" if write else "?mode=ro")
        try:
            connection = sqlite3.connect(uri, uri=True, timeout=10)
            try:
                connection.execute("PRAGMA busy_timeout=10000")
                if not write:
                    connection.execute("PRAGMA query_only=ON")
                if connection.execute("PRAGMA user_version").fetchone()[0] not in {
                    2,
                    STORAGE_SCHEMA_VERSION,
                }:
                    raise CampaignError("unsupported campaign database schema")
                yield connection
            finally:
                connection.close()
        except sqlite3.Error as exc:
            raise CampaignError(f"campaign database operation failed: {exc}") from exc

    def initialize(self, state: dict[str, Any], events: list[dict[str, Any]]) -> None:
        self._check_paths(missing=True)
        if self.path.exists():
            raise CampaignError("campaign database already exists")
        # Publish only a fully committed database. A crash while building the
        # staging file leaves the v1 authority intact and migration retryable.
        staging = self.directory / (".campaign-create-" + uuid.uuid4().hex + ".sqlite")
        descriptor = os.open(staging, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(descriptor)
        try:
            connection = sqlite3.connect(staging)
            try:
                connection.executescript(
                    "PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL;"
                    "CREATE TABLE authority (id INTEGER PRIMARY KEY CHECK(id=1), "
                    "revision INTEGER NOT NULL, generation INTEGER NOT NULL, payload TEXT NOT NULL);"
                    "CREATE TABLE events (sequence INTEGER PRIMARY KEY, kind TEXT NOT NULL, "
                    "unit TEXT NOT NULL, action TEXT NOT NULL, payload TEXT NOT NULL);"
                    "CREATE INDEX event_kind ON events(kind, sequence);"
                    "CREATE INDEX event_action ON events(action, sequence);"
                    "CREATE INDEX event_unit ON events(unit, sequence);"
                    "CREATE TABLE jobs (id TEXT PRIMARY KEY, state TEXT NOT NULL, "
                    "unit TEXT NOT NULL, payload TEXT NOT NULL);"
                    "CREATE INDEX job_state ON jobs(state, id);"
                    "CREATE TABLE observations (sequence INTEGER PRIMARY KEY, id TEXT UNIQUE NOT NULL, "
                    "kind TEXT NOT NULL, unit TEXT NOT NULL, payload TEXT NOT NULL);"
                    "CREATE INDEX observation_unit ON observations(unit, sequence);"
                    "PRAGMA user_version=3;"
                )
                with connection:
                    hot, jobs = self._split_jobs(state)
                    connection.execute(
                        "INSERT INTO authority VALUES(1,?,?,?)",
                        (state["revision"], state["generation"], encode(hot)),
                    )
                    self._append(connection, 0, events)
                    self._write_jobs(connection, jobs)
                connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            finally:
                connection.close()
            # Linking is atomic and refuses to replace an unexpected target.
            # The caller holds the campaign state lock throughout publication.
            os.link(staging, self.path)
        except sqlite3.Error as exc:
            raise CampaignError(f"cannot initialize campaign database: {exc}") from exc
        finally:
            for path in (staging, Path(str(staging) + "-wal"), Path(str(staging) + "-shm")):
                path.unlink(missing_ok=True)

    @staticmethod
    def _split_jobs(state: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        hot = dict(state)
        jobs = list(hot.pop("jobs", {}).values())
        hot["jobs"] = {}
        return hot, jobs

    @staticmethod
    def _write_jobs(connection: sqlite3.Connection, jobs: list[dict[str, Any]]) -> None:
        from .campaign_jobs import validate_record

        for job in jobs:
            validate_record(job)
            prior = connection.execute(
                "SELECT payload FROM jobs WHERE id=?", (job["id"],)
            ).fetchone()
            if prior is not None:
                previous = json.loads(prior[0])
                if previous["request"] != job["request"]:
                    raise CampaignError("cannot redefine a persisted reasoning request")
                if previous["state"] in {"applied", "canceled", "stale"} and previous != job:
                    raise CampaignError("cannot reactivate a terminal reasoning job")
            connection.execute(
                "INSERT INTO jobs VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
                "state=excluded.state, unit=excluded.unit, payload=excluded.payload",
                (job["id"], job["state"], job["request"]["unit"], encode(job)),
            )

    @staticmethod
    def _append(connection: sqlite3.Connection, offset: int, events: list[dict[str, Any]]) -> None:
        connection.executemany(
            "INSERT INTO events VALUES(?,?,?,?,?)",
            (
                (
                    offset + index + 1,
                    event.get("kind", ""),
                    event.get("unit", ""),
                    event.get("action", ""),
                    encode(event),
                )
                for index, event in enumerate(events)
            ),
        )

    def read(
        self,
        *,
        max_bytes: int | None = None,
        include_terminal_jobs: bool = False,
        minimum_schema: int = 2,
    ) -> tuple[dict[str, Any], int]:
        with self.connect() as connection:
            connection.execute("BEGIN")
            if connection.execute("PRAGMA user_version").fetchone()[0] < minimum_schema:
                raise CampaignError("campaign marker requires a newer database schema")
            if max_bytes is not None:
                size = connection.execute(
                    "SELECT length(CAST(payload AS BLOB)) FROM authority WHERE id=1"
                ).fetchone()
                if size is None or size[0] > max_bytes:
                    raise CampaignError("campaign authority exceeds byte bound")
            row = connection.execute("SELECT payload FROM authority WHERE id=1").fetchone()
            count = connection.execute("SELECT coalesce(max(sequence),0) FROM events").fetchone()[0]
            if row is None:
                raise CampaignError("campaign database has no authority")
            state = json.loads(row[0])
            state["jobs"] = {
                job[0]: json.loads(job[1])
                for job in connection.execute(
                    "SELECT id,payload FROM jobs WHERE ? OR state NOT IN ('applied','stale','canceled')",
                    (include_terminal_jobs,),
                )
            }
            return state, count

    def read_inventory(
        self, *, max_bytes: int, max_items: int, minimum_schema: int = 2
    ) -> dict[str, Any]:
        """Bound an ownership projection, not unrelated cached observations.

        This is an observation only, never a replacement authority document.
        Keep complete unit records (including source, hold, claim and workspace
        bindings), plus all nonterminal or unconfirmed reasoning claims. Omit
        their prompts/results and campaign caches, action payloads and history.
        SQLite applies the byte bound before transferring each JSON value to
        Python; one read transaction binds both authority and job observations.
        """
        if max_bytes < 1 or max_items < 1:
            raise CampaignError("campaign inventory bounds must be positive")
        with self.connect() as connection:
            connection.execute("BEGIN")
            if connection.execute("PRAGMA user_version").fetchone()[0] < minimum_schema:
                raise CampaignError("campaign marker requires a newer database schema")
            row = connection.execute(
                "WITH projection(value) AS (SELECT json_remove(payload, "
                "'$.bindings','$.outbox','$.events','$.config_snapshot',"
                "'$.processed_commands','$.jobs') FROM authority WHERE id=1) "
                "SELECT length(CAST(value AS BLOB)), "
                "CASE WHEN length(CAST(value AS BLOB))<=? THEN value END FROM projection",
                (max_bytes,),
            ).fetchone()
            if row is None:
                raise CampaignError("campaign database has no authority")
            if row[0] > max_bytes:
                raise CampaignError("campaign ownership projection exceeds byte bound")
            state = json.loads(row[1])
            if not isinstance(state, dict):
                raise CampaignError("campaign ownership authority must be an object")
            remaining = max_bytes - row[0]
            units = state.get("units", {})
            if not isinstance(units, dict):
                raise CampaignError("campaign ownership units must be an object")
            if len(units) > max_items:
                raise CampaignError("campaign ownership projection exceeds item bound")
            jobs = {}
            cursor = connection.execute(
                "WITH projection(id,value) AS (SELECT id,json_object("
                "'id',json_extract(payload,'$.id'),"
                "'state',json_extract(payload,'$.state'),"
                "'claim',json_extract(payload,'$.claim'),"
                "'request',json_object('id',json_extract(payload,'$.request.id'),"
                "'unit',json_extract(payload,'$.request.unit'))) FROM jobs "
                "WHERE json_type(payload,'$.claim') IS NOT NULL "
                "AND json_type(payload,'$.claim')!='null' "
                "AND (state NOT IN ('applied','stale','canceled') "
                "OR json_type(payload,'$.claim.quiescent') IS NOT 'true')) "
                "SELECT id,length(CAST(value AS BLOB)), "
                "CASE WHEN length(CAST(value AS BLOB))<=? THEN value END "
                "FROM projection ORDER BY id LIMIT ?",
                (remaining, max_items - len(units) + 1),
            )
            for identity, size, payload in cursor:
                if len(units) + len(jobs) >= max_items:
                    raise CampaignError("campaign ownership projection exceeds item bound")
                if size > remaining:
                    raise CampaignError("campaign ownership projection exceeds byte bound")
                jobs[identity] = json.loads(payload)
                remaining -= size
            state["jobs"] = jobs
            if len(encode(state).encode()) > max_bytes:
                raise CampaignError("campaign ownership projection exceeds byte bound")
            return state

    def commit(
        self,
        state: dict[str, Any],
        *,
        revision: int,
        event_count: int,
        events: list[dict[str, Any]],
    ) -> set[str]:
        with self.connect(write=True) as connection:
            with connection:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute("SELECT revision FROM authority WHERE id=1").fetchone()
                count = connection.execute(
                    "SELECT coalesce(max(sequence),0) FROM events"
                ).fetchone()[0]
                if row is None or row[0] != revision or count != event_count:
                    raise CampaignError("campaign database authority changed during commit")
                self._append(connection, count, events)
                hot, jobs = self._split_jobs(state)
                # The additive table keeps older schema-2 campaigns readable;
                # only pure observation history leaves their hot authority.
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS observations (sequence INTEGER PRIMARY KEY, "
                    "id TEXT UNIQUE NOT NULL, kind TEXT NOT NULL, unit TEXT NOT NULL, payload TEXT NOT NULL)"
                )
                connection.execute(
                    "CREATE INDEX IF NOT EXISTS observation_unit ON observations(unit, sequence)"
                )
                from .campaign_action_archive import archive, validate_incoming
                from .campaign_observation_archive import split

                validate_incoming(connection, hot.get("outbox", []))
                hot["outbox"], archived = split(hot.get("outbox", []))
                self._archive_observations(connection, state.get("outbox", []), archived)

                protected = {
                    unit["claim"]["launch_id"]
                    for unit in state.get("units", {}).values()
                    if unit.get("claim")
                } | {
                    job["claim"]["launch_id"]
                    for job in jobs
                    if job.get("claim") and job["state"] not in {"applied", "stale", "canceled"}
                }
                hot["outbox"], archived_effects = archive(
                    connection, hot["outbox"], revision=state["revision"], protected=protected
                )
                self._write_jobs(connection, jobs)
                connection.execute(
                    "UPDATE authority SET revision=?, generation=?, payload=? WHERE id=1",
                    (state["revision"], state["generation"], encode(hot)),
                )
                # Old schema-2 readers must refuse authority whose terminal
                # records now require indexed historical reads.
                connection.execute("PRAGMA user_version=3")
        return {row["id"] for row in archived} | archived_effects

    def actions(self, **query: Any) -> list[dict[str, Any]]:
        from .campaign_action_archive import read

        with self.connect() as connection:
            return read(connection, **query)

    def action_positions(self, identities: list[str]) -> dict[str, int]:
        from .campaign_action_archive import positions

        with self.connect() as connection:
            return positions(connection, identities)

    def action_counts(self, *, through: int) -> dict[str, int]:
        from .campaign_action_archive import counts

        with self.connect() as connection:
            return counts(connection, through=through)

    def archived_observation_exists(self, identity: str) -> bool:
        with self.connect() as connection:
            if not connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='observations'"
            ).fetchone():
                return False
            return (
                connection.execute("SELECT 1 FROM observations WHERE id=?", (identity,)).fetchone()
                is not None
            )

    def confirmation_revision(self, action_id: str, *, through: int) -> int:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT payload FROM events WHERE action=? AND kind='effect-confirmed' "
                "AND sequence<=? ORDER BY sequence DESC LIMIT 1",
                (action_id, through),
            ).fetchone()
        return int(json.loads(row[0]).get("revision", -1)) if row else -1

    @staticmethod
    def _archive_observations(connection, incoming, archived) -> None:
        # An old ID cannot be resurrected as an active effect after compaction.
        identities = [row["id"] for row in incoming]
        for offset in range(0, len(identities), 256):
            batch = identities[offset : offset + 256]
            if connection.execute(
                "SELECT id FROM observations WHERE id IN ("  # nosec B608: placeholders only.
                + ",".join("?" for _ in batch)
                + ") LIMIT 1",
                batch,
            ).fetchone():
                raise CampaignError("cannot reactivate an archived observation ID")
        connection.executemany(
            "INSERT INTO observations(id,kind,unit,payload) VALUES(?,?,?,?)",
            ((row["id"], row["kind"], row["unit"], encode(row)) for row in archived),
        )

    def observations(
        self, *, after: int = 0, limit: int = 100, unit: str = ""
    ) -> list[dict[str, Any]]:
        if after < 0 or not 1 <= limit <= 1000:
            raise CampaignError("invalid observation history pagination")
        with self.connect() as connection:
            if not connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='observations'"
            ).fetchone():
                return []
            rows = connection.execute(
                "SELECT sequence,payload FROM observations WHERE sequence>? "
                "AND (?='' OR unit=?) ORDER BY sequence LIMIT ?",
                (after, unit, unit, limit),
            ).fetchall()
        return [{"sequence": sequence, **json.loads(payload)} for sequence, payload in rows]

    def jobs(
        self, *, after: str = "", limit: int = 100, unit: str = "", job_id: str = ""
    ) -> list[dict[str, Any]]:
        if not 1 <= limit <= 1000:
            raise CampaignError("invalid reasoning job pagination")
        clauses, values = ["id > ?"], [after]
        if unit:
            clauses.append("unit = ?")
            values.append(unit)
        if job_id:
            clauses.append("id = ?")
            values.append(job_id)
        with self.connect() as connection:
            # Only fixed predicates above compose SQL; every external value is bound.
            rows = connection.execute(
                "SELECT payload FROM jobs WHERE "  # nosec B608: fixed predicates, bound values.
                + " AND ".join(clauses)
                + " ORDER BY id LIMIT ?",  # nosec B608
                (*values, limit),
            ).fetchall()
        return [json.loads(row[0]) for row in rows]

    def events(
        self,
        *,
        after: int = 0,
        limit: int = 100,
        kind: str = "",
        unit: str = "",
        through: int | None = None,
    ) -> list[tuple[int, dict[str, Any]]]:
        if after < 0 or limit < 1 or limit > 10000:
            raise CampaignError("invalid campaign history pagination")
        clauses = ["sequence > ?"]
        values: list[int | str] = [after]
        if through is not None:
            clauses.append("sequence <= ?")
            values.append(through)
        if kind:
            clauses.append("kind = ?")
            values.append(kind)
        if unit:
            clauses.append("unit = ?")
            values.append(unit)
        with self.connect() as connection:
            # Identifiers/predicates are constants; kind/unit/cursors are parameters.
            rows = connection.execute(
                "SELECT sequence,payload FROM events WHERE "  # nosec B608
                + " AND ".join(clauses)
                + " ORDER BY sequence LIMIT ?",
                (*values, limit),
            ).fetchall()
        return [(sequence, json.loads(payload)) for sequence, payload in rows]

    def backup(self, destination: Path) -> None:
        """Include committed WAL data using SQLite's consistent backup API."""
        descriptor = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(descriptor)
        with self.connect() as source:
            target = sqlite3.connect(destination)
            try:
                source.backup(target)
            finally:
                target.close()


class EventJournal(MutableSequence[dict[str, Any]]):
    """Immutable database prefix plus an in-memory append tail.

    Cloning a campaign copies only events inspected or appended by this observer.
    Explicit history export can still read all events. Mutation of an inspected
    persisted record is detected before saving, rather than silently discarded.
    """

    def __init__(self, database: CampaignDatabase, count: int):
        self.database = database
        self.base_count = count
        self.pending: list[dict[str, Any]] = []
        self.cache: dict[int, tuple[str, dict[str, Any]]] = {}

    def __deepcopy__(self, memo: dict[int, Any]) -> EventJournal:
        result = EventJournal(self.database, self.base_count)
        memo[id(self)] = result
        result.pending = copy.deepcopy(self.pending, memo)
        result.cache = copy.deepcopy(self.cache, memo)
        return result

    def __len__(self) -> int:
        return self.base_count + len(self.pending)

    @overload
    def __getitem__(self, index: int) -> dict[str, Any]: ...

    @overload
    def __getitem__(self, index: slice) -> list[dict[str, Any]]: ...

    def __getitem__(self, index: int | slice) -> dict[str, Any] | list[dict[str, Any]]:
        if isinstance(index, slice):
            return [self[item] for item in range(*index.indices(len(self)))]
        if index < 0:
            index += len(self)
        if not 0 <= index < len(self):
            raise IndexError(index)
        if index >= self.base_count:
            return self.pending[index - self.base_count]
        if index not in self.cache:
            rows = self.database.events(
                after=index, limit=min(100, self.base_count - index), through=self.base_count
            )
            for sequence, event in rows:
                self.cache[sequence - 1] = (encode(event), event)
        if index not in self.cache:
            raise CampaignError("campaign event journal has a missing sequence")
        return self.cache[index][1]

    def __setitem__(self, index: Any, value: Any) -> None:
        raise CampaignError("campaign events are append-only")

    def __delitem__(self, index: Any) -> None:
        raise CampaignError("campaign events are append-only")

    def insert(self, index: int, value: dict[str, Any]) -> None:
        if index != len(self):
            raise CampaignError("campaign events are append-only")
        self.pending.append(value)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, (list, EventJournal)):
            return False
        return len(self) == len(other) and all(a == b for a, b in zip(self, other))

    def validate_prefix(self, database: CampaignDatabase, count: int) -> None:
        if self.database.path != database.path or self.base_count != count:
            raise CampaignError("campaign event journal authority differs")
        if any(encode(event) != original for original, event in self.cache.values()):
            raise CampaignError("campaign events are append-only")

    def matching(self, kind: str) -> Iterator[dict[str, Any]]:
        after = 0
        while after < self.base_count:
            rows = self.database.events(after=after, limit=1000, kind=kind, through=self.base_count)
            if not rows:
                break
            for sequence, event in rows:
                yield event
                after = sequence
        yield from (event for event in self.pending if event.get("kind") == kind)
