"""Shared frozen child configuration and continuation request authority.

The coordinator and store derive identical bindings before a successor can run.
This module constructs data only; loading frozen archives remains explicit.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
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
        "source_revision": unit.bindings.get("source_revision", ""),
        "continuation_context": continuation_context(snapshot, unit),
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


def continuation_context(snapshot: CampaignSnapshot, unit: CampaignUnit) -> dict[str, Any]:
    """Bind relevant live authority without duplicating captured histories."""

    def digest(value: Any) -> str:
        return hashlib.sha256(
            json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
        ).hexdigest()

    handoff = unit.evidence.get("handoff", {})
    acceptance = unit.evidence.get("acceptance", {})
    pr = unit.bindings.get("pr", {})
    decisions = {
        key: value
        for key, value in snapshot.bindings.get("decision_receipts", {}).items()
        if value.get("coordinates", {}).get("unit") == unit.id
    }
    return {
        "schema_version": 1,
        "unit_spec": digest(dataclasses.asdict(unit.spec)),
        "repository": digest(
            {
                "repository": snapshot.repository,
                "common_dir": snapshot.common_dir,
                "root": snapshot.root,
                "remote_url": snapshot.remote_url,
            }
        ),
        "ownership": digest(
            {
                **{
                    key: unit.bindings.get(key)
                    for key in ("workspace", "branch", "head", "base", "canonical_source")
                },
                "pr": (
                    {key: pr.get(key) for key in ("number", "id", "node_id")}
                    if isinstance(pr, dict)
                    else pr
                ),
            }
        ),
        "source": digest(
            unit.bindings.get("effective_source", source_to_dict(source_selector(snapshot, unit)))
        ),
        "source_revision": unit.bindings.get("source_revision", ""),
        "source_paths": digest(
            {key: unit.bindings.get(key) for key in ("effective_sources", "private_paths")}
        ),
        "execution_template": dict(snapshot.bindings.get("execution_template", {})),
        "handoff": digest(
            {
                key: handoff.get(key)
                for key in (
                    "id",
                    "role",
                    "native_role",
                    "route_role",
                    "candidate",
                    "predecessor",
                    "capture",
                    "packet",
                    "original_payload",
                )
            }
        ),
        "obligations": digest(
            {
                "acceptance": {key: acceptance.get(key) for key in ("revision", "criteria")},
                "decision_input_revision": unit.evidence.get("decision_input_revision", ""),
                "decision_receipts": decisions,
                "decision_requests": {
                    key: value
                    for key, value in snapshot.bindings.get("decision_requests", {}).items()
                    if value.get("receipt") in decisions
                },
                **{
                    key: unit.evidence.get(key)
                    for key in (
                        "decisions",
                        "unresolved_approval_requirements",
                        "pending_failure_hold",
                        "pending_failure_holds",
                        "operator_prior_hold",
                    )
                },
            }
        ),
        "contributors": digest(
            {
                "primary": unit.evidence.get("developer_family"),
                "families": sorted(set(unit.evidence.get("developer_families", ()))),
                "incomplete": unit.evidence.get("developer_attribution_incomplete"),
            }
        ),
        "legacy_counters_unknown": bool(
            unit.evidence.get("usage", {}).get("legacy_counters_unknown")
        ),
    }
