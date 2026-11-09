"""Native Grok/Muse allowance and model discovery without a model turn.

Grok's ACP initialization advertises its exact accessible model catalog; its
read-only billing extension supplies the current included allowance. Muse's MSP
model/list supplies a provider catalog and usage supplies dated subscription
allowances. Neither path creates/resumes a session, authenticates interactively,
changes account settings, or sends inference. Account quota is shared, never a
new allowance per model/effort. Effort qualification stays in route readiness:
runtime capacity is deliberately keyed by profile/model, not effort.
"""

from __future__ import annotations

import math
import os
import shutil
import time
from datetime import datetime
from pathlib import Path

from .adapters.profile import ProfileError, get_profile
from .campaign_capacity_transport import ProbeError, StatusProcess
from .campaign_model import CommunicationSpec, RoleCandidate
from .campaign_scheduler import CapacityObservation
from .process_host import ProcessHostError

TIMEOUT_S = 12.0
MAX_AGE_S = 120.0


def _object(value: object) -> dict:
    if not isinstance(value, dict):
        raise ProbeError("native capacity payload schema mismatch")
    return value


def _number(value: object) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ProbeError("native capacity number is invalid")
    try:
        result = float(value)
    except OverflowError as exc:
        raise ProbeError("native capacity number is invalid") from exc
    if not math.isfinite(result):
        raise ProbeError("native capacity number is invalid")
    return result


def _percentage(value: object) -> float:
    result = _number(value)
    if not 0 <= result <= 100:
        raise ProbeError("native capacity usage percentage is invalid")
    return result


def _timestamp(value: object) -> float:
    if not isinstance(value, str):
        raise ProbeError("native capacity period is invalid")
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if stamp.utcoffset() is None:
            raise ValueError("undated timezone")
        return stamp.timestamp()
    except (ValueError, OverflowError) as exc:
        raise ProbeError("native capacity period is invalid") from exc


def _fresh(observed: float, now: float, expires: float) -> None:
    if not 0 <= now - observed <= MAX_AGE_S or not now < expires:
        raise ProbeError("native capacity observation is stale or future dated")


def _catalog(models: object, model: str, provider: str | None = None) -> bool:
    if not isinstance(models, list) or not all(isinstance(row, dict) for row in models):
        raise ProbeError("native capacity model catalog is invalid")
    return any(
        row.get("modelId") == model and (provider is None or row.get("providerId") == provider)
        for row in models
    )


def from_grok(
    raw: dict, candidate: RoleCandidate, *, observed_at: float, now: float
) -> CapacityObservation:
    """Normalize one contemporaneous initialize/billing pair, never spend totals."""
    initialized = _object(raw.get("initialize"))
    state = _object(_object(initialized.get("_meta")).get("modelState"))
    access = _catalog(state.get("availableModels"), candidate.model)
    billing = _object(raw.get("billing"))
    tier = billing.get("subscription_tier")
    if not isinstance(tier, str) or not tier.strip():
        raise ProbeError("native Grok subscription allowance is unidentified")
    config = _object(billing.get("config"))
    period = _object(config.get("currentPeriod"))
    period_type = period.get("type")
    if not isinstance(period_type, str) or period_type not in {
        "USAGE_PERIOD_TYPE_DAILY",
        "USAGE_PERIOD_TYPE_WEEKLY",
        "USAGE_PERIOD_TYPE_MONTHLY",
    }:
        raise ProbeError("native Grok billing period type is unsupported")
    start, end = _timestamp(period.get("start")), _timestamp(period.get("end"))
    if not start <= now < end:
        raise ProbeError("native Grok billing period is not current")
    observed_at = _number(observed_at)
    now = _number(now)
    _fresh(observed_at, now, end)
    used = _percentage(config.get("creditUsagePercent"))
    ready = access and used < 100
    return CapacityObservation(
        ready,
        model_access=access,
        concurrency_known=False,
        observed_at=observed_at,
        expires_at=min(observed_at + MAX_AGE_S, end),
        reason=(
            "native Grok allowance available; campaign concurrency limits apply"
            if ready
            else "native Grok model unavailable or included allowance exhausted"
        ),
    )


