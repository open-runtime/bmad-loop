"""Operator rearm requests share the scheduler's normal recovery observation."""

import json

import pytest
from test_campaign_rearm import paused_child as paused_child
from test_campaign_rearm import quota_child as quota_child

from bmad_loop import campaign_runtime, cli
from bmad_loop.campaign_model import CampaignError, Hold
from bmad_loop.campaign_store import CampaignStore


def test_cli_rearm_observes_the_bound_pause_without_spending_a_worker_attempt(
    quota_child, tmp_path, monkeypatch, capsys
):
    snapshot, unit = quota_child
    store = CampaignStore(tmp_path / "state")
    snapshot = store.create(snapshot)
    unit = snapshot.units[unit.id]
    monkeypatch.setattr(campaign_runtime, "store_for", lambda _: store)
    assert (
        cli.main(
            [
                "campaign",
                "rearm",
                snapshot.id,
                "--unit",
                unit.id,
                "--hold-id",
                unit.hold.id,
                "--json",
            ]
        )
        == 0
    )
    request = json.loads(capsys.readouterr().out)["request"]
    runtime = campaign_runtime.CampaignRuntime(tmp_path, store, clock=lambda: 100)
    predecessor = unit.claim.launch_id
    runtime.control(snapshot, request)
    runtime.control(snapshot, request)
    [effect] = [a for a in snapshot.outbox if a.kind == "resolution-reprobe"]
    assert effect.payload["rearm_binding"]["predecessor"]["launch_id"] == predecessor
    assert unit.hold.kind == "quota" and unit.claim.launch_id == predecessor and unit.attempt == 2


@pytest.mark.parametrize("change", ["hold", "head", "budget", "deadline"])
def test_obsolete_operator_rearm_cannot_authorize_a_successor(quota_child, tmp_path, change):
    from bmad_loop.campaign_rearm_control import apply, proposal

    snapshot, unit = quota_child
    hold_id = unit.hold.id
    binding = proposal(snapshot, unit.id, hold_id, now=100)
    command = {"unit": unit.id, "payload": {"hold_id": hold_id, "binding_id": binding["id"]}}
    if change == "hold":
        unit.hold = Hold("operator", "owner stopped this task", id=hold_id)
    elif change == "head":
        unit.bindings["head"] = "c" * 40
    elif change == "budget":
        unit.evidence["dev_attempts_spent"] += 1
    else:
        unit.retry_at = 150
    with pytest.raises(CampaignError):
        apply(snapshot, command, now=100)
    assert all(a.kind != "resolution-reprobe" for a in snapshot.outbox)
