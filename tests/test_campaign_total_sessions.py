"""All worker reservations share an explicit cap without rewriting old configs."""

import copy
from dataclasses import asdict, replace

import pytest
from test_campaign_core import claim, make_snapshot
from test_campaign_headless import prepared as prepared
from test_campaign_reasoning_jobs import case

from bmad_loop.campaign_model import CampaignError, UnitState, spec_from_dict, spec_to_dict
from bmad_loop.campaign_runtime import CampaignRuntime
from bmad_loop.campaign_scheduler import admit, resource_available, session_usage
from bmad_loop.campaign_store import CampaignStore, add_action


def limit(snapshot, total, *, model=8):
    snapshot.spec = replace(
        snapshot.spec,
        resources=replace(
            snapshot.spec.resources, max_sessions=model, max_total_sessions=total, verify_slots=8
        ),
    )


def test_historical_resource_configuration_retains_canonical_identity():
    snapshot = make_snapshot()
    raw = spec_to_dict(snapshot.spec)
    assert raw["resources"] == {"verify_slots": 1, "max_sessions": 4, "budget_usd": None}
    assert spec_to_dict(spec_from_dict(copy.deepcopy(raw))) == raw
    limit(snapshot, 32)
    current = spec_to_dict(snapshot.spec)
    assert current["resources"]["max_total_sessions"] == 32
    assert spec_to_dict(spec_from_dict(current)) == current


@pytest.mark.parametrize("value", [True, 0, -1, 1.5, "32"])
def test_invalid_total_session_limit_is_rejected(value):
    raw = spec_to_dict(make_snapshot().spec)
    raw["resources"]["max_total_sessions"] = value
    with pytest.raises(CampaignError, match="max_total_sessions"):
        spec_from_dict(raw)


@pytest.mark.parametrize(
    "occupied", ["execution", "review", "verify", "reasoning", "unknown-unit", "unknown-job"]
)
def test_every_reserved_actor_blocks_all_classes_until_quiescent(occupied):
    snapshot = make_snapshot()
    limit(snapshot, 1)
    unit = next(iter(snapshot.units.values()))
    if occupied in {"reasoning", "unknown-job"}:
        snapshot.jobs["job"] = {
            "state": "running",
            "claim": {"quiescent": False} if occupied == "reasoning" else None,
        }
    elif occupied == "unknown-unit":
        unit.state = UnitState.RUNNING
    else:
        unit.state = UnitState.VERIFYING
        unit.claim = claim(unit, resource=occupied)
    assert session_usage(snapshot).total == 1
    assert admit(snapshot).units == ()
    for resource in ("execution", "review", "verify", "resolution", "planning"):
        assert not resource_available(snapshot, resource)
    if snapshot.jobs:
        snapshot.jobs["job"].update(state="completed", claim={"quiescent": True})
    elif unit.claim:
        unit.claim.quiescent = True
    else:
        unit.state = UnitState.READY
    assert session_usage(snapshot).total == 0
    assert len(admit(snapshot).units) == 1
    assert all(
        resource_available(snapshot, resource) for resource in ("execution", "review", "verify")
    )


def test_legacy_model_ceiling_keeps_verification_separate():
    snapshot = make_snapshot()
    snapshot.spec = replace(
        snapshot.spec, resources=replace(snapshot.spec.resources, max_sessions=1)
    )
    unit = next(iter(snapshot.units.values()))
    unit.state = UnitState.VERIFYING
    unit.claim = claim(unit, resource="verify")
    assert session_usage(snapshot).total == 1
    assert resource_available(snapshot, "execution")
    assert len(admit(snapshot).units) == 1


@pytest.mark.parametrize("kind", ["verify", "queue_verify"])
def test_final_prepare_guard_defers_verifier_then_admits_released_slot(tmp_path, kind):
    snapshot = make_snapshot()
    limit(snapshot, 1)
    occupied, target, *_ = snapshot.units.values()
    occupied.state = UnitState.RUNNING
    occupied.claim = claim(occupied)
    target.state = UnitState.SYNCING
    target.bindings.update(workspace=str(tmp_path), branch="owned")
    runtime = CampaignRuntime(tmp_path, CampaignStore(tmp_path / "state"), clock=lambda: 100)
    action = add_action(snapshot, "launch", target.id, {"kind": kind})
    runtime.control(snapshot, {"kind": "prepare-effect", "action": asdict(action)})
    assert (
        action.state == "confirmed"
        and action.receipt["admission_deferred"] == "total session capacity is reserved"
    )
    assert target.claim is None and target.attempt == 0 and target.hold is None
    occupied.claim.quiescent = True
    successor = add_action(snapshot, "launch", target.id, {"kind": kind})
    runtime.control(snapshot, {"kind": "prepare-effect", "action": asdict(successor)})
    assert target.claim.launch_id == successor.id
    assert target.claim.resource == "verify" and target.attempt == 1
    assert session_usage(snapshot).total == 1


def test_final_prepare_guard_also_honors_independent_verification_limit(tmp_path):
    snapshot = make_snapshot()
    limit(snapshot, 8)
    snapshot.spec = replace(
        snapshot.spec, resources=replace(snapshot.spec.resources, verify_slots=1)
    )
    occupied, target, *_ = snapshot.units.values()
    occupied.state = UnitState.VERIFYING
    occupied.claim = claim(occupied, resource="verify")
    target.state = UnitState.SYNCING
    target.bindings.update(workspace=str(tmp_path), branch="owned")
    runtime = CampaignRuntime(tmp_path, CampaignStore(tmp_path / "state"), clock=lambda: 100)
    action = add_action(snapshot, "launch", target.id, {"kind": "verify"})
    runtime.control(snapshot, {"kind": "prepare-effect", "action": asdict(action)})
    assert (
        action.state == "confirmed"
        and action.receipt["admission_deferred"] == "verify capacity is reserved"
    )
    assert target.claim is None and target.attempt == 0
    assert session_usage(snapshot).total == 1 < 8


def test_verifier_reservation_blocks_reasoning_and_releases_normal_prepare(tmp_path, prepared):
    def configure(snapshot):
        limit(snapshot, 1)
        unit = next(iter(snapshot.units.values()))
        unit.state = UnitState.VERIFYING
        unit.claim = claim(unit, resource="verify")

    snapshot, runtime, supervisor, _ = case(tmp_path, prepared, count=1, configure=configure)
    proposals = runtime.observe_workers(snapshot)
    assert not any(p.kind == "reasoning-launch" for p in proposals)
    assert any(
        p.kind == "reasoning-wait" and "total session capacity" in p.payload["reason"]
        for p in proposals
    )
    snapshot.units[next(iter(snapshot.units))].claim.quiescent = True
    proposals = runtime.observe_workers(snapshot)
    assert any(p.kind == "reasoning-launch" for p in proposals)
    snapshot = supervisor._queue(snapshot, proposals)
    assert snapshot.jobs["job-0"]["claim"]["status"] == "prepared"
    assert snapshot.jobs["job-0"]["attempt"] == 1
    assert session_usage(snapshot).total == 1
