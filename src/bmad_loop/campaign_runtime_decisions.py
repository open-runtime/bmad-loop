"""Bind mapped discussions and current approval facts to decision sessions.

Native/editor proof supplies authority; ordinary discussion remains contextual.
Accepted receipts stay authoritative in campaign state. The local versioned
ledger and sanitized GitHub comments are recoverable, idempotent projections.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any

from .campaign_decision_sources import configured_locations, github_provider
from .campaign_decisions import (
    EvidenceLocation,
    EvidenceSource,
    ObservedFact,
    collect_evidence,
    decision_summary,
    project_ledger,
)
from .campaign_github import GithubError
from .campaign_model import CampaignError
from .campaign_publication import PublicationController, PublicationRequirements
from .platform_util import file_lock


@dataclass(frozen=True)
class MappedEvidence:
    sources: tuple[EvidenceSource, ...] = ()
    facts: tuple[ObservedFact, ...] = ()
    unavailable: tuple[str, ...] = ()


def _digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def _number(binding) -> int:
    value = binding.get("number") if isinstance(binding, dict) else binding
    if type(value) is not int or value <= 0:
        raise CampaignError("mapped decision discussion lacks its native number")
    return value


def _approval(gateway, snapshot, unit) -> tuple[ObservedFact, ...]:
    if unit.hold.kind not in {"github_review", "required_review"} or not unit.bindings.get("pr"):
        return ()
    observed = PublicationController(
        gateway, requirements=PublicationRequirements.from_spec(snapshot.spec.github)
    ).inspect(_number(unit.bindings["pr"]))
    head = unit.bindings.get("head", "")
    pr = observed.pr
    if not observed.rules.complete or pr.get("state") != "open":
        raise CampaignError("GitHub approval rules or open-PR coverage is incomplete")
    if pr["head"]["sha"] != head or pr["base"]["ref"] != snapshot.spec.target:
        raise CampaignError("GitHub approval belongs to another current candidate")
    expected_node = (
        unit.bindings["pr"].get("node_id") if isinstance(unit.bindings["pr"], dict) else None
    )
    if expected_node and pr.get("node_id") != expected_node:
        raise CampaignError("GitHub approval PR native identity changed")
    latest = {}
    for review in sorted(observed.reviews, key=lambda row: row.get("id", 0)):
        user = review.get("user") or {}
        author = user.get("id")
        if type(author) is not int or author <= 0:
            raise CampaignError("GitHub review lacks numeric reviewer identity")
        if review.get("state") not in {"COMMENTED", "PENDING"}:
            latest[author] = review
    approvers = sorted(
        author
        for author, row in latest.items()
        if author != (pr.get("user") or {}).get("id")
        and row.get("state") == "APPROVED"
        and row.get("commit_id") == head
    )
    satisfied = (
        observed.review_decision == "APPROVED"
        and len(approvers) >= max(1, observed.rules.required_reviews)
        and not any(row.get("state") == "CHANGES_REQUESTED" for row in latest.values())
        and all(thread.get("isResolved") is True for thread in observed.threads)
    )
    revision = _digest(
        {
            "pr": pr.get("node_id", pr.get("id")),
            "head": head,
            "rules": dataclasses.asdict(observed.rules),
            "decision": observed.review_decision,
            "reviews": latest,
            "threads": observed.threads,
        }
    )
    return (
        ObservedFact(
            "github-approval:" + str(pr["number"]),
            "github_approval",
            unit.hold.id,
            unit.id,
            head,
            satisfied,
            "github:user:" + ",".join(map(str, approvers)),
            revision,
            verified=True,
        ),
    )


def collect(runtime, snapshot, unit, *, max_sources=32, max_bytes=256_000) -> MappedEvidence:
    """Read bounded exact mapped objects; incomplete coverage cannot authorize."""
    if not snapshot.spec.github.enabled:
        return MappedEvidence()
    gateway = runtime.gateway(snapshot)
    configured = configured_locations(snapshot.spec.decisions.sources, scope=(unit.id,))
    explicit = {
        location.location.split("@", 1)[0] for location in configured if location.kind == "github"
    }
    pins = {}
    for location in configured:
        match = re.fullmatch(r"gh:(?:issue|pull):([1-9][0-9]*)@([1-9][0-9]*)", location.location)
        if match:
            pins.setdefault(int(match[1]), set()).add(match[2])
    locations = []
    unavailable = []
    seen = set()
    consumed = 0

    def add(identity, number, content, author):
        nonlocal consumed
        pin = pins.get(number, set())
        if len(pin) > 1:
            raise CampaignError("mapped discussion has conflicting numeric owner bindings")
        if identity in explicit or identity in seen:
            return
        adopted = next(iter(pin)) if pin and str(author) in pin else ""
        if adopted:
            identity += "@" + adopted
        size = len(str(content or "").encode("utf-8"))
        if len(locations) >= max_sources or consumed + size > max_bytes:
            raise CampaignError("mapped decision discussion coverage exceeds its bound")
        consumed += size
        seen.add(identity)
        locations.append(
            EvidenceLocation(
                "github:" + identity,
                "github",
                identity,
                authority="owner",
                scope=(unit.id,),
                expected_author=adopted,
            )
        )

    try:
        parents = {}
        for key in ("issue", "epic_issue", "pr"):
            binding = unit.bindings.get(key)
            if binding:
                number = _number(binding)
                parents[number] = "pull" if key == "pr" else parents.get(number, "issue")
        for number, kind in parents.items():
            item = gateway.pull(number) if kind == "pull" else gateway.issue(number)
            add(f"gh:{kind}:{number}", number, item.get("body"), (item.get("user") or {}).get("id"))
            for page in gateway.iter_pages(f"{gateway.root}/issues/{number}/comments"):
                for comment in page:
                    ident = comment.get("id")
                    if type(ident) is not int or ident <= 0:
                        raise CampaignError("mapped discussion comment lacks native identity")
                    add(
                        f"gh:comment:{number}:{ident}",
                        number,
                        comment.get("body"),
                        (comment.get("user") or {}).get("id"),
                    )
        collection = collect_evidence(
            runtime.project,
            tuple(locations),
            providers={"github": github_provider(gateway)},
            max_sources=max_sources,
            max_total_bytes=max_bytes,
        )
        unavailable.extend(collection.unavailable)
        unavailable.extend(value + ": truncated" for value in collection.truncated)
        sources = collection.sources
    except (CampaignError, GithubError, OSError, ValueError, TypeError, KeyError) as exc:
        return MappedEvidence(unavailable=("mapped GitHub discussion unavailable: " + str(exc),))
    try:
        facts = _approval(gateway, snapshot, unit)
    except (CampaignError, GithubError, OSError, ValueError, TypeError, KeyError) as exc:
        unavailable.append("current GitHub approval unavailable: " + str(exc))
        facts = ()
    return MappedEvidence(sources, facts, tuple(unavailable))


def project(runtime, snapshot, action) -> dict[str, Any]:
    """Prepare deterministic summary intents; the existing outbox does all writes."""
    from .campaign_resolution_flow import _decision, _redact

    receipt = _decision(snapshot.bindings["decision_receipts"][action.payload["decision_id"]])
    if (
        receipt.coordinates.unit != action.unit
        or receipt.coordinates.repository != snapshot.repository
    ):
        raise CampaignError("decision projection differs from its authoritative unit/repository")
    unit = snapshot.units[receipt.coordinates.unit]
    ledger = runtime.store.directory(snapshot.id) / "decision-ledger.json"
    with file_lock(ledger.with_suffix(".lock")):
        # Parallel units can accept decisions while an older projection waits.
        # Load under the projection lock so an old effect cannot drop new rows.
        project_ledger(runtime.store.load(snapshot.id), ledger)
    summary = _redact(decision_summary(receipt))
    summary["sources"] = [
        source for source in summary["sources"] if source["id"] not in receipt.private_sources
    ]
    summary["private_source_count"] = len(receipt.private_sources)
    if len(json.dumps(summary).encode()) > 32_000:
        summary["answer"] = "Decision recorded locally; consult the campaign decision ledger."
    intents = []
    if snapshot.spec.github.enabled:
        gateway = runtime.gateway(snapshot)
        numbers = sorted(
            {_number(unit.bindings[key]) for key in ("issue", "pr") if unit.bindings.get(key)}
        )
        links = " ".join(
            f"https://github.com/{snapshot.repository}/issues/{number}" for number in numbers
        )
        for number in numbers:
            identifier = "decision-summary:" + receipt.id + ":" + str(number)
            intents.append(
                gateway.prepare_comment(
                    number,
                    action_id=identifier,
                    campaign_id=snapshot.id,
                    unit_id=unit.id,
                    text="Campaign decision\n\n"
                    + json.dumps(summary, sort_keys=True, ensure_ascii=True)
                    + "\n\n"
                    + links,
                    generation=snapshot.generation,
                ).to_dict()
            )
    return {"decision_id": receipt.id, "ledger": str(ledger), "intents": intents}
