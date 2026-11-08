"""Controller-owned, indexed communication endpoints for native campaign claims.

Every endpoint belongs to one immutable launch generation. Room events are read
once per bounded observation and dispatched to matching active actors and closed
tombstones. Message bodies remain advisory and are durably queued before the room
cursor advances; neither mail nor hcom status decides native session completion.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path

from .campaign_communication import CommunicationError, HcomObserver
from .campaign_message_bridge import (
    BridgeError,
    MessageBridge,
    database_epoch,
    process_bindings,
    read,
    write,
)
from .campaign_messages import MessageLedger
from .campaign_model import CampaignError
from .platform_util import is_link_like

PAGE = 100
MAX_ENDPOINTS = 500


def _key(target):
    return hashlib.sha256(json.dumps(target, sort_keys=True).encode()).hexdigest()


def _target(snapshot, target):
    target = {"unit_id": "", "job_id": "", **target}
    if target.get("campaign_id") != snapshot.id:
        raise CampaignError("Communication target belongs to another campaign")
    if not isinstance(target.get("launch_id"), str) or not target["launch_id"]:
        raise CampaignError("Communication target needs an owned launch")
    if type(target.get("generation")) is not int or target["generation"] < 1:
        raise CampaignError("Communication target needs a positive generation")
    if any(not isinstance(target.get(field), str) for field in ("unit_id", "job_id")):
        raise CampaignError("Communication unit and job identities must be strings")
    if not target["unit_id"] and not target["job_id"]:
        raise CampaignError("Communication target needs a unit or job")
    return target


def _claim(snapshot, target):
    if target["job_id"]:
        job = snapshot.jobs.get(target["job_id"])
        if job is None:
            return None
        if (job.get("request", {}).get("unit") or "") != target["unit_id"]:
            raise CampaignError("Communication job and unit differ")
        claim = job.get("claim")
    else:
        unit = snapshot.units.get(target["unit_id"])
        claim = vars(unit.claim) if unit is not None and unit.claim is not None else None
    if claim is None or (claim.get("launch_id"), claim.get("generation")) != (
        target["launch_id"],
        target["generation"],
    ):
        return None
    return claim


def _facts(snapshot, target):
    """Bounded facts from the exact claim; old sessions never become new actors."""
    facts = {"endpoint_kind": "bridge", "model_reply": False}
    unit = snapshot.units.get(target["unit_id"])
    if unit is not None:
        facts.update(state=unit.state.value, epic=unit.spec.epic[:512], title=unit.spec.title[:512])
        for key in ("issue", "pr"):
            raw = unit.bindings.get(key, {})
            if isinstance(raw, dict):
                facts[key] = {
                    field: raw[field][:2048]
                    for field in ("url", "html_url")
                    if isinstance(raw.get(field), str)
                }
                if type(raw.get("number")) is int:
                    facts[key]["number"] = raw["number"]
        facts["hold"] = (
            {"kind": unit.hold.kind, "reason": unit.hold.reason[:2048]} if unit.hold else None
        )
    claim = _claim(snapshot, target)
    if claim is not None:
        facts.update(claim_status=claim.get("status", ""), quiescent=bool(claim.get("quiescent")))
        sessions = [
            value[:512] for value in claim.get("session_ids", []) if isinstance(value, str)
        ][-8:]
        if sessions:
            facts["native_session_ids"] = sessions
            facts["native_session_id"] = sessions[-1]
    if target["job_id"]:
        job = snapshot.jobs.get(target["job_id"], {})
        facts["job_state"] = job.get("state", "")
        selected = job.get("selection") or {}
        receipt = job.get("receipt") or {}
        if selected.get("model"):
            facts["model"] = str(selected["model"])[:512]
        if claim is not None and isinstance(receipt.get("session_id"), str):
            facts["native_session_id"] = receipt["session_id"][:512]
    action = next((row for row in snapshot.outbox if row.id == target["launch_id"]), None)
    if action is not None:
        if action.payload.get("stage"):
            facts["stage"] = str(action.payload["stage"])[:512]
        selected = action.payload.get("candidate") or {}
        if selected.get("model"):
            facts["model"] = str(selected["model"])[:512]
    return facts


class ManagedCommunication:
    def __init__(
        self,
        campaign_directory: Path,
        *,
        bridge_factory=MessageBridge,
        observer_factory=HcomObserver,
        ledger=None,
        executable=None,
        runner=None,
        clock=time.time,
    ):
        if not campaign_directory.is_absolute():
            raise CampaignError("Managed communication state directory must be absolute")
        self.directory = campaign_directory / "managed-communication"
        self.path = self.directory / "endpoints.sqlite3"
        self.ledger = ledger or MessageLedger(campaign_directory, clock=clock)
        self.bridge_factory, self.observer_factory = bridge_factory, observer_factory
        self.executable, self.runner, self.clock = executable, runner, clock

    @contextmanager
    def _db(self, *, write=False):
        if is_link_like(self.directory) or is_link_like(self.path):
            raise CampaignError("Managed communication index is redirected")
        if write:
            self.directory.mkdir(parents=True, exist_ok=True)
            db = sqlite3.connect(self.path, timeout=10)
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=FULL")
            db.executescript(
                "CREATE TABLE IF NOT EXISTS endpoints ("
                "sequence INTEGER PRIMARY KEY AUTOINCREMENT,id TEXT UNIQUE NOT NULL,"
                "room TEXT NOT NULL,name TEXT NOT NULL,state TEXT NOT NULL,"
                "target TEXT NOT NULL,record TEXT NOT NULL,error TEXT NOT NULL);"
                "CREATE INDEX IF NOT EXISTS endpoint_name ON endpoints(room,name,sequence);"
                "CREATE INDEX IF NOT EXISTS endpoint_state ON endpoints(room,state,sequence);"
                "CREATE INDEX IF NOT EXISTS endpoint_open ON endpoints(sequence) WHERE state!='closed';"
                "CREATE INDEX IF NOT EXISTS endpoint_error ON endpoints(sequence) WHERE error!='';"
                "CREATE INDEX IF NOT EXISTS endpoint_projection ON endpoints((state='closed'),sequence DESC);"
                "CREATE TABLE IF NOT EXISTS rooms (room TEXT PRIMARY KEY,record TEXT NOT NULL);"
            )
        else:
            db = sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def _actor(self, room, target, facts=None):
        kwargs = {"room": room, "target": target, "facts": facts, "clock": self.clock}
        if self.executable is not None:
            kwargs["executable"] = self.executable
        if self.runner is not None:
            kwargs["runner"] = self.runner
        return self.bridge_factory(self.directory / "actors" / _key(target), **kwargs)

    def _save(self, room, target, record, error=""):
        error = error or record.get("error", "")
        with self._db(write=True) as db:
            db.execute(
                "INSERT INTO endpoints(id,room,name,state,target,record,error) VALUES(?,?,?,?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET name=excluded.name,state=excluded.state,"
                "record=excluded.record,error=excluded.error",
                (
                    _key(target),
                    room,
                    record.get("name", ""),
                    record.get("state", "registering"),
                    json.dumps(target, sort_keys=True),
                    json.dumps(record, sort_keys=True),
                    error,
                ),
            )
        return self._public(record, target=target, error=error)

    @staticmethod
    def _public(record, *, target=None, error="", sequence=None):
        return {
            "target": target or record.get("target", {}),
            "kind": "bridge",
            "name": record.get("name", ""),
            "room": record.get("room", ""),
            "state": record.get("state", "registering"),
            "process_id": record.get("process_id", ""),
            "native_session_id": record.get("native_session_id")
            or record.get("facts", {}).get("native_session_id", ""),
            "cursor": record.get("cursor", 0),
            "facts": record.get("facts", {}),
            "delivery": record.get("delivery", []),
            "error": error or record.get("error", ""),
            "sequence": sequence,
        }

    def prepare(self, snapshot, target, facts=None):
        if snapshot.spec.communication.mode != "managed":
            raise CampaignError("Managed communication is not enabled")
        target = _target(snapshot, target)
        claim = _claim(snapshot, target)
        if claim is None or claim.get("quiescent"):
            raise CampaignError("Communication target has no live owned launch claim")
        room = str(Path(snapshot.spec.communication.room).resolve())
        actor = self._actor(room, target, facts)
        if self.path.exists():
            with self._db() as db:
                existing = db.execute(
                    "SELECT 1 FROM endpoints WHERE id=?", (_key(target),)
                ).fetchone()
            if existing is not None and not actor.path.exists():
                raise BridgeError(
                    "Indexed bridge registration binding is missing; explicit reconciliation required"
                )
        # Index intent before the first external write. Lost registration replies
        # remain discoverable even if the coordinator dies before saving binding.
        self._save(room, target, {"room": room, "target": target, "state": "registering"})
        try:
            record = actor.start(background=False)
            if facts:
                record = actor.update(**facts)
            with self._db(write=True) as db:
                db.execute(
                    "INSERT OR IGNORE INTO rooms(room,record) VALUES(?,?)",
                    (
                        room,
                        json.dumps(
                            {
                                "cursor": record["registration_watermark"],
                                "watermark": record["registration_watermark"],
                                "database_epoch": record["database_epoch"],
                                "error": "",
                            }
                        ),
                    ),
                )
            return self._save(room, target, record)
        except (
            BridgeError,
            CommunicationError,
            OSError,
            ValueError,
            subprocess.SubprocessError,
        ) as exc:
            record = (
                read(actor.path) if actor.path.exists() else {"room": room, "state": "registering"}
            )
            self._save(room, target, record, f"{type(exc).__name__}: {exc}")
            raise

    def endpoints(self, *, after=0, limit=100, state=""):
        if type(after) is not int or after < 0 or type(limit) is not int or not 1 <= limit <= 500:
            raise CampaignError("Invalid communication endpoint page")
        if not self.path.exists():
            return []
        with self._db() as db:
            rows = db.execute(
                "SELECT * FROM endpoints WHERE sequence>? AND (state=? OR ?='') ORDER BY sequence LIMIT ?",
                (after, state, state, limit),
            ).fetchall()
        return [
            self._public(json.loads(row["record"]), error=row["error"], sequence=row["sequence"])
            for row in rows
        ]

    def has_open(self):
        if not self.path.exists():
            return False
        with self._db() as db:
            return (
                db.execute("SELECT 1 FROM endpoints WHERE state!='closed' LIMIT 1").fetchone()
                is not None
            )

    def close(self, target, *, outcome="finished"):
        target = {"unit_id": "", "job_id": "", **target}
        if not self.path.exists():
            raise CampaignError("Communication endpoint is not indexed")
        with self._db() as db:
            row = db.execute("SELECT * FROM endpoints WHERE id=?", (_key(target),)).fetchone()
        if row is None:
            raise CampaignError("Communication endpoint is not indexed")
        actor = self._actor(row["room"], target)
        try:
            record = actor.close(outcome=outcome)
            return self._save(row["room"], target, record)
        except (
            BridgeError,
            CommunicationError,
            OSError,
            ValueError,
            subprocess.SubprocessError,
        ) as exc:
            self._save(row["room"], target, read(actor.path), f"{type(exc).__name__}: {exc}")
            raise

    def _room(self, room, state, observer_name):
        epoch = database_epoch(room)
        if epoch != state["database_epoch"]:
            raise CommunicationError("hcom database changed; explicit generation rebind required")
        kwargs = {"room": Path(room), "observer_name": observer_name, "anonymous": True}
        if self.executable is not None:
            kwargs["executable"] = self.executable
        if self.runner is not None:
            kwargs["runner"] = self.runner
        observer = self.observer_factory(**kwargs)
        roster = observer._read(["list", "--json"])
        if not isinstance(roster, list) or any(not isinstance(row, dict) for row in roster):
            raise CommunicationError("hcom roster schema unsupported")
        cursor = int(state["cursor"])
        predicate = f"id IN (SELECT id FROM events_v WHERE type='message' AND id>{cursor} ORDER BY id ASC LIMIT {PAGE})"  # nosec B608: integer-only bounded hcom predicate.
        events = observer._read(
            ["events", "--sql", predicate, "--last", str(PAGE), "--full"], lines=True
        )
        latest = observer._read(["events", "--last", "1", "--full"], lines=True)
        if any(
            not isinstance(row, dict)
            or type(row.get("id")) is not int
            or row["id"] < 1
            or row.get("db_reset")
            or row.get("truncated")
            for row in [*events, *latest]
        ):
            raise CommunicationError("hcom room events are partial, invalid or reset")
        if any(
            row.get("type") != "message"
            or not isinstance(row.get("data"), dict)
            or not isinstance(row["data"].get("mentions", []), list)
            or any(not isinstance(name, str) for name in row["data"].get("mentions", []))
            for row in events
        ):
            raise CommunicationError("hcom room message schema unsupported")
        watermark = max((row["id"] for row in latest), default=0)
        if watermark < max(state["watermark"], cursor) or epoch != database_epoch(room):
            raise CommunicationError("hcom room watermark reset or database changed")
        if len(events) > PAGE or any(
            row["id"] <= cursor or row["id"] > watermark for row in events
        ):
            raise CommunicationError("hcom room page lies outside the bounded cursor")
        return roster, events, watermark, epoch

    def _queue(self, actor, events):
        accepted = 0
        for event in events:
            path = actor.directory / "inbox" / f"{event['id']}.json"
            if not path.exists():
                continue
            receipt = read(path)
            if receipt.get("disposition") != "queued":
                continue
            target = receipt["target"]
            identity = hashlib.sha256(
                json.dumps(
                    {
                        "room": actor.room,
                        "database_epoch": read(actor.path)["database_epoch"],
                        "event_id": event["id"],
                        "target": target,
                    },
                    sort_keys=True,
                ).encode()
            ).hexdigest()
            try:
                self.ledger.accept(
                    message_id="hcom:" + identity,
                    unit=target["unit_id"] or "@campaign",
                    job=target["job_id"],
                    text=receipt["text"],
                    source={
                        "kind": "hcom",
                        "room": actor.room,
                        "event_id": event["id"],
                        "sender": receipt["event"]["data"].get("from", ""),
                        "target": target,
                    },
                )
            except CampaignError:
                # Terminal closure can win after the durable bridge ACK but
                # before queue insertion. Retain the message as visibly closed;
                # a completed target cannot strand the room's event cursor.
                if not self.ledger.path.exists():
                    raise
                with self.ledger._connection() as db:
                    closed = db.execute(
                        "SELECT reason FROM closed_targets WHERE unit=? AND job=?",
                        (target["unit_id"] or "@campaign", target["job_id"]),
                    ).fetchone()
                if closed is None:
                    raise
                receipt.update(disposition="closed", closure_reason=closed["reason"])
                write(path, receipt)
                continue
            accepted += 1
        return accepted

    def _close_job_mail(self, snapshot, target):
        if not target["job_id"]:
            return
        from .campaign_jobs import TERMINAL

        job = snapshot.jobs.get(target["job_id"])
        if job is None or job.get("state") in TERMINAL:
            self.ledger.close_target(
                target["unit_id"] or "@campaign",
                job=target["job_id"],
                reason="Reasoning job is terminal; undelivered advisory mail retained",
            )

    def _sweep_job_mail(self, snapshot):
        # Scan only indexed queued targets, not historical closed endpoints.
        # A receipt can quiesce its bridge before the job applies/archives.
        if not self.ledger.path.exists():
            return
        from .campaign_jobs import TERMINAL

        active = [key for key, job in snapshot.jobs.items() if job.get("state") not in TERMINAL]
        with self.ledger._connection() as db:
            rows = db.execute(
                "SELECT DISTINCT unit,job FROM messages WHERE state='queued' AND job!='' "
                "AND job NOT IN (SELECT value FROM json_each(?)) LIMIT ?",
                (json.dumps(active), PAGE),
            ).fetchall()
        for row in rows:
            self.ledger.close_target(
                row["unit"],
                job=row["job"],
                reason="Reasoning job is terminal; undelivered advisory mail retained",
            )

    def observe(self, snapshot):
        if snapshot.spec.communication.mode != "managed":
            return {
                "mode": snapshot.spec.communication.mode,
                "available": True,
                "endpoints": [],
                "counts": {},
                "errors": [],
                "queued_messages": 0,
            }
        if not self.path.exists():
            return {
                "mode": "managed",
                "available": True,
                "endpoints": [],
                "counts": {},
                "errors": [],
                "queued_messages": 0,
            }
        errors, queued = [], 0
        self._sweep_job_mail(snapshot)
        with self._db() as db:
            rows = db.execute(
                "SELECT * FROM endpoints WHERE state!='closed' ORDER BY sequence LIMIT ?",
                (MAX_ENDPOINTS + 1,),
            ).fetchall()
        if len(rows) > MAX_ENDPOINTS:
            raise CampaignError("Active communication endpoint count exceeds polling bound")
        # Reconcile uncertain registrations/restarts and close only this exact
        # bridge identity. Native processes and hooks remain outside this manager.
        for row in rows:
            target = _target(snapshot, json.loads(row["target"]))
            actor = self._actor(row["room"], target)
            try:
                claim = _claim(snapshot, target)
                if not actor.path.exists():
                    raise BridgeError(
                        "Indexed bridge registration binding is missing; explicit reconciliation required"
                    )
                if row["state"] == "registering":
                    if claim is None or claim.get("quiescent"):
                        self.close(target, outcome="claim-quiescent")
                        self._close_job_mail(snapshot, target)
                        continue
                    self.prepare(snapshot, target, json.loads(row["record"]).get("facts"))
                else:
                    record = read(actor.path)
                    if (record["pid"], record["identity"]) != (
                        actor.pid,
                        actor.identity,
                    ) and record["state"] != "closing":
                        self._save(row["room"], target, actor.start(background=False))
                self._save(row["room"], target, actor.update(**_facts(snapshot, target)))
                if claim is None or claim.get("quiescent") or row["state"] == "closing":
                    self.close(
                        target,
                        outcome="claim-quiescent" if claim is not None else "claim-superseded",
                    )
                    self._close_job_mail(snapshot, target)
            except (
                BridgeError,
                CommunicationError,
                CampaignError,
                OSError,
                ValueError,
                subprocess.SubprocessError,
            ) as exc:
                record = read(actor.path) if actor.path.exists() else json.loads(row["record"])
                self._save(row["room"], target, record, f"{type(exc).__name__}: {exc}")
                errors.append(str(exc))
        with self._db() as db:
            rooms = db.execute("SELECT * FROM rooms ORDER BY room").fetchall()
        for roomrow in rooms:
            room, state = roomrow["room"], json.loads(roomrow["record"])
            try:
                with self._db() as db:
                    first = db.execute(
                        "SELECT name FROM endpoints WHERE room=? AND name!='' ORDER BY (state='closed'),sequence LIMIT 1",
                        (room,),
                    ).fetchone()
                if first is None:
                    raise CommunicationError("No registered bridge can observe the room")
                roster, events, watermark, epoch = self._room(room, state, first["name"])
                names = sorted(
                    {name for event in events for name in event.get("data", {}).get("mentions", [])}
                )
                if len(names) > MAX_ENDPOINTS:
                    raise CommunicationError("Room message recipient page exceeds polling bound")
                with self._db() as db:
                    rows = db.execute(
                        "SELECT * FROM endpoints WHERE room=? AND (state!='closed' OR name IN (SELECT value FROM json_each(?))) ORDER BY sequence LIMIT ?",
                        (room, json.dumps(names), MAX_ENDPOINTS + 1),
                    ).fetchall()
                if len(rows) > MAX_ENDPOINTS:
                    raise CommunicationError("Room endpoint page exceeds polling bound")
                bindings = process_bindings(
                    room, tuple(json.loads(row["record"])["process_id"] for row in rows)
                )
                complete = True
                for row in rows:
                    target = json.loads(row["target"])
                    actor = self._actor(room, target)
                    try:
                        record = actor.poll(
                            events=events,
                            roster=roster,
                            watermark=watermark,
                            database_epoch=epoch,
                            bindings=bindings,
                        )
                        queued += self._queue(actor, events)
                        self._save(room, target, record)
                        if record.get("error"):
                            errors.append(record["error"])
                            complete = False
                    except (
                        BridgeError,
                        CommunicationError,
                        CampaignError,
                        OSError,
                        ValueError,
                        subprocess.SubprocessError,
                    ) as exc:
                        self._save(room, target, read(actor.path), f"{type(exc).__name__}: {exc}")
                        errors.append(str(exc))
                        complete = False
                state["watermark"] = watermark
                if complete:
                    state["cursor"] = (
                        max((row["id"] for row in events), default=watermark)
                        if len(events) == PAGE
                        else watermark
                    )
                state["error"] = "" if complete else "Endpoint reconciliation incomplete"
            except (
                BridgeError,
                CommunicationError,
                CampaignError,
                OSError,
                ValueError,
                subprocess.SubprocessError,
            ) as exc:
                state["error"] = f"{type(exc).__name__}: {exc}"
                errors.append(str(exc))
            with self._db(write=True) as db:
                db.execute("UPDATE rooms SET record=? WHERE room=?", (json.dumps(state), room))
        with self._db() as db:
            counts = {
                row["state"]: row["count"]
                for row in db.execute(
                    "SELECT state,COUNT(*) AS count FROM endpoints GROUP BY state"
                )
            }
            failed = db.execute(
                "SELECT error FROM endpoints WHERE error!='' ORDER BY sequence LIMIT 100"
            ).fetchall()
            projected = db.execute(
                "SELECT * FROM endpoints ORDER BY (state='closed'),sequence DESC LIMIT 100"
            ).fetchall()
        errors = list(dict.fromkeys([*errors, *(row["error"] for row in failed)]))
        endpoints = [
            self._public(json.loads(row["record"]), error=row["error"], sequence=row["sequence"])
            for row in projected
        ]
        return {
            "mode": "managed",
            "available": not errors,
            "endpoints": endpoints,
            "counts": counts,
            "errors": errors,
            "queued_messages": queued,
        }
