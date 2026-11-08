"""Managed mail uses fake hcom I/O: no actor registration, send, or native kill."""

from __future__ import annotations

import copy
import json
import re
import sqlite3
import subprocess
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from bmad_loop import campaign_message_bridge as bridge


def seed_room(room):
    room.mkdir(exist_ok=True)
    with sqlite3.connect(room / "hcom.db") as db:
        db.executescript(
            "CREATE TABLE instances(name TEXT PRIMARY KEY,session_id TEXT,parent_name TEXT,tool TEXT);"
            "CREATE TABLE process_bindings(process_id TEXT PRIMARY KEY,session_id TEXT,instance_name TEXT,updated_at REAL NOT NULL);"
        )


class FakeHcom:
    def __init__(self):
        self.calls = []
        self.roster = []
        self.events = []
        self.lose = ""
        self.before_send = None
        self.lock = threading.RLock()

    def message(
        self, text="status?", *, intent="request", name="boma", sender="soma", event_id=None
    ):
        event = {
            "id": event_id or max((row["id"] for row in self.events), default=0) + 1,
            "type": "message",
            "data": {
                "from": sender,
                "intent": intent,
                "scope": "mentions",
                "mentions": [name],
                "text": text,
            },
        }
        self.events.append(event)
        return event

    def __call__(self, argv, **kwargs):
        with self.lock:
            return self._call(argv, **kwargs)

    def _call(self, argv, **kwargs):
        self.calls.append((list(argv), kwargs))
        cmd = argv[1]
        output = ""
        name = argv[argv.index("--name") + 1] if "--name" in argv else ""
        if name and not any(row["name"] == name for row in self.roster):
            return SimpleNamespace(stdout="", stderr="unknown actor", returncode=1)
        database = Path(kwargs["env"]["HCOM_DIR"]) / "hcom.db"
        process_id = kwargs["env"].get("HCOM_PROCESS_ID", "")
        if not name and process_id:
            with sqlite3.connect(database) as db:
                row = db.execute(
                    "SELECT instance_name FROM process_bindings WHERE process_id=?", (process_id,)
                ).fetchone()
                name = row[0] if row else ""
        if cmd == "start":
            index = self.count("start") - 1
            actor_name = ("boma", "doma", "foma", "goma")[index] if index < 4 else f"b{index:03}"
            self.roster += [
                {
                    "name": actor_name,
                    "base_name": actor_name,
                    "tool": "adhoc",
                    "session_id": "",
                    "parent_name": "",
                    "launch_context": {},
                }
            ]
            with sqlite3.connect(database) as db:
                db.execute(
                    "INSERT INTO instances VALUES(?,?,?,?)", (actor_name, None, None, "adhoc")
                )
                db.execute(
                    "INSERT INTO process_bindings VALUES(?,?,?,?)",
                    (process_id, None, actor_name, 100),
                )
            output = "Registered boma"
        elif cmd == "list":
            output = json.dumps(self.roster)
        elif cmd == "events":
            if "--sql" not in argv:
                selected = self.events[-1:]
            else:
                sql = argv[argv.index("--sql") + 1]
                if "msg_from=" in sql:
                    thread = re.search(r"msg_thread='([^']+)'", sql)[1]
                    selected = [row for row in self.events if row["data"].get("thread") == thread]
                else:
                    cursor = int(re.search(r"id>([0-9]+)", sql)[1])
                    if "msg_scope" in sql:
                        name = re.search(r"value='([^']+)'", sql)[1]
                    selected = sorted(
                        [
                            row
                            for row in self.events
                            if row["id"] > cursor
                            and ("msg_scope" not in sql or name in row["data"].get("mentions", []))
                        ],
                        key=lambda row: row["id"],
                    )[:100]
            output = "\n".join(json.dumps(row) for row in selected)
        elif cmd == "send":
            if len(argv[argv.index("--thread") + 1]) > 64:
                return SimpleNamespace(stdout="", stderr="Thread name too long", returncode=1)
            if self.before_send is not None:
                self.before_send()
            recipient = argv[2].removeprefix("@")
            event = self.message(kwargs["input"], intent="ack", name=recipient, sender=name)
            event["data"]["thread"] = argv[argv.index("--thread") + 1]
            output = json.dumps({"id": event["id"]})
        elif cmd == "stop":
            self.roster = [row for row in self.roster if row["name"] != name]
            with sqlite3.connect(database) as db:
                db.execute("DELETE FROM process_bindings WHERE process_id=?", (process_id,))
        else:
            raise AssertionError(argv)
        if self.lose == cmd:
            self.lose = ""
            raise subprocess.TimeoutExpired(argv, 10)
        return SimpleNamespace(stdout=output, stderr="", returncode=0)

    def count(self, command):
        return sum(argv[1] == command for argv, _ in self.calls)