def from_muse(raw: dict, candidate: RoleCandidate, *, now: float) -> CapacityObservation:
    """A provider catalog plus fresh authenticated window and weekly allowance."""
    catalog = _object(raw.get("models"))
    if catalog.get("providerId") != "meta" or catalog.get("source") != "providerCatalog":
        raise ProbeError("native Muse catalog is not a provider observation")
    access = _catalog(catalog.get("models"), candidate.model, "meta")
    usage = _object(raw.get("usage"))
    if type(usage.get("schemaVersion")) is not int or usage["schemaVersion"] != 1:
        raise ProbeError("native Muse allowance schema is unsupported")
    allowance = _object(usage.get("allowance"))
    if allowance.get("source") != "mint" or usage.get("reason") is not None:
        raise ProbeError("native Muse allowance is unavailable")
    tier = allowance.get("tier")
    if not isinstance(tier, str) or not tier.strip():
        raise ProbeError("native Muse subscription allowance is unidentified")
    observed = _number(allowance.get("asOfMs")) / 1000
    now = _number(now)
    expires = observed + MAX_AGE_S
    remaining = True
    for key in ("window", "weekly"):
        window = _object(allowance.get(key))
        remaining &= _percentage(window.get("usedPercent")) < 100
        reset = _number(window.get("resetsAtMs")) / 1000
        _fresh(observed, now, reset)
        expires = min(expires, reset)
    ready = access and remaining
    return CapacityObservation(
        ready,
        model_access=access,
        concurrency_known=False,
        observed_at=observed,
        expires_at=expires,
        reason=(
            "native Muse allowance available; campaign concurrency limits apply"
            if ready
            else "native Muse model unavailable or included allowance exhausted"
        ),
    )


def _read_grok(binary: str, project: Path, env: dict[str, str], deadline: float) -> dict:
    with StatusProcess([binary, "agent", "--no-leader", "stdio"], project, env, deadline) as child:
        initialized = child.request(
            1,
            "initialize",
            {
                "protocolVersion": 1,
                "clientCapabilities": {
                    "fs": {"readTextFile": False, "writeTextFile": False},
                    "terminal": False,
                },
                "clientInfo": {"name": "bmad-capacity", "version": "1"},
            },
        )
        billing = child.request(2, "_x.ai/billing", {})
    return {"initialize": initialized, "billing": billing}


def _read_muse(binary: str, project: Path, env: dict[str, str], deadline: float) -> dict:
    env = {**env, "MUSE_NO_AUTO_UPDATE": "1"}
    with StatusProcess([binary, "usage", "--json"], project, env, deadline) as child:
        usage = child.json_output()
    with StatusProcess(
        [
            binary,
            "serve",
            "--provider",
            "meta",
            "--no-session-log",
            "--disable-write",
            "--disable-shell",
            "--disable-request-user-input",
        ],
        project,
        env,
        deadline,
    ) as child:
        child.request(
            1,
            "initialize",
            {
                "clientInfo": {"name": "bmad_capacity", "version": "1"},
                "capabilities": {
                    "requestedCapabilities": [],
                    "experimentalApi": False,
                    "userInputDialogs": False,
                },
            },
        )
        child.notify("initialized")
        models = child.request(2, "model/list", {})
    return {"models": models, "usage": usage}


def observe(
    candidate: RoleCandidate, project: Path, *, now: float | None = None
) -> CapacityObservation | None:
    """Use the configured profile launcher; unsupported overrides stay visible."""
    if candidate.profile not in {"grok", "muse"}:
        return None
    try:
        profile = get_profile(candidate.profile, project)
        if profile.binary != candidate.profile:
            raise ProbeError("native capacity launcher overrides require a custom capacity command")
        if profile.launch_args:
            raise ProbeError("native capacity launch overrides require a custom capacity command")
        if profile.env:
            raise ProbeError(
                "native capacity environment overrides require a custom capacity command"
            )
        from .campaign_reflection_flow import sanitized_actor_env

        # Match the headless worker's inherited account environment, without
        # borrowing its parent actor identity or enrolling a probe session.
        # Headless jobs do not apply profile.env; neither may this observer.
        env = sanitized_actor_env(
            {
                key: value
                for key, value in os.environ.items()
                if not key.startswith(("GH_", "GITHUB_"))
            },
            communication=CommunicationSpec(),
        )
        binary = shutil.which(profile.binary, path=env.get("PATH"))
        if not binary:
            raise ProbeError("native capacity configured launcher is unavailable")
        observed = time.time() if now is None else now
        deadline = time.monotonic() + TIMEOUT_S
        if candidate.profile == "grok":
            raw = _read_grok(binary, project, env, deadline)
            return from_grok(
                raw, candidate, observed_at=observed, now=time.time() if now is None else now
            )
        raw = _read_muse(binary, project, env, deadline)
        return from_muse(raw, candidate, now=time.time() if now is None else now)
    except ProbeError as exc:
        reason = str(exc)
        if getattr(exc, "_native_capacity_cleanup_unconfirmed", False):
            reason += "; native capacity cleanup is unconfirmed"
        return CapacityObservation(False, known=False, reason=reason)
    except (OSError, ValueError, ProfileError, ProcessHostError) as exc:
        reason = "native capacity observation failed: " + type(exc).__name__
        if getattr(exc, "_native_capacity_cleanup_unconfirmed", False):
            reason += "; native capacity cleanup is unconfirmed"
        return CapacityObservation(False, known=False, reason=reason)
