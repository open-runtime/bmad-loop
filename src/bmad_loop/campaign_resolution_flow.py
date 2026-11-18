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
    development_choice,
    validate_decision,
)
from .campaign_model import (
    CampaignError,
    CampaignSnapshot,
    EvidenceCoordinates,
    Hold,
    RoleCandidate,
    UnitState,
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


def execution_input_digest(packet):
    """An intake wake schedules a fresh read. The resulting native sources bind execution."""
    if "implementation_authority_revision" not in packet:
        return _digest(packet)
    return _digest(
        {key: value for key, value in packet.items() if key != "decision_input_revision"}
    )


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
    pause = unit.hold.evidence.get("child_pause") if unit.hold else None
    if (
        unit.hold is not None
        and isinstance(pause, dict)
        and pause.get("binding", {}).get("launch_id") == unit.hold.id
    ):
        from .campaign_child_pause import cause
        from .model import ENV_FAULT_SITES, PauseKind

        typed = cause({**pause["binding"], "child_pause": pause})
        code = {
            PauseKind.SOURCE_CHECKPOINT: CauseCode.APPROVAL,
            PauseKind.TECHNICAL_FAILURE: CauseCode.UNKNOWN,
            PauseKind.AUTHENTICATION_FAILURE: CauseCode.CREDENTIALS,
            PauseKind.QUOTA_WAIT: CauseCode.QUOTA,
            PauseKind.TRANSPORT_FAILURE: CauseCode.TRANSPORT,
            PauseKind.OWNER_DECISION: CauseCode.OWNER,
            PauseKind.UNKNOWN: CauseCode.UNKNOWN,
        }.get(typed.kind if typed is not None else PauseKind.UNKNOWN, CauseCode.UNKNOWN)
        if typed is not None and typed.kind == PauseKind.TECHNICAL_FAILURE:
            if (typed.producer, typed.code) == ("engine.verification", "verification_failed"):
                code = CauseCode.VERIFICATION
            elif (typed.producer, typed.code) == ("engine.session", "environment") or (
                typed.producer == "engine.environment"
                and typed.code in ENV_FAULT_SITES | {"dispatch_probe"}
            ):
                code = CauseCode.ENVIRONMENT
        return failure_from_receipt(
            {
                "code": str(code),
                "producer": "campaign.child_pause",
                "id": unit.hold.id,
                "revision": pause["binding"]["head"],
                "detail": (
                    typed.code if typed is not None else "typed producer evidence unavailable"
                ),
            }
        )
    explicit = unit.evidence.get("failure")
    if isinstance(explicit, dict):
        return failure_from_receipt(explicit)
    history = unit.evidence.get("worker_history", [])
    receipt = history[-1] if history else {}
    if unit.hold and history and unit.evidence.get("resolution_failures"):
        # Retained legacy snapshots may already end with one or more rejected
        # diagnosis receipts. Those launches did not produce the held failure.
        hold = dataclasses.asdict(unit.hold)
        rejected = [
            row["receipt"]
            for row in unit.evidence["resolution_failures"]
            if row.get("hold") == hold
        ]
        rejected_launches = {row["launch_id"] for row in rejected if row.get("launch_id")}
        receipt = next(
            (
                row
                for row in reversed(history)
                if row.get("launch_id") not in rejected_launches
                and (row.get("launch_id") or row not in rejected)
            ),
            {},
        )
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
        and (
            "question" not in packet
            or packet.get("delegation_recheck_revision", "")
            == snapshot.units[unit_id].evidence.get("delegation_recheck_revision", "")
        )
    )


