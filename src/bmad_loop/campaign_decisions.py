"""Bounded evidence retrieval and authorization checks before owner escalation.

Reasoning sessions may interpret existing authority; they cannot create it. Only
configured source identities and independently observed facts confer authority.
Accepted receipts and permitted hold satisfaction live in the same campaign
snapshot; the repository ledger is a recoverable observer projection.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from . import verify
from .campaign_action_history import action as historical_action
from .campaign_model import CampaignError, CampaignSnapshot, EvidenceCoordinates, Hold
from .campaign_store import add_action, add_event
from .platform_util import atomic_write_text

_DISPOSITIONS = frozenset(
    {
        "prior_decision_applies",
        "delegated_choice",
        "already_satisfied",
        "needs_new_owner_decision",
        "conflicting_evidence",
        "external_wait",
    }
)
_AUTONOMOUS = frozenset({"prior_decision_applies", "delegated_choice", "already_satisfied"})
_AUTHORITIES = {"context": 0, "adopted_policy": 1, "delegation": 2, "owner": 3}


@dataclass(frozen=True)
class EvidenceLocation:
    """Owner-configured binding, never a claim parsed out of retrieved text."""

    id: str
    kind: str
    location: str
    authority: str = "context"
    scope: tuple[str, ...] = ("*",)
    expected_author: str = ""
    private: bool = False
    approved_head: str = ""
    release_hold: str = ""
    supersedes: tuple[str, ...] = ()


@dataclass(frozen=True)
class EvidenceSource:
    id: str
    kind: str
    identity: str
    revision: str
    content: str
    authority: str = "context"
    scope: tuple[str, ...] = ("*",)
    author_id: str = ""
    timestamp: float = 0
    origin_verified: bool = False
    private: bool = False
    url: str = ""
    approved_head: str = ""
    release_hold: str = ""
    supersedes: tuple[str, ...] = ()


@dataclass(frozen=True)
class ObservedFact:
    """A fact populated by trusted deterministic reconciliation, not the model."""

    id: str
    kind: str
    hold_id: str
    unit: str
    head: str
    satisfied: bool
    actor: str
    revision: str
    verified: bool = False
    attested: bool = False


@dataclass(frozen=True)
class EvidenceCollection:
    sources: tuple[EvidenceSource, ...]
    unavailable: tuple[str, ...] = ()
    truncated: tuple[str, ...] = ()


@dataclass(frozen=True)
class DecisionContext:
    coordinates: EvidenceCoordinates
    hold: Hold
    question: str
    sources: tuple[EvidenceSource, ...]
    facts: tuple[ObservedFact, ...] = ()
    allowed_actions: tuple[str, ...] = ("choose", "continue")
    allowed_paths: tuple[str, ...] = ()
    unavailable: tuple[str, ...] = ()


@dataclass(frozen=True)
class DecisionReceipt:
    id: str
    coordinates: EvidenceCoordinates
    hold_id: str
    disposition: str
    action: str
    answer: str
    citations: tuple[tuple[str, str, str], ...]
    contrary: tuple[str, ...]
    constraints: tuple[str, ...]
    invalidation_triggers: tuple[str, ...]
    fact_ids: tuple[str, ...]
    actual_actor: str
    clear_hold: bool
    private_sources: tuple[str, ...] = ()


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _git(repo: Path, *args: str) -> str:
    result = verify.git_bytes(repo, *args)
    if result.returncode:
        raise CampaignError(f"decision evidence git observation failed: {args[0]}")
    return result.stdout.decode("utf-8", errors="strict").strip()


def collect_evidence(
    repo: Path,
    configured_sources: tuple[EvidenceLocation, ...],
    *,
    providers: Mapping[str, Callable[[EvidenceLocation], EvidenceSource]] | None = None,
    max_sources: int = 32,
    max_source_bytes: int = 64_000,
    max_total_bytes: int = 256_000,
) -> EvidenceCollection:
    """Read only explicitly bound sources; missing provenance stays contextual.

    Local adopted-policy authority is anchored in a committed project file.
    Owner-imported chat requires an explicitly bound provider to authenticate the
    original roles/IDs; an arbitrary JSON file cannot self-assert owner authority.
    """
    repo = repo.resolve()
    sources: list[EvidenceSource] = []
    unavailable: list[str] = []
    truncated: list[str] = []
    consumed = 0
    if len({item.id for item in configured_sources}) != len(configured_sources):
        raise CampaignError("duplicate decision evidence source IDs")
    for index, location in enumerate(configured_sources):
        if location.authority not in _AUTHORITIES:
            raise CampaignError("unknown configured decision authority")
        if index >= max_sources:
            truncated.append(location.id)
            continue
        try:
            if location.kind == "document":
                path = (repo / location.location).resolve()
                if not path.is_relative_to(repo) or not path.is_file():
                    raise CampaignError(
                        "decision document escapes configured repository or is missing"
                    )
                with path.open("rb") as stream:
                    data = stream.read(max_source_bytes + 1)
                if len(data) > max_source_bytes or consumed + len(data) > max_total_bytes:
                    truncated.append(location.id)
                    continue
                content = data.decode("utf-8")
                relative = path.relative_to(repo).as_posix()
                # The file may be edited between these reads. Exact equality
                # against the committed blob is required for policy authority.
                head = _git(repo, "rev-parse", "HEAD")
                committed = verify.git_bytes(repo, "show", f"{head}:{relative}")
                committed_match = committed.returncode == 0 and committed.stdout == data
                revision = f"{head}:{_digest(content)}" if committed_match else _digest(content)
                provenance = (
                    _git(
                        repo, "log", "-1", "--format=%H%x00%aN%x00%at", head, "--", relative
                    ).split("\x00")
                    if committed_match
                    else []
                )
                author = provenance[1] if len(provenance) == 3 else ""
                timestamp = float(provenance[2]) if len(provenance) == 3 else 0
                source = EvidenceSource(
                    location.id,
                    location.kind,
                    str(path),
                    revision,
                    content,
                    authority=(
                        location.authority
                        if committed_match and location.authority == "adopted_policy"
                        else "context"
                    ),
                    scope=location.scope,
                    author_id=author,
                    timestamp=timestamp,
                    url=f"git:{head}:{relative}" if committed_match else "",
                    origin_verified=committed_match,
                    private=location.private,
                    approved_head=location.approved_head,
                    release_hold=location.release_hold,
                    supersedes=location.supersedes,
                )
            elif providers and location.kind in providers:
                source = providers[location.kind](location)
                if source.id != location.id or source.identity != location.location:
                    raise CampaignError("decision provider source identity mismatch")
                if source.authority != location.authority or source.scope != location.scope:
                    raise CampaignError("decision provider altered configured authority or scope")
                if location.expected_author and source.author_id != location.expected_author:
                    raise CampaignError("decision evidence author mismatch")
                if not source.origin_verified and source.authority != "context":
                    # Keep the accessible text useful while explicitly reducing
                    # its authority. Missing origin is never an owner decision.
                    source = EvidenceSource(**{**asdict(source), "authority": "context"})
            else:
                raise CampaignError(f"no authorized {location.kind} transcript/source provider")
            size = len(source.content.encode("utf-8"))
            if size > max_source_bytes or consumed + size > max_total_bytes:
                truncated.append(location.id)
                continue
            consumed += size
            sources.append(source)
        except (OSError, UnicodeError, CampaignError) as exc:
            unavailable.append(f"{location.id}: {exc}")
    return EvidenceCollection(tuple(sources), tuple(unavailable), tuple(truncated))


def decision_packet(context: DecisionContext) -> dict[str, Any]:
    """Private stage input; callers only publish the receipt's safe summary."""
    return {
        "schema_version": 1,
        "coordinates": asdict(context.coordinates),
        "hold": asdict(context.hold),
        "question": context.question,
        "allowed_actions": list(context.allowed_actions),
        "allowed_paths": list(context.allowed_paths),
        "sources": [asdict(source) for source in context.sources],
        "facts": [asdict(fact) for fact in context.facts],
        "unavailable": list(context.unavailable),
        "instructions": "Interpret only existing verified authority. Cite exact passages, original IDs and revisions; account for contrary direction. Never invent approval, change policy, dismiss reviews or clear a human-only obligation.",
    }


