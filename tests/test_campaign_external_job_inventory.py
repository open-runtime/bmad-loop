"""Runtime ownership filtering preserves other campaigns' observed native jobs."""

from dataclasses import asdict

from test_campaign_inventory_projection import native_state
from test_campaign_migration import observed

from bmad_loop.campaign_model import OutboxAction
from bmad_loop.campaign_runtime import CampaignRuntime
from bmad_loop.campaign_store import CampaignStore
from bmad_loop.campaign_supervisor import Observation


def test_inventory_excludes_only_this_campaign_native_units_and_jobs(project, tmp_path):
    store = CampaignStore(tmp_path / "native")
    current = store.create(native_state(project, "current"))
    store.create(native_state(project, "other", phase="stopped"))
    inventory = observed(project, campaign_root=store.root)
    assert inventory.complete, inventory.unavailable
    recorded = {item.id for item in inventory.items if item.kind in {"native-unit", "native-job"}}
    assert recorded == {
        "native-unit:current:s:project:u",
        "native-job:current:reasoning-job",
        "native-unit:other:s:project:u",
        "native-job:other:reasoning-job",
    }
    runtime = CampaignRuntime(project.project, store, clock=lambda: 100)
    runtime._apply_effect(
        current,
        OutboxAction("inventory", "inventory", "", {}, current.generation),
        Observation(
            "confirmed",
            {"complete": True, "observed_at": 100, "external_ownership": asdict(inventory)},
        ),
    )
    # Keep the authoritative inventory intact, including our own claims.
    assert {
        item["id"]
        for item in current.bindings["external_ownership"]["items"]
        if item["kind"] in {"native-unit", "native-job"}
    } == recorded
    external = current.bindings["external_workers"]
    assert {item["id"] for item in external} == {
        "native-unit:other:s:project:u",
        "native-job:other:reasoning-job",
    }
    job = next(item for item in external if item["kind"] == "native-job")
    assert job["liveness"] == "unknown" and job["ownership"] == "owned"
    assert job["details"]["claim"]["quiescent"] is False
    assert job["details"]["admissions_open"] is False
