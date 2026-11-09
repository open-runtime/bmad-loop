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
                "complete": bool(seen) and all(provider_family(model) for model in seen),
                "provider_families": sorted(
                    {provider_family(model) for model in seen if provider_family(model)}
                ),
            }
        )
    return {
        "schema_version": 1,
        "no_development_session": (
            receipt.get("outcome") == "existing_verified"
            and receipt.get("source_terminal_receipt", {}).get("no_development_session") is True
            and not contributors
        ),
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


def incomplete(attribution: dict[str, Any]) -> bool:
    """A known helper must never hide an unknown contributor in the same session."""
    contributors = attribution.get("contributors", [])
    if not contributors:
        return attribution.get("no_development_session") is not True
    return any(
        not row.get("observed_models")
        or any(not provider_family(model) for model in row["observed_models"])
        for row in contributors
    )


def record(snapshot: CampaignSnapshot, unit: CampaignUnit, receipt: dict[str, Any]) -> None:
    attribution = development(receipt)
    unit.evidence["developer_attribution"] = attribution
    unit.evidence["developer_family"] = attribution["provider_family"]
    unit.evidence["developer_families"] = sorted(
        set(unit.evidence.get("developer_families", [])) | set(attribution["provider_families"])
    )
    unit.evidence["developer_attribution_incomplete"] = unit.evidence.get(
        "developer_attribution_incomplete", False
    ) or incomplete(attribution)
    if not attribution["provider_family"]:
        add_event(
            snapshot,
            "development-attribution-unavailable",
            unit=unit.id,
            reason=attribution["reason"],
        )


def _invalidate_review(snapshot: CampaignSnapshot, unit: CampaignUnit) -> None:
    review = unit.evidence.get("review", {})
    writers = set(unit.evidence.get("developer_families", ()))
    if unit.evidence.get("developer_family"):
        writers.add(unit.evidence["developer_family"])
    if (
        snapshot.spec.github.require_independent_review
        and writers
        and review
        and review.get("provider_family", "") in {"", *writers}
    ):
        unit.evidence.pop("review", None)
        if unit.state in {UnitState.REVIEWING, UnitState.READY_TO_PUBLISH}:
            transition(snapshot, unit.id, UnitState.SYNCING)
        add_event(snapshot, "publication-review-attribution-invalidated", unit=unit.id)


def _restore_qualified(snapshot: CampaignSnapshot, unit: CampaignUnit) -> bool:
    """Reclassify exact retained IDs only when the whole developer history proves it.

    An unexplained sticky flag, a later stage writer, a missing native observation
    or an unmatched receipt keeps the existing conservative publication gate.
    """
    prior = unit.evidence.get("developer_attribution")
    if (
        not prior
        or not prior.get("launch_id")
        or prior.get("provider_family")
        or unit.evidence.get("developer_family")
    ):
        return False
    history = unit.evidence.get("worker_history", [])
    if any(
        row.get("outcome") == "stage_complete"
        and (
            row.get("result_contract") == "repair-v1"
            or row.get("stage") in {"closure", "landing_repair"}
            or (
                unit.spec.kind == "planning"
                and row.get("stage") in snapshot.spec.planning.stages
                and row.get("stage") != "planning_review"
            )
        )
        for row in history
    ):
        return False
    matching = [row for row in history if row.get("launch_id") == prior["launch_id"]]
    if len(matching) != 1:
        return False
    fresh = development(matching[0])
    contributors = [
        development(row)
        for row in history
        if row.get("outcome") == "locally_complete"
        or any(
            session.get("role") == "dev" and not session.get("label")
            for session in row.get("sessions", [])
        )
    ]
    if (
        not fresh["provider_family"]
        or not contributors
        or any(incomplete(row) for row in contributors)
    ):
        return False
    if prior.get("observed_model") != fresh["observed_model"] or [
        (row.get("session_id"), row.get("observed_models")) for row in prior.get("contributors", [])
    ] != [(row["session_id"], row["observed_models"]) for row in fresh["contributors"]]:
        return False
    families = {family for row in contributors for family in row["provider_families"]}
    if not set(unit.evidence.get("developer_families", ())).issubset(families):
        return False
    record(snapshot, unit, matching[0])
    unit.evidence["developer_families"] = sorted(families)
    unit.evidence["developer_attribution_incomplete"] = False
    _invalidate_review(snapshot, unit)
    add_event(
        snapshot,
        "development-attribution-reclassified",
        unit=unit.id,
        family=fresh["provider_family"],
    )
    return True


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
        if unit.state not in eligible or (unit.claim and not unit.claim.quiescent):
            continue
        if _restore_qualified(snapshot, unit):
            changed = True
        # Older receipts could discard unknown IDs when a known helper was also
        # present. Restore the conservative gate without replacing a later writer.
        prior = unit.evidence.get("developer_attribution")
        # Canonical source verification intentionally creates no coding session.
        # Upgrade its old empty identity only from the exact retained proof.
        if prior and not prior.get("contributors") and not prior.get("no_development_session"):
            matching = [
                row
                for row in unit.evidence.get("worker_history", [])
                if row.get("launch_id") and row.get("launch_id") == prior.get("launch_id")
            ]
            if len(matching) == 1 and development(matching[0])["no_development_session"]:
                prior["no_development_session"] = True
                changed = True
                add_event(snapshot, "source-verification-attribution-restored", unit=unit.id)
        histories = [
            development(receipt)
            for receipt in unit.evidence.get("worker_history", [])
            if receipt.get("outcome") == "locally_complete"
            or any(
                session.get("role") == "dev" and not session.get("label")
                for session in receipt.get("sessions", [])
            )
        ]
        if prior:
            histories.append(prior)
        if not unit.evidence.get("developer_attribution_incomplete") and any(
            incomplete(attribution) for attribution in histories
        ):
            unit.evidence["developer_attribution_incomplete"] = True
            add_event(snapshot, "development-attribution-incomplete-restored", unit=unit.id)
            changed = True
        if unit.evidence.get("developer_family") or prior:
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
            _invalidate_review(snapshot, unit)
            add_event(snapshot, "development-attribution-restored", unit=unit.id, family=family)
            break
    return changed