def _in_scope(source: EvidenceSource, unit: str) -> bool:
    return any(fnmatch.fnmatchcase(unit, pattern) for pattern in source.scope)


def validate_decision(
    raw: Mapping[str, Any],
    context: DecisionContext,
    *,
    current_sources: Mapping[str, EvidenceSource] | None = None,
) -> DecisionReceipt:
    """Validate scope, provenance and hold-specific permission deterministically."""
    if raw.get("schema_version") != 1 or raw.get("coordinates") != asdict(context.coordinates):
        raise CampaignError("decision artifact coordinates are stale or malformed")
    if raw.get("hold_id") != context.hold.id:
        raise CampaignError("decision artifact hold identity mismatch")
    disposition = raw.get("disposition")
    if disposition not in _DISPOSITIONS:
        raise CampaignError("unknown decision disposition")
    if disposition in _AUTONOMOUS and context.unavailable:
        raise CampaignError("decision evidence coverage is incomplete")
    answer = raw.get("answer", "")
    action = raw.get("action", "")
    if not isinstance(answer, str) or not isinstance(action, str):
        raise CampaignError("decision answer/action must be strings")
    if disposition in _AUTONOMOUS and (not answer.strip() or action not in context.allowed_actions):
        raise CampaignError("decision action is outside the existing policy envelope")
    for path in raw.get("affected_paths", []):
        if (
            not isinstance(path, str)
            or not context.allowed_paths
            or not any(
                path == scope or path.startswith(scope.rstrip("/") + "/")
                for scope in context.allowed_paths
            )
        ):
            raise CampaignError("decision changes an unapproved path")
    indexed = {source.id: source for source in context.sources}
    current = current_sources if current_sources is not None else indexed
    citations: list[tuple[str, str, str]] = []
    support: list[EvidenceSource] = []
    for citation in raw.get("support", []):
        if not isinstance(citation, dict):
            raise CampaignError("decision citation must be an object")
        source = indexed.get(citation.get("source_id", ""))
        if source is None or citation.get("revision") != source.revision:
            raise CampaignError("decision cites unknown/stale source")
        if current.get(source.id) != source:
            raise CampaignError("decision evidence changed or was revoked")
        quote = citation.get("quote")
        if not isinstance(quote, str) or not quote.strip() or quote not in source.content:
            raise CampaignError("decision passage is not present in its cited source")
        if not _in_scope(source, context.coordinates.unit):
            raise CampaignError("decision evidence belongs to another scope")
        if source.approved_head and source.approved_head != context.coordinates.head:
            raise CampaignError("decision approval applies to another candidate head")
        citations.append((source.id, source.revision, quote))
        support.append(source)
    contrary = tuple(raw.get("contrary", []))
    if any(not isinstance(key, str) or key not in indexed for key in contrary):
        raise CampaignError("decision contrary evidence must name known sources")
    authorities = [
        source for source in support if source.origin_verified and source.authority != "context"
    ]
    applicable = [
        source
        for source in context.sources
        if source.origin_verified
        and source.authority != "context"
        and _in_scope(source, context.coordinates.unit)
    ]
    accounted = {source.id for source in support} | set(contrary)
    if disposition in _AUTONOMOUS and any(source.id not in accounted for source in applicable):
        raise CampaignError("decision omitted applicable authoritative direction")
    superseded = {key for source in authorities for key in source.supersedes}
    if disposition in _AUTONOMOUS:
        for key in contrary:
            opposing = indexed[key]
            valid_supersession = key in superseded and any(
                key in source.supersedes
                and _AUTHORITIES[source.authority] >= _AUTHORITIES[opposing.authority]
                and source.timestamp >= opposing.timestamp
                for source in authorities
            )
            if (
                opposing.origin_verified
                and opposing.authority != "context"
                and not valid_supersession
            ):
                raise CampaignError("conflicting authoritative evidence remains unresolved")
    fact_ids = tuple(raw.get("fact_ids", []))
    facts = {fact.id: fact for fact in context.facts}
    selected_facts: list[ObservedFact] = []
    for key in fact_ids:
        fact = facts.get(key)
        if (
            fact is None
            or not fact.verified
            or not fact.satisfied
            or not fact.revision
            or not fact.actor
        ):
            raise CampaignError("decision fact is not independently verified")
        if (fact.hold_id, fact.unit, fact.head) != (
            context.hold.id,
            context.coordinates.unit,
            context.coordinates.head,
        ):
            raise CampaignError("decision fact does not match current hold/head")
        selected_facts.append(fact)
    if disposition == "prior_decision_applies" and not any(
        source.authority in {"owner", "adopted_policy"} for source in authorities
    ):
        raise CampaignError("prior decision requires verified owner or adopted policy authority")
    if disposition == "delegated_choice" and not any(
        source.authority == "delegation" for source in authorities
    ):
        raise CampaignError("delegated choice requires existing verified delegation")
    if disposition == "already_satisfied" and not selected_facts:
        raise CampaignError("satisfaction requires deterministic current facts")
    clear = disposition in _AUTONOMOUS
    hold = context.hold
    if clear:
        if hold.kind in {"owner", "owner_stop", "operator"}:
            releases = [
                source
                for source in authorities
                if source.authority == "owner"
                and source.release_hold == hold.id
                and source.timestamp > hold.created_at
            ]
            conditions = [fact for fact in selected_facts if fact.kind == "release_condition"]
            if not releases and not conditions:
                raise CampaignError(
                    "explicit owner hold requires a later authorized release or its verified release condition"
                )
        elif hold.kind in {"github_review", "required_review"}:
            if disposition != "already_satisfied" or not any(
                fact.kind == "github_approval" for fact in selected_facts
            ):
                raise CampaignError("required GitHub review needs actual current eligible approval")
        elif hold.kind in {"human_action", "human_only"}:
            if disposition != "already_satisfied" or not any(
                fact.kind == "human_action"
                and (not hold.evidence.get("requires_attestation") or fact.attested)
                for fact in selected_facts
            ):
                raise CampaignError("human-only action/attestation remains outstanding")
        elif hold.kind in {"plan_checkpoint", "spec_checkpoint", "checkpoint"}:
            if hold.evidence.get("fresh_signoff") and not any(
                fact.kind == "fresh_approval" for fact in selected_facts
            ):
                raise CampaignError("checkpoint requires a fresh actual signoff")
            if disposition != "already_satisfied" and not hold.evidence.get(
                "allow_prior_approval", False
            ):
                raise CampaignError("checkpoint does not permit prior approval")
        elif hold.kind not in {"product_choice", "planning_decision", "design_choice", "decision"}:
            # CI, tracking, ownership and capacity waits use deterministic
            # producer-specific reconciliation, never model-issued approval.
            raise CampaignError("this hold is not clearable by decision evidence review")
    constraints = tuple(raw.get("constraints", []))
    invalidation = tuple(raw.get("invalidation_triggers", []))
    if any(not isinstance(item, str) for item in (*constraints, *invalidation)):
        raise CampaignError("decision constraints/invalidation triggers must be strings")
    identity = _digest(
        json.dumps(
            {
                "coordinates": asdict(context.coordinates),
                "hold": hold.id,
                "disposition": disposition,
                "action": action,
                "answer": answer,
                "citations": citations,
                "facts": fact_ids,
            },
            sort_keys=True,
        )
    )
    actor = (
        selected_facts[0].actor
        if selected_facts
        else authorities[0].author_id if authorities else ""
    )
    return DecisionReceipt(
        identity,
        context.coordinates,
        hold.id,
        disposition,
        action,
        answer,
        tuple(citations),
        contrary,
        constraints,
        invalidation,
        fact_ids,
        actor,
        clear,
        tuple(source.id for source in support if source.private),
    )


