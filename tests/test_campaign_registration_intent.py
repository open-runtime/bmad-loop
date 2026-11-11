"""Deterministic producer/observer interleavings over real endpoint SQLite."""

import json
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from test_campaign_managed_communication import managed as managed
from test_campaign_managed_communication import send, target

from bmad_loop import campaign_message_bridge as bridge


def pause_start(monkeypatch, unit):
    entered, release = threading.Event(), threading.Event()
    original = bridge.MessageBridge.start

    def start(actor, **kwargs):
        if actor.target["unit_id"] == unit:
            entered.set()
            assert release.wait(10), "registration fixture was not released"
        return original(actor, **kwargs)

    monkeypatch.setattr(bridge.MessageBridge, "start", start)
    return entered, release


def room_cursor(manager):
    with manager._db() as db:
        return json.loads(db.execute("SELECT record FROM rooms").fetchone()[0])["cursor"]


def test_refresh_preserves_registered_identity_while_prepare_is_in_flight(managed, monkeypatch):
    manager, snapshot, fake, _ = managed
    endpoint = manager.prepare(snapshot, target(snapshot))
    send(fake, endpoint)
    entered, release = pause_start(monkeypatch, "u1")
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(manager.prepare, snapshot, target(snapshot), {"label": "new"})
        try:
            assert entered.wait(10)
            indexed = manager.endpoints()[0]
            assert indexed["process_id"] == endpoint["process_id"]
            assert indexed["name"] == endpoint["name"] and indexed["state"] == "accepting"
            observed = manager.observe(snapshot)
            assert observed["available"] and observed["queued_messages"] == 1, observed
        finally:
            release.set()
        refreshed = future.result(timeout=10)
    assert refreshed["process_id"] == endpoint["process_id"] and fake.count("start") == 1
    assert refreshed["facts"]["label"] == "new"
    assert manager.observe(snapshot)["queued_messages"] == 0
    assert len(manager.ledger.pending("u1")) == fake.count("send") == 1


def test_initial_intent_between_observer_passes_preserves_room_mail(managed, monkeypatch):
    manager, snapshot, fake, _ = managed
    endpoint = manager.prepare(snapshot, target(snapshot))
    event = send(fake, endpoint)
    cursor = room_cursor(manager)
    entered, release = pause_start(monkeypatch, "u2")
    read_room = manager._room
    futures = []
    with ThreadPoolExecutor(max_workers=1) as executor:

        def observe_room(*args):
            result = read_room(*args)
            if not futures:
                futures.append(executor.submit(manager.prepare, snapshot, target(snapshot, "u2")))
                assert entered.wait(10)
            return result

        monkeypatch.setattr(manager, "_room", observe_room)
        try:
            observed = manager.observe(snapshot)
            assert not observed["available"]
            assert any(
                "registration identity is incomplete" in error for error in observed["errors"]
            )
            assert observed["queued_messages"] == fake.count("send") == 0
            assert not manager.ledger.pending("u1")
            assert room_cursor(manager) == cursor
            pending = next(row for row in observed["endpoints"] if row["target"]["unit_id"] == "u2")
            assert pending["state"] == "registering" and not pending["process_id"]
        finally:
            release.set()
        futures[0].result(timeout=10)
    recovered = manager.observe(snapshot)
    assert recovered["available"] and recovered["queued_messages"] == 1, recovered
    assert room_cursor(manager) >= event["id"]
    assert fake.count("start") == 2 and fake.count("send") == 1
    assert len(manager.ledger.pending("u1")) == 1
    assert manager.observe(snapshot)["queued_messages"] == 0


def test_initial_intent_without_room_is_visible_until_original_registration_finishes(
    managed, monkeypatch
):
    manager, snapshot, fake, _ = managed
    entered, release = pause_start(monkeypatch, "u1")
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(manager.prepare, snapshot, target(snapshot))
        try:
            assert entered.wait(10)
            observed = manager.observe(snapshot)
            assert not observed["available"] and observed["counts"] == {"registering": 1}
            assert "binding is missing" in observed["errors"][0]
            assert fake.count("start") == fake.count("send") == 0
        finally:
            release.set()
        future.result(timeout=10)
    assert manager.observe(snapshot)["available"] and fake.count("start") == 1


def test_legacy_minimal_index_recovers_from_exact_existing_actor(managed):
    manager, snapshot, fake, _ = managed
    endpoint = manager.prepare(snapshot, target(snapshot))
    send(fake, endpoint)
    manager._save(
        endpoint["room"],
        endpoint["target"],
        {"room": endpoint["room"], "target": endpoint["target"], "state": "registering"},
    )
    observed = manager.observe(snapshot)
    assert observed["available"] and observed["queued_messages"] == 1
    assert manager.endpoints()[0]["process_id"] == endpoint["process_id"]
    assert fake.count("start") == fake.count("send") == 1


@pytest.mark.parametrize("invalid", [None, "", 17, []])
def test_invalid_index_identity_does_not_consume_room_mail(managed, monkeypatch, invalid):
    manager, snapshot, fake, _ = managed
    endpoint = manager.prepare(snapshot, target(snapshot))
    send(fake, endpoint)
    cursor = room_cursor(manager)
    read_room = manager._room

    def observe_room(*args):
        result = read_room(*args)
        actor = manager._actor(endpoint["room"], endpoint["target"])
        record = bridge.read(actor.path)
        manager._save(endpoint["room"], endpoint["target"], {**record, "process_id": invalid})
        return result

    monkeypatch.setattr(manager, "_room", observe_room)
    observed = manager.observe(snapshot)
    assert not observed["available"]
    assert any("registration identity is incomplete" in error for error in observed["errors"])
    assert observed["queued_messages"] == fake.count("send") == 0
    assert not manager.ledger.pending("u1") and room_cursor(manager) == cursor
    monkeypatch.setattr(manager, "_room", read_room)
    assert manager.observe(snapshot)["queued_messages"] == 1
    assert fake.count("start") == fake.count("send") == 1
