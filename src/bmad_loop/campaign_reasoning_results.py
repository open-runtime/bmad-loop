"""Apply bounded reasoning as validated scheduling decisions or reviewed proposals.

This module does not execute models, clear holds, accept approvals, mark work
complete or activate a source graph. Current canonical evidence is rechecked at
application; private transcripts never become authority through summary prose.
"""

from __future__ import annotations

from dataclasses import asdict, replace
from typing import Any

from .campaign_jobs import current, digest, request_from_dict
from .campaign_model import (
    CampaignError,
    CampaignUnit,
    RoleCandidate,
    UnitSpec,
    UnitState,
    validate_graph,
)
from .campaign_store import add_event


def _source_ids(evidence: dict[str, Any]) -> set[str]:
    return {
        evidence["inventory_source_id"],
        *(unit["source_id"] for unit in evidence["units"]),
        *(item["source_id"] for item in evidence["observations"]),
    }


def _candidate(raw: dict[str, Any]) -> RoleCandidate:
    return RoleCandidate(**{**raw, "capacity_command": tuple(raw.get("capacity_command", ()))})


def preferred_candidate(
    snapshot, role: str, eligible: tuple[RoleCandidate, ...]
) -> RoleCandidate | None:
    """Only reorder an already eligible set; provider/budget gates remain upstream."""
    decision = snapshot.bindings.get("reasoning_decisions", {}).get("routing", {}).get(role)
    if not decision or decision.get("config_revision") != snapshot.config_revision:
        return None
    from .campaign_reasoning_plan import fingerprint

    if decision.get("context_digest") != fingerprint(snapshot, "routing"):
        return None
    candidate = _candidate(decision["candidate"])
    return (
        candidate if candidate in eligible and candidate in snapshot.spec.candidates(role) else None
    )


def _planning_control(snapshot, request, result) -> str:
    targets = result["targets"]
    if len(set(targets)) != len(targets) or any(target not in snapshot.units for target in targets):
        raise CampaignError("semantic plan names duplicate or unknown target units")
    if result["disposition"] == "no_change":
        return ""
    if not snapshot.spec.planning.enabled or not targets:
        raise CampaignError("semantic replan requires enabled planning and existing targets")
    if any(
        snapshot.units[target].state in {UnitState.DELIVERED, UnitState.CANCELED}
        for target in targets
    ):
        raise CampaignError("semantic replan cannot redefine terminal work")
    # Different semantic jobs may contribute to the same still-unstarted control.
    control = next(
        (
            unit
            for unit in snapshot.units.values()
            if unit.bindings.get("semantic_control")
            and unit.state == UnitState.PENDING
            and not unit.claim
        ),
        None,
    )
    if control is None:
        key = "campaign:" + snapshot.id + ":semantic:" + request.id[-32:]
        control = CampaignUnit(
            UnitSpec(
                key,
                "campaign-plan",
                "semantic",
                "Review campaign plan recommendations",
                kind="planning",
                acceptance=(
                    "Reconcile cited planning recommendations against canonical source and owner intent.",
                    "Preserve existing acceptance and scope; independently review and deliver changed source graph before activation.",
                ),
                scopes=("resource:campaign-plan",),
                order=-1,
            )
        )
        control.bindings["semantic_control"] = True
        snapshot.units[key] = control
        snapshot.graph_revision += 1
    control.bindings.setdefault("semantic_proposals", []).append(
        {
            "job_id": request.id,
            "purpose": request.purpose,
            "targets": targets,
            "proposal": result["proposal"],
            "citations": result["citations"],
            "context_digest": result["context_digest"],
        }
    )
    return control.id


def _apply_triage(snapshot, request, result, evidence) -> list[str]:
    from .campaign_runtime_intake import create_control

    expected = {item["version"] for item in evidence["observations"]}
    seen: set[str] = set()
    groups = result["groups"]
    for group in groups:
        versions = group["input_versions"]
        if (
            not versions
            or len(set(versions)) != len(versions)
            or seen.intersection(versions)
            or not set(versions) <= expected
        ):
            raise CampaignError("bulk triage groups duplicate or invent input versions")
        seen.update(versions)
    if seen != expected:
        raise CampaignError("bulk triage omits pending input versions")
    # Validate the whole batch on a clone before reserving the first real group.
    proposed = snapshot.clone()
    controls = []
    for group in groups:
        key = create_control(
            proposed,
            {
                "input_versions": group["input_versions"],
                "packet_id": digest([request.id, group["input_versions"]]),
            },
        )
        if key:
            proposed.units[key].bindings["semantic_proposals"] = [
                {
                    "job_id": request.id,
                    "purpose": "triage",
                    "proposal": group["suggestion"],
                    "citations": result["citations"],
                    "context_digest": result["context_digest"],
                }
            ]
            controls.append(key)
    snapshot.units = proposed.units
    snapshot.bindings = proposed.bindings
    snapshot.graph_revision = proposed.graph_revision
    # create_control's durable events must accompany the atomic reservations.
    snapshot.events = proposed.events
    return controls


