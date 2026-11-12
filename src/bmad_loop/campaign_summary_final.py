"""Closed final selection: models choose references; Python renders source claims.

No free-form model text becomes a final fact. Complete child results remain in
the immutable input, including facts omitted from the bounded presentation.
Owner quotations and current controller controls are separate mandatory context.
"""

from __future__ import annotations

import copy
import json
from typing import Any

from .campaign_model import CampaignError
from .campaign_transcripts import digest, encoded

CLOSED_VERSIONS = ("history-final-v4", "history-final-v5")
VERSION = CLOSED_VERSIONS[-1]
PACKING = "whole-fact-rank-scan-v1"
PRESENTATION = "historical-summary-claim"


def attribution_text(child_job_id: str, text: str) -> str:
    return f"Historical summary claim from child {child_job_id}: " + json.dumps(
        text, ensure_ascii=False
    )


def _identity(value: Any) -> bool:
    return isinstance(value, str) and bool(value)


def _sha(value: Any) -> bool:
    return (
        isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)
    )


def validate_attribution(fact: dict[str, Any], *, version: str = VERSION) -> None:
    """Validate the closed presentation shape; full source equality is checked at expansion."""
    if version not in CLOSED_VERSIONS:
        raise CampaignError("Unknown closed final attribution contract")
    ref = fact.get("selected_from")
    if (
        not isinstance(ref, dict)
        or set(ref)
        != {"catalog_id", "child_job_id", "child_result_sha256", "fact_id", "presentation"}
        or not _identity(ref.get("child_job_id"))
        or not _identity(ref.get("fact_id"))
        or not _sha(ref.get("child_result_sha256"))
        or ref.get("presentation") != PRESENTATION
        or ref.get("catalog_id")
        != digest([ref["child_job_id"], ref["child_result_sha256"], ref["fact_id"]])
        or fact.get("id") != digest([version, ref["catalog_id"]])
        or fact.get("authority") != "context"
        or not isinstance(fact.get("text"), str)
    ):
        raise CampaignError("Final source claim lacks immutable historical attribution")
    prefix = f"Historical summary claim from child {ref['child_job_id']}: "
    if not fact["text"].startswith(prefix):
        raise CampaignError("Final source claim lacks its historical quotation prefix")
    try:
        original = json.loads(fact["text"][len(prefix) :])
    except (ValueError, TypeError) as exc:
        raise CampaignError("Final historical claim is not an exact quoted string") from exc
    if (
        not isinstance(original, str)
        or attribution_text(ref["child_job_id"], original) != fact["text"]
    ):
        raise CampaignError("Final historical claim quotation changed")


