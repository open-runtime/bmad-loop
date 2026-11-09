"""Polling history is cold without losing mutation or live-action authority."""

import json

import pytest
from test_campaign_database import state

from bmad_loop import cli
from bmad_loop.campaign_database import CampaignDatabase
from bmad_loop.campaign_model import CampaignError, OutboxAction
from bmad_loop.campaign_store import CampaignStore, add_action


def polling(snapshot, count=100):
    for index in range(count):
        action = add_action(
            snapshot, "capacity", "", {"observation": index}, action_id=f"poll{index}"
        )
        action.state = "confirmed"
        action.receipt = {"observed_at": index, "complete": True}
    return snapshot


def test_repeated_observation_hot_state_is_bounded_and_history_complete(tmp_path):
    store = CampaignStore(tmp_path)
    snapshot = store.create(state())
    snapshot = store.save(polling(snapshot, 1000), snapshot.revision)
    assert len(snapshot.outbox) == 1 and snapshot.outbox[0].id == "poll999"
    assert store.load(snapshot.id).outbox == snapshot.outbox
    first = store.observations(snapshot.id, limit=400)
    second = store.observations(snapshot.id, after=first[-1]["sequence"], limit=1000)
    rows = first + second
    assert [row["id"] for row in rows] == [f"poll{index}" for index in range(999)]
    assert rows[198]["receipt"] == {"observed_at": 198, "complete": True}
    raw, _ = CampaignDatabase(store.directory(snapshot.id)).read()
    assert len(json.dumps(raw)) < 20000
    current = add_action(snapshot, "capacity", "", {"observation": 1000}, action_id="poll1000")
    current.state = "confirmed"
    snapshot = store.save(snapshot, snapshot.revision)
    assert snapshot.outbox == [current]
    assert len(store.observations(snapshot.id, limit=1000)) == 1000


def test_archive_keeps_uncertain_mutations_and_current_per_unit(tmp_path):
    store = CampaignStore(tmp_path)
    snapshot = store.create(state())
    polling(snapshot, 3)
    expected = {"poll2"}
    for index, (kind, state_, unit, payload, receipt) in enumerate(
        [
            ("capacity", "sent_unknown", "", {}, {}),
            ("capacity", "prepared", "", {}, {}),
            ("capacity", "rejected", "", {}, {}),
            ("github", "confirmed", "", {}, {}),
            ("launch", "confirmed", "", {}, {}),
            ("checkpoint", "confirmed", "", {}, {}),
            ("capacity", "confirmed", "another", {}, {}),
            ("capacity", "confirmed", "", {"intent": {"kind": "mutation"}}, {}),
            ("capacity", "confirmed", "", {}, {"intent": {"kind": "mutation"}}),
        ]
    ):
        row = add_action(snapshot, kind, unit, payload, action_id=f"keep{index}")
        row.state, row.receipt = state_, receipt
        expected.add(row.id)
    result = store.save(snapshot, snapshot.revision)
    from bmad_loop.campaign_action_history import actions

    assert {row.id for row in actions(result)} == expected
    assert {"keep0", "keep1"} <= {row.id for row in result.outbox}
    assert [row["id"] for row in store.observations(result.id)] == ["poll0", "poll1"]


def test_archive_and_hot_pruning_are_one_transaction(tmp_path, monkeypatch):
    store = CampaignStore(tmp_path)
    snapshot = store.create(state())
    snapshot = store.save(polling(snapshot, 1), snapshot.revision)
    row = add_action(snapshot, "capacity", "", {}, action_id="next")
    row.state = "confirmed"
    archive = CampaignDatabase._archive_observations

    def fail(connection, incoming, archived):
        archive(connection, incoming, archived)
        raise OSError("interrupted archive transaction")

    monkeypatch.setattr(CampaignDatabase, "_archive_observations", staticmethod(fail))
    with pytest.raises(OSError, match="interrupted archive"):
        store.save(snapshot, snapshot.revision)
    assert [row.id for row in store.load(snapshot.id).outbox] == ["poll0"]
    assert store.observations(snapshot.id) == []


def test_archived_id_cannot_be_reused_as_an_active_action(tmp_path):
    store = CampaignStore(tmp_path)
    snapshot = store.create(state())
    snapshot = store.save(polling(snapshot, 2), snapshot.revision)
    with pytest.raises(CampaignError, match="already exists"):
        add_action(snapshot, "push", "", {"head": "new"}, action_id="poll0")
    snapshot.outbox.append(OutboxAction("poll0", "push", "", {"head": "new"}, snapshot.generation))
    with pytest.raises(CampaignError, match="reactivate"):
        store.save(snapshot, snapshot.revision)
    assert store.load(snapshot.id).outbox[0].id == "poll1"


def test_old_schema_two_database_gains_archive_without_authority_switch(tmp_path):
    store = CampaignStore(tmp_path)
    snapshot = store.create(state())
    database = CampaignDatabase(store.directory(snapshot.id))
    with database.connect(write=True) as connection:
        connection.execute("DROP TABLE observations")
    assert store.observations(snapshot.id) == []
    snapshot = store.save(polling(snapshot, 2), snapshot.revision)
    assert store.observations(snapshot.id)[0]["id"] == "poll0"
    assert snapshot.outbox[0].id == "poll1"


def test_cli_exposes_archived_observation_cursor(tmp_path, monkeypatch, capsys):
    from bmad_loop import campaign_runtime

    store = CampaignStore(tmp_path)
    snapshot = store.create(state())
    snapshot = store.save(polling(snapshot, 4), snapshot.revision)
    monkeypatch.setattr(campaign_runtime, "store_for", lambda _: store)
    assert (
        cli.main(["campaign", "history", snapshot.id, "--observations", "--limit", "2", "--json"])
        == 0
    )
    result = json.loads(capsys.readouterr().out)
    assert result["scope"] == "archived-observations"
    assert result["next_after"] == 2 and result["has_more"] is True
    assert [row["id"] for row in result["observations"]] == ["poll0", "poll1"]
