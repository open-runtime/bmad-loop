"""Owner scope approval travels through the actual CLI inbox and runtime control."""

import json
from dataclasses import replace

import pytest
from test_campaign_core import make_snapshot

from bmad_loop import cli
from bmad_loop.campaign_acceptance import ensure
from bmad_loop.campaign_model import CampaignError, CampaignUnit, UnitSpec, UnitState
from bmad_loop.campaign_runtime import CampaignRuntime
from bmad_loop.campaign_scope_authority import proposal
from bmad_loop.campaign_store import CampaignStore


@pytest.fixture
def case(tmp_path):
    snapshot = make_snapshot()
    unit = next(iter(snapshot.units.values()))
    unit.spec = replace(unit.spec, acceptance=("core proof", "extended proof"))
    ensure(unit)
    unit.spec = replace(unit.spec, acceptance=("core proof",))
    planning = CampaignUnit(
        UnitSpec("planning", "planning", "epic", "Revised scope", kind="planning"),
        UnitState.DELIVERED,
        bindings={"pr": {"number": 12}},
        evidence={"planning_flow": {"canonical_revision": "merged-plan"}},
    )
    snapshot.units[planning.id] = planning
    unit.bindings["planning_unit"] = planning.id
    store = CampaignStore(tmp_path / "state")
    snapshot = store.create(snapshot)
    return store, snapshot, unit.id


def test_cli_queued_authorization_reduces_only_exact_delivered_scope(
    case, monkeypatch, capsys, tmp_path
):
    from bmad_loop import campaign_runtime

    store, snapshot, unit_id = case
    monkeypatch.setattr(campaign_runtime, "store_for", lambda _: store)
    revision = ensure(snapshot.units[unit_id])["revision"]
    assert (
        cli.main(
            [
                "campaign",
                "authorize-scope",
                snapshot.id,
                "--unit",
                unit_id,
                "--expected-revision",
                str(revision),
                "--reason",
                "Owner accepts revised core scope",
                "--json",
            ]
        )
        == 0
    )
    request = json.loads(capsys.readouterr().out)["request"]
    assert (
        len(
            [
                row
                for row in ensure(store.load(snapshot.id).units[unit_id])["criteria"]
                if row["required"]
            ]
        )
        == 2
    )
    runtime = CampaignRuntime(tmp_path, store)
    runtime.control(snapshot, store.commands(snapshot.id)[0])
    snapshot = store.save(snapshot, snapshot.revision)
    ledger = ensure(snapshot.units[unit_id])
    assert [row["text"] for row in ledger["criteria"] if row["required"]] == ["core proof"]
    assert ledger["amendments"][-1]["authority"]["id"] == request["id"]
    assert ledger["amendments"][-1]["authority"]["source"].startswith("campaign-command:")
    snapshot.units[unit_id].spec = replace(
        snapshot.units[unit_id].spec, acceptance=("core proof", "extended proof")
    )
    assert len([row for row in ensure(snapshot.units[unit_id])["criteria"] if row["required"]]) == 2


def test_changed_graph_refuses_queued_scope_authorization(case, tmp_path):
    store, snapshot, unit_id = case
    payload = proposal(
        snapshot, unit_id, ensure(snapshot.units[unit_id])["revision"], "reduced scope"
    )
    command = store.enqueue(snapshot.id, "authorize-scope", unit=unit_id, scope=payload)
    snapshot.graph_revision += 1
    with pytest.raises(CampaignError, match="scope changed"):
        CampaignRuntime(tmp_path, store).control(snapshot, command)
    assert all(row["required"] for row in ensure(snapshot.units[unit_id])["criteria"])


def test_unmerged_plan_cannot_offer_scope_authorization(case):
    _, snapshot, unit_id = case
    snapshot.units["planning"].state = UnitState.REVIEWING
    with pytest.raises(CampaignError, match="delivered revised"):
        proposal(snapshot, unit_id, ensure(snapshot.units[unit_id])["revision"], "reduce")
