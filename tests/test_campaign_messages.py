"""Durable safe-boundary mail and normal CLI delivery projections."""

import json

import pytest
from test_campaign_cli import runtime as runtime

from bmad_loop import cli
from bmad_loop.campaign_messages import MessageLedger, context
from bmad_loop.campaign_model import CampaignError, UnitState


def test_read_only_empty_ledger_does_not_create_state(tmp_path):
    ledger = MessageLedger(tmp_path / "missing")
    assert ledger.list() == []
    assert not ledger.directory.exists()


def test_accept_replay_pagination_and_application_survive_restart(tmp_path):
    ledger = MessageLedger(tmp_path)
    for number in range(5):
        ledger.accept(message_id=str(number), unit="unit", text=f"instruction {number}")
    ledger.accept(message_id="0", unit="unit", text="instruction 0")
    first = ledger.list(limit=2)
    assert [row["id"] for row in first] == ["0", "1"]
    assert [row["id"] for row in ledger.list(after=first[-1]["sequence"], limit=2)] == ["2", "3"]
    ledger.applied(["0"], launch_id="launch", unit="unit")
    reopened = MessageLedger(tmp_path)
    assert reopened.list()[0]["state"] == "applied"
    assert reopened.list()[0]["result"]["compliance_verified"] is False
    assert [row["id"] for row in reopened.pending("unit")] == ["1", "2", "3", "4"]


def test_identity_cannot_be_reused_and_wrong_target_rolls_back_application(tmp_path):
    ledger = MessageLedger(tmp_path)
    ledger.accept(message_id="one", unit="unit", text="keep acceptance")
    ledger.accept(message_id="two", unit="other", text="other task")
    with pytest.raises(CampaignError, match="reused"):
        ledger.accept(message_id="one", unit="unit", text="drop acceptance")
    with pytest.raises(CampaignError, match="target differs"):
        ledger.applied(["one", "two"], launch_id="launch", unit="unit")
    assert ledger.list()[0]["state"] == "queued"


def test_prepared_context_is_immutable_and_new_mail_waits_for_next_boundary(tmp_path):
    ledger = MessageLedger(tmp_path)
    ledger.accept(message_id="one", unit="unit", text="first")
    frozen = ledger.freeze("unit", "launch")
    ledger.accept(message_id="two", unit="unit", text="second")
    assert ledger.freeze("unit", "launch") == frozen
    assert "second" not in context(frozen)
    ledger.applied(["one"], launch_id="launch", unit="unit")
    assert [row["id"] for row in ledger.freeze("unit", "next")] == ["two"]


def test_job_mail_is_not_consumed_by_another_job_and_payloads_are_bounded(tmp_path):
    ledger = MessageLedger(tmp_path)
    ledger.accept(message_id="job-one", unit="unit", job="one", text="specific")
    assert ledger.pending("unit", job="two") == []
    assert ledger.pending("unit", job="one")[0]["id"] == "job-one"
    with pytest.raises(CampaignError, match="16384"):
        ledger.accept(message_id="oversize", unit="unit", text="x" * 16385)
    assert ledger.pending("unit", job="one", max_bytes=1) == []


def test_cli_steer_and_messages_use_same_durable_ledger(runtime, capsys):
    store, snapshot, calls = runtime
    assert (
        cli.main(
            [
                "campaign",
                "steer",
                snapshot.id,
                "--unit",
                "s:e:u",
                "--text",
                "Keep all acceptance",
                "--request-id",
                "owner-message",
                "--json",
            ]
        )
        == 0
    )
    accepted = json.loads(capsys.readouterr().out)
    assert accepted["state"] == "queued"
    assert accepted["message"]["authority"] == "advisory"
    assert cli.main(["campaign", "messages", snapshot.id, "--unit", "s:e:u", "--json"]) == 0
    document = json.loads(capsys.readouterr().out)
    assert document["messages"][0]["id"] == "owner-message"
    assert document["messages"][0]["state"] == "queued"
    assert calls == []
    assert store.load(snapshot.id).revision == snapshot.revision


def test_job_cannot_consume_unit_mail_and_closing_is_durable(tmp_path):
    ledger = MessageLedger(tmp_path)
    ledger.accept(message_id="unit-mail", unit="unit", text="next coding stage")
    assert ledger.pending("unit", job="reasoner") == []
    ledger.close_target("unit", reason="unit-delivered")
    assert ledger.list()[0]["state"] == "closed"
    with pytest.raises(CampaignError, match="closed"):
        MessageLedger(tmp_path).accept(message_id="late", unit="unit", text="late writer")
    ledger.accept(message_id="unit-mail", unit="unit", text="next coding stage")
    assert ledger.list()[0]["state"] == "closed"


def test_cli_cannot_reopen_delivered_unit_through_mail(runtime, capsys):
    store, snapshot, _ = runtime
    snapshot.units["s:e:u"].state = UnitState.DELIVERED
    snapshot.id = "closed-campaign"
    store.create(snapshot)
    assert (
        cli.main(
            [
                "campaign",
                "steer",
                snapshot.id,
                "--unit",
                "s:e:u",
                "--text",
                "reopen",
                "--json",
            ]
        )
        != 0
    )
    assert "closed" in capsys.readouterr().err
    assert MessageLedger(store.directory(snapshot.id)).list() == []
