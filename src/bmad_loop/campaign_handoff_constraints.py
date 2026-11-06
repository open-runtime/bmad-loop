"""Source-linked compression of already audited continuation constraints.

Each reduction is independently reviewed against its complete immediate input.
The immutable tree retains exact original quotations; reduced text remains
context and cannot waive canonical acceptance or confer approval.
"""

from __future__ import annotations

from .campaign_model import CampaignError
from .campaign_transcripts import digest, encoded

REDUCTION_SCHEMA = {
    "type": "object",
    "required": ["handoff_id", "capture_digest", "covered_ids", "groups", "ambiguities"],
    "properties": {
        "handoff_id": {"type": "string"},
        "capture_digest": {"type": "string"},
        "covered_ids": {"type": "array", "items": {"type": "string"}},
        "ambiguities": {"type": "array", "items": {"type": "string"}},
        "groups": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["text", "refs"],
                "properties": {
                    "text": {"type": "string", "minLength": 1},
                    "refs": {"type": "array", "minItems": 1, "items": {"type": "string"}},
                },
            },
        },
    },
}

AUDIT_SCHEMA = {
    "type": "object",
    "required": ["handoff_id", "capture_digest", "covered_ids", "verdict", "ambiguities"],
    "properties": {
        "handoff_id": {"type": "string"},
        "capture_digest": {"type": "string"},
        "covered_ids": {"type": "array", "items": {"type": "string"}},
        "verdict": {"type": "string", "enum": ["faithful", "insufficient"]},
        "ambiguities": {"type": "array", "items": {"type": "string"}},
    },
}


def partition(rows, bound, *, max_nodes=None):
    """Never split a quoted obligation or silently omit a source node."""
    groups, current = [], []
    for row in rows:
        if len(encoded([row])) > bound:
            raise CampaignError("A current constraint needs a larger qualified reduction route")
        if current and (
            len(encoded([*current, row])) > bound or (max_nodes and len(current) >= max_nodes)
        ):
            groups.append(current)
            current = []
        current.append(row)
    if current:
        groups.append(current)
    return groups


def original_nodes(audits):
    return list(
        {
            digest(row): {"id": digest(row), "text": row["interpretation"], "original": row}
            for audit in audits
            for row in audit["constraints"]
            if row["relevance"] == "current"
        }.values()
    )


def validate(result, inputs, *, audit=False):
    expected = {row["id"] for row in inputs["nodes"]}
    if any(result.get(key) != inputs[key] for key in ("handoff_id", "capture_digest")):
        raise CampaignError("Constraint compression belongs to another current capture")
    if (
        len(result.get("covered_ids", [])) != len(expected)
        or set(result["covered_ids"]) != expected
    ):
        raise CampaignError("Constraint compression omitted or invented source coverage")
    if result.get("ambiguities"):
        raise CampaignError("Current constraint compression remains ambiguous")
    if audit:
        if result.get("verdict") != "faithful":
            raise CampaignError("Independent constraint audit did not establish fidelity")
        return {**result, "validated": True, "authority": "context-only"}
    groups = []
    covered = set()
    for row in result.get("groups", []):
        if not row.get("text") or not row.get("refs") or not set(row["refs"]) <= expected:
            raise CampaignError("Constraint group has missing text or unbound source references")
        covered.update(row["refs"])
        groups.append({**row, "id": digest(row), "authority": "context-only"})
    if covered != expected:
        raise CampaignError("Constraint groups dropped an input obligation")
    return {**result, "groups": groups, "validated": True, "authority": "context-only"}
