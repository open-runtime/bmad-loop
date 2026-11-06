"""Production stage contracts for evidence-first campaign recovery.

Proposal calculation is pure. Context retrieval, native stages, verification and
checkpoint recovery execute as durable effects outside the supervisor loop. The
single state writer records each accepted context, bounded attempt and result.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import re
from pathlib import Path
from typing import Any, Callable, Mapping

from . import verify
from .campaign_decisions import (
    DecisionContext,
    DecisionReceipt,
    EvidenceLocation,
    EvidenceSource,
    ObservedFact,
    apply_decision,
    collect_evidence,
    decision_packet,
    validate_decision,
)
from .campaign_model import (
    CampaignError,
    CampaignSnapshot,
    EvidenceCoordinates,
    Hold,
    RoleCandidate,
)
from .campaign_recovery import CampaignRecovery, PushObservation, Quiescence
from .campaign_resolution import (
    CauseCode,
    FailureEvidence,
    ResolutionContext,
    ResolutionProposal,
    ResolutionResult,
    apply_resolution,
    begin_resolution,
    failure_from_receipt,
    record_proposal,
    resolution_fingerprint,
    resolution_packet,
    transient_retry,
    validate_proposal,
)
from .campaign_scheduler import resource_available
from .campaign_store import add_event
from .campaign_supervisor import Proposal

_DECISION_HOLDS = frozenset(
    {
        "owner",
        "operator",
        "owner_stop",
        "product_choice",
        "planning_decision",
        "design_choice",
        "decision",
        "plan_checkpoint",
        "spec_checkpoint",
        "checkpoint",
        "github_review",
        "required_review",
        "human_only",
        "human_action",
    }
)
_TECHNICAL_HOLDS = frozenset(
    {
        "failed",
        "verification_failed",
        "review_findings",
        "integration_conflict",
        "conflicted",
        "paused",
        "recovery_required",
        "technical_failure",
        "resolution",
        "quota",
        "capacity",
        "rate_limit",
        "transport",
    }
)


def _redact(value: Any, key: str = "") -> Any:
    """Keep useful diagnostic prose while removing credential-bearing fields."""
    if key.lower().replace("-", "_") in {
        "api_key",
        "access_token",
        "refresh_token",
        "authorization",
        "password",
        "secret",
        "credential",
    }:
        return "<redacted:credential>"
    if isinstance(value, dict):
        return {str(name): _redact(item, str(name)) for name, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_redact(item) for item in value]
    if isinstance(value, str):
        text = re.sub(
            r"(?:sk-(?:ant-)?|gh[pousr]_|github_pat_|glpat-|xox[baprs]-|AKIA|ASIA|AIza)[A-Za-z0-9_-]+",
            "<redacted:credential>",
            value,
        )
        text = re.sub(r"https?://[^/\s@]+@", "https://<redacted:credential>@", text)
        return text
    return value


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def _coordinates(snapshot: CampaignSnapshot, unit_id: str) -> EvidenceCoordinates:
    unit = snapshot.units[unit_id]
    integrated = unit.evidence.get("integrated", {})
    return EvidenceCoordinates(
        snapshot.repository,
        unit_id,
        unit.bindings.get("head", ""),
        integrated.get("base", unit.bindings.get("base", "")),
        snapshot.graph_revision,
        snapshot.config_revision,
        _digest(snapshot.bindings.get("source_observations", {}).get(unit_id, {})),
    )


def typed_failure(snapshot: CampaignSnapshot, unit_id: str) -> FailureEvidence:
    """Use machine outcomes only; an arbitrary exception string stays unknown."""
    unit = snapshot.units[unit_id]
    explicit = unit.evidence.get("failure")
    if isinstance(explicit, dict):
        return failure_from_receipt(explicit)
    history = unit.evidence.get("worker_history", [])
    receipt = history[-1] if history else {}
    outcome = receipt.get("outcome")
    code = {
        "verification_failed": CauseCode.VERIFICATION,
        "integration_conflict": CauseCode.CONFLICT,
        "conflicted": CauseCode.CONFLICT,
    }.get(str(outcome), CauseCode.UNKNOWN)
    if unit.hold and unit.hold.kind == "review_findings" and unit.evidence.get("required_findings"):
        code = CauseCode.REVIEW
    return FailureEvidence(
        code,
        "campaign.worker_receipt",
        receipt.get("launch_id", unit.hold.id if unit.hold else unit.id),
        receipt.get("error", unit.hold.reason if unit.hold else "Unclassified failure"),
        _digest({"receipt": receipt, "findings": unit.evidence.get("required_findings", [])}),
    )


def _context_current(snapshot: CampaignSnapshot, unit_id: str, packet: Mapping[str, Any]) -> bool:
    hold = snapshot.units[unit_id].hold
    return (
        packet.get("coordinates") == dataclasses.asdict(_coordinates(snapshot, unit_id))
        and packet.get("hold", {}).get("id") == (hold.id if hold else None)
        and packet.get("github_input_revision", "")
        == snapshot.units[unit_id].evidence.get("github_input_revision", "")
        and packet.get("decision_input_revision", "")
        == snapshot.units[unit_id].evidence.get("decision_input_revision", "")
    )


def proposals(
    snapshot: CampaignSnapshot,
    project: Path,
    candidates: Mapping[str, RoleCandidate],
    *,
    now: float,
) -> tuple[Proposal, ...]:
    """Propose work from saved observations only; no Git/provider I/O here."""
    del project  # supplied for uniform runtime seam; retrieval is an effect
    output = []
    if snapshot.phase != "active":
        return ()
    for key, unit in snapshot.units.items():
        if not unit.hold or (unit.claim and not unit.claim.quiescent):
            continue
        if unit.hold.kind == "recovery_required" and unit.claim is not None:
            continue  # the sealed child handoff owns recovery, never a second resolver
        if unit.retry_at is not None and now < unit.retry_at:
            continue
        mode = (
            "decision"
            if unit.hold.kind in _DECISION_HOLDS
            else "diagnosis" if unit.hold.kind in _TECHNICAL_HOLDS else ""
        )
        if unit.evidence.get("resolution_owner_review_pending"):
            mode = "decision"
        if not mode or not unit.bindings.get("workspace"):
            continue
        if mode == "decision" and snapshot.spec.decisions.mode == "off":
            continue
        if mode == "diagnosis":
            if snapshot.spec.resolution.mode == "off":
                continue
            cause = typed_failure(snapshot, key)
            if transient_retry(cause, attempt=unit.failure_attempts, now=now) is not None:
                recorded = unit.evidence.get("transient_retry")
                if not recorded or recorded["revision"] != cause.revision:
                    output.append(
                        Proposal(
                            "resolution-backoff",
                            key,
                            {"cause": dataclasses.asdict(cause), "at": now},
                        )
                    )
                elif now >= recorded["retry_at"]:
                    output.append(
                        Proposal("resolution-reprobe", key, {"cause": dataclasses.asdict(cause)})
                    )
                continue
        if mode == "diagnosis" and unit.evidence.get("accepted_resolution"):
            accepted = _proposal(unit.evidence["accepted_resolution"])
            history = unit.evidence.get("resolution_history", [])
            if history and history[-1].get("state") == "proposed":
                from .campaign_resolution_delivery import next_step as delivery_step

                followup = delivery_step(snapshot, unit)
                if followup is not None:
                    output.append(followup)
                    continue
                if accepted.action == "wait":
                    continue
                role = (
                    "conflict_repair" if accepted.cause_code == CauseCode.CONFLICT else "escalation"
                )
                followup = _next_step(
                    unit_id=key, proposal=accepted, candidate=candidates.get(role)
                )
                if followup is not None:
                    output.append(followup)
                continue
        saved = unit.evidence.get("resolution_context")
        if (
            not isinstance(saved, dict)
            or saved.get("mode") != mode
            or not _context_current(snapshot, key, saved["packet"])
            or (mode == "decision" and "observed_at" in saved and now - saved["observed_at"] >= 60)
        ):
            output.append(
                Proposal(
                    "resolution-context",
                    key,
                    {"mode": mode, "hold_id": unit.hold.id, "head": unit.bindings.get("head", "")},
                )
            )
            continue
        packet = saved["packet"]
        fingerprint = _digest(packet)
        if fingerprint in unit.evidence.get("resolution_contexts_attempted", []):
            continue  # re-evaluate only after relevant new observations
        role = "decision_review" if mode == "decision" else "escalation"
        candidate = candidates.get(role)
        if candidate is None or not resource_available(snapshot, "resolution"):
            continue
        if mode == "diagnosis":
            context = resolution_context(packet)
            if (
                context.attempts >= snapshot.spec.resolution.max_attempts_per_unit
                or resolution_fingerprint(context) in context.prior_fingerprints
            ):
                continue
        instructions = (
            "Return the versioned decision artifact with hold_id, disposition, action, answer, support[{source_id,revision,quote}], contrary, constraints, invalidation_triggers, fact_ids and affected_paths."
            if mode == "decision"
            else "Return the versioned diagnosis artifact with cause_id, cause_code, action, rationale, evidence_ids, affected_paths, preserved_refs, verification, unresolved_decisions and command."
        )
        output.append(
            Proposal(
                "launch",
                key,
                {
                    "kind": "stage",
                    "role": role,
                    "stage": role,
                    "candidate": dataclasses.asdict(candidate),
                    "read_only": True,
                    "context_packet": packet,
                    "result_contract": "decision-v1" if mode == "decision" else "resolution-v1",
                    "custom_prompt": instructions + "\n\n" + json.dumps(packet, sort_keys=True),
                },
            )
        )
    return tuple(output)


def collect_context(
    project: Path,
    snapshot: CampaignSnapshot,
    unit_id: str,
    mode: str,
    *,
    providers: Mapping[str, Callable[[EvidenceLocation], EvidenceSource]] | None = None,
    mapped_sources: tuple[EvidenceSource, ...] = (),
    facts: tuple[ObservedFact, ...] = (),
    mapped_unavailable: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Bounded read-only I/O effect. Save its returned packet before dispatch."""
    unit = snapshot.units[unit_id]
    if not unit.hold or unit.claim and not unit.claim.quiescent:
        raise CampaignError("recovery context requires a held, quiescent unit")
    workspace = Path(unit.bindings["workspace"])
    coordinates = _coordinates(snapshot, unit_id)
    if verify.rev_parse_head(workspace) != coordinates.head:
        raise CampaignError("recovery candidate moved before context collection")
    if mode == "decision":
        from .campaign_decision_sources import configured_locations

        locations = configured_locations(snapshot.spec.decisions.sources, scope=(unit_id,))
        locations += tuple(
            EvidenceLocation(
                f"transcript:{identity}",
                "transcript",
                identity,
                authority="owner",
                scope=(unit_id,),
                private=True,
            )
            for identity in snapshot.spec.decisions.transcript_sources
        )
        collection = collect_evidence(project, locations, providers=providers)
        context = DecisionContext(
            coordinates,
            unit.hold,
            unit.hold.reason,
            (*collection.sources, *mapped_sources),
            facts,
            allowed_paths=unit.spec.scopes,
            unavailable=(
                *collection.unavailable,
                *(f"{source}: truncated" for source in collection.truncated),
                *mapped_unavailable,
            ),
        )
        packet = decision_packet(context)
    elif mode == "diagnosis":
        if unit.hold.kind in _DECISION_HOLDS:
            raise CampaignError("owner and approval holds require decision evidence review first")
        cause = typed_failure(snapshot, unit_id)
        cause = dataclasses.replace(cause, detail=_redact(cause.detail))
        history = unit.evidence.get("worker_history", [])[-4:]
        evidence = (
            (cause.id, _redact(cause.detail)),
            ("worker-receipts", json.dumps(_redact(history), sort_keys=True)[-64000:]),
            ("accepted-intent", json.dumps(dataclasses.asdict(unit.spec), sort_keys=True)),
            (
                "required-findings",
                json.dumps(unit.evidence.get("required_findings", []), sort_keys=True),
            ),
            (
                "remote-feedback",
                json.dumps(_redact(unit.evidence.get("remote_feedback", {})), sort_keys=True)[
                    -64000:
                ],
            ),
            (
                "github-stage-inputs",
                json.dumps(unit.evidence.get("github_boundary_inputs", {}), sort_keys=True),
            ),
        )
        prior = tuple(row["fingerprint"] for row in unit.evidence.get("resolution_history", []))
        context = ResolutionContext(
            coordinates,
            cause,
            str(unit.resume_state or unit.state),
            evidence,
            allowed_paths=unit.spec.scopes,
            preserved_refs=tuple(unit.evidence.get("preserved_refs", [])),
            expected_verification=snapshot.spec.verify_commands,
            quiescent=True,
            candidate_clean=verify.worktree_clean(workspace),
            prior_fingerprints=prior,
            attempts=unit.resolution_attempts,
        )
        packet = resolution_packet(context)
        packet["hold"] = dataclasses.asdict(unit.hold)
        packet["quiescent"] = True
        packet["candidate_clean"] = context.candidate_clean
        packet["prior_fingerprints"] = list(prior)
    else:
        raise CampaignError("unknown recovery context mode")
    packet["github_input_revision"] = unit.evidence.get("github_input_revision", "")
    packet["decision_input_revision"] = unit.evidence.get("decision_input_revision", "")
    return {"mode": mode, "packet": packet}