def current_owner_advice(snapshot, unit):
    """Only expose recommendations bound to the current hold and decision inputs."""
    binding = unit.evidence.get("decision_advice", {})
    receipt = snapshot.bindings.get("decision_receipts", {}).get(binding.get("receipt"), {})
    if (
        not (receipt.get("options") or receipt.get("clarification"))
        or not unit.hold
        or binding.get("hold") != _digest(dataclasses.asdict(unit.hold))
        or not _context_current(
            snapshot,
            unit.id,
            {**binding, "coordinates": receipt["coordinates"], "hold": {"id": receipt["hold_id"]}},
        )
    ):
        return None
    return {
        "decision_id": receipt["id"],
        "private": bool(receipt.get("private_sources")),
        "options": () if receipt.get("private_sources") else receipt["options"],
        **({"clarification": receipt["clarification"]} if receipt.get("clarification") else {}),
    }


def _routine_choice(unit):
    return (
        unit.hold
        and unit.hold.kind in {"product_choice", "decision"}
        and unit.spec.kind != "planning"
        and unit.state not in {UnitState.PENDING, UnitState.PLANNING, UnitState.DRAFT}
    )


def _planned_choice(snapshot, unit):
    return next(
        (item for item in snapshot.spec.decisions.implementation_choices if item.unit == unit.id),
        None,
    )


def _choice_binding(unit, choice):
    return _digest(
        [
            dataclasses.asdict(choice),
            unit.id,
            unit.spec.source,
            unit.spec.selector,
            unit.spec.scopes,
            unit.spec.paths,
            unit.spec.acceptance,
        ]
    )


def request_implementation_choice(snapshot, unit):
    """Issue the frozen plan's question once, after the story's draft PR is ready."""
    from .campaign_existing import requires_existing
    from .campaign_hold import defer_failure

    choice = _planned_choice(snapshot, unit)
    if (
        choice is None
        or unit.spec.kind != "story"
        or unit.evidence.get("planned_choice_id")
        or requires_existing(snapshot, unit)
    ):
        return
    binding = _choice_binding(unit, choice)
    hold = Hold(
        "product_choice",
        choice.question,
        id="implementation:" + binding,
        actor="campaign",
        evidence={"planned_choice": binding},
    )
    if unit.hold:
        defer_failure(unit, hold)
    else:
        unit.hold = hold
    unit.evidence["planned_choice_id"] = hold.id
    add_event(snapshot, "implementation-choice-requested", unit=unit.id, hold=hold.id)


def routine_review_pending(snapshot, unit):
    """Whether a routine choice is awaiting its first review of the current inputs."""
    if (
        not _routine_choice(unit)
        or snapshot.spec.decisions.mode == "off"
        or not unit.bindings.get("workspace")
    ):
        return False
    if unit.claim and not unit.claim.quiescent:
        return unit.claim.role == "decision_review"
    saved = unit.evidence.get("resolution_context", {})
    packet = saved.get("packet", {})
    return (
        saved.get("mode") != "decision"
        or not _context_current(snapshot, unit.id, packet)
        or _digest(packet) not in unit.evidence.get("resolution_contexts_attempted", ())
    )


def _refusal_subject(snapshot, unit):
    return _digest(
        {
            "coordinates": dataclasses.asdict(_coordinates(snapshot, unit.id)),
            "hold": dataclasses.asdict(unit.hold) if unit.hold else None,
            "failure": dataclasses.asdict(typed_failure(snapshot, unit.id)),
            "github_inputs": unit.evidence.get("github_input_revision", ""),
            "decision_inputs": unit.evidence.get("decision_input_revision", ""),
            "verification": snapshot.spec.verify_commands,
            "repair": unit.evidence.get("active_resolution_packet", {}).get("repair_commands", ()),
        }
    )