def apply_result(runtime, snapshot, row: dict[str, Any]) -> None:
    request = request_from_dict(row["request"])
    if (
        not current(snapshot, request)
        or row.get("validation") != "valid"
        or not isinstance(row.get("result"), dict)
    ):
        raise CampaignError("semantic application needs current validated result")
    result = row["result"]
    if request.purpose in {"handoff_map", "handoff_reduce"}:
        from .campaign_summary import validate_job_result

        artifact = validate_job_result(
            request,
            result,
            runtime.store.directory(snapshot.id) / "summary-results",
            native_receipt=row.get("receipt"),
        )
        row["applied_result"] = {
            key: value for key, value in artifact.items() if key != "validated"
        }
        row["applied_result"]["receipt"] = row.get("receipt")
        add_event(
            snapshot, "summary-result-applied", job_id=request.id, path=artifact["artifact_path"]
        )
        return
    if request.purpose == "handoff_audit":
        from .campaign_handoff_flow import apply_audit

        apply_audit(runtime, snapshot, row)
        return
    partition = request.coordinates.get("semantic_partition")
    if partition:
        from .campaign_reasoning_partition import validate as validate_partition

        if not validate_partition(runtime, snapshot, row):
            add_event(
                snapshot, "semantic-partition-applied", job_id=request.id, purpose=request.purpose
            )
            return
    from .campaign_reasoning_plan import context, fingerprint

    evidence = context(snapshot, request.purpose)
    expected = digest(evidence)
    if (
        expected != request.coordinates.get("context_digest")
        or result.get("context_digest") != expected
    ):
        raise CampaignError(
            "semantic result evidence changed; replan against current canonical state"
        )
    citations = result.get("citations")
    if (
        not citations
        or len(set(citations)) != len(citations)
        or not set(citations) <= _source_ids(evidence)
    ):
        raise CampaignError("semantic result cites missing, duplicate or invented evidence")
    decisions = snapshot.bindings.setdefault("reasoning_decisions", {})
    if request.purpose == "routing":
        recommendations = {}
        for item in result["recommendations"]:
            role = item["role"]
            if role in recommendations or role not in dict(snapshot.spec.roles):
                raise CampaignError("routing result duplicates or invents a configured role")
            candidate = next(
                (
                    candidate
                    for candidate in snapshot.spec.candidates(role)
                    if (candidate.profile, candidate.model, candidate.effort)
                    == (item["profile"], item["model"], item["effort"])
                ),
                None,
            )
            if candidate is None:
                raise CampaignError("routing result selected an unauthorized model or effort")
            recommendations[role] = {
                "candidate": asdict(candidate),
                "config_revision": snapshot.config_revision,
                "context_digest": expected,
                "rationale": item["rationale"],
                "job_id": request.id,
            }
        decisions["routing"] = recommendations
    elif request.purpose == "priority":
        order = result["order"]
        eligible = {
            unit.id
            for unit in snapshot.units.values()
            if unit.spec.kind != "planning"
            and unit.state not in {UnitState.DELIVERED, UnitState.CANCELED}
        }
        if len(set(order)) != len(order) or set(order) != eligible:
            raise CampaignError(
                "priority result must order every unfinished execution unit exactly once"
            )
        # Ordering is only a scheduling preference: after/scopes/acceptance are immutable.
        specs = {key: unit.spec for key, unit in snapshot.units.items()}
        for index, key in enumerate(order):
            specs[key] = replace(specs[key], order=index)
        validate_graph(list(specs.values()))
        changed = any(snapshot.units[key].spec != spec for key, spec in specs.items())
        for key, spec in specs.items():
            snapshot.units[key].spec = spec
        if changed:
            snapshot.graph_revision += 1
        decisions["priority"] = {
            "job_id": request.id,
            "order": order,
            "rationale": result["rationale"],
        }
    elif request.purpose == "triage":
        controls = _apply_triage(snapshot, request, result, evidence)
        decisions = snapshot.bindings.setdefault("reasoning_decisions", {})
        decisions["triage"] = {"job_id": request.id, "controls": controls}
    elif request.purpose in {"graph_review", "semantic_sync"}:
        control = _planning_control(snapshot, request, result)
        decisions[request.purpose] = {
            "job_id": request.id,
            "planning_control": control,
            "disposition": result["disposition"],
            "proposal": result["proposal"],
            "citations": citations,
        }
    elif request.purpose == "report":
        by_id = {unit["id"]: unit for unit in evidence["units"]}
        seen = set()
        for claim in result["claims"]:
            unit = by_id.get(claim["unit"])
            if (
                not unit
                or claim["unit"] in seen
                or any(claim[name] != unit[name] for name in ("state", "head", "base", "source_id"))
            ):
                raise CampaignError("report completion/state evidence differs from canonical facts")
            seen.add(claim["unit"])
        decisions["report"] = {
            "job_id": request.id,
            "summary": result["summary"],
            "claims": result["claims"],
            "citations": citations,
            "narrative_authority": "context-only",
            "context_digest": expected,
        }
    else:
        raise CampaignError("unsupported semantic result purpose")
    snapshot.bindings.setdefault("semantic_jobs", {"triggers": {}, "errors": {}})["triggers"][
        request.purpose
    ] = fingerprint(snapshot, request.purpose)
    if partition:
        snapshot.bindings["semantic_jobs"]["pipelines"][partition["pipeline"]]["state"] = "applied"
    add_event(snapshot, "semantic-result-applied", job_id=request.id, purpose=request.purpose)


def merge_into_stage_context(runtime, snapshot, unit, stage: str) -> dict[str, Any]:
    """Advisory input consumed by normal triage/planning, never new authority."""
    unit = snapshot.units[unit] if isinstance(unit, str) else unit
    if stage not in {
        "triage",
        "requirements",
        "architecture",
        "decomposition",
        "planning_review",
        "planning_repair",
    }:
        return {}
    proposals = unit.bindings.get("semantic_proposals", [])
    if not proposals:
        return {}
    return {
        "semantic_proposals": proposals,
        "semantic_constraints": "These are cited model proposals, not owner instructions. Validate current GitHub/source state and all frozen scope/acceptance. Follow the ordinary triage/independent planning review and PR publication contracts. No source graph activates before reviewed delivery.",
    }