def decision_context(packet: Mapping[str, Any]) -> DecisionContext:
    def restore_source(raw):
        return EvidenceSource(
            **{
                **raw,
                "scope": tuple(raw.get("scope", ())),
                "supersedes": tuple(raw.get("supersedes", ())),
            }
        )

    return DecisionContext(
        EvidenceCoordinates(**packet["coordinates"]),
        Hold(**packet["hold"]),
        packet["question"],
        tuple(restore_source(raw) for raw in packet["sources"]),
        tuple(ObservedFact(**row) for row in packet["facts"]),
        tuple(packet["allowed_actions"]),
        tuple(packet["allowed_paths"]),
        tuple(packet.get("unavailable", [])),
    )


def resolution_context(packet: Mapping[str, Any]) -> ResolutionContext:
    cause = {
        **packet["cause"],
        "code": CauseCode(packet["cause"]["code"]),
        "paths": tuple(packet["cause"].get("paths", [])),
    }
    evidence = packet["evidence"]
    return ResolutionContext(
        EvidenceCoordinates(**packet["coordinates"]),
        FailureEvidence(**cause),
        packet["original_stage"],
        tuple(sorted(evidence.items())),
        tuple(packet["allowed_paths"]),
        tuple(packet["preserved_refs"]),
        tuple(tuple(command) for command in packet["expected_verification"]),
        tuple(tuple(command) for command in packet["repair_commands"]),
        bool(packet.get("quiescent")),
        bool(packet.get("candidate_clean")),
        tuple(packet.get("prior_fingerprints", [])),
        packet["attempts"],
    )