def apply_decision(snapshot: CampaignSnapshot, unit_id: str, receipt: DecisionReceipt) -> None:
    """Caller saves once: receipt and permitted hold transition are atomic."""
    unit = snapshot.units[unit_id]
    coordinates = receipt.coordinates
    if (
        coordinates.unit != unit_id
        or coordinates.repository != snapshot.repository
        or coordinates.graph_revision != snapshot.graph_revision
        or coordinates.config_revision != snapshot.config_revision
    ):
        raise CampaignError("decision receipt is stale for campaign bindings")
    if not unit.hold or unit.hold.id != receipt.hold_id:
        raise CampaignError("decision hold changed")
    head = unit.bindings.get("head", unit.evidence.get("head"))
    if head and head != coordinates.head:
        raise CampaignError("decision candidate head changed")
    receipts = snapshot.bindings.setdefault("decision_receipts", {})
    receipts[receipt.id] = asdict(receipt)
    projection = "decision-project:" + receipt.id
    if historical_action(snapshot, projection) is None:
        add_action(
            snapshot, "decision-project", unit_id, {"decision_id": receipt.id}, action_id=projection
        )
    if receipt.clear_hold:
        previous = asdict(unit.hold)
        unit.hold = None
        add_event(
            snapshot,
            "hold-satisfied",
            unit=unit_id,
            hold=previous,
            decision=receipt.id,
            actor=receipt.actual_actor,
            disposition=receipt.disposition,
        )
    else:
        requests = snapshot.bindings.setdefault("decision_requests", {})
        request_key = _digest(
            json.dumps(
                {"hold": receipt.hold_id, "head": coordinates.head, "sources": receipt.citations},
                sort_keys=True,
            )
        )
        if request_key not in requests:
            requests[request_key] = {
                "receipt": receipt.id,
                "hold_id": receipt.hold_id,
                "question": unit.hold.reason,
                "recommended_answer": receipt.answer,
                "disposition": receipt.disposition,
            }
            add_event(
                snapshot,
                "owner-decision-needed",
                unit=unit_id,
                request=request_key,
                decision=receipt.id,
            )


def decision_summary(receipt: DecisionReceipt) -> dict[str, Any]:
    """Permitted public summary; never raw private messages or quoted passages."""
    return {
        "decision_id": receipt.id,
        "disposition": receipt.disposition,
        "answer": (
            receipt.answer
            if not receipt.private_sources
            else "Decision recorded locally; private evidence retained in the campaign ledger."
        ),
        "sources": [
            {"id": source, "revision": revision} for source, revision, _ in receipt.citations
        ],
        "hold_id": receipt.hold_id,
        "head": receipt.coordinates.head,
    }


def project_ledger(snapshot: CampaignSnapshot, path: Path) -> None:
    """Rebuild an index from authoritative snapshot receipts, never vice versa."""
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(
        path,
        json.dumps(
            {
                "schema_version": 1,
                "campaign_id": snapshot.id,
                "revision": snapshot.revision,
                "receipts": snapshot.bindings.get("decision_receipts", {}),
            },
            sort_keys=True,
        )
        + "\n",
        follow_symlinks=False,
    )