def refuse_persisted_resolution(snapshot, unit, payload):
    """Retire only the observed invalid proposal and keep its original condition."""
    accepted = unit.evidence.get("accepted_resolution")
    history = unit.evidence.get("resolution_history", [])
    if (
        not accepted
        or not history
        or (unit.claim and not unit.claim.quiescent)
        or history[-1].get("state") != "proposed"
        or _digest(accepted) != payload["proposal_digest"]
        or _refusal_subject(snapshot, unit) != payload["subject"]
    ):
        return
    accepted_id = accepted.get("id") if isinstance(accepted, dict) else None
    if (
        unit.hold
        and unit.hold.kind == "resolution"
        and accepted_id
        and unit.hold.evidence.get("proposal") == accepted_id
    ):
        original = unit.hold.evidence.get("original_hold")
        if isinstance(original, dict) and original.get("id") == unit.hold.id:
            unit.hold = Hold(**original)
    history[-1].update(
        state="refused",
        refusal={
            "reason": payload["reason"],
            "proposal": accepted,
            "subject": _refusal_subject(snapshot, unit),
        },
    )
    unit.evidence.pop("accepted_resolution")
    unit.evidence.pop("resolution_next_step", None)
    add_event(
        snapshot, "resolution-refused", unit=unit.id, proposal=accepted_id, reason=payload["reason"]
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
            history = unit.evidence.get("resolution_history", [])
            if (
                history
                and history[-1].get("state") == "refused"
                and history[-1].get("refusal", {}).get("subject")
                == _refusal_subject(snapshot, unit)
            ):
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
            history = unit.evidence.get("resolution_history", [])
            if history and history[-1].get("state") == "proposed":
                from .campaign_resolution_delivery import next_step as delivery_step

                raw = unit.evidence["accepted_resolution"]
                try:
                    accepted = _proposal(raw)
                    followup = delivery_step(snapshot, unit)
                except (CampaignError, KeyError, TypeError, ValueError) as exc:
                    output.append(
                        Proposal(
                            "resolution-refused",
                            key,
                            {
                                "proposal_digest": _digest(raw),
                                "subject": _refusal_subject(snapshot, unit),
                                "reason": (
                                    str(exc)
                                    if isinstance(exc, CampaignError)
                                    else "invalid stored resolution proposal"
                                ),
                            },
                        )
                    )
                    continue
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


def _directed_source(snapshot, unit_id, source):
    """Keep a directed message visible; its authority applies only to its outstanding question."""
    if (
        source.kind == "github"
        and source.identity.startswith("gh:comment:")
        and source.authority == "owner"
    ):
        header = re.match(r"Decision ([0-9a-f]{64})\n", source.content.strip())
        if header:
            from .campaign_answer import question

            try:
                question(snapshot, unit_id, header[1])
            except CampaignError:
                return dataclasses.replace(source, authority="context")
    return source


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
    prior_projections=(),
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
        from .campaign_decision_sources import configured_locations, controller_comment

        locations = configured_locations(snapshot.spec.decisions.sources, scope=(unit_id,))
        unavailable = ()
        if _routine_choice(unit):
            documents = snapshot.spec.decisions.delegation_documents
            if binding := unit.hold.evidence.get("planned_choice"):
                choice = _planned_choice(snapshot, unit)
                if (
                    choice
                    and _choice_binding(unit, choice) == binding
                    and unit.hold.id == "implementation:" + binding
                ):
                    documents = (choice.delegation_document,)
                else:
                    documents = ()
                    unavailable = ("planned implementation question or its accepted scope changed",)
            locations += tuple(
                EvidenceLocation(
                    "delegation:" + ref,
                    "delegation_document",
                    ref,
                    authority="delegation",
                    scope=(unit_id,),
                )
                for ref in documents
            )
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
            tuple(
                _directed_source(snapshot, unit_id, source)
                for source in (*collection.sources, *mapped_sources)
                if not controller_comment(snapshot, source, prior_projections=prior_projections)
            ),
            facts,
            allowed_paths=unit.spec.scopes,
            unavailable=(
                *unavailable,
                *collection.unavailable,
                *(f"{source}: truncated" for source in collection.truncated),
                *mapped_unavailable,
            ),
            require_owner_options=True,
        )
        packet = decision_packet(context)
        packet["accepted_work"] = {
            "title": unit.spec.title,
            "scopes": list(unit.spec.scopes),
            "acceptance": list(unit.spec.acceptance),
        }
        if _routine_choice(unit):
            wake = unit.evidence.get("decision_input_revision", "")
            record = snapshot.bindings.get("intake_inbox", {}).get("records", {}).get(wake, {})
            observation = record.get("observation", {})
            mapped = {
                value.get("number") if isinstance(value, dict) else value
                for name in ("issue", "epic_issue", "pr")
                if (value := unit.bindings.get(name))
            }
            mapped_wake = (
                observation.get("repository") == snapshot.repository
                and observation.get("number") in mapped
                and observation.get("kind") in {"issue", "comment"}
            )
            answers = unit.evidence.get("owner_answers", {})
            active_reply = unit.evidence.get("owner_answer_revisions", {}).get(unit.hold.id, "")
            packet["implementation_authority_revision"] = {
                "owner_answer": active_reply,
                "other": "" if mapped_wake or (wake == active_reply and wake in answers) else wake,
            }
        if revision := unit.evidence.get("delegation_recheck_revision"):
            packet["delegation_recheck_revision"] = revision
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
        bool(packet.get("require_owner_options")),
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
    evidence = snapshot.units[unit_id].evidence
    if evidence.get("decision_advice") and _digest(
        evidence.get("resolution_context", {}).get("packet")
    ) != _digest(result["packet"]):
        evidence.pop("decision_advice")
    evidence["resolution_context"] = dict(result)
    add_event(snapshot, "recovery-context-collected", unit=unit_id, mode=result["mode"])