def apply_context(snapshot: CampaignSnapshot, unit_id: str, result: Mapping[str, Any]) -> None:
    if not _context_current(snapshot, unit_id, result["packet"]):
        raise CampaignError("recovery context is stale")
    snapshot.units[unit_id].evidence["resolution_context"] = dict(result)
    add_event(snapshot, "recovery-context-collected", unit=unit_id, mode=result["mode"])


def prepare(snapshot: CampaignSnapshot, unit_id: str, payload: Mapping[str, Any]) -> None:
    """Run within prepare-effect, before the launch claim becomes durable."""
    if payload.get("result_contract") not in {"decision-v1", "resolution-v1"}:
        return
    packet = payload["context_packet"]
    if not _context_current(snapshot, unit_id, packet):
        raise CampaignError("recovery stage input changed before launch")
    if payload["result_contract"] == "resolution-v1":
        begin_resolution(snapshot, unit_id, resolution_context(packet))
    unit = snapshot.units[unit_id]
    unit.evidence.setdefault("resolution_contexts_attempted", []).append(_digest(packet))
    unit.evidence["active_resolution_packet"] = dict(packet)


def validate_stage_result(
    project: Path,
    snapshot: CampaignSnapshot,
    unit_id: str,
    receipt: Mapping[str, Any],
    *,
    providers: Mapping[str, Callable[[EvidenceLocation], EvidenceSource]] | None = None,
    mapped_sources: tuple[EvidenceSource, ...] = (),
    facts: tuple[ObservedFact, ...] | None = None,
    mapped_unavailable: tuple[str, ...] = (),
) -> dict[str, Any]:
    """I/O executor validates fresh source/head bindings after read-only session."""
    unit = snapshot.units[unit_id]
    packet = unit.evidence["active_resolution_packet"]
    if not _context_current(snapshot, unit_id, packet):
        raise CampaignError("recovery stage no longer matches current hold/head")
    if (
        receipt.get("outcome") != "stage_complete"
        or receipt.get("head") != packet["coordinates"]["head"]
    ):
        raise CampaignError("diagnosis did not complete on its unchanged candidate")
    if "disposition" in receipt["artifact"]:
        context = decision_context(packet)
        observed = snapshot.clone()
        observed_claim = observed.units[unit_id].claim
        if observed_claim:
            observed_claim.quiescent = True
        if context.facts and facts is None:
            raise CampaignError("current approval/action facts must be independently refreshed")
        refreshed_facts = {fact.id: fact for fact in facts or ()}
        for fact_id in receipt["artifact"].get("fact_ids", []):
            if next(
                (fact for fact in context.facts if fact.id == fact_id), None
            ) != refreshed_facts.get(fact_id):
                raise CampaignError("approval/action facts changed after evidence review")
        fresh = collect_context(
            project,
            observed,
            unit_id,
            "decision",
            providers=providers,
            mapped_sources=mapped_sources,
            facts=facts or (),
            mapped_unavailable=mapped_unavailable,
        )
        refreshed = decision_context(fresh["packet"])
        fresh_sources = {source.id: source for source in refreshed.sources}
        if fresh_sources != {source.id: source for source in context.sources}:
            raise CampaignError("decision evidence changed or was revoked after evidence review")
        if refreshed.unavailable:
            context = dataclasses.replace(context, unavailable=refreshed.unavailable)
        accepted = validate_decision(receipt["artifact"], context, current_sources=fresh_sources)
        return {"kind": "decision", "accepted": dataclasses.asdict(accepted)}
    context = resolution_context(packet)
    accepted = validate_proposal(receipt["artifact"], context, snapshot.spec.resolution)
    return {"kind": "resolution", "accepted": dataclasses.asdict(accepted)}


