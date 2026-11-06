"""Typed, bounded campaign diagnosis and repair authorization.

This module never launches a model or changes repository content. A coding-CLI
stage produces a proposal; trusted producer codes, preserved identities, budgets
and independent repair verification determine whether it may be applied. Repair
always returns to ordinary acceptance rather than directly declaring delivery.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import Any, Mapping

from .campaign_model import (
    CampaignError,
    CampaignSnapshot,
    EvidenceCoordinates,
    Hold,
    ResolutionSpec,
    UnitState,
)
from .campaign_scheduler import resource_available
from .campaign_store import add_event, transition


class CauseCode(StrEnum):
    QUOTA = "quota"
    CAPACITY = "capacity"
    RATE_LIMIT = "rate_limit"
    TRANSPORT = "transport"
    ENVIRONMENT = "environment"
    INTERRUPTED = "interrupted"
    VERIFICATION = "verification_failed"
    REVIEW = "review_findings"
    CONFLICT = "integration_conflict"
    OWNER = "owner_decision"
    HUMAN_ONLY = "human_only"
    CREDENTIALS = "credentials"
    PERMISSIONS = "permissions"
    SPEND = "spend"
    POLICY = "policy"
    OWNERSHIP = "ownership_uncertain"
    APPROVAL = "required_approval"
    UNKNOWN = "unknown"


_TRANSIENT = frozenset(
    {CauseCode.QUOTA, CauseCode.CAPACITY, CauseCode.RATE_LIMIT, CauseCode.TRANSPORT}
)
_RESTRICTED = frozenset(
    {
        CauseCode.OWNER,
        CauseCode.HUMAN_ONLY,
        CauseCode.CREDENTIALS,
        CauseCode.PERMISSIONS,
        CauseCode.SPEND,
        CauseCode.POLICY,
        CauseCode.OWNERSHIP,
        CauseCode.APPROVAL,
        CauseCode.UNKNOWN,
    }
)
_ACTIONS = frozenset(
    {"wait", "reprobe", "retry", "reverify", "repair", "plan_amendment", "owner_input"}
)


@dataclass(frozen=True)
class FailureEvidence:
    code: CauseCode
    producer: str
    id: str
    detail: str
    revision: str
    paths: tuple[str, ...] = ()
    retry_after_s: float | None = None


@dataclass(frozen=True)
class ResolutionContext:
    coordinates: EvidenceCoordinates
    cause: FailureEvidence
    original_stage: str
    evidence: tuple[tuple[str, str], ...] = ()
    allowed_paths: tuple[str, ...] = ()
    preserved_refs: tuple[str, ...] = ()
    expected_verification: tuple[tuple[str, ...], ...] = ()
    repair_commands: tuple[tuple[str, ...], ...] = ()
    quiescent: bool = False
    candidate_clean: bool = False
    prior_fingerprints: tuple[str, ...] = ()
    attempts: int = 0


@dataclass(frozen=True)
class ResolutionProposal:
    id: str
    coordinates: EvidenceCoordinates
    cause_id: str
    cause_code: str
    action: str
    rationale: str
    evidence_ids: tuple[str, ...]
    affected_paths: tuple[str, ...]
    preserved_refs: tuple[str, ...]
    verification: tuple[tuple[str, ...], ...]
    unresolved_decisions: tuple[str, ...]
    command: tuple[str, ...] = ()
    automatic: bool = False
    fingerprint: str = ""


@dataclass(frozen=True)
class ResolutionResult:
    proposal_id: str
    coordinates: EvidenceCoordinates
    parent_head: str
    head: str
    quiescent: bool
    preserves_history: bool
    source_identity: str
    verification: tuple[tuple[tuple[str, ...], int], ...] = ()
    reprobe_passed: bool = False
    recovery_push_confirmed: bool = False


def failure_from_receipt(raw: Mapping[str, Any]) -> FailureEvidence:
    """Only typed producer data classify causes; free-form prose is contextual."""
    producer = raw.get("producer", "")
    revision = raw.get("revision", "")
    if not isinstance(producer, str) or not isinstance(revision, str):
        raise CampaignError("failure producer/revision must be strings")
    try:
        code = CauseCode(raw.get("code", "unknown"))
    except ValueError:
        code = CauseCode.UNKNOWN
    # Old engines that supplied only a message cannot suddenly gain automatic
    # repair authority because a message contains the words "quota" or "retry".
    if not producer or not revision:
        code = CauseCode.UNKNOWN
    identity = (
        raw.get("id") or hashlib.sha256(json.dumps(dict(raw), sort_keys=True).encode()).hexdigest()
    )
    retry_after = raw.get("retry_after_s")
    if retry_after is not None and (type(retry_after) not in {int, float} or retry_after < 0):
        raise CampaignError("invalid producer retry delay")
    paths = raw.get("paths", [])
    if not isinstance(paths, (list, tuple)) or any(not isinstance(path, str) for path in paths):
        raise CampaignError("failure paths must be strings")
    return FailureEvidence(
        code, producer, identity, str(raw.get("detail", "")), revision, tuple(paths), retry_after
    )


def transient_retry(
    cause: FailureEvidence, *, attempt: int, now: float, cap_s: float = 3600
) -> float | None:
    """A durable next retry time, never a sleep occupying an execution slot."""
    if cause.code not in _TRANSIENT:
        return None
    delay = max(min(cap_s, 5 * 2 ** min(max(attempt, 0), 12)), cause.retry_after_s or 0)
    return now + delay


def resolution_fingerprint(context: ResolutionContext) -> str:
    # Failure detail may contain timestamps; stable identity is the typed cause,
    # candidate, source revision and exact supporting observations.
    return hashlib.sha256(
        json.dumps(
            {
                "code": context.cause.code,
                "revision": context.cause.revision,
                "head": context.coordinates.head,
                "base": context.coordinates.base,
                "graph": context.coordinates.graph_revision,
                "config": context.coordinates.config_revision,
                "evidence": context.evidence,
            },
            sort_keys=True,
        ).encode()
    ).hexdigest()


def resolution_packet(context: ResolutionContext) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "coordinates": asdict(context.coordinates),
        "cause": asdict(context.cause),
        "original_stage": context.original_stage,
        "evidence": dict(context.evidence),
        "allowed_paths": list(context.allowed_paths),
        "preserved_refs": list(context.preserved_refs),
        "expected_verification": [list(command) for command in context.expected_verification],
        "repair_commands": [list(command) for command in context.repair_commands],
        "attempts": context.attempts,
        "instructions": "Diagnose read-only. Candidate contents must remain unchanged. Preserve acceptance and both sides' intent. Propose a typed action; never clear holds, edit policy, run host repair commands, adopt ownership or fabricate approval.",
    }


def validate_proposal(
    raw: Mapping[str, Any], context: ResolutionContext, policy: ResolutionSpec
) -> ResolutionProposal:
    if raw.get("schema_version") != 1 or raw.get("coordinates") != asdict(context.coordinates):
        raise CampaignError("resolution artifact coordinates do not match current candidate")
    if raw.get("cause_id") != context.cause.id:
        raise CampaignError("resolution artifact cause identity changed")
    if raw.get("cause_code") != context.cause.code:
        raise CampaignError("resolver cannot downgrade or reclassify a producer cause")
    action = raw.get("action")
    if action not in _ACTIONS:
        raise CampaignError("unknown resolution action")
    if not context.quiescent:
        raise CampaignError("resolution requires confirmed quiescent ownership")
    evidence_ids = tuple(raw.get("evidence_ids", []))
    known_ids = {key for key, _ in context.evidence} | {context.cause.id}
    if not evidence_ids or any(key not in known_ids for key in evidence_ids):
        raise CampaignError("resolution must cite known failure evidence")
    rationale = raw.get("rationale")
    if not isinstance(rationale, str) or not rationale.strip():
        raise CampaignError("resolution rationale is required")
    affected = tuple(raw.get("affected_paths", []))
    for path in affected:
        if (
            not isinstance(path, str)
            or path.startswith(("/", "\\"))
            or ".." in path.replace("\\", "/").split("/")
            or not any(
                scope == "*" or path == scope or path.startswith(scope.rstrip("/") + "/")
                for scope in context.allowed_paths
            )
        ):
            raise CampaignError("resolution repair escapes accepted ownership")
    preserved = tuple(raw.get("preserved_refs", []))
    if not set(context.preserved_refs).issubset(preserved):
        raise CampaignError("resolution omitted preserved work")
    commands = raw.get("verification", [])
    if not isinstance(commands, (list, tuple)) or any(
        not isinstance(command, (list, tuple))
        or not command
        or any(not isinstance(arg, str) for arg in command)
        for command in commands
    ):
        raise CampaignError("resolution verification must contain argv arrays")
    verification = tuple(tuple(command) for command in commands)
    if action in {"repair", "reverify"} and not set(context.expected_verification).issubset(
        verification
    ):
        raise CampaignError("resolution cannot weaken expected verification")
    unresolved = tuple(raw.get("unresolved_decisions", []))
    if any(not isinstance(item, str) for item in unresolved):
        raise CampaignError("resolution decisions must be strings")
    command = tuple(raw.get("command", []))
    if command and command not in context.repair_commands:
        raise CampaignError("environment repair command is not explicitly authorized")
    if command and context.cause.code != CauseCode.ENVIRONMENT:
        raise CampaignError("host repair commands only apply to explicit environment repair")
    if action == "repair" and not affected and not command:
        raise CampaignError(
            "repair requires owned affected paths or an authorized environment command"
        )
    fingerprint = resolution_fingerprint(context)
    automatic = policy.mode == "bounded-auto" and action in {
        "wait",
        "reprobe",
        "retry",
        "reverify",
        "repair",
    }
    if context.cause.code in _RESTRICTED or unresolved:
        automatic = False
    if (
        context.attempts >= policy.max_attempts_per_unit
        or fingerprint in context.prior_fingerprints
    ):
        automatic = False
    if policy.mode == "off":
        raise CampaignError("campaign resolution is disabled")
    if action in {"retry", "reverify", "repair"} and context.cause.code in _TRANSIENT:
        automatic = False  # deterministic quota/transport reconciliation first
    payload = {
        "coordinates": asdict(context.coordinates),
        "cause": context.cause.id,
        "action": action,
        "fingerprint": fingerprint,
        "paths": affected,
        "rationale": rationale,
    }
    identity = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    return ResolutionProposal(
        identity,
        context.coordinates,
        context.cause.id,
        str(context.cause.code),
        action,
        rationale,
        evidence_ids,
        affected,
        preserved,
        verification,
        unresolved,
        command,
        automatic,
        fingerprint,
    )


def diagnosis_unchanged(before: Mapping[str, Any], after: Mapping[str, Any]) -> None:
    """Check Git/index/untracked/source fingerprints captured outside the model."""
    required = {"head", "index", "worktree", "untracked", "source"}
    if not required.issubset(before) or not required.issubset(after):
        raise CampaignError("diagnosis content observations are incomplete")
    if any(before[key] != after[key] for key in required):
        raise CampaignError("read-only diagnosis changed candidate content")


def begin_resolution(snapshot: CampaignSnapshot, unit_id: str, context: ResolutionContext) -> None:
    unit = snapshot.units[unit_id]
    if not context.quiescent or (unit.claim and not unit.claim.quiescent):
        raise CampaignError("failed worker must be confirmed quiescent before resolution")
    if (
        context.coordinates.unit != unit_id
        or context.coordinates.repository != snapshot.repository
        or context.coordinates.graph_revision != snapshot.graph_revision
        or context.coordinates.config_revision != snapshot.config_revision
    ):
        raise CampaignError("resolution context is stale")
    if snapshot.spec.resolution.mode == "off":
        raise CampaignError("campaign resolution is disabled")
    if not resource_available(snapshot, "resolution"):
        raise CampaignError("resolution session capacity unavailable")
    fingerprint = resolution_fingerprint(context)
    history = unit.evidence.setdefault("resolution_history", [])
    if unit.resolution_attempts >= snapshot.spec.resolution.max_attempts_per_unit:
        raise CampaignError("lifetime automatic resolution budget exhausted")
    if any(record.get("fingerprint") == fingerprint for record in history):
        raise CampaignError("same failure repeated without changed evidence")
    unit.resolution_attempts += 1
    history.append(
        {
            "fingerprint": fingerprint,
            "cause": asdict(context.cause),
            "attempt": unit.resolution_attempts,
            "coordinates": asdict(context.coordinates),
            "state": "diagnosing",
        }
    )
    add_event(
        snapshot,
        "resolution-start",
        unit=unit_id,
        attempt=unit.resolution_attempts,
        cause=context.cause.id,
        fingerprint=fingerprint,
    )


def record_proposal(snapshot: CampaignSnapshot, unit_id: str, proposal: ResolutionProposal) -> None:
    unit = snapshot.units[unit_id]
    history = unit.evidence.setdefault("resolution_history", [])
    if not history or history[-1]["fingerprint"] != proposal.fingerprint:
        raise CampaignError("resolution proposal has no matching diagnosis transaction")
    history[-1]["proposal"] = asdict(proposal)
    history[-1]["state"] = "proposed" if proposal.automatic else "held"
    if not proposal.automatic:
        unit.hold = unit.hold or Hold(
            "resolution",
            "Resolution requires existing decision evidence or owner input",
            id=f"resolution:{proposal.id}",
        )
    add_event(
        snapshot,
        "resolution-proposed",
        unit=unit_id,
        proposal=proposal.id,
        action=proposal.action,
        automatic=proposal.automatic,
    )


def apply_resolution(
    snapshot: CampaignSnapshot, unit_id: str, proposal: ResolutionProposal, result: ResolutionResult
) -> None:
    """Accept independent repair evidence; caller saves the transaction once."""
    unit = snapshot.units[unit_id]
    if not proposal.automatic:
        raise CampaignError("proposal has no automatic repair authority")
    if result.proposal_id != proposal.id or result.coordinates != proposal.coordinates:
        raise CampaignError("resolution result does not belong to the accepted proposal")
    coordinates = proposal.coordinates
    if (
        coordinates.repository != snapshot.repository
        or coordinates.unit != unit_id
        or coordinates.graph_revision != snapshot.graph_revision
        or coordinates.config_revision != snapshot.config_revision
    ):
        raise CampaignError("resolution result is stale")
    if unit.claim and not unit.claim.quiescent or not result.quiescent:
        raise CampaignError("repair writer has not exited")
    if unit.hold and unit.hold.kind not in {"resolution", "technical_failure"}:
        raise CampaignError("resolution cannot waive an unrelated hold")
    if (
        result.parent_head != coordinates.head
        or not result.preserves_history
        or not result.source_identity
    ):
        raise CampaignError("repair did not preserve candidate/source history")
    if proposal.action == "repair" and not result.recovery_push_confirmed:
        raise CampaignError("repair push is not confirmed")
    if proposal.command and not result.reprobe_passed:
        raise CampaignError("environment reprobe has not succeeded")
    observed = {tuple(command): code for command, code in result.verification}
    if any(observed.get(command) != 0 for command in proposal.verification):
        raise CampaignError("repair verification is missing or failed")
    history = unit.evidence.get("resolution_history", [])
    if not history or history[-1].get("proposal", {}).get("id") != proposal.id:
        raise CampaignError("resolution result lacks its persisted accepted proposal")
    if proposal.action in {"wait", "reprobe"}:
        if proposal.action == "reprobe" and not result.reprobe_passed:
            raise CampaignError("reprobe has not succeeded")
    else:
        # Never jump into a later acceptance state using pre-repair evidence.
        if unit.state not in {
            UnitState.READY,
            UnitState.RUNNING,
            UnitState.LOCALLY_COMPLETE,
            UnitState.SYNCING,
            UnitState.VERIFYING,
            UnitState.REVIEWING,
            UnitState.READY_TO_PUBLISH,
        }:
            raise CampaignError("unit cannot be rearmed from its current publication state")
        resume_state = unit.resume_state or UnitState.READY
        if resume_state not in {UnitState.READY, UnitState.SYNCING}:
            raise CampaignError("repair resume state cannot bypass candidate acceptance")
        if unit.state != resume_state:
            transition(snapshot, unit_id, resume_state)
        for key in ("integrated", "pushed", "verification", "review", "publication", "checks"):
            unit.evidence.pop(key, None)
        unit.bindings["head"] = result.head
        unit.hold = None
        unit.claim = None
        unit.resume_state = None
        unit.attempt += 1
    history[-1]["result"] = asdict(result)
    history[-1]["state"] = "verified"
    add_event(snapshot, "resolution-verified", unit=unit_id, proposal=proposal.id, head=result.head)
