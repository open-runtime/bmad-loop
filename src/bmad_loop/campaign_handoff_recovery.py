"""Read-only reconstruction of current constraints when native history is missing.

Missing history stays explicitly missing. Configured, provenance-checked policy
or owner evidence can establish the current working contract after independent
review; contextual GitHub discussion and canonical acceptance alone cannot erase
an unknown owner instruction or manufacture approval.
"""

from __future__ import annotations

import dataclasses

from .campaign_model import CampaignError
from .campaign_transcripts import digest

SCHEMA = {
    "type": "object",
    "required": [
        "handoff_id",
        "capture_digest",
        "covered_evidence_ids",
        "obligation_ids",
        "missing_history_ids",
        "disposition",
        "constraints",
        "authority_basis",
        "unresolved",
    ],
    "properties": {
        "handoff_id": {"type": "string"},
        "capture_digest": {"type": "string"},
        "covered_evidence_ids": {"type": "array", "items": {"type": "string"}},
        "obligation_ids": {"type": "array", "items": {"type": "string"}},
        "missing_history_ids": {"type": "array", "items": {"type": "string"}},
        "disposition": {
            "type": "string",
            "enum": ["current_constraints_reconstructed", "needs_owner_evidence"],
        },
        "constraints": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["source_id", "source_quote", "interpretation"],
                "properties": {
                    "source_id": {"type": "string"},
                    "source_quote": {"type": "string", "minLength": 1},
                    "interpretation": {"type": "string", "minLength": 1},
                },
            },
        },
        "authority_basis": {"type": "array", "items": {"type": "string"}},
        "unresolved": {"type": "array", "items": {"type": "string"}},
    },
}

AUDIT_SCHEMA = {
    "type": "object",
    "required": [
        "handoff_id",
        "capture_digest",
        "covered_evidence_ids",
        "obligation_ids",
        "missing_history_ids",
        "verdict",
        "unresolved",
    ],
    "properties": {
        key: value
        for key, value in SCHEMA["properties"].items()
        if key
        in {
            "handoff_id",
            "capture_digest",
            "covered_evidence_ids",
            "obligation_ids",
            "missing_history_ids",
            "unresolved",
        }
    },
}
AUDIT_SCHEMA["properties"]["verdict"] = {
    "type": "string",
    "enum": ["current_contract_established", "needs_owner_evidence"],
}


def collect(runtime, snapshot, unit, capture, *, max_bytes):
    from .campaign_decision_sources import configured_locations, github_provider
    from .campaign_decisions import EvidenceLocation, collect_evidence

    locations = list(configured_locations(snapshot.spec.decisions.sources, scope=(unit.id,)))
    configured = {row.id for row in locations}
    for name in ("AGENTS.md", "README.md"):
        if (runtime.project / name).is_file() and "document:" + name not in configured:
            locations.append(
                EvidenceLocation("context:" + name, "document", name, scope=(unit.id,))
            )
    providers = dict(getattr(runtime, "evidence_providers", {}))
    if snapshot.spec.github.enabled and hasattr(runtime, "gateway"):
        providers["github"] = github_provider(runtime.gateway(snapshot))
    collection = collect_evidence(
        runtime.project,
        tuple(locations),
        providers=providers,
        max_source_bytes=max_bytes // 2,
        max_total_bytes=max_bytes // 2,
    )
    sources = [dataclasses.asdict(source) for source in collection.sources]
    unavailable = [*collection.unavailable, *(key + ": truncated" for key in collection.truncated)]
    if snapshot.spec.github.enabled and hasattr(runtime, "gateway"):
        from .campaign_runtime_decisions import collect as mapped_collect

        mapped = mapped_collect(runtime, snapshot, unit, max_sources=16, max_bytes=max_bytes // 2)
        sources.extend(dataclasses.asdict(source) for source in mapped.sources)
        unavailable.extend(mapped.unavailable)
    sources = list({row["id"]: row for row in sources}.values())
    for obligation in capture["obligations"]:
        sources.append(
            {
                "id": "canonical:" + obligation["id"],
                "content": obligation.get("text", ""),
                "authority": "canonical_acceptance",
                "origin_verified": True,
                "revision": digest(obligation),
                "scope": [unit.id],
            }
        )
    return {
        "sources": sources,
        "unavailable": unavailable,
        "missing_history": [{"id": digest(gap), "description": gap} for gap in capture["gaps"]],
        "obligation_ids": [row["id"] for row in capture["obligations"]],
    }


def validate(result, inputs, *, audit=False):
    for key in ("handoff_id", "capture_digest"):
        if result.get(key) != inputs[key]:
            raise CampaignError("History recovery belongs to another current capture")
    sources = {row["id"]: row for row in inputs["sources"]}
    for key, expected in {
        "covered_evidence_ids": set(sources),
        "obligation_ids": set(inputs["obligation_ids"]),
        "missing_history_ids": {row["id"] for row in inputs["missing_history"]},
    }.items():
        observed = result.get(key)
        if (
            not isinstance(observed, list)
            or len(observed) != len(expected)
            or set(observed) != expected
        ):
            raise CampaignError("History recovery omitted evidence, acceptance or missing coverage")
    if audit:
        ready = result.get("verdict") == "current_contract_established" and not result.get(
            "unresolved"
        )
        if ready and (not inputs["proposed"].get("ready") or inputs["unavailable"]):
            raise CampaignError("Independent recovery audit cannot upgrade unresolved authority")
        return {**result, "ready": ready, "authority": "context-only", "validated": True}
    cited = set()
    for row in result.get("constraints", []):
        source = sources.get(row.get("source_id"))
        if (
            not source
            or not row.get("source_quote")
            or row["source_quote"] not in source["content"]
            or not row.get("interpretation")
        ):
            raise CampaignError("Recovered constraint lacks an exact bound evidence quote")
        cited.add(source["id"])
    basis = set(result.get("authority_basis", []))
    if not basis <= cited:
        raise CampaignError("Recovery authority basis lacks a quoted constraint")
    authoritative = {
        key
        for key, source in sources.items()
        if source.get("origin_verified")
        and source.get("authority") in {"adopted_policy", "owner", "delegation"}
    }
    ready = result.get("disposition") == "current_constraints_reconstructed"
    if ready and (
        not basis or not basis <= authoritative or inputs["unavailable"] or result.get("unresolved")
    ):
        raise CampaignError("Missing native history still lacks authoritative current constraints")
    return {**result, "ready": ready, "authority": "context-only", "validated": True}
