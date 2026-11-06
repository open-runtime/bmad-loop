"""Role-specific development identity from durable native session observations.

Usage across a worker includes development, review and injected workflows. It is
not a single author identity. The main model is first in SessionRecord.models;
configured model names and unrelated reviewers cannot fill missing telemetry.
"""

from __future__ import annotations

from typing import Any

from .campaign_headless import provider_family
from .campaign_model import CampaignSnapshot, CampaignUnit, UnitState
from .campaign_store import add_event, transition


def development(receipt: dict[str, Any]) -> dict[str, Any]:
    selected = next(
        (
            row
            for row in reversed(receipt.get("sessions", []))
            if row.get("role") == "dev"
            and row.get("status") == "completed"
            and not row.get("label")
        ),
        None,
    )
    models = selected.get("models", []) if selected else []
    observed = (
        models[0]
        if isinstance(models, (list, tuple)) and models and isinstance(models[0], str)
        else (selected.get("native_binding") or {}).get("observed_model", "") if selected else ""
    )
    family = provider_family(observed)
    contributors = []
    for session in receipt.get("sessions", []):
        if session.get("role") != "dev" or session.get("label"):
            continue
        native = session.get("native_binding") or {}
        seen = (
            session.get("models")
            or native.get("observed_models")
            or [native.get("observed_model", "")]
        )
        contributors.append(
            {
                "session_id": session.get("session_id"),
                "observed_models": list(seen),
                "provider_families": sorted(
                    {provider_family(model) for model in seen if provider_family(model)}
                ),
            }
        )
    return {
        "schema_version": 1,
        "launch_id": receipt.get("launch_id", ""),
        "role": "dev",
        "session_id": selected.get("session_id") if selected else None,
        "observed_model": observed,
        "provider_family": family,
        "contributors": contributors,
        "provider_families": sorted(
            {family for row in contributors for family in row["provider_families"]}
        ),
        "reason": (
            ""
            if family
            else "No recognized observed primary model for completed development; inspect the native session receipt before publication"
        ),
    }


def record(snapshot: CampaignSnapshot, unit: CampaignUnit, receipt: dict[str, Any]) -> None:
    attribution = development(receipt)
    unit.evidence["developer_attribution"] = attribution
    unit.evidence["developer_family"] = attribution["provider_family"]
    unit.evidence["developer_families"] = sorted(
        set(unit.evidence.get("developer_families", [])) | set(attribution["provider_families"])
    )
    unit.evidence["developer_attribution_incomplete"] = unit.evidence.get(
        "developer_attribution_incomplete", False
    ) or any(not row["provider_families"] for row in attribution["contributors"])
    if not attribution["provider_family"]:
        add_event(
            snapshot,
            "development-attribution-unavailable",
            unit=unit.id,
            reason=attribution["reason"],
        )


def restore(snapshot: CampaignSnapshot) -> bool:
    """Repair old mixed-role empty identities before replaying publication effects.

    Do not overwrite an explicit later planning/repair/closure author's identity, or
    change already-issued merge state. Existing unknown observations remain visible.
    """
    changed = False
    eligible = {
        UnitState.LOCALLY_COMPLETE,
        UnitState.SYNCING,
        UnitState.VERIFYING,
        UnitState.REVIEWING,
        UnitState.READY_TO_PUBLISH,
    }
    for unit in snapshot.units.values():
        if (
            unit.state not in eligible
            or unit.evidence.get("developer_family")
            or unit.evidence.get("developer_attribution")
            or (unit.claim and not unit.claim.quiescent)
        ):
            continue
        for receipt in reversed(unit.evidence.get("worker_history", [])):
            if receipt.get("outcome") == "stage_complete" and (
                receipt.get("result_contract") == "repair-v1"
                or receipt.get("stage") == "closure"
                or (
                    unit.spec.kind == "planning"
                    and receipt.get("stage") in snapshot.spec.planning.stages
                    and receipt.get("stage") != "planning_review"
                )
            ):
                break
            if receipt.get("outcome") != "locally_complete":
                continue
            record(snapshot, unit, receipt)
            changed = True
            family = unit.evidence["developer_family"]
            review = unit.evidence.get("review", {})
            if (
                snapshot.spec.github.require_independent_review
                and family
                and review
                and review.get("provider_family", "") in {"", family}
            ):
                unit.evidence.pop("review", None)
                if unit.state in {UnitState.REVIEWING, UnitState.READY_TO_PUBLISH}:
                    transition(snapshot, unit.id, UnitState.SYNCING)
                add_event(snapshot, "publication-review-attribution-invalidated", unit=unit.id)
            add_event(snapshot, "development-attribution-restored", unit=unit.id, family=family)
            break
    return changed
