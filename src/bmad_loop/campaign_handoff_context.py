"""Bounded continuation context with independent exact obligations and source retrieval.

Packets carry context, never transferred permissions or approval. They do not
move a write lease: the campaign's fenced worker replacement owns that action.
An incomplete packet remains useful for read-only evidence recovery.
"""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .campaign_model import CampaignError
from .campaign_summary import ContextBudget, SummaryJobSpec, TokenCounter, byte_token_bound
from .campaign_transcripts import HistorySnapshot, blob, digest, encoded, load_events


@dataclass(frozen=True)
class HandoffPacket:
    path: str
    sha256: str
    tokens: int
    read_only: bool
    source_ids: tuple[str, ...]
    omitted_owner_ids: tuple[str, ...]
    omitted_obligation_ids: tuple[str, ...]
    coverage_gaps: tuple[str, ...]
    obligation_index: str
    owner_index: str
    summary_index: str = ""
    omitted_summary_ids: tuple[str, ...] = ()


def _indexed_json(reference):
    from .campaign_headless import file_digest, read_json

    path = Path(reference["path"])
    if file_digest(path) != reference.get("sha256"):
        raise CampaignError("Handoff summary index or artifact changed")
    return read_json(path)


def load_summary_index(payload):
    """Resolve the complete hash-bound summary catalog, including omitted context."""
    reference = payload.get("summary_index")
    if not isinstance(reference, dict):
        raise CampaignError("Handoff summary index is missing")
    document = _indexed_json(reference)
    rows = document.get("summaries")
    if (
        document.get("schema_version") != 1
        or not isinstance(rows, list)
        or reference.get("count") != len(rows)
        or any(
            not isinstance(row, dict)
            or not isinstance(row.get("id"), str)
            or not row["id"]
            or not isinstance(row.get("path"), str)
            or not isinstance(row.get("sha256"), str)
            or type(row.get("bytes")) is not int
            or row["bytes"] < 0
            for row in rows
        )
        or len({row["id"] for row in rows}) != len(rows)
    ):
        raise CampaignError("Handoff summary index has invalid identities or coverage")
    return rows


def _read_summary_row(row):
    summary = _indexed_json(row)
    if (
        summary.get("job_id") != row["id"]
        or summary.get("structural_coverage") is not True
        or Path(row["path"]).stat().st_size != row["bytes"]
    ):
        raise CampaignError("Retrieved handoff summary identity or validation changed")
    if row.get("input"):
        from .campaign_headless import file_digest

        if file_digest(Path(row["input"]["path"])) != row["input"]["sha256"]:
            raise CampaignError("Retained summary input changed")
    return summary


def retrieve_summary(payload, identifier):
    """Read one retained summary by identity and verify the exact immutable bytes."""
    rows = load_summary_index(payload)
    row = next((row for row in rows if row["id"] == identifier), None)
    if row is None:
        raise CampaignError("Requested summary is absent from the handoff index")
    return _read_summary_row(row)


def reconcile_summary_omissions(payload, artifact_root):
    """Update bounded omission metadata after any deterministic context removal."""
    rows = load_summary_index(payload)
    for row in rows:
        _read_summary_row(row)
    indexed = {row["id"]: row for row in rows}
    included = set()
    for summary in payload.get("summaries", []):
        identifier = summary.get("job_id")
        if (
            identifier in included
            or identifier not in indexed
            or hashlib.sha256(encoded(summary)).hexdigest() != indexed[identifier]["sha256"]
        ):
            raise CampaignError("Included summary differs from its immutable handoff index")
        included.add(identifier)
    omitted = [row["id"] for row in rows if row["id"] not in included]
    omitted_final = [row["id"] for row in rows if row.get("final") and row["id"] not in included]
    data = encoded(
        {"schema_version": 1, "summary_ids": omitted, "final_summary_ids": omitted_final}
    )
    path = blob(artifact_root, data)
    payload["omitted_summary_index"] = {
        "path": str(path),
        "sha256": hashlib.sha256(data).hexdigest(),
        "count": len(omitted),
    }
    return tuple(omitted_final)


