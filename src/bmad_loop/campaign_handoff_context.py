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


class HistoryContextRecoveryNeeded(CampaignError):
    """The bounded initial view cannot carry a useful complete historical fact."""


class AuditedConstraintsTooLarge(HistoryContextRecoveryNeeded):
    """Owner constraints fit only after their existing independently audited reduction."""


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


def _history_facts(payload):
    """Read final nodes only; intermediate evidence remains in the complete index."""
    result = {}
    for row in load_summary_index(payload):
        if not row.get("final"):
            continue
        summary = _read_summary_row(row)
        for fact in summary.get("facts", []):
            identity = (row["id"], fact.get("id"))
            if not isinstance(identity[1], str) or not identity[1] or identity in result:
                raise CampaignError("Retained summary facts require unique validated identities")
            result[identity] = {
                "summary_id": row["id"],
                "summary_sha256": row["sha256"],
                "fact_id": fact["id"],
                "kind": fact["kind"],
                "text": fact["text"],
                "authority": "context",
                "semantic_fidelity": "unqualified",
            }
    return result


def _fact_omissions(facts, selected):
    included = {(row["summary_id"], row["fact_id"]) for row in selected}
    return {
        "schema_version": 1,
        "omitted": [
            {"summary_id": parent, "fact_id": fact}
            for parent, fact in facts
            if (parent, fact) not in included
        ],
    }


def _fact_index_reference(root, content):
    data = encoded(content)
    sha256 = hashlib.sha256(data).hexdigest()
    return {
        "path": str(root / (sha256 + ".json")),
        "sha256": sha256,
        "count": len(content["omitted"]),
    }


def validate_history_view(payload):
    """Bind every selected/omitted fact to its unchanged complete parent artifact."""
    if "history_view_version" not in payload:
        if "history_view" in payload or "omitted_history_fact_index" in payload:
            raise CampaignError("Handoff history view version is missing")
        return  # Legacy packets retain their original complete-summary format.
    if (
        type(payload["history_view_version"]) is not int
        or payload["history_view_version"] != 1
        or payload.get("summaries") != []
        or payload.get("latest_owner_direction") != []
    ):
        raise CampaignError(
            "Partial history views cannot carry unaudited owner text or complete summaries"
        )
    facts = _history_facts(payload)
    selected = payload.get("history_view")
    if not isinstance(selected, list):
        raise CampaignError("Handoff history view is missing")
    seen = set()
    for row in selected:
        identity = (row.get("summary_id"), row.get("fact_id"))
        if identity in seen or facts.get(identity) != row:
            raise CampaignError("Handoff history view differs from its immutable parent fact")
        seen.add(identity)
    reference = payload.get("omitted_history_fact_index")
    if not isinstance(reference, dict):
        raise CampaignError("Handoff history fact omission index is missing")
    expected = _fact_omissions(facts, selected)
    if _indexed_json(reference) != expected or reference.get("count") != len(expected["omitted"]):
        raise CampaignError("Handoff history fact omission coverage changed")
    if facts and not selected:
        raise HistoryContextRecoveryNeeded("No useful complete history fact fits the successor")
    rows = load_summary_index(payload)
    omitted = _indexed_json(payload["omitted_summary_index"])
    if omitted != {
        "schema_version": 1,
        "summary_ids": [row["id"] for row in rows],
        "final_summary_ids": [row["id"] for row in rows if row.get("final")],
    }:
        raise CampaignError("Partial history view lost complete summary omission coverage")


def retrieve_history_fact(payload, summary_id, fact_id):
    """Retrieve exact provenance behind a selected or omitted bounded-view fact."""
    validate_history_view(payload)
    parent = retrieve_summary(payload, summary_id)
    found = next((row for row in parent.get("facts", []) if row.get("id") == fact_id), None)
    if found is None:
        raise CampaignError("Requested history fact is absent from its retained parent")
    return found


def mandatory_context_digest(payload):
    """Bind authority/current state independently of optional historical views."""
    optional = {
        "summaries",
        "latest_owner_direction",
        "summary_index",
        "omitted_summary_index",
        "history_view_version",
        "history_view",
        "omitted_history_fact_index",
        "history_compaction",
    }
    return digest({key: value for key, value in payload.items() if key not in optional})


def _verify_authority_references(references):
    seen = {}
    for reference in references:
        path, expected = reference.get("path"), reference.get("sha256")
        if not isinstance(path, str) or not isinstance(expected, str):
            raise CampaignError("Handoff authority reference is malformed")
        if path in seen and seen[path] != expected:
            raise CampaignError("Handoff authority reference has conflicting identities")
        seen[path] = expected
        if hashlib.sha256(Path(path).read_bytes()).hexdigest() != expected:
            raise CampaignError("Handoff authority evidence changed before continuation")


