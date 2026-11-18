"""Versioned pause facts bound to a terminal campaign child and its saved task.

Only deterministic producers classify a pause. Historical missing causes remain
unknown even when a reason string resembles a checkpoint, quota or owner answer.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from .campaign_model import CampaignError
from .model import PauseCause, PauseKind

_BINDINGS = frozenset(
    {
        "campaign_id",
        "unit_id",
        "launch_id",
        "attempt",
        "generation",
        "workspace",
        "branch",
        "common_dir",
        "head",
        "run_id",
        "run_dir",
    }
)
_FIELDS = frozenset(
    {
        "schema_version",
        "binding",
        "config_revision",
        "source",
        "source_revision",
        "task_selector",
        "task_digest",
        "cause",
        "stage",
        "reason",
        "paused",
        "stopped",
        "crashed",
        "id",
    }
)


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def build(state, request, envelope, receipt) -> dict[str, Any]:
    """Seal producer facts alongside exact source, task and terminal bindings."""
    selector = request["unit"]["selector"]
    task = state.tasks.get(selector)
    body = {
        "schema_version": 1,
        "binding": {
            key: receipt[key]
            for key in (
                "campaign_id",
                "unit_id",
                "launch_id",
                "attempt",
                "generation",
                "workspace",
                "branch",
                "common_dir",
                "head",
                "run_id",
                "run_dir",
            )
        },
        "config_revision": envelope.config_revision,
        "source": request["source"],
        "source_revision": request.get("source_revision", ""),
        "task_selector": selector,
        "task_digest": _digest(task.to_dict()) if task is not None else None,
        "cause": state.paused_cause.to_dict() if state.paused_cause is not None else None,
        "stage": state.paused_stage,
        "reason": state.paused_reason,
        "paused": state.paused,
        "stopped": state.stopped,
        "crashed": state.crashed,
    }
    body = json.loads(json.dumps(body, sort_keys=True))
    return {**body, "id": _digest(body)}


def validate(state, request, envelope, receipt) -> None:
    """A changed native task or producer fact cannot retain an earlier receipt."""
    if receipt.get("child_pause") != build(state, request, envelope, receipt):
        raise CampaignError(
            "child pause receipt differs from its exact persisted task and authority"
        )


def cause(receipt) -> PauseCause | None:
    """Project only self-consistent typed data; missing evidence stays unknown."""
    raw = receipt.get("child_pause")
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise CampaignError("child pause receipt is not an object")
    if set(raw) != _FIELDS:
        raise CampaignError("child pause receipt fields are incomplete or unsupported")
    binding = raw.get("binding")
    if not isinstance(binding, dict) or set(binding) != _BINDINGS:
        raise CampaignError("child pause receipt lacks exact terminal bindings")
    body = {key: value for key, value in raw.items() if key != "id"}
    if (
        type(raw.get("schema_version")) is not int
        or raw["schema_version"] != 1
        or raw.get("id") != _digest(body)
    ):
        raise CampaignError("child pause receipt version or identity changed")
    if any(receipt.get(key) != value for key, value in raw.get("binding", {}).items()):
        raise CampaignError("child pause receipt names another terminal child")
    value = raw.get("cause")
    return PauseCause.from_dict(value) if value is not None else None


def hold_kind(receipt) -> str:
    typed = cause(receipt)
    if typed is None:
        return "pause_unknown"
    return {
        PauseKind.SOURCE_CHECKPOINT: "checkpoint",
        PauseKind.TECHNICAL_FAILURE: "technical_failure",
        PauseKind.AUTHENTICATION_FAILURE: "authentication_required",
        PauseKind.QUOTA_WAIT: "quota",
        PauseKind.TRANSPORT_FAILURE: "transport",
        PauseKind.OWNER_DECISION: "owner",
        PauseKind.UNKNOWN: "pause_unknown",
    }[typed.kind]