def _decision(raw: Mapping[str, Any]) -> DecisionReceipt:
    values = dict(raw)
    values["coordinates"] = EvidenceCoordinates(**values["coordinates"])
    for key in (
        "citations",
        "contrary",
        "constraints",
        "invalidation_triggers",
        "fact_ids",
        "private_sources",
    ):
        values[key] = tuple(
            tuple(item) if isinstance(item, list) else item for item in values.get(key, [])
        )
    return DecisionReceipt(**values)


def _proposal(raw: Mapping[str, Any]) -> ResolutionProposal:
    values = dict(raw)
    values["coordinates"] = EvidenceCoordinates(**values["coordinates"])
    for key in (
        "evidence_ids",
        "affected_paths",
        "preserved_refs",
        "verification",
        "unresolved_decisions",
        "command",
    ):
        values[key] = tuple(
            tuple(item) if isinstance(item, list) else item for item in values.get(key, [])
        )
    return ResolutionProposal(**values)


def apply_diagnosis(
    snapshot: CampaignSnapshot,
    unit_id: str,
    receipt: Mapping[str, Any],
    *,
    candidate: RoleCandidate | None = None,
) -> Proposal | None:
    """Apply a trusted validated artifact and return only an authorized next step."""
    unit = snapshot.units[unit_id]
    if unit.claim and not unit.claim.quiescent:
        raise CampaignError("diagnosis actor is not quiescent")
    validated = receipt.get("validated_resolution")
    if not isinstance(validated, dict):
        raise CampaignError("resolution receipt lacks fresh independent artifact validation")
    if validated["kind"] == "decision":
        apply_decision(snapshot, unit_id, _decision(validated["accepted"]))
        return None
    proposal = _proposal(validated["accepted"])
    if proposal.automatic and unit.hold and unit.hold.kind not in _TECHNICAL_HOLDS:
        raise CampaignError("technical resolution cannot replace an owner or approval hold")
    record_proposal(snapshot, unit_id, proposal)
    if not proposal.automatic:
        unit.evidence["resolution_owner_review_pending"] = True
        return None
    unit.evidence["accepted_resolution"] = dataclasses.asdict(proposal)
    if unit.hold:
        original_hold = dataclasses.asdict(unit.hold)
        unit.hold = Hold(
            "resolution",
            unit.hold.reason,
            id=unit.hold.id,
            actor="campaign",
            created_at=unit.hold.created_at,
            evidence={"original_hold": original_hold, "proposal": proposal.id},
        )
    return _next_step(unit_id=unit_id, proposal=proposal, candidate=candidate)


