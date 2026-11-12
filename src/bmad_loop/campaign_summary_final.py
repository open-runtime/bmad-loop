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

CLOSED_VERSIONS = ("history-final-v4", "history-final-v5", "history-final-v6")
VERSION = CLOSED_VERSIONS[-1]
PACKING = "whole-fact-required-rank-scan-v1"
EXCLUDED_REASON = "model-excluded-context"
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
        "schema_version": 2 + CLOSED_VERSIONS.index(version),
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
    result = {
        "type": "object",
        "additionalProperties": False,
        "required": ["schema_version", "job_id", "input_sha256", "covered_ids", field],
        "properties": {
            "schema_version": {"type": "integer", "enum": [2 + CLOSED_VERSIONS.index(version)]},
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
    if version == VERSION:
        for name in ("required_fact_ids", "excluded_fact_ids"):
            result["required"].append(name)
            result["properties"][name] = {
                "type": "array",
                "items": {"type": "string", **({"enum": list(ids)} if ids else {})},
                "maxItems": len(ids),
            }
    return result


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
    fields = {"schema_version", "job_id", "input_sha256", "covered_ids", field}
    if version == VERSION:
        fields.update(("required_fact_ids", "excluded_fact_ids"))
    if (
        not isinstance(result, dict)
        or set(result) != fields
        or type(result.get("schema_version")) is not int
        or result["schema_version"] != 2 + CLOSED_VERSIONS.index(version)
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
    if version == VERSION:
        _classifications(result, ordered)
    return indexed, ordered


def _classifications(result, ordered):
    groups = []
    known = set(ordered)
    for name in ("required_fact_ids", "excluded_fact_ids"):
        values = result.get(name)
        if (
            not isinstance(values, list)
            or not all(isinstance(value, str) and value in known for value in values)
            or len(values) != len(set(values))
        ):
            raise CampaignError("Final required/excluded facts must be unique ranked references")
        groups.append(set(values))
    required, excluded = groups
    if required & excluded:
        raise CampaignError("Final required and excluded references must be disjoint")
    return required, excluded


def _gaps(payload):
    return list(
        dict.fromkeys(gap for child in payload["children"] for gap in child["summary"]["gaps"])
    )


def _omission(ref, *, excluded=False):
    return {
        "catalog_id": ref["catalog_id"],
        "reason": EXCLUDED_REASON if excluded else "artifact-byte-budget",
    }


def _empty_ranked(result, payload, indexed, ordered, metadata, version):
    excluded = set(result["excluded_fact_ids"]) if version == VERSION else set()
    value = {
        **result,
        "facts": [],
        "gaps": _gaps(payload),
        "final_contract": version,
        "selected_fact_ids": [],
        "omitted_fact_ids": list(ordered),
        "omitted_facts": [
            _omission(indexed[identity], excluded=identity in excluded) for identity in ordered
        ],
        "packing": PACKING if version == VERSION else "whole-fact-rank-scan-v1",
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
    version: str = VERSION,
) -> int:
    """Exact ranked envelope before adding facts, not proof that required facts fit.

    For v6 preflight, all-excluded classification reserves the largest disjoint
    classification lists and omission records. Actual required facts are checked
    during expansion. Historical v5 accounting remains available explicitly.
    """
    if version == CLOSED_VERSIONS[0]:
        raise CampaignError("Final packing envelope requires a ranking contract")
    indexed, ordered = _validated_ids(result, payload, job_id, input_sha256, children, version)
    return len(encoded(_empty_ranked(result, payload, indexed, ordered, metadata, version)))


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
    """Render exact attributed claims; v6 packs required facts before optional context.

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
    required, excluded = _classifications(result, ordered) if version == VERSION else (set(), set())
    value = _empty_ranked(result, payload, indexed, ordered, metadata, version)
    size = len(encoded(value))
    if artifact_bytes and size > artifact_bytes:
        raise CampaignError("Final ranking envelope exceeds artifact byte limit")
    selected, facts = [], []
    omitted = {ref["catalog_id"]: ref for ref in value["omitted_facts"]}
    remaining = len(ordered)
    scan = [identity for identity in ordered if identity in required] + [
        identity for identity in ordered if identity not in required and identity not in excluded
    ]
    for identity in scan:
        fact = render(identity)
        omission = omitted[identity]
        # Two lists gain a member (facts/selected IDs), two lose one
        # (omission provenance/omitted IDs). Exact encoded IDs cancel.
        delta = (
            len(encoded(fact)) - len(encoded(omission)) + 2 * bool(selected) - 2 * (remaining > 1)
        )
        if delta < 0:
            raise CampaignError("Final omission provenance unexpectedly exceeds its full fact")
        if artifact_bytes and size + delta > artifact_bytes:
            if identity in required:
                raise CampaignError("Final required source facts exceed artifact byte limit")
            continue
        selected.append(identity)
        facts.append(fact)
        del omitted[identity]
        remaining -= 1
        size += delta
    value.update(
        facts=facts,
        selected_fact_ids=selected,
        omitted_fact_ids=list(omitted),
        omitted_facts=list(omitted.values()),
    )
    actual = len(encoded(value))
    if actual != size or artifact_bytes and actual > artifact_bytes:
        raise CampaignError("Final deterministic packing byte accounting differs")
    if version == VERSION:
        validate_packing(value)
    return value


def validate_packing(value: dict[str, Any]) -> None:
    """Check retained v6 packing/attribution; source truth still requires its audit.

    Exclusion describes context presentation, never owner approval, current
    control state or permission to discard the immutable source catalog.
    """
    if value.get("final_contract") != VERSION or value.get("packing") != PACKING:
        raise CampaignError("Final packing contract is not current v6")
    ordered, selected = value.get("ranked_fact_ids"), value.get("selected_fact_ids")
    if (
        not isinstance(ordered, list)
        or not all(_sha(identity) for identity in ordered)
        or len(ordered) != len(set(ordered))
    ):
        raise CampaignError("Final packed references lack unique ranked identities")
    known = set(ordered)
    if (
        not isinstance(selected, list)
        or not all(isinstance(identity, str) and identity in known for identity in selected)
        or len(selected) != len(set(selected))
    ):
        raise CampaignError("Final packed references lack unique ranked identities")
    required, excluded = _classifications(value, ordered)
    chosen = set(selected)
    if not required <= chosen or excluded & chosen:
        raise CampaignError("Final packing omitted required or displayed excluded source facts")
    expected = [identity for identity in ordered if identity in required] + [
        identity for identity in ordered if identity in chosen and identity not in required
    ]
    if selected != expected:
        raise CampaignError("Final packing changed required-first rank order")
    omitted = [identity for identity in ordered if identity not in chosen]
    if value.get("omitted_fact_ids") != omitted or value.get("omitted_facts") != [
        _omission({"catalog_id": identity}, excluded=identity in excluded) for identity in omitted
    ]:
        raise CampaignError("Final packing lost omission identities or classification reasons")
    facts = value.get("facts")
    if not isinstance(facts, list) or len(facts) != len(selected):
        raise CampaignError("Final packed facts differ from their selected references")
    for identity, fact in zip(selected, facts):
        if not isinstance(fact, dict):
            raise CampaignError("Final packed source claim is malformed")
        validate_attribution(fact, version=VERSION)
        if fact["selected_from"]["catalog_id"] != identity:
            raise CampaignError("Final packed source claim differs from its selected reference")
