"""Runtime historical readers preserve exact facts after hot-state compaction."""

import json

import pytest
from test_campaign_database import state

from bmad_loop.campaign_action_history import TrackingReceipts, action, actions, counts
from bmad_loop.campaign_boundary_sync import metadata_preimage
from bmad_loop.campaign_database import LEGACY_MARKER, MARKER, CampaignDatabase
from bmad_loop.campaign_model import CampaignError, OutboxAction, WorkerClaim, snapshot_to_dict
from bmad_loop.campaign_runtime_intake import _owned_projection
from bmad_loop.campaign_store import CampaignStore, add_action, add_event


def confirmed(snapshot, identity, *, kind="github", intent=None):
    key = next(iter(snapshot.units))
    row = add_action(snapshot, kind, key, {}, action_id=identity)
    row.state = "confirmed"
    if intent is not None:
        row.receipt = {"intent": intent}
    add_event(snapshot, "effect-confirmed", action=identity)
    return row


def issue_intent(kind, native, *, before="", state_value="open"):
    fields = {
        "id": native,
        "title": "story",
        "body": "required work",
        "labels": [],
        "assignees": [],
        "state": state_value,
        "updated_at": "after-" + kind,
    }
    return {
        "action_id": kind,
        "kind": kind,
        "state": "confirmed",
        "generation": 0,
        "method": "POST",
        "path": "repos/owner/repo/issues",
        "payload": {"title": fields["title"], "body": fields["body"]},
        "coordinates": {"id": native, "updated_at": before},
        "receipt": fields,
    }


def test_cold_actions_preserve_frozen_order_export_and_duplicate_fence(tmp_path):
    store = CampaignStore(tmp_path)
    snapshot = store.create(state())
    for identity in ("first", "second"):
        confirmed(snapshot, identity)
        snapshot = store.save(snapshot, snapshot.revision)
    frozen = snapshot.clone()
    confirmed(snapshot, "third")
    snapshot = store.save(snapshot, snapshot.revision)
    assert [row.id for row in snapshot.outbox] == ["third"]
    assert [row.id for row in actions(snapshot)] == ["first", "second", "third"]
    assert [row.id for row in actions(frozen)] == ["first", "second"]
    assert [row["id"] for row in snapshot_to_dict(snapshot)["outbox"]] == [
        "first",
        "second",
        "third",
    ]
    assert [row["id"] for row in snapshot_to_dict(snapshot, include_actions=False)["outbox"]] == [
        "third"
    ]
    assert counts(snapshot)["confirmed"] == 3
    assert len(store.action_history(snapshot.id)) == 2
    old = action(snapshot, "first")
    old.receipt["observer-only"] = True
    assert "observer-only" not in action(snapshot, "first").receipt
    with pytest.raises(CampaignError, match="already exists"):
        add_action(snapshot, "github", "", {}, action_id="first")


def test_current_claim_keeps_its_launch_while_old_launch_remains_readable(tmp_path):
    store = CampaignStore(tmp_path)
    snapshot = store.create(state())
    unit = next(iter(snapshot.units.values()))
    confirmed(snapshot, "old-launch", kind="launch")
    confirmed(snapshot, "current-launch", kind="launch")
    unit.claim = WorkerClaim(unit.id, 1, 0, "current-launch", quiescent=True)
    confirmed(snapshot, "newer-record", kind="launch")
    snapshot = store.save(snapshot, snapshot.revision)
    assert {row.id for row in snapshot.outbox} == {"current-launch", "newer-record"}
    assert action(snapshot, "old-launch").kind == "launch"


def test_historical_mutation_absence_cannot_be_inferred_from_hot_outbox(tmp_path):
    store = CampaignStore(tmp_path)
    snapshot = store.create(state())
    forbidden = confirmed(snapshot, "early-merge")
    forbidden.payload["purpose"] = "merge"
    confirmed(snapshot, "later-tracking").payload["purpose"] = "tracking"
    snapshot = store.save(snapshot, snapshot.revision)
    assert all(row.payload.get("purpose") != "merge" for row in snapshot.outbox)
    assert any(row.payload.get("purpose") == "merge" for row in actions(snapshot))


def test_owned_issue_reopen_and_pending_metadata_keep_creation_order_after_archive(tmp_path):
    store = CampaignStore(tmp_path)
    snapshot = store.create(state())
    for identity, intent in (
        ("create", issue_intent("issue", 42)),
        ("close", issue_intent("close_issue", 42, before="after-issue", state_value="closed")),
        ("reopen", issue_intent("reopen_issue", 42, before="after-close_issue")),
    ):
        confirmed(snapshot, identity, intent=intent)
        snapshot = store.save(snapshot, snapshot.revision)
    observed = {
        "kind": "issue",
        "object_id": "42",
        "payload": issue_intent("reopen_issue", 42)["receipt"],
    }
    assert _owned_projection(snapshot, observed)
    observed["payload"]["body"] = "new owner requirement"
    assert not _owned_projection(snapshot, observed)
    observed["payload"]["body"] = "required work"
    old = issue_intent("close_issue", 42, state_value="closed")["receipt"]
    assert metadata_preimage(snapshot, old, {"authority_revision": 2})
    assert not metadata_preimage(
        snapshot, old, {"authority_revision": 2, "pending_metadata": ["reopen"]}
    )
    pending = add_action(
        snapshot,
        "github",
        next(iter(snapshot.units)),
        {"intent": issue_intent("close_issue", 42)},
        action_id="pending-close",
    )
    pending.payload["intent"]["state"] = "prepared"
    pending.payload["intent"]["receipt"] = {}
    observed["payload"]["state"] = "closed"
    assert _owned_projection(snapshot, observed)


