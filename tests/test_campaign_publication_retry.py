"""Repeated main movement has a durable bound without reusing old acceptance."""

import pytest

from bmad_loop.campaign_model import (
    CampaignError,
    CampaignSnapshot,
    CampaignSpec,
    CampaignUnit,
    GitHubSpec,
    Hold,
    OutboxAction,
    UnitSpec,
    UnitState,
    snapshot_from_dict,
    snapshot_to_dict,
    spec_from_dict,
)
from bmad_loop.campaign_runtime import CampaignRuntime
from bmad_loop.campaign_store import CampaignStore
from bmad_loop.campaign_supervisor import Observation


@pytest.mark.parametrize("kind", ["prepare-merge", "github"])
def test_repeated_main_movement_holds_only_starved_unit_and_survives_resume(
    project, tmp_path, kind
):
    unit = CampaignUnit(UnitSpec("unit", "source", "epic", "Candidate"))
    sibling = CampaignUnit(UnitSpec("sibling", "source", "epic", "Independent"))
    spec = CampaignSpec(
        "retry", units=(unit.spec, sibling.spec), github=GitHubSpec(max_base_refreshes=2)
    )
    snapshot = CampaignSnapshot(
        "campaign", "org/repo", spec, units={unit.id: unit, sibling.id: sibling}
    )
    store = CampaignStore(tmp_path / "campaigns")
    snapshot = store.create(snapshot)
    runtime = CampaignRuntime(project.project, store)
    for index in range(2):
        unit = snapshot.units["unit"]
        unit.state = UnitState.READY_TO_PUBLISH
        unit.evidence.update(
            integrated={"base": f"base-{index}"},
            review={"accepted": True},
            verification={"accepted": True},
            pushed="old-head",
        )
        data = {"needs_integration": True, "base": f"base-{index + 1}"}
        if kind == "github":
            data["canceled_before_write"] = True
        action = OutboxAction(f"move-{index}", kind, unit.id, {}, snapshot.generation)
        runtime._apply_effect(snapshot, action, Observation("confirmed", data))
        assert unit.state == UnitState.SYNCING
        assert not any(
            key in unit.evidence for key in ("integrated", "review", "verification", "pushed")
        )
        if index == 0:
            assert unit.hold is None
        snapshot = snapshot_from_dict(snapshot_to_dict(snapshot))
    unit = snapshot.units["unit"]
    assert unit.hold is not None and "starved" in unit.hold.reason
    assert unit.hold.evidence["base_refreshes"] == 2
    assert snapshot.units["sibling"].hold is None
    assert len(unit.evidence["base_refreshes"]) == 2
    assert snapshot.events[-1]["kind"] == "publication-starved"

    # An explicit retry follows normal fresh release observation and retains
    # the lifetime counter; it cannot resurrect prior approval evidence.
    runtime.control(snapshot, {"kind": "release", "unit": unit.id, "id": "owner-retry"})
    release = OutboxAction(
        "release", "release-observation", unit.id, unit.bindings["operator_release"], 0
    )
    observed = runtime._perform_release_observation(snapshot, release)
    runtime._apply_effect(snapshot, release, observed)
    assert unit.hold is None
    assert unit.state == UnitState.SYNCING
    assert len(unit.evidence["base_refreshes"]) == 2
    assert not any(
        key in unit.evidence for key in ("integrated", "review", "verification", "pushed")
    )


def test_exhausted_publication_preserves_owner_hold(project, tmp_path):
    spec = CampaignSpec("retry", github=GitHubSpec(max_base_refreshes=1))
    unit = CampaignUnit(
        UnitSpec("unit", "source", "epic", "Candidate"), state=UnitState.READY_TO_PUBLISH
    )
    unit.hold = Hold("owner", "Owner stopped this scope", id="owner-stop")
    unit.evidence["integrated"] = {"base": "old"}
    snapshot = CampaignSnapshot("campaign", "org/repo", spec, units={unit.id: unit})
    runtime = CampaignRuntime(project.project, CampaignStore(tmp_path / "campaigns"))
    runtime._apply_effect(
        snapshot,
        OutboxAction("move", "prepare-merge", unit.id, {}, 0),
        Observation("confirmed", {"needs_integration": True, "base": "new"}),
    )
    assert unit.hold.id == "owner-stop"
    assert "starved" in unit.evidence["pending_failure_hold"]["reason"]


@pytest.mark.parametrize("limit", [0, -1, True, "8"])
def test_invalid_base_refresh_budget_refused(limit):
    with pytest.raises(CampaignError, match="max_base_refreshes"):
        spec_from_dict(
            {"schema_version": 1, "name": "retry", "github": {"max_base_refreshes": limit}}
        )
