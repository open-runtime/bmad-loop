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
from typing import Any, TypeGuard

from .campaign_model import RoleCandidate
from .campaign_scheduler import CapacityObservation
from .childrun import run_argv


def record_unavailable(snapshot, profile: str, model: str, *, now: float, reason: str) -> None:
    """Invalidate an older positive observation after an owned native quota failure."""
    if not profile or not model:
        return
    snapshot.bindings.setdefault("capacity_faults", {})[profile + "\0" + model] = {
        "observed_at": now,
        "reason": reason,
    }
    snapshot.bindings.setdefault("capacity", {})["observed_at"] = 0


def _finite_number(value: Any) -> TypeGuard[int | float]:
    if type(value) not in (int, float):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _fresh_ms(value: Any, now: float, max_age_s: float) -> TypeGuard[int | float]:
    return _finite_number(value) and 0 <= now - value / 1000 <= max_age_s


def _spent_shared_quota(account: dict, blockers: list, now: float) -> bool:
    quota = account.get("quota", {})
    if not isinstance(quota, dict):
        return False
    return bool(blockers) and all(
        item.get("code") == "quota"
        and isinstance(item.get("bucket"), str)
        and item.get("bucket") in {"unified5h", "unified7d"}
        and _finite_number(quota.get(item["bucket"]))
        and quota[item["bucket"]] >= 1
        and _finite_number(item.get("until"))
        and item["until"] / 1000 > now
        for item in blockers
    )


def _credit_expiry(
    raw: dict, entry: dict, model: str, now: float, max_age_s: float
) -> float | None:
    """Recognize an already authorized, observed Codex credit route conservatively.

    Router availability describes ordinary quota, not its paid fallback. Join the
    exact model verdict to the same opted-in account; provider-wide billing alone
    is insufficient. Fresh paid-credit evidence is independent of subscription
    quotaKnown, which requires a named Codex subscription bucket. The provider
    gate omits the model, so refuse peer free bands/temporary
    holds instead of reconstructing the router's model-specific waiting clocks.
    """
    if entry.get("provider") != "codex" or entry.get("model") != model:
        return None
    extra = raw.get("extraUsage", {})
    paid = extra.get("codex", {}) if isinstance(extra, dict) else {}
    if not isinstance(paid, dict):
        return None
    gate = paid.get("gate", {})
    if (
        not isinstance(gate, dict)
        or gate.get("open") is not True
        or gate.get("shutBy") is not None
        or gate.get("held") != 0
        or paid.get("waiting") is not None
    ):
        return None
    accounts = raw.get("accounts")
    rows = entry["accounts"]
    if not isinstance(accounts, list) or any(not isinstance(a, dict) for a in accounts + rows):
        return None
    ids = [a.get("id") for a in accounts]
    row_ids = [a.get("id") for a in rows]
    if (
        any(not isinstance(key, str) or not key for key in ids + row_ids)
        or len(set(ids)) != len(ids)
        or len(set(row_ids)) != len(row_ids)
        or set(ids) != set(row_ids)
    ):
        return None
    by_id = {a["id"]: a for a in accounts}
    candidates = []
    for row in rows:
        account = by_id[row["id"]]
        if account.get("provider") != "codex":
            continue
        blockers = row.get("blockers")
        if (
            not isinstance(blockers, list)
            or not blockers
            or any(not isinstance(b, dict) for b in blockers)
        ):
            return None
        if any(not isinstance(b.get("code"), str) for b in blockers):
            return None
        codes = {b["code"] for b in blockers}
        source = account.get("canonicalSource") or {}
        error = account.get("errorReason") or {}
        if not isinstance(source, dict) or not isinstance(error, dict):
            return None
        if codes != {"quota"}:
            hard = {
                "auth",
                "disabled",
                "capped",
                "spend-capped",
                "route",
                "model-blocked",
                "entitlement",
                "credential",
            }
            if (
                not codes <= hard | {"quota"}
                or not codes & hard
                or source.get("errno")
                or error.get("code") == "source_refresh_failed"
            ):
                return None
            continue
        if not _spent_shared_quota(account, blockers, now):
            return None
        if (
            account.get("type") != "oauth"
            or account.get("disabled") is not False
            or account.get("status") != "active"
            or source.get("serving") is not True
            or source.get("errno")
            or error.get("code")
            or account.get("allowExtraUsage") is not True
        ):
            continue
        credit = account["quota"].get("purchasedCredits")
        if not isinstance(credit, dict) or not _fresh_ms(credit.get("seenAt"), now, max_age_s):
            continue
        balance = credit.get("balance")
        if balance is not None and not _finite_number(balance):
            continue
        unlimited = credit.get("unlimited") is True
        if not unlimited and (
            credit.get("hasCredits") is False or (balance is not None and balance <= 0)
        ):
            continue
        if unlimited or credit.get("hasCredits") is True or (balance is not None and balance > 0):
            candidates.append(credit["seenAt"] / 1000 + max_age_s)
    return max(candidates) if candidates else None


