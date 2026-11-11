"""Creating a stopped successor never opens an admission window."""

from dataclasses import replace

from test_campaign_runtime import production_case as production_case

from bmad_loop import campaign_runtime
from bmad_loop.campaign_store import CampaignStore


def test_stopped_campaign_is_persisted_from_its_first_authority_write(production_case, monkeypatch):
    case = production_case
    original = CampaignStore.create
    observed = []

    def create(store, snapshot):
        observed.append(snapshot.phase)
        assert snapshot.phase == "stopped"
        return original(store, snapshot)

    monkeypatch.setattr(CampaignStore, "create", create)
    store, saved = campaign_runtime.create(
        case.repo, replace(case.snapshot.spec, units=()), prepare_only=True
    )
    assert observed == ["stopped"]
    assert saved.phase == store.load(saved.id).phase == "stopped"
    assert not saved.outbox and not saved.jobs
    assert all(unit.attempt == 0 and unit.claim is None for unit in saved.units.values())
    assert all(not unit.bindings.get("workspace") for unit in saved.units.values())
    assert saved.bindings["execution_template"]["digest"]
    assert any(event["kind"] == "campaign-prepared" for event in saved.events)
