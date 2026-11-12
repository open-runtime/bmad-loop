"""Native interruption evidence, without classifying model prose as failures."""

from __future__ import annotations

import math
from typing import Any, cast

from .adapters.base import SessionResult
from .campaign_model import CampaignError


class CampaignStageInterrupted(CampaignError):
    def __init__(self, role: str, interruption: dict[str, Any]):
        super().__init__(f"{role} session interrupted: {interruption['kind']}")
        self.interruption = interruption


def quota_retry_evidence(binding: dict[str, Any]) -> dict[str, Any]:
    """Carry an optional native reset without deriving time from replayed prose."""
    if "quota_retry_at" not in binding and "quota_source" not in binding:
        return {}
    from .campaign_headless_protocol import AGY_QUOTA_SOURCE

    deadline = binding.get("quota_retry_at")
    try:
        number = cast(int | float, deadline)
        valid_deadline = type(deadline) in (int, float) and math.isfinite(number) and number > 0
    except OverflowError:
        valid_deadline = False
    if (
        binding.get("outcome") != "quota"
        or binding.get("quota_source") != AGY_QUOTA_SOURCE
        or not valid_deadline
    ):
        raise CampaignError("Native quota interruption has invalid reset evidence")
    return {"quota_retry_at": deadline, "quota_source": AGY_QUOTA_SOURCE}


def session_interruption(result: SessionResult) -> dict[str, Any] | None:
    if result.status == "completed":
        return None
    binding = result.native_binding or {}
    kind = binding.get("outcome", "")
    if kind not in {"quota", "authentication", "transport", "limit", "canceled"}:
        kind = (
            "quota"
            if result.usage_limited or result.transport_fault in {"usage", "pool"}
            else (
                "transport"
                if result.transport_fault == "transport"
                else (
                    "environment"
                    if result.transport_fault == "environment"
                    else (
                        "limit"
                        if result.status in {"timeout", "over_budget"}
                        else "canceled" if result.status == "aborted" else "failed"
                    )
                )
            )
        )
    return {
        "schema_version": 1,
        "kind": kind,
        "session_id": result.session_id,
        "status": result.status,
        "reason": result.usage_limited or result.transport_evidence or "",
        "retry_at": result.usage_limit_resets or result.pool_return,
        "native_binding": binding,
        **quota_retry_evidence(binding),
    }


def execution_interruption(sessions: list[dict[str, Any]]) -> dict[str, Any] | None:
    # The latest session is the actual interrupted boundary, never an earlier
    # quota event that a later session successfully recovered from.
    if not sessions or sessions[-1].get("status") == "completed":
        return None
    last = sessions[-1]
    binding = last.get("native_binding") or {}
    kind = binding.get("outcome", "")
    if kind not in {"quota", "authentication", "transport", "limit", "canceled"}:
        kind = {
            "usage": "quota",
            "pool": "quota",
            "transport": "transport",
            "environment": "environment",
            "parked": "owner_input",
        }.get(last.get("ended", ""), "failed")
    return {
        "schema_version": 1,
        "kind": kind,
        "session_id": last.get("session_id"),
        "role": last.get("role", ""),
        "task_id": last.get("task_id", ""),
        "status": last.get("status", ""),
        "reason": last.get("ended", ""),
        "native_binding": binding,
        **quota_retry_evidence(binding),
    }