@pytest.fixture
def harness(tmp_path, monkeypatch):
    room = tmp_path / "room"
    seed_room(room)
    host = SimpleNamespace(identity=lambda pid: 42.5, liveness_of=lambda pid, identity: "dead")
    monkeypatch.setattr(bridge, "get_process_host", lambda: host)
    fake = FakeHcom()
    actor = bridge.MessageBridge(
        tmp_path / "bridge",
        room=str(room),
        target={
            "campaign_id": "campaign",
            "unit_id": "story",
            "launch_id": "launch",
            "generation": 1,
        },
        facts={"stage": "planning", "model": "astra"},
        executable="fake-hcom",
        runner=fake,
        clock=lambda: 100,
    )
    return actor, fake, host


def steer(actor, text="Continue the accepted scope", *, generation=None):
    target = dict(actor.target)
    if generation is not None:
        target["generation"] = generation
    return json.dumps({"bmad": 1, "target": target, "kind": "steer", "text": text})


def inbox(actor, event):
    return bridge.read(actor.directory / "inbox" / f"{event['id']}.json")


def test_registration_watermark_excludes_reused_name_history_and_durability_precedes_reply(harness):
    actor, fake, _ = harness
    old = fake.message(steer(actor))
    record = actor.start()
    assert record["registration_watermark"] == old["id"]
    fresh = fake.message(steer(actor))
    fake.before_send = lambda: (
        pytest.fail("inbox not durable before send")
        if inbox(actor, fresh)["text"] != "Continue the accepted scope"
        else None
    )
    result = actor.poll()
    assert result["cursor"] == fresh["id"]
    assert not (actor.directory / "inbox" / f"{old['id']}.json").exists()
    receipt = inbox(actor, fresh)
    assert receipt["disposition"] == "queued" and receipt["authority"] == "advisory"
    assert receipt["delivered_to_model"] is False
    reply = json.loads(fake.events[-1]["data"]["text"])
    assert reply["model_reply"] is False and reply["source"] == "bmad-controller-bridge"
    assert reply["facts"]["stage"] == "planning"
    assert fake.count("send") == 1 and fake.count("start") == 1
    for argv, options in fake.calls:
        if argv[1] in {"list", "events"}:
            assert "--name" not in argv and "HCOM_PROCESS_ID" not in options["env"]
        if argv[1] == "send":
            assert (
                "--name" not in argv and options["env"]["HCOM_PROCESS_ID"] == record["process_id"]
            )


def test_independent_bridge_instances_cannot_overwrite_an_inflight_poll(harness):
    from bmad_loop.platform_util import LockUnavailableError

    actor, fake, _ = harness
    actor.start()
    peer = bridge.MessageBridge(
        actor.directory, room=actor.room, target=actor.target, runner=fake, executable="fake-hcom"
    )
    event = fake.message(steer(actor))
    observed = []

    def overlapping_update():
        with pytest.raises(LockUnavailableError):
            peer.update(stage="must-not-overwrite-cursor")
        observed.append(True)

    fake.before_send = overlapping_update
    assert actor.poll()["cursor"] == event["id"]
    assert observed == [True]
    record = peer.update(stage="safe-after-poll")
    assert record["cursor"] == event["id"] and record["facts"]["stage"] == "safe-after-poll"


def test_batch_poll_sorts_mail_keeps_exact_generation_and_does_not_ack_acks(harness):
    actor, fake, _ = harness
    actor.start()
    events = [
        fake.message("status?"),
        fake.message(steer(actor, generation=2)),
        fake.message(steer(actor, generation=True)),
        fake.message(steer(actor)),
        fake.message("acknowledged", intent="ack"),
    ]
    before = len(fake.calls)
    result = actor.poll(
        events=list(reversed(events)),
        roster=copy.deepcopy(fake.roster),
        watermark=5,
        database_epoch=bridge.database_epoch(actor.room),
    )
    assert result["cursor"] == 5
    assert [inbox(actor, event)["disposition"] for event in events] == [
        "status-only",
        "stale",
        "stale",
        "queued",
        "observed-ack",
    ]
    assert fake.count("send") == 4
    assert all(argv[1] == "send" for argv, _ in fake.calls[before:])
    actor.poll(events=events, roster=fake.roster, watermark=max(row["id"] for row in fake.events))
    assert fake.count("send") == 4


