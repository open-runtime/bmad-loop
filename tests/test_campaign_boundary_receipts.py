"""Completed receipt compaction retains active and uncertain launch authority."""

from bmad_loop.campaign_boundary_receipts import project
from bmad_loop.campaign_model import (
    CampaignSnapshot,
    CampaignSpec,
    CampaignUnit,
    OutboxAction,
    UnitSpec,
    WorkerClaim,
)


def setup():
    unit = CampaignUnit(UnitSpec("s:e:u", "s", "e", "Unit"))
    inputs = {
        "receipt_path": "/external/receipt.json",
        "receipt_digest": "digest",
        "observed_at": 10,
        "complete": True,
        "github": {"body": "x" * 2000000},
    }
    boundary = {"key": "bound", "inputs": inputs, "id": "before"}
    unit.evidence["stage_boundary_ready"] = boundary
    state = CampaignSnapshot("campaign", "org/repo", CampaignSpec("test"), {unit.id: unit})
    return state, unit, boundary


def test_completed_boundary_receipt_points_to_external_inputs_without_mutating_active_context():
    state, unit, boundary = setup()
    action = OutboxAction("before", "stage-boundary-before", unit.id, {}, 1)
    receipt = project(state, action, {"inputs": boundary["inputs"], "key": boundary["key"]})
    assert receipt["inputs"]["receipt_path"] == "/external/receipt.json"
    assert "github" not in receipt["inputs"]
    assert len(unit.evidence["stage_boundary_ready"]["inputs"]["github"]["body"]) == 2000000


def test_confirmed_launch_compacts_only_after_request_handoff_and_keeps_candidate_binding():
    state, unit, boundary = setup()
    action = OutboxAction(
        "launch",
        "launch",
        unit.id,
        {
            "stage_boundary": boundary,
            "candidate": {"profile": "codex", "model": "astra"},
        },
        1,
    )
    unit.claim = WorkerClaim(unit.id, 1, 1, action.id)
    assert len(action.payload["stage_boundary"]["inputs"]["github"]["body"]) == 2000000
    project(state, action, {"launched": True})
    assert action.payload["stage_boundary"]["inputs"]["receipt_digest"] == "digest"
    assert "github" not in action.payload["stage_boundary"]["inputs"]
    assert action.payload["candidate"] == {"profile": "codex", "model": "astra"}
    assert "stage_boundary_ready" not in unit.evidence


def test_never_launched_or_historical_receipts_keep_their_inputs():
    state, unit, boundary = setup()
    action = OutboxAction("launch", "launch", unit.id, {"stage_boundary": boundary}, 1)
    project(state, action, {"not_launched": True})
    assert "github" in action.payload["stage_boundary"]["inputs"]
    assert "stage_boundary_ready" in unit.evidence
    old = OutboxAction("before", "stage-boundary-before", unit.id, {}, 1)
    assert project(state, old, {"inputs": {"github": "historical"}}) == {
        "inputs": {"github": "historical"}
    }


def test_held_boundary_is_harvested_with_its_suppression_receipt(project, tmp_path):
    import dataclasses
    from concurrent.futures import Future

    from bmad_loop.campaign_model import Hold
    from bmad_loop.campaign_runtime import CampaignRuntime
    from bmad_loop.campaign_store import CampaignStore
    from bmad_loop.campaign_supervisor import CampaignSupervisor

    state, unit, _ = setup()
    state.spec = dataclasses.replace(state.spec, units=(unit.spec,))
    unit.hold = Hold("operator", "Pause new work", id="hold")
    action = OutboxAction(
        "held-boundary",
        "stage-boundary-before",
        unit.id,
        {},
        state.generation,
        state="sent_unknown",
    )
    state.outbox.append(action)
    store = CampaignStore(tmp_path / "state")
    state = store.create(state)
    runtime = CampaignRuntime(project.project, store)
    observed = runtime.perform(state, action)
    assert observed.data == {"operator_suppressed": True}
    future = Future()
    future.set_result(observed)
    supervisor = CampaignSupervisor(store, runtime)
    supervisor._active[action.id] = future
    harvested = supervisor._harvest(state)
    saved = store.load(harvested.id)
    assert saved.outbox[0].state == "confirmed"
    assert saved.outbox[0].receipt == {"operator_suppressed": True}
    assert saved.units[unit.id].hold.kind == "operator"
    assert any(event["kind"] == "operator-held-effect-suppressed" for event in saved.events)
