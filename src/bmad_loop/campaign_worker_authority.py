"""Shared frozen child configuration and continuation request authority.

The coordinator and store derive identical bindings before a successor can run.
This module constructs data only; loading frozen archives remains explicit.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any

from .campaign_execution import CampaignExecution
from .campaign_model import (
    CampaignError,
    CampaignSnapshot,
    CampaignUnit,
    SourceSelector,
    WorkerClaim,
    source_to_dict,
)


class ChildAllowanceExhausted(CampaignError):
    """The frozen native attempt allowance cannot admit another child."""


def source_selector(snapshot: CampaignSnapshot, unit: CampaignUnit) -> SourceSelector:
    if unit.spec.kind in {"tracking", "landing_repair"}:
        return SourceSelector("explicit", id=unit.spec.source)
    found = next(
        (source for source in snapshot.spec.sources if source.id == unit.spec.source), None
    )
    if found is not None:
        return found
    if unit.spec.kind == "planning":
        return SourceSelector("explicit", id=unit.spec.source)
    raise CampaignError(f"unit {unit.id} has no configured BMAD source selector")


def execution_envelope(
    template: CampaignExecution,
    snapshot: CampaignSnapshot,
    unit: CampaignUnit,
    claim: WorkerClaim,
    payload: dict[str, Any],
    directory: Path,
) -> CampaignExecution:
    envelope = dataclasses.replace(
        template,
        unit_id=unit.id,
        attempt=claim.attempt,
        generation=claim.generation,
        directory=directory,
        checkpoint_interval_s=float(snapshot.spec.checkpoints.target_interval_s),
    )
    if payload["kind"] == "execution":
        from .campaign_usage import remaining

        remaining_dev, remaining_review = remaining(snapshot, unit)
        handoff = unit.evidence.get("handoff", {})
        resuming_review = (
            payload.get("handoff_id") == handoff.get("id")
            and bool(handoff.get("id"))
            and handoff.get("native_role") == "review"
        )
        if (not resuming_review and remaining_dev <= 0) or (
            resuming_review and remaining_review <= 0
        ):
            raise ChildAllowanceExhausted("campaign lifetime child attempt budget exhausted")
        envelope = dataclasses.replace(
            envelope,
            policy=dataclasses.replace(
                template.policy,
                limits=dataclasses.replace(
                    template.policy.limits,
                    max_dev_attempts=remaining_dev,
                    max_review_cycles=max(0, remaining_review),
                ),
            ),
        )
        if payload.get("candidate"):
            candidate = payload["candidate"]
            selection = dataclasses.replace(
                envelope.policy.adapter.dev,
                name=candidate["profile"],
                model=candidate.get("model", ""),
                effort=candidate.get("effort", ""),
            )
            profiles = dict(envelope.profiles)
            selected_profile = profiles.get("campaign:" + candidate["profile"], profiles.get("dev"))
            if selected_profile is None or selected_profile.name != candidate["profile"]:
                raise CampaignError("execution candidate is absent from the frozen profiles")
            profiles["dev"] = selected_profile
            envelope = dataclasses.replace(
                envelope,
                profiles=profiles,
                policy=dataclasses.replace(
                    envelope.policy,
                    adapter=dataclasses.replace(
                        envelope.policy.adapter,
                        dev=selection,
                    ),
                ),
            )
        if payload.get("review_candidate"):
            candidate = payload["review_candidate"]
            selection = dataclasses.replace(
                envelope.policy.adapter.review,
                name=candidate["profile"],
                model=candidate.get("model", ""),
                effort=candidate.get("effort", ""),
            )
            profiles = dict(envelope.profiles)
            selected_profile = profiles.get("campaign:" + candidate["profile"])
            if selected_profile is None or selected_profile.name != candidate["profile"]:
                raise CampaignError("review candidate is absent from the frozen profiles")
            profiles["review"] = selected_profile
            envelope = dataclasses.replace(
                envelope,
                profiles=profiles,
                policy=dataclasses.replace(
                    envelope.policy,
                    adapter=dataclasses.replace(
                        envelope.policy.adapter,
                        review=selection,
                    ),
                ),
            )
    return envelope


def continuation_request(
    snapshot: CampaignSnapshot, unit: CampaignUnit, claim: WorkerClaim
) -> dict[str, Any]:
    handoff = unit.evidence["handoff"]
    current = handoff["capture"]["current"]
    return {
        "handoff_id": handoff["id"],
        "native_predecessor": handoff["predecessor"],
        "workspace": unit.bindings["workspace"],
        "common_dir": snapshot.common_dir,
        "branch": unit.bindings["branch"],
        "pr_number": unit.bindings["pr"]["number"],
        "launch_id": claim.launch_id,
        "run_id": claim.run_id,
        "source": unit.bindings.get(
            "effective_source", source_to_dict(source_selector(snapshot, unit))
        ),
        "unit": dataclasses.asdict(unit.spec),
        "handoff_state": {
            "capture_digest": handoff["capture"]["capture_digest"],
            "fingerprint": current["fingerprint"],
            "native_role": "dev",
            **{
                key: current.get(key)
                for key in (
                    "original_run_dir",
                    "original_run_id",
                    "source_roots",
                    "original_state_path",
                    "original_state_sha256",
                    "predecessor_receipt_path",
                    "predecessor_receipt_sha256",
                )
            },
        },
    }