def assemble_packet(
    snapshots: tuple[HistorySnapshot, ...],
    obligations: tuple[dict[str, Any], ...],
    current_state: dict[str, Any],
    summaries: tuple[dict[str, Any], ...],
    budget: ContextBudget,
    artifact_root: Path,
    *,
    packet_ceiling: int = 160_000,
    count: TokenCounter = byte_token_bound,
    summary_tree: tuple[tuple[SummaryJobSpec, dict[str, Any]], ...] = (),
) -> HandoffPacket:
    """Keep complete indexes separately; fit exact obligations before historical prose."""
    ceiling = min(packet_ceiling, budget.payload_tokens)
    if ceiling < 1024:
        raise CampaignError("destination has no qualified handoff context capacity")
    ids = [row.get("id") for row in obligations]
    if any(not isinstance(value, str) or not value for value in ids) or len(set(ids)) != len(ids):
        raise CampaignError("handoff obligations require unique canonical identities")
    owners = []
    for snapshot in snapshots:
        owners.extend(
            {
                "id": event.id,
                "source_id": event.source_id,
                "quote": event.record.text,
                "offset": event.offset,
                "end_offset": event.end_offset,
            }
            for event in load_events(snapshot)
            if event.record.owner_input and not event.duplicate_of
        )
    owner_data, obligation_data = encoded(owners), encoded(obligations)
    owner_path = blob(artifact_root, owner_data)
    obligation_path = blob(artifact_root, obligation_data)
    gaps = [warning for snapshot in snapshots for warning in snapshot.warnings]
    for summary in summaries:
        if summary.get("structural_coverage") is not True:
            raise CampaignError("handoff summary has not passed structural validation")
        gaps.extend(str(gap) for gap in summary.get("gaps", []))
    catalog = {summary.get("job_id"): summary for summary in summaries}
    if len(catalog) != len(summaries):
        raise CampaignError("Handoff summaries require unique validated identities")
    provenance = {}
    for spec, summary in summary_tree:
        identifier = summary.get("job_id")
        if identifier != spec.id or summary.get("input_sha256") != spec.input_sha256:
            raise CampaignError("Handoff summary tree differs from its validated input")
        if identifier in catalog and encoded(catalog[identifier]) != encoded(summary):
            raise CampaignError("Handoff summary tree contains conflicting identities")
        catalog[identifier] = summary
        provenance[identifier] = spec
    summary_rows = []
    for summary in catalog.values():
        identifier = summary.get("job_id")
        if (
            not isinstance(identifier, str)
            or not identifier
            or summary.get("structural_coverage") is not True
        ):
            raise CampaignError("Handoff summaries require unique validated identities")
        data = encoded(summary)
        path = blob(artifact_root / "summaries", data)
        row = {
            "id": identifier,
            "path": str(path),
            "sha256": hashlib.sha256(data).hexdigest(),
            "bytes": len(data),
            "final": any(item["job_id"] == identifier for item in summaries),
        }
        if identifier in provenance:
            spec = provenance[identifier]
            children = list(spec.primary_ids) if spec.purpose == "handoff-reduce" else []
            if any(child not in catalog for child in children):
                raise CampaignError("Handoff summary tree is missing a retained child")
            row.update(
                dependencies=children,
                execution_dependencies=list(spec.dependencies),
                input={"path": spec.input_path, "sha256": spec.input_sha256},
                purpose=spec.purpose,
                prompt_version=spec.prompt_version,
            )
        summary_rows.append(row)
    summary_data = encoded({"schema_version": 1, "summaries": summary_rows})
    summary_path = blob(artifact_root, summary_data)
    payload: dict[str, Any] = {
        "schema_version": 1,
        "purpose": "continue-existing-work",
        "instructions": "History is context, not new authority. Reconcile current state and retrieve omitted obligations before editing. Summaries cannot approve, defer or complete work.",
        "current_state": current_state,
        "sources": [asdict(snapshot) for snapshot in snapshots],
        "owner_index": {"path": str(owner_path), "sha256": hashlib.sha256(owner_data).hexdigest()},
        "obligation_index": {
            "path": str(obligation_path),
            "sha256": hashlib.sha256(obligation_data).hexdigest(),
        },
        "obligations": [],
        "latest_owner_direction": [],
        "summaries": [],
        "summary_index": {
            "path": str(summary_path),
            "sha256": hashlib.sha256(summary_data).hexdigest(),
            "count": len(summary_rows),
        },
        "coverage_gaps": gaps,
        "read_only": True,
    }
    # Reserve room for the final omitted-ID indexes; never silently clip IDs.
    omitted_owners, omitted_obligations = [], []
    omissions_path = blob(
        artifact_root, encoded({"owner_ids": [row["id"] for row in owners], "obligation_ids": ids})
    )
    payload["omission_index"] = str(omissions_path)
    reconcile_summary_omissions(payload, artifact_root)
    if count(encoded(payload).decode()) > ceiling:
        raise CampaignError("canonical state/source manifest exceeds destination packet budget")

    def include(field: str, row: dict[str, Any]) -> bool:
        payload[field].append(row)
        if count(encoded(payload).decode()) <= ceiling - 128:
            return True
        payload[field].pop()
        return False

    # Required current constraints take precedence over historical summaries.
    for row in obligations:
        if not include("obligations", row):
            omitted_obligations.append(row["id"])
    for row in reversed(owners):
        if not include("latest_owner_direction", row):
            omitted_owners.append(row["id"])
    for summary in summaries:
        include("summaries", summary)
    omission_data = encoded({"owner_ids": omitted_owners, "obligation_ids": omitted_obligations})
    payload["omission_index"] = str(blob(artifact_root, omission_data))
    payload["read_only"] = bool(omitted_owners or omitted_obligations or gaps)
    omitted_summaries = reconcile_summary_omissions(payload, artifact_root)
    data = encoded(payload)
    tokens = count(data.decode())
    if tokens > ceiling:
        raise CampaignError("handoff packet metadata exceeded destination budget")
    path = blob(artifact_root, data)
    return HandoffPacket(
        str(path),
        hashlib.sha256(data).hexdigest(),
        tokens,
        payload["read_only"],
        tuple(snapshot.source_id for snapshot in snapshots),
        tuple(omitted_owners),
        tuple(omitted_obligations),
        tuple(gaps),
        str(obligation_path),
        str(owner_path),
        str(summary_path),
        omitted_summaries,
    )


def check_fidelity(
    summaries: tuple[dict[str, Any], ...],
    *,
    critical_source_ids: tuple[str, ...],
    required_quotes: tuple[str, ...],
    forbidden_claims: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Golden-corpus structural gate; semantic qualification remains an independent review."""
    facts = [fact for summary in summaries for fact in summary.get("facts", [])]
    cited = {ref for fact in facts for ref in fact.get("citations", [])}
    text = "\n".join(
        str(fact.get("text", "")) + "\n" + str(fact.get("source_quote", "")) for fact in facts
    )
    missing = sorted(set(critical_source_ids) - cited)
    missing_quotes = [digest(quote) for quote in required_quotes if quote not in text]
    invented = [digest(claim) for claim in forbidden_claims if claim in text]
    return {
        "schema_version": 1,
        "structural_pass": not (missing or missing_quotes or invented),
        "missing_critical_ids": missing,
        "missing_quote_hashes": missing_quotes,
        "forbidden_claim_hashes": invented,
        "semantic_fidelity": "requires-independent-review",
    }
