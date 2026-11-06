"""Read-only model route capacity observations for fair campaign admission.

Commands are operator-configured argv, never issue text. TeamRouter's advertised
model eligibility supplies quota/access evidence without sending a model prompt.
Missing telemetry remains an explicit wait rather than guessed availability.
"""

from __future__ import annotations

import json
import math
import time
from pathlib import Path
from typing import Any

from .campaign_model import RoleCandidate
from .campaign_scheduler import CapacityObservation
from .childrun import run_argv


def from_teamrouter(
    raw: dict[str, Any], candidate: RoleCandidate, *, now: float, max_age_s: float = 120
) -> CapacityObservation:
    entry = raw.get("availability", {}).get(candidate.model)
    if not isinstance(entry, dict):
        return CapacityObservation(
            False, reason="model absent from router availability", known=False
        )
    observed = entry.get("observedAt")
    if not isinstance(observed, (int, float)) or not 0 <= now - observed / 1000 <= max_age_s:
        return CapacityObservation(
            False, reason="router availability stale or unversioned", known=False
        )
    accounts = entry.get("accounts")
    if not isinstance(accounts, list):
        return CapacityObservation(
            False, reason="router account eligibility unavailable", known=False
        )
    eligible = [
        account
        for account in accounts
        if isinstance(account, dict)
        and account.get("eligible") is True
        and account.get("quotaKnown") is True
        and not account.get("blockers")
    ]
    if not eligible:
        return CapacityObservation(
            False, reason="no eligible account with known quota", model_access=False
        )
    if candidate.model in raw.get("blockedModels", []):
        return CapacityObservation(
            False, reason="model blocked by router policy", model_access=False
        )
    return CapacityObservation(
        True,
        model_access=True,
        concurrency_known=False,
        observed_at=observed / 1000,
        expires_at=observed / 1000 + max_age_s,
        reason="route eligible; provider concurrency unadvertised, explicit campaign limits apply",
    )


def observe(
    candidate: RoleCandidate, project: Path, *, now: float | None = None
) -> CapacityObservation:
    observed_at = time.time() if now is None else now
    command = candidate.capacity_command or ("teamrouter", "status", "--json")
    result = run_argv(command, cwd=project, timeout=15, encoding="utf-8")
    if result.returncode or result.timed_out or result.interrupted:
        return CapacityObservation(False, reason="capacity probe unavailable", known=False)
    try:
        raw = json.loads(result.stdout)
    except ValueError:
        return CapacityObservation(
            False, reason="capacity probe returned invalid JSON", known=False
        )
    if not isinstance(raw, dict):
        return CapacityObservation(False, reason="capacity probe schema mismatch", known=False)
    if not candidate.capacity_command:
        return from_teamrouter(raw, candidate, now=time.time() if now is None else now)
    if (
        raw.get("schema_version") != 1
        or raw.get("model") != candidate.model
        or raw.get("quota_known") is not True
    ):
        return CapacityObservation(
            False, reason="capacity probe lacks exact-model quota evidence", known=False
        )
    slots = raw.get("slots")
    if type(slots) is not int or slots < 0:
        return CapacityObservation(False, reason="capacity probe slot count invalid", known=False)
    reported_at = raw.get("observed_at", observed_at)
    expires_at = raw.get(
        "expires_at", reported_at + 120 if isinstance(reported_at, (int, float)) else None
    )
    if (
        any(
            type(value) not in (int, float) or not math.isfinite(value)
            for value in (reported_at, expires_at)
        )
        or not reported_at <= observed_at <= expires_at <= reported_at + 120
    ):
        return CapacityObservation(
            False, reason="capacity probe observation expired or invalid", known=False
        )
    return CapacityObservation(
        raw.get("available") is True,
        slots=slots,
        model_access=raw.get("model_access") is True,
        budget_remaining=raw.get("budget_remaining"),
        reason=str(raw.get("reason", "")),
        observed_at=reported_at,
        expires_at=expires_at,
    )