def _next_step(
    *, unit_id: str, proposal: ResolutionProposal, candidate: RoleCandidate | None
) -> Proposal | None:
    if proposal.action == "repair":
        if candidate is None:
            return None  # retained authorized work; route capacity can resume it
        prompt = (
            "Repair only the accepted contract on this same branch/PR, preserving every checkpoint. Return artifact_paths, affected_paths, preserved_refs and verification in the structured result.\n"
            + json.dumps(dataclasses.asdict(proposal), sort_keys=True)
        )
        return Proposal(
            "launch",
            unit_id,
            {
                "kind": "stage",
                "role": (
                    "conflict_repair" if proposal.cause_code == CauseCode.CONFLICT else "escalation"
                ),
                "stage": "technical_repair",
                "candidate": dataclasses.asdict(candidate),
                "read_only": False,
                "custom_prompt": prompt,
                "result_contract": "repair-v1",
                "resolution_proposal": dataclasses.asdict(proposal),
            },
        )
    if proposal.action == "reverify":
        return Proposal(
            "launch",
            unit_id,
            {
                "kind": "verify",
                "stage": "resolution_verify",
                "commands": [list(command) for command in proposal.verification],
                "resolution_proposal": dataclasses.asdict(proposal),
            },
        )
    return Proposal(
        "resolution-accept", unit_id, {"resolution_proposal": dataclasses.asdict(proposal)}
    )


