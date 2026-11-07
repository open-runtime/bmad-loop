"""Complete source-span coverage for native owner history.

Interpretations are context, never a replacement for the exact source. Every
character of every owner event remains in the successor's current constraint
view, including contextual questions and adopted quoted requirements. No current
producer supplies authenticated span-specific supersession/completion evidence;
therefore neither the model nor an event-level coverage flag can discard a span.
"""

from __future__ import annotations

import copy
import hashlib

from .campaign_model import CampaignError
from .campaign_transcripts import digest, encoded

SPAN_CHARS = 512
AUDIT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "handoff_id",
        "capture_digest",
        "covered_owner_ids",
        "covered_span_ids",
        "obligation_ids",
        "constraints",
        "ambiguities",
    ],
    "properties": {
        "handoff_id": {"type": "string"},
        "capture_digest": {"type": "string"},
        "covered_owner_ids": {"type": "array", "items": {"type": "string"}},
        "covered_span_ids": {"type": "array", "items": {"type": "string"}},
        "obligation_ids": {"type": "array", "items": {"type": "string"}},
        "ambiguities": {"type": "array", "items": {"type": "string"}},
        "constraints": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["span_id", "interpretation", "relevance", "exclusion_evidence_id"],
                "properties": {
                    "span_id": {"type": "string"},
                    "interpretation": {"type": "string", "minLength": 1},
                    "relevance": {"type": "string", "enum": ["current", "context"]},
                    "exclusion_evidence_id": {"type": "string", "enum": [""]},
                },
            },
        },
    },
}
PROMPT = (
    "Independently audit the history and summaries against canonical current work. "
    "Return exactly one constraint for every owner_spans span_id, with an interpretation "
    "covering ALL requirements, qualifiers, corrections and adopted quoted requirements in that span. "
    "Read adjacent spans as a continuous owner message; a split is not a semantic boundary. "
    "The controller preserves the exact text, so do not return source quotations. "
    "Use relevance=current for requirements and context for questions or background; BOTH are retained "
    "verbatim for the successor. No validated span-specific supersession or canonical-completion "
    "evidence is available, so exclusion_evidence_id must be empty and historical exclusion is forbidden. "
    "Earlier instructions and incomplete work cannot be discarded merely because they are old. "
    "Preserve canonical obligations. Never invent approval, waive acceptance, or treat an assistant/tool "
    "claim as owner authority. Clear unfinished work and unanswered status questions do not themselves "
    "make authorized continuation ambiguous. Report ambiguities only for genuine current scope/authority "
    "conflicts the supplied evidence cannot resolve. Return all covered_owner_ids, covered_span_ids and obligation_ids."
)


def owner_spans(events):
    """Stable character offsets cover all native text; UTF-8 hashes bind exact bytes."""
    result = []
    seen = set()
    for event in events:
        if not event.record.owner_input or event.id in seen or getattr(event, "duplicate_of", ""):
            continue
        seen.add(event.id)
        text = event.record.text
        start = 0
        while start < len(text) or (not text and start == 0):
            end = min(len(text), start + SPAN_CHARS)
            if end < len(text):
                newline = text.rfind("\n", start, end)
                if newline >= start:
                    end = newline + 1
            part = text[start:end]
            result.append(
                {
                    "span_id": "owner-span-" + digest([event.id, start, end]),
                    "source_id": event.id,
                    "start": start,
                    "end": end,
                    "text": part,
                    "sha256": hashlib.sha256(part.encode()).hexdigest(),
                }
            )
            if end == len(text):
                break
            start = end
    return result


def bound_schema(coordinates):
    schema = copy.deepcopy(AUDIT_SCHEMA)
    props = schema["properties"]
    for name in ("handoff_id", "capture_digest"):
        props[name]["enum"] = [coordinates[name]]
    for name, values in (
        ("covered_owner_ids", coordinates["owner_ids"]),
        ("covered_span_ids", [row["span_id"] for row in coordinates["owner_spans"]]),
        ("obligation_ids", coordinates["obligation_ids"]),
    ):
        props[name].update(minItems=len(values), maxItems=len(values))
        if values:
            props[name]["items"]["enum"] = values
    spans = [row["span_id"] for row in coordinates["owner_spans"]]
    props["constraints"].update(minItems=len(spans), maxItems=len(spans))
    if spans:
        props["constraints"]["items"]["properties"]["span_id"]["enum"] = spans
    return schema


def batches(spans, *, content, input_bound, output_bound):
    """Budget complete spans and mandatory result IDs; never clip a requirement."""
    result, current = [], []

    # An interpretation has no finite maximum, but reserve enough for the exact
    # span plus JSON/IDs. Native measured output still enforces the hard limit.
    def fits(rows):
        minimum_output = 1024 + sum(len(encoded(row)) + 768 for row in rows)
        return (
            len(encoded({**content, "owner_spans": rows})) <= input_bound
            and minimum_output <= output_bound
        )

    for span in spans:
        if not fits([span]):
            raise CampaignError("A complete owner span needs a larger qualified audit route")
        if current and not fits([*current, span]):
            result.append(current)
            current = []
        current.append(span)
    if current or not spans:
        if not fits(current):
            raise CampaignError("History audit context exceeds the qualified input/output bound")
        result.append(current)
    return result


def validate(result, coordinates, events):
    """Reconstruct all quotes locally and reject omission or invented exclusion."""
    canonical = {row["span_id"]: row for row in owner_spans(events)}
    spans = coordinates.get("owner_spans")
    if not isinstance(spans, list) or any(
        canonical.get(row.get("span_id")) != row for row in spans
    ):
        raise CampaignError("Handoff owner span catalog changed from exact native-owner sources")
    expected = {row["span_id"] for row in spans}
    if len(expected) != len(spans):
        raise CampaignError("Handoff owner span catalog contains duplicate coverage")
    covered = result.get("covered_span_ids")
    constraints = result.get("constraints")
    if (
        not isinstance(covered, list)
        or len(covered) != len(expected)
        or set(covered) != expected
        or not isinstance(constraints, list)
        or len(constraints) != len(expected)
        or {row.get("span_id") for row in constraints} != expected
    ):
        raise CampaignError("Handoff audit omitted or invented required owner span coverage")
    normalized = []
    for row in constraints:
        if (
            set(row) != {"span_id", "interpretation", "relevance", "exclusion_evidence_id"}
            or not isinstance(row.get("interpretation"), str)
            or not row["interpretation"].strip()
        ):
            raise CampaignError(
                "Handoff constraint requires an interpretation and controller-owned exact native-owner quote"
            )
        if (
            row.get("relevance") not in {"current", "context"}
            or row.get("exclusion_evidence_id") != ""
        ):
            raise CampaignError(
                "Historical exclusion lacks authenticated span-specific resolution evidence"
            )
        span = canonical[row["span_id"]]
        normalized.append(
            {
                **row,
                "source_id": span["source_id"],
                "source_quote": span["text"],
                "source_start": span["start"],
                "source_end": span["end"],
                "source_sha256": span["sha256"],
                "authority": "context-only",
            }
        )
    return {**result, "constraints": normalized}
