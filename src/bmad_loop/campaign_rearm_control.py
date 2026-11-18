"""An operator may request the same bounded quota recovery the scheduler uses."""

from __future__ import annotations

from .campaign_model import CampaignError
from .campaign_rearm import capture_binding
from .campaign_store import add_action


def proposal(snapshot, unit_id: str, hold_id: str, *, now: float):
    from .campaign_handoff_flow import ACTIVE

    unit = snapshot.units.get(unit_id)
    if not unit or not unit.hold or unit.hold.id != hold_id or unit.hold.kind != "quota":
        raise CampaignError("rearm requires the exact active quota hold")
    if snapshot.spec.resolution.mode == "off":
        raise CampaignError("technical recovery is disabled in this campaign")
    if unit.retry_at is not None and now < unit.retry_at:
        raise CampaignError("the recorded retry deadline has not arrived")
    if unit.evidence.get("handoff", {}).get("phase") in ACTIVE or (
        unit.evidence.get("technical_rearm", {}).get("phase") in {"ready", "continuing"}
    ):
        raise CampaignError("a continuation already owns this pause")
    return capture_binding(snapshot, unit)


def apply(snapshot, command, *, now: float) -> None:
    unit_id = command["unit"]
    payload = command["payload"]
    binding = proposal(snapshot, unit_id, payload.get("hold_id", ""), now=now)
    if binding["id"] != payload.get("binding_id"):
        raise CampaignError("rearm request no longer matches the original pause")
    if any(
        action.unit == unit_id
        and action.state in {"prepared", "sent_unknown"}
        and action.kind in {"resolution-reprobe", "launch"}
        for action in snapshot.outbox
    ):
        return
    add_action(snapshot, "resolution-reprobe", unit_id, {"rearm_binding": binding})