def preserve_failed_diagnosis(snapshot, unit, request, receipt) -> bool:
    """Keep the unresolved condition authoritative when its diagnosis is invalid.

    A rejected read-only proposal is evidence about a failed resolution attempt;
    it does not replace the original approval, acceptance or technical condition.
    The attempted-context and lifetime counters remain unchanged.
    """
    if request.get("result_contract") not in {"decision-v1", "resolution-v1"} or not unit.hold:
        return False
    unit.evidence.setdefault("resolution_failures", []).append(
        {"hold": dataclasses.asdict(unit.hold), "receipt": dict(receipt)}
    )
    add_event(
        snapshot,
        "recovery-proposal-rejected",
        unit=unit.id,
        hold=unit.hold.id,
        launch=receipt.get("launch_id", ""),
        reason=receipt.get("error", "Invalid diagnosis result"),
    )
    return True


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


def _ordinal_reply(answer, earlier):
    """Only an explicit number in the original ranked list can select an option."""
    direct = re.fullmatch(
        r"(?:(?:I\s+)?(?:choose|select|use)\s+)?(?:option|choice)\s+#?([1-9][0-9]{0,2})[.!]?",
        answer,
        re.IGNORECASE,
    )
    options = earlier.get("options", ())
    if (
        direct
        and isinstance(options, (list, tuple))
        and 1 <= int(direct[1]) <= len(options) <= 4
        and isinstance(earlier.get("question"), str)
        and earlier["question"].strip()
        and all(
            isinstance(row, (list, tuple))
            and len(row) == 3
            and isinstance(row[0], str)
            and row[0].strip()
            for row in options
        )
    ):
        return {
            "decision_id": earlier["id"],
            "question": earlier["question"],
            "reviewer_proposed_options": [row[0] for row in options],
            "owner_selected_number": int(direct[1]),
        }, ""
    if direct or re.search(
        r"\b(?:options?|choices?|alternatives?)\s*#?\s*(?:\d|one\b|two\b|three\b|four\b|[A-D]\b)"
        r"|\b(?:first|second|third|fourth|last|other)\s+(?:one|choice|option|alternative)\b"
        r"|^(?:#?\d+|first|second|third|fourth|the latter|the former)[.!]?$",
        answer,
        re.IGNORECASE,
    ):
        return (
            None,
            "Reply with exactly one listed option number (for example, Option 2), or write the complete implementation choice.",
        )
    return None, ""


