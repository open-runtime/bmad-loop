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
from .campaign_transcripts import digest

VERSION = "history-final-v4"
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


def validate_attribution(fact: dict[str, Any]) -> None:
    """Validate the closed presentation shape; full source equality is checked at expansion."""
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
        or fact.get("id") != digest([VERSION, ref["catalog_id"]])
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


def input_payload(source: dict[str, Any], source_ref: dict[str, str]) -> dict[str, Any]:
    """Complete source appears once; the selectable catalog adds only bound references."""
    return {
        "schema_version": 2,
        "citation_presentation": VERSION,
        "source_input": dict(source_ref),
        "children": copy.deepcopy(source["children"]),
        "owner_quotes": copy.deepcopy(source.get("owner_quotes", {})),
        "fact_catalog": catalog(source),
    }


def schema(
    job_id: str, input_sha256: str, children: tuple[str, ...], ids: tuple[str, ...]
) -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "schema_version",
            "job_id",
            "input_sha256",
            "covered_ids",
            "selected_fact_ids",
        ],
        "properties": {
            "schema_version": {"type": "integer", "enum": [2]},
            "job_id": {"type": "string", "enum": [job_id]},
            "input_sha256": {"type": "string", "enum": [input_sha256]},
            "covered_ids": {
                "type": "array",
                "items": {"type": "string", "enum": list(children)},
                "minItems": len(children),
                "maxItems": len(children),
            },
            "selected_fact_ids": {
                "type": "array",
                "items": {"type": "string", **({"enum": list(ids)} if ids else {})},
                "maxItems": len(ids),
            },
        },
    }


def schema_template() -> dict[str, Any]:
    return {
        "with_facts": schema("job", "input", ("child",), ("fact",)),
        "without_facts": schema("job", "input", ("child",), ()),
    }


def expand(
    result: dict[str, Any],
    payload: dict[str, Any],
    *,
    job_id: str,
    input_sha256: str,
    children: tuple[str, ...],
) -> dict[str, Any]:
    """Expand only known references, retaining exact attribution and all omitted identities."""
    indexed = catalog(payload)
    if payload.get("fact_catalog") != indexed or payload.get("citation_presentation") != VERSION:
        raise CampaignError("Final input catalog differs from its complete child sources")
    if tuple(child["job_id"] for child in payload["children"]) != children:
        raise CampaignError("Final child frontier differs from its request")
    if (
        not isinstance(result, dict)
        or set(result)
        != {"schema_version", "job_id", "input_sha256", "covered_ids", "selected_fact_ids"}
        or type(result.get("schema_version")) is not int
        or result["schema_version"] != 2
        or result.get("job_id") != job_id
        or result.get("input_sha256") != input_sha256
    ):
        raise CampaignError("Final selection has stale identity or model-written content")
    covered, selected = result.get("covered_ids"), result.get("selected_fact_ids")
    if (
        not isinstance(covered, list)
        or not all(isinstance(x, str) for x in covered)
        or len(covered) != len(set(covered))
        or set(covered) != set(children)
    ):
        raise CampaignError("Final selection omitted or duplicated child coverage")
    if (
        not isinstance(selected, list)
        or not all(isinstance(x, str) and x in indexed for x in selected)
        or len(selected) != len(set(selected))
    ):
        raise CampaignError("Final selection invented or duplicated a source fact")
    originals = {
        (child["job_id"], fact["id"]): fact
        for child in payload["children"]
        for fact in child["summary"]["facts"]
    }
    facts = []
    for identity in selected:
        ref = indexed[identity]
        fact = copy.deepcopy(originals[(ref["child_job_id"], ref["fact_id"])])
        fact.update(
            id=digest([VERSION, identity]),
            text=attribution_text(ref["child_job_id"], fact["text"]),
            authority="context",
            selected_from=dict(ref),
        )
        validate_attribution(fact)
        facts.append(fact)
    gaps = list(
        dict.fromkeys(gap for child in payload["children"] for gap in child["summary"]["gaps"])
    )
    return {
        **result,
        "facts": facts,
        "gaps": gaps,
        "final_contract": VERSION,
        "omitted_fact_ids": [identity for identity in indexed if identity not in set(selected)],
    }
