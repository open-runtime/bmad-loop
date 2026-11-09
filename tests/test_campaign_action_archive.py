"""Cold effect records retain order, frozen visibility, provenance and ID fences."""

import copy
import sqlite3

import pytest

from bmad_loop.campaign_action_archive import archive, counts, positions, read, split
from bmad_loop.campaign_model import CampaignError


def action(key, *, state="confirmed", kind="github", unit="story", native_id="native-1"):
    return {
        "id": key,
        "kind": kind,
        "unit": unit,
        "state": state,
        "payload": {"tracking_key": "story:create"},
        "receipt": {"intent": {"receipt": {"id": native_id}, "payload": {"body": key}}},
        "generation": 1,
    }


@pytest.fixture
def database(tmp_path):
    connection = sqlite3.connect(tmp_path / "history.sqlite")
    yield connection
    connection.close()


def test_terminal_payload_growth_is_bounded_while_all_receipts_remain_queryable(database):
    hot = []
    for revision in range(1, 101):
        hot.append(action(str(revision)))
        with database:
            hot, _ = archive(database, hot, revision=revision)
        assert len(hot) == 1
    history = read(database, through=100)
    assert len(history) == 99
    assert [row["action"]["id"] for row in history] == [str(number) for number in range(1, 100)]
    assert counts(database, through=100) == {"confirmed": 99}
    assert history[0]["action"]["receipt"]["intent"]["payload"]["body"] == "1"


def test_frozen_revision_cannot_see_later_archival_and_read_values_are_isolated(database):
    first, second = action("first"), action("second")
    with database:
        archive(database, [first], revision=1)
        archive(database, [first, second], revision=2)
    assert read(database, through=1) == []
    record = read(database, through=2, action_id="first")[0]
    record["action"]["receipt"]["intent"]["payload"]["body"] = "mutated observer"
    assert read(database, through=2, action_id="first")[0]["action"] == first
    assert counts(database, through=1) == {}


def test_creation_order_survives_later_confirmation_and_indexed_pagination(database):
    first, second, third = action("first", state="sent_unknown"), action("second"), action("third")
    with database:
        hot, _ = archive(database, [first, second, third], revision=1)
    assert [row["action"]["id"] for row in read(database, through=1)] == ["second"]
    first["state"] = "confirmed"
    with database:
        hot, _ = archive(database, hot, revision=2)
    rows = read(database, through=2, kinds=("github",), unit="story", native_id="native-1")
    assert [row["action"]["id"] for row in rows] == ["first", "second"]
    assert [row["archived_revision"] for row in rows] == [2, 1]
    assert read(database, through=2, after=rows[0]["sequence"], limit=1) == rows[1:]
    assert positions(database, ("first", "second", "third", "missing")) == {
        "first": 1,
        "second": 2,
        "third": 3,
    }
    assert read(database, through=2, tracking_key="story:create") == rows


def test_pending_effects_and_current_claims_are_never_evicted():
    rows = [
        action("prepared", state="prepared"),
        action("uncertain", state="sent_unknown"),
        action("claim"),
        action("old"),
        action("latest"),
    ]
    hot, cold = split(rows, protected=("claim",))
    assert [row["id"] for row in hot] == ["prepared", "uncertain", "claim", "latest"]
    assert [row["id"] for row in cold] == ["old"]


def test_lost_ack_keeps_committed_history_and_refuses_id_reactivation(database):
    first, second = action("first"), action("second")
    with database:
        archive(database, [first, second], revision=1)
    old = copy.deepcopy(first)
    old["state"] = "prepared"
    with pytest.raises(CampaignError, match="reactivate"):
        with database:
            archive(database, [old, second], revision=2)
    assert read(database, through=2, action_id="first")[0]["action"] == first


def test_authority_transaction_rollback_also_rolls_back_archival(database):
    first, second = action("first"), action("second")
    with database:
        archive(database, [first], revision=1)
    with pytest.raises(RuntimeError, match="authority write failed"):
        with database:
            archive(database, [first, second], revision=2)
            raise RuntimeError("authority write failed")
    assert read(database, through=2) == []
    assert positions(database, ("second",)) == {}
    with database:
        archive(database, [first, second], revision=2)
    assert read(database, through=2)[0]["action"] == first


def test_reading_older_database_does_not_mutate_schema(database):
    assert read(database, through=1) == []
    assert positions(database, ("missing",)) == {}
    assert counts(database, through=1) == {}
    assert database.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall() == []


def test_registered_creation_identity_cannot_be_rebound(database):
    row = action("first", state="prepared")
    with database:
        archive(database, [row], revision=1)
    row["unit"] = "different-story"
    with pytest.raises(CampaignError, match="redefine"):
        with database:
            archive(database, [row], revision=2)
