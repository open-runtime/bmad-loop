"""Freeze the aggregate PR evidence budget independently of other limits."""

import copy
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_campaign_core import make_snapshot
from test_campaign_headless import prepared as prepared
from test_campaign_requalification import apply
from test_campaign_requalification import campaign as campaign
from test_campaign_runtime_admission import configured

from bmad_loop.campaign_database import CampaignDatabase
from bmad_loop.campaign_discovery import RepositoryDiscovery
from bmad_loop.campaign_execution import CampaignExecution
from bmad_loop.campaign_model import (
    CampaignError,
    parse_spec,
    snapshot_from_dict,
    snapshot_to_dict,
    spec_from_dict,
    spec_to_dict,
)


def test_pr_detail_budget_toml_and_snapshot_freeze(tmp_path):
    path = tmp_path / "campaign.toml"
    path.write_text(
        'schema_version = 1\nname = "Budget"\n'
        '[[sources]]\nkind = "stories"\npath = "specs"\n'
        "[github]\ninventory_pr_detail_bytes = 12345678\n",
        encoding="utf-8",
    )
    snapshot = make_snapshot()
    snapshot.spec = parse_spec(path)
    raw = snapshot_to_dict(snapshot)
    restored = snapshot_from_dict(raw)
    raw["spec"]["github"]["inventory_pr_detail_bytes"] = 23456789
    assert restored.spec.github.inventory_pr_detail_bytes == 12345678
    assert spec_to_dict(restored.spec)["github"]["inventory_pr_detail_bytes"] == 12345678
    assert restored.spec.github.inventory_metadata_bytes == 64 * 1024 * 1024


@pytest.mark.parametrize("value", [None, True, False, 0, -1, 1.5, "16777216", [], {}])
def test_pr_detail_budget_rejects_nonpositive_or_noninteger_values(value):
    raw = spec_to_dict(make_snapshot().spec)
    raw["github"]["inventory_pr_detail_bytes"] = value
    with pytest.raises(CampaignError, match="github.inventory_pr_detail_bytes.*positive integer"):
        spec_from_dict(raw)


@pytest.mark.parametrize("budget", [None, 7654321])
def test_inventory_constructor_receives_frozen_pr_budget(project, tmp_path, monkeypatch, budget):
    runtime, snapshot, _, _ = configured(project, tmp_path)
    raw = spec_to_dict(snapshot.spec)
    raw["sources"] = [{"kind": "stories", "path": "specs"}]
    if budget is None:
        raw["github"].pop("inventory_pr_detail_bytes")
    else:
        raw["github"]["inventory_pr_detail_bytes"] = budget
    snapshot.spec = spec_from_dict(raw)
    observed = []

    def collect(discovery, **kwargs):
        observed.append(
            {
                "pr": discovery.max_pr_detail_bytes,
                "metadata": discovery.max_metadata_bytes,
                "individual": discovery.max_detail_bytes,
            }
        )
        return SimpleNamespace(to_dict=lambda: {"complete": True}, digest="inventory")

    monkeypatch.setattr(RepositoryDiscovery, "collect", collect)
    assert runtime._perform_inventory(snapshot, None).state == "confirmed"
    assert observed == [
        {
            "pr": 16 * 1024 * 1024 if budget is None else budget,
            "metadata": 64 * 1024 * 1024,
            "individual": 1_000_000,
        }
    ]


def test_legacy_snapshot_budget_default_keeps_startup_renewal_authority(campaign):
    store, before, template, _ = campaign
    database = CampaignDatabase(store.directory(before.id))
    with database.connect(write=True) as connection:
        with connection:
            connection.execute(
                "UPDATE authority SET payload=json_remove(payload, ?) WHERE id=1",
                ("$.spec.github.inventory_pr_detail_bytes",),
            )
    raw, _ = database.read()
    assert "inventory_pr_detail_bytes" not in raw["spec"]["github"]
    frozen_config = copy.deepcopy(raw["config_snapshot"])
    loaded = store.load(before.id)
    assert loaded.spec.github.inventory_pr_detail_bytes == 16 * 1024 * 1024
    assert loaded.config_snapshot == frozen_config
    binding = loaded.bindings["execution_template"]
    old_path = Path(binding["path"])
    old_bytes = old_path.read_bytes()
    assert CampaignExecution.load(old_path, binding["digest"]) == template
    after = apply(campaign)
    assert after.phase == "stopped" and after.generation == before.generation
    assert after.config_revision == before.config_revision + 1
    assert after.spec == loaded.spec and after.units == loaded.units
    assert after.config_snapshot["policy"] == frozen_config["policy"]
    assert old_path.read_bytes() == old_bytes
    renewed = after.bindings["execution_template"]
    restored = CampaignExecution.load(Path(renewed["path"]), renewed["digest"])
    assert replace(restored, directory=template.directory, config_revision=1) == template
    saved, _ = database.read()
    assert saved["spec"]["github"]["inventory_pr_detail_bytes"] == 16 * 1024 * 1024