def _clarify_reply(decision, earlier, revision, context, message):
    return dataclasses.replace(
        decision,
        id=_digest([earlier["id"], revision, "clarification"]),
        disposition="needs_new_owner_decision",
        action="",
        answer=message,
        actual_actor="",
        clear_hold=False,
        constraints=(),
        options=tuple(earlier.get("options") or ()),
        question=earlier.get("question") or context.question,
        clarification=message,
        private_sources=tuple(
            dict.fromkeys(
                [*earlier.get("private_sources", ()), *(s.id for s in context.sources if s.private)]
            )
        ),
    )


def _prior_implementation_choice(snapshot, unit, decision, context):
    """Attribute existing authority; a reply to this question supplies its exact answer."""
    ids = {ident for ident, _, _ in decision.citations}
    sources = [
        source
        for source in context.sources
        if source.id in ids
        and source.origin_verified
        and source.authority in {"owner", "adopted_policy"}
    ]
    source = next((item for item in sources if item.authority == "owner"), sources[0])
    answer = decision.answer
    revision = unit.evidence.get("owner_answer_revisions", {}).get(
        unit.hold.id, unit.evidence.get("decision_input_revision", "")
    )
    reply = unit.evidence.get("owner_answers", {}).get(revision)
    directed = bool(reply and reply.get("hold_id") == unit.hold.id)
    if not directed:
        replies = []
        for item in sources:
            header = re.match(r"Decision ([0-9a-f]{64})\n", item.content.strip())
            if not (
                header
                and item.authority == "owner"
                and item.kind == "github"
                and item.identity.startswith("gh:comment:")
            ):
                continue
            replies.append((item, header[1]))
        if len(replies) > 1:
            return _clarify_reply(
                decision,
                snapshot.bindings["decision_receipts"][replies[0][1]],
                sorted(item.revision for item, _ in replies),
                context,
                "Multiple directed replies address this decision. Confirm the current implementation choice in a single reply to this question.",
            )
        if replies:
            item, decision_id = replies[0]
            reply = {"source": item.identity, "actor": item.author_id, "decision_id": decision_id}
            revision = item.revision
            directed = True
    selection = None
    if directed:
        question = snapshot.bindings.get("decision_receipts", {}).get(reply["decision_id"], {})
        matched = next(
            (
                item
                for item in sources
                if item.identity == reply["source"]
                and item.revision == revision
                and item.author_id == reply["actor"]
                and item.authority == "owner"
            ),
            None,
        )
        prefix = "Decision " + reply["decision_id"] + "\n"
        if (
            not matched
            or not matched.content.strip().startswith(prefix)
            or question.get("coordinates") != dataclasses.asdict(context.coordinates)
            or question.get("hold_id") != unit.hold.id
            or (question.get("question") and question["question"] != context.question)
        ):
            raise CampaignError(
                "prior choice requires the current authenticated reply to this question"
            )
        source = matched
        answer = source.content.strip()[len(prefix) :].strip()
        selection, clarification = _ordinal_reply(answer, question)
        if clarification:
            return _clarify_reply(decision, question, revision, context, clarification)
    return dataclasses.replace(
        decision,
        answer=answer,
        actual_actor=source.author_id,
        decision_authority=source.authority,
        directed_reply=directed,
        owner_selection=selection,
        constraints=() if directed else decision.constraints,
        private_sources=tuple(s.id for s in context.sources if s.private),
    )


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
    prior_projections=(),
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
            prior_projections=prior_projections,
        )
        refreshed = decision_context(fresh["packet"])
        fresh_sources = {source.id: source for source in refreshed.sources}
        if fresh_sources != {source.id: source for source in context.sources}:
            raise CampaignError("decision evidence changed or was revoked after evidence review")
        if refreshed.unavailable:
            context = dataclasses.replace(context, unavailable=refreshed.unavailable)
        accepted = validate_decision(receipt["artifact"], context, current_sources=fresh_sources)
        prior_choice = accepted.disposition == "prior_decision_applies" and _routine_choice(unit)
        if prior_choice:
            accepted = _prior_implementation_choice(snapshot, unit, accepted, context)
            accepted = dataclasses.replace(
                accepted,
                id=_digest(
                    [
                        accepted.id,
                        accepted.answer,
                        accepted.actual_actor,
                        accepted.constraints,
                        _digest(packet),
                    ]
                ),
            )
        if accepted.disposition == "delegated_choice":
            if context.hold.id in unit.evidence.get("owner_answer_revisions", {}):
                raise CampaignError(
                    "a registered owner reply must be reviewed as existing authority"
                )
            actor = receipt.get("observed_model")
            source = receipt.get("observed_model_source")
            observed_models = receipt.get("observed_models", [])
            if (
                not isinstance(actor, str)
                or not actor.strip()
                or len(actor) > 256
                or source not in {"transcript_primary", "native_session_hook"}
                or not isinstance(observed_models, (list, tuple))
                or len(observed_models) > 32
                or any(
                    not isinstance(model, str) or not model or len(model) > 256
                    for model in observed_models
                )
            ):
                raise CampaignError("delegated choice requires an observed native decision model")
            accepted = dataclasses.replace(
                accepted,
                id=_digest(
                    [
                        accepted.id,
                        accepted.constraints,
                        accepted.invalidation_triggers,
                        actor,
                        source,
                        observed_models,
                        _digest(packet),
                    ]
                ),
                actual_actor=actor,
                observed_model_source=source,
                observed_models=tuple(dict.fromkeys([actor, *observed_models])),
            )
        if accepted.disposition == "delegated_choice" or (prior_choice and accepted.clear_hold):
            if _digest(fresh["packet"]) != _digest(packet):
                raise CampaignError("implementation decision inputs changed during review")
            if (
                len(accepted.answer) > 2048
                or len(accepted.constraints) > 8
                or any(len(item) > 512 for item in accepted.constraints)
            ):
                raise CampaignError("implementation choice exceeds the bounded execution input")
            pending = [
                development_choice(snapshot.bindings["decision_receipts"][binding["receipt"]])
                for key, binding in unit.evidence.get("pending_delegated_choices", {}).items()
                if key != accepted.hold_id
            ]
            choices = json.dumps([*pending, development_choice(dataclasses.asdict(accepted))])
            if len(choices.encode()) > 32_000 or _redact(choices) != choices:
                raise CampaignError("delegated choices exceed the safe bounded development input")
        if not accepted.clear_hold and (accepted.options or accepted.clarification):
            accepted = dataclasses.replace(accepted, id=_digest([snapshot.id, accepted.id]))
        return {"kind": "decision", "accepted": dataclasses.asdict(accepted)}
    context = resolution_context(packet)
    accepted = validate_proposal(receipt["artifact"], context, snapshot.spec.resolution)
    return {"kind": "resolution", "accepted": dataclasses.asdict(accepted)}