def test_lost_registration_ack_reconciles_exact_process_without_duplicate_start(harness):
    actor, fake, _ = harness
    fake.lose = "start"
    with pytest.raises(subprocess.TimeoutExpired):
        actor.start()
    intent = bridge.read(actor.path)
    assert intent["registration_sent"] and intent["state"] == "registering"
    result = actor.start()
    assert result["name"] == "boma" and result["state"] == "accepting"
    assert fake.count("start") == 1


def test_missing_actor_after_uncertain_registration_does_not_register_again(harness):
    actor, fake, _ = harness
    fake.lose = "start"
    with pytest.raises(subprocess.TimeoutExpired):
        actor.start()
    fake.roster = []
    with sqlite3.connect(Path(actor.room) / "hcom.db") as db:
        db.execute("DELETE FROM process_bindings")
    with pytest.raises(bridge.BridgeError, match="uncertain; actor absent"):
        actor.start()
    assert fake.count("start") == 1


def test_actual_uuid_binding_rejects_native_or_inconsistent_roster_without_side_effect(harness):
    actor, fake, _ = harness
    started = actor.start()
    assert fake.roster[0]["launch_context"] == {} and fake.roster[0]["session_id"] == ""
    with sqlite3.connect(Path(actor.room) / "hcom.db") as db:
        db.execute("UPDATE instances SET tool='codex',session_id='native-session'")
    with pytest.raises(bridge.BridgeError, match="different actor kind"):
        actor.close()
    assert fake.count("stop") == 0
    with sqlite3.connect(Path(actor.room) / "hcom.db") as db:
        db.execute("UPDATE instances SET tool='adhoc',session_id=NULL")
    fake.roster = []
    with pytest.raises(bridge.BridgeError, match="missing from the roster"):
        actor.poll(events=[], roster=[], watermark=0)
    assert (
        fake.count("stop") == 0 and bridge.read(actor.path)["process_id"] == started["process_id"]
    )


def test_lost_reply_ack_keeps_mail_and_reconciles_without_duplicate_send(harness):
    actor, fake, _ = harness
    actor.start()
    event = fake.message(steer(actor))
    fake.lose = "send"
    with pytest.raises(subprocess.TimeoutExpired):
        actor.poll()
    assert inbox(actor, event)["disposition"] == "queued"
    assert bridge.read(actor.path)["cursor"] == 0
    assert bridge.read(actor.path)["error"]
    result = actor.poll()
    assert result["cursor"] == event["id"] and not result["error"]
    assert inbox(actor, event)["response_state"] == "confirmed"
    assert fake.count("send") == 1


def test_uncertain_reply_is_not_retried_or_skipped(harness):
    actor, fake, _ = harness
    actor.start()
    event = fake.message()
    fake.lose = "send"
    with pytest.raises(subprocess.TimeoutExpired):
        actor.poll()
    response = fake.events.pop()
    result = actor.poll()
    assert result["cursor"] == 0 and "uncertain" in result["error"]
    assert inbox(actor, event)["response_state"] == "uncertain"
    assert fake.count("send") == 1
    fake.events.append(response)
    assert actor.poll()["cursor"] == event["id"]
    assert fake.count("send") == 1


def test_lost_stop_ack_closes_exact_bridge_and_retains_late_mail_tombstone(harness):
    actor, fake, _ = harness
    actor.start()
    fake.lose = "stop"
    result = actor.close(outcome="quota")
    assert result["state"] == "closed" and result["outcome"] == "quota"
    assert actor.close() == result and fake.count("stop") == 1
    late = fake.message(steer(actor))
    actor.poll(events=[late], roster=[], watermark=late["id"])
    assert inbox(actor, late)["disposition"] == "closed"
    assert fake.count("send") == 0
    with pytest.raises(bridge.BridgeError, match="cannot reopen"):
        actor.start()
    assert all(argv[1] != "kill" for argv, _ in fake.calls)


def test_uncertain_stop_with_actor_still_present_is_not_repeated(harness):
    actor, fake, _ = harness
    actor.start()
    attempts = []

    def fail_stop(argv, **kwargs):
        if argv[1] == "stop":
            attempts.append(argv)
            raise subprocess.TimeoutExpired(argv, 10)
        return fake(argv, **kwargs)

    actor.runner = fail_stop
    with pytest.raises(subprocess.TimeoutExpired):
        actor.close()
    assert bridge.read(actor.path)["state"] == "closing"
    with pytest.raises(bridge.BridgeError, match="disconnect acknowledgement"):
        actor.close()
    assert len(attempts) == 1
    fake.roster = []
    with sqlite3.connect(Path(actor.room) / "hcom.db") as db:
        db.execute("DELETE FROM process_bindings")
    assert actor.close()["state"] == "closed"
    assert len(attempts) == 1


