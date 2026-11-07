"""Native interruption evidence, without classifying model prose as failures."""

from __future__ import annotations

from typing import Any

from .adapters.base import SessionResult
from .campaign_model import CampaignError


class CampaignStageInterrupted(CampaignError):
    def __init__(self, role: str, interruption: dict[str, Any]):
        super().__init__(f"{role} session interrupted: {interruption['kind']}")
        self.interruption = interruption


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
    }
