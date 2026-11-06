"""Keep immutable stage inputs outside the frequently committed authority JSON.

Active preparation and uncertain effects retain their complete inputs. Once an
effect has been applied, its history only needs the external receipt identity;
the launched worker owns its sealed request and completed source evidence.
"""

from __future__ import annotations

from typing import Any

from .campaign_model import CampaignSnapshot, OutboxAction


def input_reference(inputs: dict[str, Any]) -> dict[str, Any]:
    if not inputs.get("receipt_path") or not inputs.get("receipt_digest"):
        return dict(inputs)  # A historical receipt lacking an external copy stays intact.
    return {
        key: inputs[key]
        for key in (
            "receipt_path",
            "receipt_digest",
            "observed_at",
            "complete",
            "unit",
            "graph_revision",
            "config_revision",
        )
        if key in inputs
    }


def project(
    snapshot: CampaignSnapshot, action: OutboxAction, data: dict[str, Any]
) -> dict[str, Any]:
    """Called only after a confirmed effect's full inputs have been applied."""
    if action.kind in {"stage-boundary-before", "stage-boundary-after"}:
        return {**data, "inputs": input_reference(data["inputs"])}
    if action.kind == "launch" and data.get("launched") and action.payload.get("stage_boundary"):
        boundary = action.payload["stage_boundary"]
        action.payload["stage_boundary"] = {
            **boundary,
            "inputs": input_reference(boundary["inputs"]),
        }
        unit = snapshot.units[action.unit]
        if unit.claim and unit.claim.launch_id == action.id:
            unit.evidence.pop("stage_boundary_ready", None)
    return data
