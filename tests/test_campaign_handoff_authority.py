"""Current controls come from the controller, never a historical hold rule."""

import copy
import dataclasses

import pytest
from test_campaign_handoff_flow import campaign as campaign
from test_campaign_handoff_flow import complete_audits, complete_maps, step
from test_campaign_headless import prepared as prepared

from bmad_loop import campaign_handoff_flow as flow
from bmad_loop.campaign_model import CampaignError, Hold
from bmad_loop.campaign_transcripts import digest


def test_capture_distinguishes_conditional_owner_rule_from_current_hold(campaign):
    runtime, snapshot, unit, receipt = campaign
    flow.intercept_terminal(runtime, snapshot, unit, receipt)
    step(runtime, snapshot, unit, "handoff-capture")
    value = unit.evidence["handoff"]
    controls = value["capture"]["current"]["controller_controls"]
    assert controls["observed_revision"] == snapshot.revision
    assert controls["state"]["active_hold"] is None
    assert controls["state_digest"] == digest(controls["state"])
    assert unit.hold.kind == "handoff"
    before = flow._audit_context(snapshot, unit)
    # Internal release is not a new owner decision or a continuing stop order.
    unit.hold = None
    assert flow._audit_context(snapshot, unit) == before
    # A real hold is carried in full and invalidates the captured context.
    hold = Hold("operator", "Wait for my review", id="owner-hold", actor="owner")
    unit.hold = hold
    assert flow._controller_controls(snapshot, unit)["active_hold"] == dataclasses.asdict(hold)
    assert flow._audit_context(snapshot, unit) != before


@pytest.mark.parametrize(
    "field", ["operator_prior_hold", "pending_failure_hold", "pending_failure_holds"]
)
def test_pending_control_changes_invalidate_context_without_rewriting_history(campaign, field):
    _, snapshot, unit, _ = campaign
    before = flow._audit_context(snapshot, unit)
    hold = dataclasses.asdict(Hold("owner", "Keep this hold", id="pending-owner"))
    raw = [hold] if field == "pending_failure_holds" else hold
    unit.evidence[field] = copy.deepcopy(raw)
    after = flow._audit_context(snapshot, unit)
    assert after != before
    snapshot.revision += 1  # Routine persistence does not invalidate the same controls.
    assert flow._audit_context(snapshot, unit) == after
    assert unit.evidence[field] == raw


@pytest.mark.parametrize("control", ["active", "queued"])
def test_new_control_after_packet_refuses_successor_without_changing_history(campaign, control):
    from pathlib import Path

    runtime, snapshot, unit, receipt = campaign
    flow.intercept_terminal(runtime, snapshot, unit, receipt)
    step(runtime, snapshot, unit, "handoff-capture")
    step(runtime, snapshot, unit, "handoff-plan")
    complete_maps(runtime, snapshot, unit)
    step(runtime, snapshot, unit, "handoff-audit-plan")
    complete_audits(runtime, snapshot, unit)
    step(runtime, snapshot, unit, "handoff-assemble")
    step(runtime, snapshot, unit, "handoff-release")
    value = unit.evidence["handoff"]
    packet = Path(value["packet"]["path"])
    original_bytes = packet.read_bytes()
    proposed = flow.resume_proposal(snapshot, unit, runtime)
    assert proposed is not None
    request = {**proposed.payload, "role": "execution"}
    flow.decorate_launch(snapshot, unit, request)
    hold = Hold("operator", "Wait for new owner review", id="after-packet", actor="owner")
    if control == "active":
        unit.hold = hold
    else:
        unit.evidence["pending_failure_holds"] = [dataclasses.asdict(hold)]
    assert flow.resume_proposal(snapshot, unit, runtime) is None
    with pytest.raises(CampaignError, match="constraints changed before launch"):
        flow.decorate_launch(snapshot, unit, request)
    assert packet.read_bytes() == original_bytes
    assert value["capture"]["current"]["controller_controls"]["state"]["active_hold"] is None


def test_control_projection_retains_observed_values_when_live_queue_changes(campaign):
    _, snapshot, unit, _ = campaign
    hold = dataclasses.asdict(Hold("operator", "Original hold", id="owned-hold"))
    unit.evidence.update(
        operator_prior_hold=copy.deepcopy(hold),
        pending_failure_hold=copy.deepcopy(hold),
        pending_failure_holds=[copy.deepcopy(hold)],
    )
    captured = flow._controller_controls(snapshot, unit)
    original = copy.deepcopy(captured)
    unit.evidence["operator_prior_hold"]["reason"] = "Changed reason"
    unit.evidence["pending_failure_hold"].clear()
    unit.evidence["pending_failure_holds"].append(dataclasses.asdict(Hold("owner", "New hold")))
    assert captured == original
    assert flow._controller_controls(snapshot, unit) != captured