def test_oldest_page_drain_does_not_skip_bursty_mail(harness):
    actor, fake, _ = harness
    actor.start()
    for index in range(250):
        fake.message(f"Advisory {index}", intent="inform")
    assert actor.poll()["cursor"] == 100
    assert actor.poll()["cursor"] == 200
    assert actor.poll()["cursor"] == 250
    assert len(list((actor.directory / "inbox").glob("*.json"))) == 250
    assert fake.count("send") == 0


def test_unaddressable_sender_is_retained_without_cli_argument_injection(harness):
    actor, fake, _ = harness
    actor.start()
    event = fake.message(steer(actor), sender="--kill @everyone")
    assert actor.poll()["cursor"] == event["id"]
    assert inbox(actor, event)["response_state"] == "unaddressable-sender"
    assert fake.count("send") == 0


@pytest.mark.parametrize("liveness", ["alive", "unknown", "dead"])
def test_restart_adopts_only_confirmed_dead_owner_and_preserves_registration(harness, liveness):
    actor, fake, host = harness
    record = actor.start()
    record.update(pid=987654, identity=3.0)
    bridge.write(actor.path, record)
    host.liveness_of = lambda *args: liveness
    if liveness != "dead":
        with pytest.raises(bridge.BridgeError, match="live or uncertain"):
            actor.start()
        assert bridge.read(actor.path)["pid"] == 987654
    else:
        result = actor.start()
        assert result["pid"] == actor.pid
        assert result["previous_owners"] == [{"pid": 987654, "identity": 3.0, "adopted_at": 100}]
        assert result["process_id"] == record["process_id"]
    assert fake.count("start") == 1


@pytest.mark.parametrize("reset", ["replace", "watermark", "marker"])
def test_database_reset_never_advances_cursor_or_admits_reused_event_ids(harness, tmp_path, reset):
    actor, fake, _ = harness
    fake.message("prior", event_id=10)
    actor.start()
    event = fake.message(steer(actor))
    args = {"events": [event], "roster": fake.roster, "watermark": event["id"]}
    if reset == "replace":
        replacement = tmp_path / "replacement.db"
        replacement.write_bytes(b"new DB")
        replacement.replace(Path(actor.room) / "hcom.db")
    elif reset == "watermark":
        args["watermark"] = 0
    else:
        args["events"] = [{"db_reset": True}]
    with pytest.raises(bridge.BridgeError, match="database|reset"):
        actor.poll(**args)
    assert bridge.read(actor.path)["cursor"] == 10
    assert fake.count("send") == 0
    assert not (actor.directory / "inbox" / f"{event['id']}.json").exists()


def test_replaced_roster_is_rejected_and_recovered_observation_clears_error(harness):
    actor, fake, _ = harness
    actor.start()
    replaced = copy.deepcopy(fake.roster)
    with pytest.raises(bridge.BridgeError, match="bound"):
        actor.poll(events=[], roster=replaced, bindings={}, watermark=0)
    assert bridge.read(actor.path)["error"]
    assert actor.poll(events=[], roster=fake.roster, watermark=0)["error"] == ""


def test_closed_generation_and_wrong_target_cannot_be_rebound(harness):
    actor, fake, _ = harness
    actor.start()
    actor.target = {**actor.target, "generation": 2}
    with pytest.raises(bridge.BridgeError, match="launch or room"):
        actor.start()
    assert fake.count("start") == 1


def test_output_and_mail_bounds_fail_visibly_without_truncating_or_acknowledging(harness):
    actor, fake, _ = harness
    actor.start()
    event = fake.message(steer(actor, text="x" * 17000))
    actor.poll()
    assert inbox(actor, event)["disposition"] == "rejected"
    actor.runner = lambda *a, **k: SimpleNamespace(
        stdout="x" * (bridge.LIMIT + 1), stderr="", returncode=0
    )
    with pytest.raises(bridge.BridgeError, match="byte bound"):
        actor.poll()
    assert "byte bound" in bridge.read(actor.path)["error"]


def test_bridge_environment_never_inherits_native_identity_or_harness_detection(harness):
    actor, _, _ = harness
    environment = bridge.bridge_environment(
        {
            "PATH": "/fake",
            "HCOM_INSTANCE_NAME": "tina",
            "HCOM_PROCESS_ID": "parent",
            "CLAUDECODE": "1",
            "GROK_SESSION_ID": "native",
            "CODEX_SANDBOX": "1",
        },
        actor.room,
        "own-uuid",
    )
    assert environment["HCOM_PROCESS_ID"] == "own-uuid" and environment["HCOM_DIR"] == actor.room
    assert (
        not {"HCOM_INSTANCE_NAME", "CLAUDECODE", "GROK_SESSION_ID", "CODEX_SANDBOX"}
        & environment.keys()
    )