def apply_backoff(snapshot: CampaignSnapshot, unit_id: str, *, now: float) -> None:
    unit = snapshot.units[unit_id]
    cause = typed_failure(snapshot, unit_id)
    retry = transient_retry(cause, attempt=unit.failure_attempts, now=now)
    if retry is None:
        raise CampaignError("only typed transient causes use deterministic backoff")
    unit.retry_at = retry
    unit.evidence["transient_retry"] = {
        "cause": cause.id,
        "revision": cause.revision,
        "retry_at": retry,
    }
    add_event(snapshot, "resolution-backoff", unit=unit_id, cause=cause.id, retry_at=retry)


def finish_repair(snapshot: CampaignSnapshot, unit_id: str, result: ResolutionResult) -> None:
    proposal = _proposal(snapshot.units[unit_id].evidence["accepted_resolution"])
    apply_resolution(snapshot, unit_id, proposal, result)


def recover_append_only(
    recovery: CampaignRecovery,
    action_id: str,
    *,
    context: ResolutionContext,
    baseline: str,
    owned_paths: tuple[str, ...],
    quiescence: Quiescence,
    source_preimages: dict[str, bytes | None] | None = None,
    pending_pushes: tuple[PushObservation, ...] = (),
    baseline_untracked: tuple[str, ...] = (),
    restore_sources=None,
    verify_sources=None,
) -> dict[str, Any]:
    """Bounded recovery effect; unresolved source or push evidence retains fence."""
    if not context.quiescent or quiescence.head != context.coordinates.head:
        raise CampaignError("append-only recovery lacks exact quiescent context")
    recovery.begin(
        action_id,
        expected_head=context.coordinates.head,
        baseline=baseline,
        owned_paths=owned_paths,
        baseline_untracked=baseline_untracked,
        source_preimages=source_preimages,
        pending_pushes=pending_pushes,
        quiescence=quiescence,
    )
    corrected = recovery.recover(action_id, quiescence=quiescence, restore_sources=restore_sources)
    settled = recovery.finish(
        action_id,
        quiescence=dataclasses.replace(quiescence, head=corrected.head),
        verify_sources=verify_sources,
    )
    return dataclasses.asdict(settled)
