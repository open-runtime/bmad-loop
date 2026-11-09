"""Owned hcom bridge identities for disposable sessions without a receive loop.

The bridge reports controller facts, never impersonates a model response. Mail is
copied durably before acknowledgement and is advisory input for a subsequent safe
boundary. Its accepting/closing/closed state does not determine native completion.
No model call, shell command from a message, or product-tree write occurs here.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable

from .platform_util import atomic_write_text, file_lock, is_link_like
from .process_host import get_process_host

NAME = re.compile(r"[a-z][a-z0-9_-]{2,31}")
LIMIT = 512 * 1024


class BridgeError(RuntimeError):
    """Communication is unavailable or its exact actor ownership is uncertain."""


def read(path: Path) -> dict:
    if is_link_like(path) or not path.is_file():
        raise BridgeError("Bridge record is missing or redirected")
    with path.open("rb") as stream:
        raw = stream.read(LIMIT + 1)
    if len(raw) > LIMIT:
        raise BridgeError("Bridge record exceeds its byte bound")
    result = json.loads(raw)
    if not isinstance(result, dict):
        raise BridgeError("Bridge record is not an object")
    return result


def write(path: Path, value: dict) -> None:
    raw = json.dumps(value, sort_keys=True)
    if len(raw.encode()) > LIMIT:
        raise BridgeError("Bridge record exceeds its byte bound")
    atomic_write_text(path, raw, follow_symlinks=False)


def database_epoch(room: str | Path) -> dict[str, int]:
    """Observe the event database identity without opening or modifying SQLite."""
    path = Path(room) / "hcom.db"
    if is_link_like(path) or not path.is_file():
        raise BridgeError("hcom event database is missing or redirected")
    stat = path.stat()
    return {
        "device": stat.st_dev,
        "inode": stat.st_ino,
        "birth_ns": int(getattr(stat, "st_birthtime", 0) * 1_000_000_000),
    }


def process_bindings(room: str | Path, process_ids: tuple[str, ...]) -> dict[str, dict]:
    """Read hcom's actual ad-hoc UUID binding; list JSON has no such field.

    This is a versioned, read-only schema seam. Unknown schemas or replaced
    databases refuse ownership; no native session or hook identity is invented.
    """
    if len(process_ids) > 500 or any(
        not isinstance(value, str) or not value for value in process_ids
    ):
        raise BridgeError("Bridge process binding page is invalid")
    before = database_epoch(room)
    path = Path(room) / "hcom.db"
    try:
        with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=2) as db:
            db.execute("PRAGMA query_only=ON")
            db.row_factory = sqlite3.Row
            rows = db.execute(
                "SELECT p.process_id,p.session_id AS binding_session,i.name,i.session_id,i.parent_name,i.tool "
                "FROM process_bindings AS p JOIN instances AS i ON i.name=p.instance_name "
                "WHERE p.process_id IN (SELECT value FROM json_each(?)) LIMIT 501",
                (json.dumps(process_ids),),
            ).fetchall()
    except sqlite3.Error as exc:
        raise BridgeError("hcom ad-hoc process binding schema is unavailable") from exc
    if before != database_epoch(room):
        raise BridgeError("hcom database changed during process binding observation")
    return {row["process_id"]: dict(row) for row in rows}


def bridge_environment(base: dict[str, str], room: str, process_id: str) -> dict[str, str]:
    """An ad-hoc identity is separate from the caller and the native model."""
    from .campaign_model import CommunicationSpec
    from .campaign_reflection_flow import sanitized_actor_env

    env = sanitized_actor_env(base, communication=CommunicationSpec(room=room))
    # Native harness detection must not turn this status bridge into a coding
    # session or attach it to a still-running parent conversation.
    for key in (
        "CLAUDECODE",
        "CLAUDE_ENV_FILE",
        "GEMINI_CLI",
        "GEMINI_SYSTEM_MD",
        "GEMINI_CLI_NO_RELAUNCH",
        "CODEX_SANDBOX",
        "CODEX_SANDBOX_NETWORK_DISABLED",
        "CODEX_MANAGED_BY_NPM",
        "CODEX_MANAGED_BY_BUN",
        "OPENCODE",
        "OPENCODE_PID",
        "KILO",
        "ANTIGRAVITY_AGENT",
        "CURSOR_AGENT",
        "CURSOR_PROJECT_DIR",
        "KIMI_CODE_CLI",
        "PI_CODING_AGENT",
        "PI_CODING_AGENT_SESSION_DIR",
        "HERMES_AGENT",
        "QODER_CLI",
        "QODER_PID",
        "GROK_AGENT",
        "GROK_SESSION_ID",
        "GROK_HOOK_EVENT",
        "GROK_HOOK_NAME",
        "GROK_LEADER_SOCKET",
    ):
        env.pop(key, None)
    env["HCOM_PROCESS_ID"] = process_id
    return env


class MessageBridge:
    """One real ad-hoc actor per owned launch, with an append-only mail inbox.

    ``runner`` is a test seam. The production hcom process is a short bounded CLI
    invocation; actor ownership remains with this wrapper's process birth identity.
    """

    def __init__(
        self,
        directory: Path,
        *,
        room: str,
        target: dict[str, Any],
        facts: dict[str, Any] | None = None,
        executable: str | None = None,
        runner: Callable = subprocess.run,
        clock: Callable[[], float] = time.time,
    ):
        if not Path(room).is_absolute() or not directory.is_absolute():
            raise BridgeError("Bridge room and state directory must be absolute")
        for key in ("campaign_id", "launch_id"):
            if not isinstance(target.get(key), str) or not target[key]:
                raise BridgeError("Bridge requires an exact campaign and launch")
        if type(target.get("generation")) is not int or target["generation"] < 1:
            raise BridgeError("Bridge requires a positive launch generation")
        if is_link_like(directory):
            raise BridgeError("Bridge state directory is redirected")
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.directory, self.room = directory, str(Path(room).resolve())
        self.target, self.facts = dict(target), dict(facts or {})
        self.runner, self.clock = runner, clock
        self.executable = executable or shutil.which("hcom")
        if not self.executable:
            raise BridgeError("Managed communication requires hcom")
        self.path = directory / "binding.json"
        self.host = get_process_host()
        self.pid = os.getpid()
        self.identity = self.host.identity(self.pid)
        if self.identity is None:
            raise BridgeError("Bridge owner process identity is unavailable")
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._guard = threading.RLock()
        self._lock_depth = 0

    @contextmanager
    def _locked(self):
        # Separate manager instances may observe/prepare this endpoint in
        # parallel. The OS lock serializes their durable read-modify-write;
        # nested operations on this instance retain a single lock handle.
        with self._guard:
            if self._lock_depth:
                self._lock_depth += 1
                try:
                    yield
                finally:
                    self._lock_depth -= 1
                return
            with file_lock(self.directory / ".registration.lock", blocking=False):
                self._lock_depth = 1
                try:
                    yield
                finally:
                    self._lock_depth = 0

    def _record(self, *, adopt_dead: bool = False) -> dict:
        if is_link_like(self.directory):
            raise BridgeError("Bridge state directory is redirected")
        record = read(self.path)
        if (
            record.get("schema_version") != 1
            or record.get("kind") != "bridge"
            or type(record.get("pid")) is not int
            or record["pid"] <= 0
            or type(record.get("identity")) not in (int, float)
            or record.get("state") not in {"registering", "accepting", "closing", "closed"}
        ):
            raise BridgeError("Bridge binding is invalid")
        if record.get("room") != self.room or record.get("target") != self.target:
            raise BridgeError("Bridge launch or room changed; replacement forbidden")
        if record.get("pid") == self.pid and record.get("identity") == self.identity:
            return record
        if (
            not adopt_dead
            or self.host.liveness_of(record.get("pid", 0), record.get("identity")) != "dead"
        ):
            raise BridgeError("Bridge owner remains live or uncertain; replacement forbidden")
        record.setdefault("previous_owners", []).append(
            {
                "pid": record["pid"],
                "identity": record["identity"],
                "adopted_at": self.clock(),
            }
        )
        record.update(pid=self.pid, identity=self.identity)
        write(self.path, record)
        return record

    def _call(self, args: list[str], *, name: str = "", text: str | None = None):
        record = self._record()
        argv = [str(self.executable), *args]
        # Observations must be anonymous: hcom appends inline unread delivery
        # for ad-hoc identities even to events JSONL, advancing its inbox cursor.
        # Writes resolve self by UUID; --name could instead hit a reused name.
        env = bridge_environment(dict(os.environ), self.room, record["process_id"])
        if args[0] in {"list", "events"}:
            env.pop("HCOM_PROCESS_ID", None)
        result = self.runner(
            argv,
            cwd=self.directory,
            env=env,
            input=text,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=10,
            check=False,
        )
        if not isinstance(result.stdout, str) or not isinstance(result.stderr, str):
            raise BridgeError("hcom response is not text")
        if len(result.stdout.encode()) > LIMIT or len(result.stderr.encode()) > LIMIT:
            raise BridgeError("hcom response exceeds bridge byte bound")
        if result.returncode:
            raise BridgeError("hcom bridge command failed")
        return result.stdout

    def _owned_row(self, *, name: str = "") -> dict | None:
        record = self._record()
        bindings = process_bindings(self.room, (record["process_id"],))
        if record["process_id"] not in bindings:
            return None
        rows = json.loads(self._call(["list", "--json"], name=name))
        if not isinstance(rows, list) or any(
            not isinstance(row, dict) or not isinstance(row.get("launch_context") or {}, dict)
            for row in rows
        ):
            raise BridgeError("hcom actor listing is invalid")
        return self._bound_row(record, rows, bindings, expected_name=name)

    @staticmethod
    def _bound_row(record, rows, bindings, *, expected_name=""):
        binding = bindings.get(record["process_id"])
        if binding is None:
            return None
        if (
            binding["tool"] != "adhoc"
            or binding["session_id"] not in (None, "")
            or binding["binding_session"] not in (None, "")
            or binding["parent_name"] not in (None, "")
        ):
            raise BridgeError("Bridge process UUID belongs to a different actor kind")
        matches = [row for row in rows if row.get("base_name", row.get("name")) == binding["name"]]
        if not matches:
            raise BridgeError("Bound bridge actor is missing from the roster")
        if len(matches) != 1:
            raise BridgeError("Multiple actors claim this bridge process")
        row = {**matches[0], "name": binding["name"]}
        if (
            not NAME.fullmatch(str(row.get("name", "")))
            or row.get("tool") != "adhoc"
            or row.get("session_id") not in (None, "")
            or row.get("parent_name") not in (None, "")
            or (expected_name and row.get("name") != expected_name)
        ):
            raise BridgeError("Bridge actor identity differs from its owned registration")
        return row

    def _watermark(self) -> int:
        rows = self._call(["events", "--last", "1", "--full"])
        values = [json.loads(line) for line in rows.splitlines() if line.strip()]
        if any(
            not isinstance(row, dict)
            or type(row.get("id")) is not int
            or row["id"] < 1
            or row.get("truncated")
            or row.get("db_reset")
            for row in values
        ):
            raise BridgeError("hcom event watermark is invalid")
        return max((row["id"] for row in values), default=0)

    def start(self, *, background: bool = False) -> dict:
        """Persist registration intent first; reconcile the UUID after a lost reply."""
        with self._locked():
            if self.path.exists():
                record = self._record(adopt_dead=True)
                if record["state"] in {"closing", "closed"}:
                    raise BridgeError("Closed bridge generations cannot reopen")
            else:
                record = {
                    "schema_version": 1,
                    "kind": "bridge",
                    "state": "registering",
                    "room": self.room,
                    "target": self.target,
                    "pid": self.pid,
                    "identity": self.identity,
                    "process_id": str(uuid.uuid4()),
                    "name": "",
                    "native_session_id": "",
                    "cursor": 0,
                    "facts": self.facts,
                    "created_at": self.clock(),
                    "delivery": ["factual-status", "queued-steering"],
                }
                write(self.path, record)
            if "registration_watermark" not in record:
                record["registration_watermark"] = self._watermark()
                record["cursor"] = record["registration_watermark"]
                record["database_epoch"] = database_epoch(self.room)
                record["observed_watermark"] = record["registration_watermark"]
                write(self.path, record)
            self._check_epoch(record, watermark=self._watermark())
            row = self._owned_row(name=record["name"])
            if row is None:
                if record.get("registration_sent"):
                    raise BridgeError("Registration acknowledgement is uncertain; actor absent")
                record["registration_sent"] = True
                write(self.path, record)
                bootstrap = self._call(["start"])
                atomic_write_text(
                    self.directory / "bootstrap.txt", bootstrap, follow_symlinks=False
                )
                row = self._owned_row()
            if row is None:
                raise BridgeError("hcom registration was not observed")
            record.update(name=row["name"], state="accepting", observed_at=self.clock(), error="")
            write(self.path, record)
        if background and self._thread is None:
            self._thread = threading.Thread(
                target=self._serve, name="bmad-hcom-bridge", daemon=True
            )
            self._thread.start()
        return record

    def _check_epoch(self, record, *, watermark=None, epoch=None):
        current = database_epoch(self.room)
        if epoch is not None and epoch != current:
            raise BridgeError("hcom event database changed during observation")
        if current != record.get("database_epoch"):
            raise BridgeError(
                "hcom event database was replaced; explicit generation rebind required"
            )
        if watermark is not None:
            if type(watermark) is not int or watermark < max(
                record["cursor"], record.get("observed_watermark", 0)
            ):
                raise BridgeError(
                    "hcom event database watermark reset; explicit generation rebind required"
                )
            record["observed_watermark"] = watermark

    def _events(self, name: str, cursor: int) -> list[dict]:
        # Select the oldest page, not the newest page: a burst must not advance
        # the cursor past messages that were never copied to the durable inbox.
        if not NAME.fullmatch(name):
            raise BridgeError("Bridge actor name is invalid")
        predicate = (
            f"type='message' AND id>{int(cursor)} AND msg_scope='mentions' "  # nosec B608: hcom predicate, integer cursor and validated identifier below.
            f"AND EXISTS (SELECT 1 FROM json_each(msg_mentions) WHERE value='{name}')"
        )
        sql = f"id IN (SELECT id FROM events_v WHERE {predicate} ORDER BY id ASC LIMIT 100)"  # nosec B608: predicate above contains only validated identifiers and integers.
        raw = self._call(["events", "--sql", sql, "--last", "100", "--full"], name=name)
        rows = [json.loads(line) for line in raw.splitlines() if line.strip()]
        if any(
            not isinstance(row, dict) or row.get("truncated") or row.get("db_reset") for row in rows
        ):
            raise BridgeError("Bridge message observation is partial or reset")
        return sorted(rows, key=lambda row: row.get("id", 0))

    def _reply(self, record: dict, event: dict, receipt: dict) -> None:
        sender = event["data"].get("from", "")
        if not isinstance(sender, str) or not NAME.fullmatch(sender):
            # External arbitrary sender strings are data, not CLI recipients.
            receipt["response_state"] = "unaddressable-sender"
            return
        response_id = hashlib.sha256(
            (record["process_id"] + ":" + str(event["id"])).encode()
        ).hexdigest()
        target = self.directory / "responses" / f"{response_id}.json"
        target.parent.mkdir(exist_ok=True)
        if target.exists():
            prior = read(target)
            if prior["state"] == "confirmed":
                receipt["response_state"] = "confirmed"
                return
            # Sending is not idempotent. Reconcile the exact response marker
            # before considering another send; uncertainty never means retry.
            thread = prior.get("thread", "bmad-" + response_id)
            if not re.fullmatch(r"[a-z0-9_-]{1,69}", thread):
                raise BridgeError("Durable bridge response thread is invalid")
            sql = f"type='message' AND msg_from='{record['name']}' AND msg_thread='{thread}'"
            rows = self._call(
                ["events", "--sql", sql, "--last", "2", "--full"], name=record["name"]
            )
            matches = [json.loads(line) for line in rows.splitlines() if line.strip()]
            if (
                len(matches) == 1
                and isinstance(matches[0], dict)
                and (
                    isinstance(matches[0].get("data"), dict)
                    and (matches[0].get("data") or {}).get("text") == prior["text"]
                    and (matches[0].get("data") or {}).get("from") == record["name"]
                    and (matches[0].get("data") or {}).get("thread") == thread
                    and sender in (matches[0].get("data") or {}).get("mentions", [])
                    and not matches[0].get("truncated")
                    and not matches[0].get("db_reset")
                )
            ):
                prior["state"] = "confirmed"
                write(target, prior)
                receipt["response_state"] = "confirmed"
            else:
                receipt["response_state"] = "uncertain"
            return
        facts = read(self.path).get("facts", {})
        response = json.dumps(
            {
                "source": "bmad-controller-bridge",
                "model_reply": False,
                "target": self.target,
                "bridge_state": record["state"],
                "facts": facts,
                "message_id": event["id"],
                "disposition": receipt["disposition"],
                "delivered_to_model": False,
            },
            sort_keys=True,
        )
        thread = "bmad-" + response_id[:48]  # hcom thread names have a 64-character bound.
        write(
            target,
            {"state": "sent_unknown", "text": response, "event_id": event["id"], "thread": thread},
        )
        self._call(
            [
                "send",
                "@" + sender,
                "--intent",
                "ack",
                "--reply-to",
                str(event["id"]),
                "--thread",
                thread,
                "--json",
            ],
            name=record["name"],
            text=response,
        )
        write(
            target,
            {"state": "confirmed", "text": response, "event_id": event["id"], "thread": thread},
        )
        receipt["response_state"] = "confirmed"

    def _disposition(self, record: dict, event: dict) -> tuple[str, str]:
        """Only explicitly addressed steering can cross a launch generation.

        Plain hcom requests receive useful factual status. Their text is retained
        as advisory history, but names alone cannot authorize delivery to a model:
        a sender can have resolved an older actor before that name was reused.
        """
        data = event["data"]
        if data.get("intent") == "ack":
            return "observed-ack", ""
        if record["state"] != "accepting":
            return "closed", ""
        try:
            envelope = json.loads(data["text"])
        except ValueError:
            return "status-only", ""
        if (
            not isinstance(envelope, dict)
            or type(envelope.get("bmad")) is not int
            or envelope.get("bmad") != 1
        ):
            return "status-only", ""
        if json.dumps(envelope.get("target"), sort_keys=True) != json.dumps(
            self.target, sort_keys=True
        ):
            return "stale", ""
        if envelope.get("kind") == "status":
            return "status-only", ""
        text = envelope.get("text")
        if envelope.get("kind") != "steer" or not isinstance(text, str) or not text.strip():
            return "rejected", ""
        if len(text.encode()) > 16384:
            return "rejected", ""
        return "queued", text

    def poll(
        self,
        *,
        events: list[dict] | None = None,
        roster: list[dict] | None = None,
        watermark: int | None = None,
        database_epoch: dict | None = None,
        bindings: dict | None = None,
    ) -> dict:
        try:
            return self._poll(
                events=events,
                roster=roster,
                watermark=watermark,
                epoch=database_epoch,
                bindings=bindings,
            )
        except (BridgeError, OSError, ValueError, subprocess.SubprocessError) as exc:
            with self._locked():
                record = self._record()
                record["error"] = f"{type(exc).__name__}: {exc}"
                write(self.path, record)
            raise

    def _poll(self, *, events, roster, watermark, epoch, bindings) -> dict:
        with self._locked():
            record = self._record(adopt_dead=True)
            self._check_epoch(record, watermark=watermark, epoch=epoch)
            if record["state"] != "closed":
                if roster is None:
                    owned = self._owned_row(name=record["name"])
                else:
                    if any(
                        not isinstance(row, dict)
                        or not isinstance(row.get("launch_context") or {}, dict)
                        for row in roster
                    ):
                        raise BridgeError("hcom actor listing is invalid")
                    owned = self._bound_row(
                        record,
                        roster,
                        (
                            bindings
                            if bindings is not None
                            else process_bindings(self.room, (record["process_id"],))
                        ),
                        expected_name=record["name"],
                    )
                if owned is None:
                    raise BridgeError("Bridge actor is no longer bound")
            inbox = self.directory / "inbox"
            inbox.mkdir(exist_ok=True)
            batch = events if events is not None else self._events(record["name"], record["cursor"])
            if any(
                not isinstance(event, dict)
                or event.get("truncated")
                or event.get("db_reset")
                or type(event.get("id")) is not int
                or event["id"] < 1
                or not isinstance(event.get("data"), dict)
                for event in batch
            ):
                raise BridgeError("Bridge message observation is partial, reset or invalid")
            if events is None:
                self._check_epoch(record, watermark=self._watermark())
            for event in sorted(batch, key=lambda row: row["id"]):
                data, event_id = event.get("data", {}), event.get("id")
                if event.get("type") != "message":
                    continue
                if not isinstance(data.get("mentions"), list) or any(
                    not isinstance(name, str) for name in data["mentions"]
                ):
                    raise BridgeError("Bridge message recipients are invalid")
                if events is not None and (
                    type(event_id) is int
                    and event_id <= record["cursor"]
                    or record["name"] not in data.get("mentions", [])
                ):
                    continue
                if (
                    type(event_id) is not int
                    or event_id <= record["cursor"]
                    or data.get("scope") != "mentions"
                    or record["name"] not in data.get("mentions", [])
                    or not isinstance(data.get("text"), str)
                ):
                    raise BridgeError("Bridge message does not match its exact recipient")
                disposition, text = self._disposition(record, event)
                path = inbox / f"{event_id}.json"
                receipt = (
                    read(path)
                    if path.exists()
                    else {
                        "schema_version": 1,
                        "target": self.target,
                        "event": event,
                        "authority": "advisory",
                        "received_at": self.clock(),
                        "disposition": disposition,
                        "text": text,
                        "delivered_to_model": False,
                    }
                )
                if receipt.get("event") != event or receipt.get("target") != self.target:
                    raise BridgeError("Durable bridge message differs from the observed event")
                write(path, receipt)
                if data.get("intent") == "request" and record["state"] != "closed":
                    self._reply(record, event, receipt)
                    write(path, receipt)
                    if receipt.get("response_state") == "uncertain":
                        record["error"] = "Response acknowledgement remains uncertain"
                        write(self.path, record)
                        return record
                record["cursor"] = event_id
                record["observed_at"] = self.clock()
                write(self.path, record)
            record["error"] = ""
            record["observed_at"] = self.clock()
            write(self.path, record)
            return record

    def update(self, **facts: Any) -> dict:
        with self._locked():
            record = self._record()
            record["facts"].update(facts)
            record["observed_at"] = self.clock()
            write(self.path, record)
            return record

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                self.poll()
            except (BridgeError, OSError, ValueError, subprocess.SubprocessError) as exc:
                with self._locked():
                    record = read(self.path)
                    record["error"] = f"{type(exc).__name__}: {exc}"
                    write(self.path, record)
            self._stop.wait(1)

    def close(self, *, outcome: str = "finished") -> dict:
        """Fence input before stopping this bridge; never signal the native actor."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=12)
            if self._thread.is_alive():
                raise BridgeError("Bridge polling has not drained")
        with self._locked():
            record = self._record(adopt_dead=True)
            if record["state"] == "closed":
                return record
            record.update(state="closing", outcome=outcome)
            write(self.path, record)
            self._check_epoch(record, watermark=self._watermark())
            owned = self._owned_row(name=record["name"])
            if owned is not None:
                if not record["name"]:
                    record["name"] = owned["name"]
                    write(self.path, record)
                self.poll()
                record = self._record()
                if record.get("stop_sent"):
                    raise BridgeError("Bridge disconnect acknowledgement remains uncertain")
                record["stop_sent"] = True
                write(self.path, record)
                try:
                    self._call(["stop"], name=record["name"])
                except (BridgeError, OSError, ValueError, subprocess.SubprocessError):
                    if self._owned_row(name=record["name"]) is not None:
                        raise
                if self._owned_row(name=record["name"]) is not None:
                    raise BridgeError("Bridge disconnect is not confirmed")
            record.update(state="closed", closed_at=self.clock(), error="")
            write(self.path, record)
            # A send can resolve its recipient before stop and log afterward.
            # Retain this tombstone and continue room-level late reconciliation;
            # a stopped event is not a final message watermark.
            return record