def bind_authority_evidence(payload, artifact_root, references):
    """Seal the exact audited inputs/results/indexes used by fresh assembly."""
    unique = list({(row["path"], row["sha256"]): row for row in references}.values())
    _verify_authority_references(unique)
    data = encoded({"schema_version": 1, "references": unique})
    path = blob(artifact_root / "authority", data)
    payload["authority_provenance"] = {
        "path": str(path),
        "sha256": hashlib.sha256(data).hexdigest(),
        "count": len(unique),
    }


def validate_authority_evidence(payload, expected_digest):
    """Read back every mandatory evidence leaf before releasing a writer."""
    if not expected_digest or mandatory_context_digest(payload) != expected_digest:
        raise CampaignError("Handoff mandatory context differs from its audited packet")
    reference = payload.get("authority_provenance")
    if not isinstance(reference, dict):
        raise CampaignError("Handoff authority provenance requires a fresh audited packet")
    document = _indexed_json(reference)
    rows = document.get("references")
    if (
        document.get("schema_version") != 1
        or not isinstance(rows, list)
        or reference.get("count") != len(rows)
    ):
        raise CampaignError("Handoff authority provenance index is malformed")
    _verify_authority_references(rows)


def fit_audited_history(payload, artifact_root, *, ceiling):
    """Fill only the bytes left after mandatory audited state; never clip a fact.

    The complete artifacts are independent of the initial context size. A partial
    view is context, not summary validation or permission to omit obligations.
    """
    payload["summaries"] = []
    payload["latest_owner_direction"] = []  # Exact audited constraints already retain these.
    payload["history_view_version"] = 1
    payload["history_view"] = []
    reconcile_summary_omissions(payload, artifact_root)
    facts = _history_facts(payload)
    index_root = artifact_root / "history-view"
    # Content hashes and their file names have fixed serialized width. Budget
    # this exact reference shape now; write/hash the potentially huge omitted
    # index once after selection, not once per candidate fact.
    payload["omitted_history_fact_index"] = {
        "path": str(index_root / ("0" * 64 + ".json")),
        "sha256": "0" * 64,
        "count": len(facts),
    }
    current_bytes = len(encoded(payload))
    if current_bytes > ceiling:
        canonical_only = {
            key: value
            for key, value in payload.items()
            if key not in {"resolved_constraints", "recovered_constraints"}
        }
        if len(encoded(canonical_only)) <= ceiling:
            raise AuditedConstraintsTooLarge(
                "Audited owner constraints exceed the complete packet budget"
            )
        raise HistoryContextRecoveryNeeded("Mandatory continuation context exceeds the successor")
    priorities = {
        kind: rank
        for rank, kind in enumerate(
            (
                "next_action",
                "contradiction",
                "question",
                "failure",
                "constraint",
                "file",
                "decision",
                "goal",
                "progress",
                "verification_claim",
                "owner_request",
                "nuance",
            )
        )
    }
    # Newer final nodes first, round-robin within each kind so one workstream
    # cannot consume every historical slot. No model decides what has authority.
    parents = list(dict.fromkeys(parent for parent, _ in facts))[::-1]
    grouped = {}
    for (parent, _), row in facts.items():
        grouped.setdefault((row["kind"], parent), []).append(row)
    for kind in priorities:
        queues = [grouped.get((kind, parent), [])[::-1] for parent in parents]
        for position in range(max((len(queue) for queue in queues), default=0)):
            for queue in queues:
                if position >= len(queue):
                    continue
                selected = payload["history_view"]
                omitted_count = len(facts) - len(selected)
                proposed_bytes = (
                    current_bytes
                    + len(encoded(queue[position]))
                    + bool(selected)
                    + len(str(omitted_count - 1))
                    - len(str(omitted_count))
                )
                if proposed_bytes <= ceiling:
                    selected.append(queue[position])
                    current_bytes = proposed_bytes
    content = _fact_omissions(facts, payload["history_view"])
    payload["omitted_history_fact_index"] = _fact_index_reference(index_root, content)
    blob(index_root, encoded(content))
    validate_history_view(payload)
    final_bytes = len(encoded(payload))
    if final_bytes != current_bytes or final_bytes > ceiling:
        raise CampaignError("Final history view metadata exceeded destination budget")


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
