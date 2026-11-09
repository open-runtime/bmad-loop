"""Native lifecycle observations are visible and never delay product delivery."""

from pathlib import Path
from types import SimpleNamespace

from test_campaign_reflection_flow import flow as flow

from bmad_loop.campaign_lifecycle import LifecycleError
from bmad_loop.campaign_model import OutboxAction, UnitState
from bmad_loop.campaign_runtime import CampaignRuntime
from bmad_loop.campaign_supervisor import Observation


def runtime(flow):
    snapshot, unit, claim, _, _, _ = flow
    driver = CampaignRuntime.__new__(CampaignRuntime)
    driver.clock = lambda: 1
    driver.project = Path(claim.workspace)
    driver._role = lambda *args, **kwargs: None
    snapshot.bindings.update(
        inventory={"complete": True}, capacity={"observed_at": 1}, intake={"observed_at": 1}
    )
    unit.state, claim.quiescent = UnitState.DELIVERED, True
    return driver, snapshot, unit


def test_runtime_queues_retention_before_campaign_settles(flow):
    driver, snapshot, unit = runtime(flow)
    proposals = driver.proposals(snapshot)
    assert [(row.kind, row.unit) for row in proposals if row.kind == "lifecycle"] == [
        ("lifecycle", unit.id)
    ]
    action = OutboxAction("retire", "lifecycle", unit.id, {}, snapshot.generation)
    driver.apply(
        snapshot, action, Observation("confirmed", {"state": "retained", "detail": "owner held"})
    )
    assert unit.state == UnitState.DELIVERED
    assert unit.bindings["lifecycle"]["detail"] == "owner held"
    assert not any(row.kind == "lifecycle" for row in driver.proposals(snapshot))


def test_runtime_retains_unknown_or_protected_native_ownership(flow):
    driver, snapshot, unit = runtime(flow)

    def refuse(*args, **kwargs):
        raise LifecycleError("Controller protects exact workspace")

    driver._lifecycle_bridge = lambda *args: SimpleNamespace(retire=refuse)
    action = OutboxAction("retire", "lifecycle", unit.id, {}, snapshot.generation)
    result = driver._perform_lifecycle(snapshot, action)
    assert result.state == "confirmed" and result.data["state"] == "retained"
    assert "Controller protects" in result.data["detail"]
    assert result.data["worktree_retained"] and result.data["git_data_retained"]


def test_runtime_promotes_only_exact_active_hcom_binding(flow):
    driver, snapshot, unit = runtime(flow)
    peer = {
        "campaign_id": snapshot.id,
        "unit_id": unit.id,
        "attempt": 1,
        "name": "luna",
        "process_id": "process-incarnation",
        "session_id": "native-session",
        "terminal_id": "terminal",
        "room": "/bound-room",
    }
    action = OutboxAction("reflect", "reflection", unit.id, {}, snapshot.generation)
    data = {"lifecycle": {"stage": {"windows": [{"state": "active", "hcom_binding": peer}]}}}
    driver.apply(snapshot, action, Observation("confirmed", data))
    assert unit.bindings["hcom"] == peer and unit.bindings["hcom_history"] == [peer]
    driver.apply(snapshot, action, Observation("confirmed", data))
    assert unit.bindings["hcom_history"] == [peer]