def catalog(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Bind every full validated child result and fact, never its projected position."""
    children = payload.get("children")
    if not isinstance(children, list) or not children:
        raise CampaignError("Final selection requires complete child results")
    result: dict[str, dict[str, Any]] = {}
    jobs = set()
    for child in children:
        if not isinstance(child, dict) or set(child) != {"job_id", "summary"}:
            raise CampaignError("Final child source is malformed")
        job_id, summary = child["job_id"], child["summary"]
        if (
            not _identity(job_id)
            or job_id in jobs
            or not isinstance(summary, dict)
            or summary.get("job_id") != job_id
            or summary.get("structural_coverage") is not True
            or not isinstance(summary.get("facts"), list)
            or not isinstance(summary.get("gaps"), list)
            or not all(isinstance(gap, str) for gap in summary["gaps"])
        ):
            raise CampaignError("Final child lacks complete validated identity/coverage")
        jobs.add(job_id)
        result_sha = digest(summary)
        facts = set()
        for fact in summary["facts"]:
            if (
                not isinstance(fact, dict)
                or not _identity(fact.get("id"))
                or fact["id"] in facts
                or not _identity(fact.get("kind"))
                or not _identity(fact.get("text"))
                or fact.get("authority") != "context"
                or not isinstance(fact.get("citations"), list)
                or not fact["citations"]
                or not all(_identity(ref) for ref in fact["citations"])
            ):
                raise CampaignError("Final child fact lacks immutable source identity")
            facts.add(fact["id"])
            identity = digest([job_id, result_sha, fact["id"]])
            result[identity] = {
                "catalog_id": identity,
                "child_job_id": job_id,
                "child_result_sha256": result_sha,
                "fact_id": fact["id"],
                "presentation": PRESENTATION,
            }
    return result


def input_payload(
    source: dict[str, Any], source_ref: dict[str, str], *, version: str = VERSION
) -> dict[str, Any]:
    """Complete source appears once; the selectable catalog adds only bound references."""
    if version not in CLOSED_VERSIONS:
        raise CampaignError("Unknown closed final input contract")
    return {
        "schema_version": 2 if version == CLOSED_VERSIONS[0] else 3,
        "citation_presentation": version,
        "source_input": dict(source_ref),
        "children": copy.deepcopy(source["children"]),
        "owner_quotes": copy.deepcopy(source.get("owner_quotes", {})),
        "fact_catalog": catalog(source),
    }


def schema(
    job_id: str,
    input_sha256: str,
    children: tuple[str, ...],
    ids: tuple[str, ...],
    *,
    version: str = VERSION,
) -> dict[str, Any]:
    if version not in CLOSED_VERSIONS:
        raise CampaignError("Unknown closed final schema contract")
    legacy = version == CLOSED_VERSIONS[0]
    field = "selected_fact_ids" if legacy else "ranked_fact_ids"
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["schema_version", "job_id", "input_sha256", "covered_ids", field],
        "properties": {
            "schema_version": {"type": "integer", "enum": [2 if legacy else 3]},
            "job_id": {"type": "string", "enum": [job_id]},
            "input_sha256": {"type": "string", "enum": [input_sha256]},
            "covered_ids": {
                "type": "array",
                "items": {"type": "string", "enum": list(children)},
                "minItems": len(children),
                "maxItems": len(children),
            },
            field: {
                "type": "array",
                "items": {"type": "string", **({"enum": list(ids)} if ids else {})},
                **({} if legacy else {"minItems": len(ids)}),
                "maxItems": len(ids),
            },
        },
    }


def schema_template(*, version: str = VERSION) -> dict[str, Any]:
    return {
        "with_facts": schema("job", "input", ("child",), ("fact",), version=version),
        "without_facts": schema("job", "input", ("child",), (), version=version),
    }


def _validated_ids(result, payload, job_id, input_sha256, children, version):
    if version not in CLOSED_VERSIONS:
        raise CampaignError("Unknown closed final expansion contract")
    indexed = catalog(payload)
    if payload.get("fact_catalog") != indexed or payload.get("citation_presentation") != version:
        raise CampaignError("Final input catalog differs from its complete child sources")
    if tuple(child["job_id"] for child in payload["children"]) != tuple(children):
        raise CampaignError("Final child frontier differs from its request")
    legacy = version == CLOSED_VERSIONS[0]
    field = "selected_fact_ids" if legacy else "ranked_fact_ids"
    if (
        not isinstance(result, dict)
        or set(result) != {"schema_version", "job_id", "input_sha256", "covered_ids", field}
        or type(result.get("schema_version")) is not int
        or result["schema_version"] != (2 if legacy else 3)
        or result.get("job_id") != job_id
        or result.get("input_sha256") != input_sha256
    ):
        raise CampaignError("Final selection has stale identity or model-written content")
    covered, ordered = result.get("covered_ids"), result.get(field)
    if (
        not isinstance(covered, list)
        or not all(isinstance(x, str) for x in covered)
        or len(covered) != len(set(covered))
        or set(covered) != set(children)
    ):
        raise CampaignError("Final selection omitted or duplicated child coverage")
    if (
        not isinstance(ordered, list)
        or not all(isinstance(x, str) and x in indexed for x in ordered)
        or len(ordered) != len(set(ordered))
    ):
        raise CampaignError("Final selection invented or duplicated a source fact")
    if not legacy and set(ordered) != set(indexed):
        raise CampaignError("Final ranking must include every source fact exactly once")
    return indexed, ordered


def _gaps(payload):
    return list(
        dict.fromkeys(gap for child in payload["children"] for gap in child["summary"]["gaps"])
    )


def _omission(ref):
    return {"catalog_id": ref["catalog_id"], "reason": "artifact-byte-budget"}


def _empty_ranked(result, payload, indexed, ordered, metadata):
    value = {
        **result,
        "facts": [],
        "gaps": _gaps(payload),
        "final_contract": VERSION,
        "selected_fact_ids": [],
        "omitted_fact_ids": list(ordered),
        "omitted_facts": [_omission(indexed[identity]) for identity in ordered],
        "packing": PACKING,
    }
    if metadata:
        if set(metadata).intersection(value):
            raise CampaignError("Final normalization metadata overwrites source fields")
        value.update(copy.deepcopy(metadata))
    return value


def minimum_artifact_bytes(
    result: dict[str, Any],
    payload: dict[str, Any],
    *,
    job_id: str,
    input_sha256: str,
    children: tuple[str, ...],
    metadata: dict[str, Any] | None = None,
) -> int:
    """Exact v5 envelope with all omitted provenance and no displayed source facts."""
    indexed, ordered = _validated_ids(result, payload, job_id, input_sha256, children, VERSION)
    return len(encoded(_empty_ranked(result, payload, indexed, ordered, metadata)))


def expand(
    result: dict[str, Any],
    payload: dict[str, Any],
    *,
    job_id: str,
    input_sha256: str,
    children: tuple[str, ...],
    version: str = VERSION,
    artifact_bytes: int = 0,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Render exact attributed claims; v5 packs whole facts in model rank order.

    Each fact and omission is serialized once for additive list accounting. The
    final full serialization independently verifies the computed byte count.
    Passing this bound does not certify semantic fidelity of omitted context.
    """
    indexed, ordered = _validated_ids(result, payload, job_id, input_sha256, children, version)
    if type(artifact_bytes) is not int or artifact_bytes < 0:
        raise CampaignError("Invalid final artifact byte limit")
    originals = {
        (child["job_id"], fact["id"]): fact
        for child in payload["children"]
        for fact in child["summary"]["facts"]
    }

    def render(identity):
        ref = indexed[identity]
        fact = copy.deepcopy(originals[(ref["child_job_id"], ref["fact_id"])])
        fact.update(
            id=digest([version, identity]),
            text=attribution_text(ref["child_job_id"], fact["text"]),
            authority="context",
            selected_from=dict(ref),
        )
        validate_attribution(fact, version=version)
        return fact

    if version == CLOSED_VERSIONS[0]:
        selected = set(ordered)
        return {
            **result,
            "facts": [render(identity) for identity in ordered],
            "gaps": _gaps(payload),
            "final_contract": version,
            "omitted_fact_ids": [identity for identity in indexed if identity not in selected],
        }
    value = _empty_ranked(result, payload, indexed, ordered, metadata)
    size = len(encoded(value))
    if artifact_bytes and size > artifact_bytes:
        raise CampaignError("Final ranking envelope exceeds artifact byte limit")
    selected, facts, omitted = [], [], []
    remaining = len(ordered)
    for identity in ordered:
        fact = render(identity)
        omission = _omission(indexed[identity])
        # Two lists gain a member (facts/selected IDs), two lose one
        # (omission provenance/omitted IDs). Exact encoded IDs cancel.
        delta = (
            len(encoded(fact)) - len(encoded(omission)) + 2 * bool(selected) - 2 * (remaining > 1)
        )
        if delta < 0:
            raise CampaignError("Final omission provenance unexpectedly exceeds its full fact")
        if artifact_bytes and size + delta > artifact_bytes:
            omitted.append(omission)
            continue
        selected.append(identity)
        facts.append(fact)
        remaining -= 1
        size += delta
    value.update(
        facts=facts,
        selected_fact_ids=selected,
        omitted_fact_ids=[ref["catalog_id"] for ref in omitted],
        omitted_facts=omitted,
    )
    actual = len(encoded(value))
    if actual != size or artifact_bytes and actual > artifact_bytes:
        raise CampaignError("Final deterministic packing byte accounting differs")
    return value