def _decision(raw: Mapping[str, Any]) -> DecisionReceipt:
    values = dict(raw)
    values["coordinates"] = EvidenceCoordinates(**values["coordinates"])
    values["options"] = tuple(
        (answer, reason, tuple(sources)) for answer, reason, sources in values.get("options", ())
    )
    for key in (
        "citations",
        "contrary",
        "constraints",
        "invalidation_triggers",
        "fact_ids",
        "private_sources",
        "observed_models",
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
        decision = _decision(validated["accepted"])
        original = dataclasses.asdict(unit.hold) if unit.hold else None
        apply_decision(snapshot, unit_id, decision)
        if (
            decision.disposition == "delegated_choice"
            and decision.observed_model_source
            and original
        ):
            from .campaign_attribution import record_stage_writer

            record_stage_writer(
                unit,
                {
                    "observed_model": decision.actual_actor,
                    "observed_models": decision.observed_models,
                },
            )
        if original and (decision.observed_model_source or decision.decision_authority):
            unit.evidence.setdefault("pending_delegated_choices", {})[decision.hold_id] = {
                "receipt": decision.id,
                "hold": original,
                "packet_digest": execution_input_digest(unit.evidence["active_resolution_packet"]),
            }
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
    if proposal.action == "reverify":
        from .campaign_resolution_delivery import next_step

        return next_step(snapshot, unit)
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