def from_teamrouter(
    raw: dict[str, Any], candidate: RoleCandidate, *, now: float, max_age_s: float = 120
) -> CapacityObservation:
    availability = raw.get("availability")
    entry = availability.get(candidate.model) if isinstance(availability, dict) else None
    if not isinstance(entry, dict):
        return CapacityObservation(
            False, reason="model absent from router availability", known=False
        )
    observed = entry.get("observedAt")
    if not _fresh_ms(observed, now, max_age_s):
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
    credit_expires = (
        None if eligible else _credit_expiry(raw, entry, candidate.model, now, max_age_s)
    )
    if not eligible and credit_expires is None:
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
        expires_at=min(observed / 1000 + max_age_s, credit_expires or math.inf),
        reason=(
            "authorized Codex credits observed; "
            if credit_expires is not None
            else "route eligible; "
        )
        + "provider concurrency unadvertised, explicit campaign limits apply",
    )


def observe(
    candidate: RoleCandidate, project: Path, *, now: float | None = None
) -> CapacityObservation:
    if not candidate.capacity_command and candidate.profile == "antigravity":
        return CapacityObservation(
            False,
            known=False,
            reason="AGY capacity requires a supported explicit native capacity command",
        )
    if not candidate.capacity_command and candidate.profile in {"grok", "muse"}:
        from .campaign_native_capacity import observe as observe_native

        native = observe_native(candidate, project, now=now)
        if native is not None:
            return native
        return CapacityObservation(
            False, reason="native capacity observer unavailable", known=False
        )
    observed_at = time.time() if now is None else now
    command = candidate.capacity_command or ("teamrouter", "status", "--json")
    try:
        result = run_argv(command, cwd=project, timeout=15, encoding="utf-8")
    except OSError as exc:
        return CapacityObservation(
            False,
            reason=f"capacity probe could not start: {type(exc).__name__}",
            known=False,
        )
    if result.returncode or result.timed_out or result.interrupted:
        return CapacityObservation(False, reason="capacity probe unavailable", known=False)
    try:
        raw = json.loads(result.stdout)
    except (ValueError, RecursionError):
        return CapacityObservation(
            False, reason="capacity probe returned invalid JSON", known=False
        )
    if not isinstance(raw, dict):
        return CapacityObservation(False, reason="capacity probe schema mismatch", known=False)
    if not candidate.capacity_command:
        return from_teamrouter(raw, candidate, now=time.time() if now is None else now)
    observed_at = time.time() if now is None else now
    if (
        raw.get("schema_version") != 1
        or raw.get("model") != candidate.model
        or raw.get("quota_known") is not True
    ):
        return CapacityObservation(
            False, reason="capacity probe lacks exact-model quota evidence", known=False
        )
    concurrency_known = raw.get("concurrency_known", True)
    if type(concurrency_known) is not bool:
        return CapacityObservation(
            False, reason="capacity probe concurrency evidence invalid", known=False
        )
    slots = raw.get("slots", 0 if not concurrency_known else None)
    if not concurrency_known and slots is None:
        slots = 0
    if type(slots) is not int or slots < 0:
        return CapacityObservation(False, reason="capacity probe slot count invalid", known=False)
    reported_at = raw.get("observed_at", observed_at)
    expires_at = raw.get(
        "expires_at", reported_at + 120 if isinstance(reported_at, (int, float)) else None
    )
    if (
        any(not _finite_number(value) for value in (reported_at, expires_at))
        or not reported_at <= observed_at <= expires_at <= reported_at + 120
    ):
        return CapacityObservation(
            False, reason="capacity probe observation expired or invalid", known=False
        )
    return CapacityObservation(
        raw.get("available") is True,
        slots=slots if concurrency_known else 0,
        concurrency_known=concurrency_known,
        model_access=raw.get("model_access") is True,
        budget_remaining=raw.get("budget_remaining"),
        reason=str(raw.get("reason", "")),
        observed_at=reported_at,
        expires_at=expires_at,
    )
