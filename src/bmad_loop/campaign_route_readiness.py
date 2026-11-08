"""Read-only readiness of the command a campaign will actually launch.

Provider quota and local launchability are independent facts. Frozen profiles
choose the transport; permission-specific native proofs may promote a legacy
profile, but a read-only proof never authorizes a writer.
"""

from __future__ import annotations

import shutil
from typing import Any

from .campaign_headless import capability_for, validate_capability
from .campaign_model import CampaignError


def permissions_for(role: str, *, stage: str = "", read_only: bool | None = None) -> str:
    if (stage or role) == "native_review_pair":
        if read_only is False:
            raise CampaignError("Native review pair must be read-only")
        return "native-review-pair"
    if read_only is not False and (stage or role) in {
        "publication_review",
        "landing_review",
        "planning_review",
    }:
        return "review"
    if read_only is not None:
        return "reasoning" if read_only else "workspace-tools"
    if stage == "closure":
        return "workspace-tools"
    return (
        "reasoning"
        if (stage or role)
        in {
            "publication_review",
            "landing_review",
            "planning_review",
            "escalation",
            "decision_review",
            "admission_review",
            "triage",
            "summarization",
            "handoff_audit",
        }
        else "workspace-tools"
    )


def selected_capability(profile, candidate, routes, permissions):
    """Mirror worker transport selection, retaining unmatched legacy adapters."""
    if permissions == "native-review-pair":
        from .campaign_codex_review_pair import selected

        cap = capability_for(
            routes, candidate.profile, candidate.model, "review", effort=candidate.effort
        )
        validate_capability(cap, candidate, permissions="read-only-tools")
        if not selected(cap):
            raise CampaignError("Native review pair requires an explicitly qualified pair route")
        return cap
    value = routes.get(candidate.profile + ":" + candidate.model, {})
    if isinstance(value, dict) and "by_effort" in value:
        value = value["by_effort"].get(candidate.effort, {})
    mode = (
        "review"
        if permissions == "review" and isinstance(value, dict) and "review" in value
        else (
            "reasoning"
            if permissions in {"reasoning", "review", "no-tools", "read-only-tools"}
            else "execution"
        )
    )
    compatible = bool(value) and isinstance(value, dict)
    if compatible and set(value) & {"reasoning", "review", "execution"}:
        compatible = mode in value
    if compatible and "permissions" in value:
        compatible = value["permissions"] in (
            {"no-tools", "read-only-tools"}
            if permissions in {"reasoning", "review"}
            else {permissions}
        )
    if profile.adapter != "bounded-headless" and not compatible:
        return None
    cap = capability_for(
        routes, candidate.profile, candidate.model, permissions, effort=candidate.effort
    )
    validate_capability(cap, candidate, permissions=cap["permissions"])
    return cap


def inspect(profile, candidate, routes, permissions):
    """No subprocess, profile reload or model prompt is needed for this check."""
    from .campaign_admission_files import admission_reads

    result: dict[str, Any] = {
        "ready": False,
        "binary": profile.binary,
        "installed": False,
        "transport": profile.adapter,
        "permissions": permissions,
        "reason": "",
    }
    try:
        with admission_reads():
            cap = selected_capability(profile, candidate, routes, permissions)
        if cap is not None:
            result.update(
                binary=cap["binary"], transport="bounded-headless", permissions=cap["permissions"]
            )
        result["installed"] = bool(shutil.which(str(result["binary"])))
        if not result["installed"]:
            raise CampaignError("configured launcher is unavailable or not executable")
        result["ready"] = True
    except (CampaignError, OSError, ValueError) as exc:
        result["reason"] = str(exc)
    return result