def test_owned_comment_uses_cold_native_id_and_exact_body(tmp_path):
    store = CampaignStore(tmp_path)
    snapshot = store.create(state())
    for identity in ("comment-1", "comment-2"):
        intent = issue_intent("comment", identity)
        intent["payload"] = {"body": identity}
        confirmed(snapshot, identity, intent=intent)
        snapshot = store.save(snapshot, snapshot.revision)
    observation = {"kind": "comment", "object_id": "comment-1", "payload": {"body": "comment-1"}}
    assert _owned_projection(snapshot, observation)
    observation["payload"]["body"] = "owner edited the comment"
    assert not _owned_projection(snapshot, observation)


def test_lazy_tracking_receipts_find_latest_created_receipt_even_when_it_is_cold(tmp_path):
    store = CampaignStore(tmp_path)
    snapshot = store.create(state())
    for identity in ("first", "second"):
        row = confirmed(snapshot, identity, intent=issue_intent("issue", identity))
        row.payload["tracking_key"] = identity
        snapshot = store.save(snapshot, snapshot.revision)
    receipts = TrackingReceipts(snapshot)
    assert receipts["first"].receipt["id"] == "first"
    assert receipts["second"].receipt["id"] == "second"
    assert set(receipts) == {"first", "second"}


def test_schema2_upgrade_survives_database_commit_before_marker_write(tmp_path, monkeypatch):
    store = CampaignStore(tmp_path)
    snapshot = store.create(state())
    database = CampaignDatabase(store.directory(snapshot.id))
    with database.connect(write=True) as connection:
        connection.execute("PRAGMA user_version=2")
    marker = store.directory(snapshot.id) / "snapshot.json"
    marker.write_text(json.dumps(LEGACY_MARKER))
    confirmed(snapshot, "first")
    confirmed(snapshot, "second")
    write = store._write

    def lost_ack(path, value):
        if value == MARKER:
            raise OSError("marker acknowledgement lost")
        return write(path, value)

    monkeypatch.setattr(store, "_write", lost_ack)
    with pytest.raises(OSError, match="marker acknowledgement"):
        store.save(snapshot, snapshot.revision)
    assert json.loads(marker.read_text()) == LEGACY_MARKER
    restored = store.load(snapshot.id)
    assert restored.revision == 1
    assert [row.id for row in actions(restored)] == ["first", "second"]
    with database.connect() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 3
    import bmad_loop.campaign_database as database_module

    with monkeypatch.context() as old_reader:
        old_reader.setattr(database_module, "STORAGE_SCHEMA_VERSION", 2)
        with pytest.raises(CampaignError, match="unsupported campaign database schema"):
            database.read()
    monkeypatch.setattr(store, "_write", write)
    store.save(restored, restored.revision)
    assert json.loads(marker.read_text()) == MARKER


def test_new_marker_cannot_accept_an_old_database(tmp_path):
    store = CampaignStore(tmp_path)
    snapshot = store.create(state())
    with CampaignDatabase(store.directory(snapshot.id)).connect(write=True) as connection:
        connection.execute("PRAGMA user_version=2")
    with pytest.raises(CampaignError, match="marker requires a newer"):
        store.load(snapshot.id)


def test_archived_polling_observation_identity_cannot_be_reused(tmp_path):
    store = CampaignStore(tmp_path)
    snapshot = store.create(state())
    confirmed(snapshot, "old-observation", kind="inventory")
    confirmed(snapshot, "new-observation", kind="inventory")
    snapshot = store.save(snapshot, snapshot.revision)
    assert [row["id"] for row in store.observations(snapshot.id)] == ["old-observation"]
    with pytest.raises(CampaignError, match="already exists"):
        add_action(snapshot, "github", "", {}, action_id="old-observation")


def test_cold_mutation_cannot_be_reused_and_hidden_by_observation_archival(tmp_path):
    store = CampaignStore(tmp_path)
    snapshot = store.create(state())
    confirmed(snapshot, "old-write")
    confirmed(snapshot, "new-write")
    snapshot = store.save(snapshot, snapshot.revision)
    # Bypass add_action deliberately: commit must defend its own namespace,
    # including rows the observation splitter would remove before action archival.
    for identity in ("old-write", "new-poll"):
        row = OutboxAction(identity, "capacity", "", {}, snapshot.generation)
        row.state = "confirmed"
        snapshot.outbox.append(row)
    with pytest.raises(CampaignError, match="reactivate"):
        store.save(snapshot, snapshot.revision)
    restored = store.load(snapshot.id)
    assert action(restored, "old-write").kind == "github"
    assert store.observations(snapshot.id) == []
