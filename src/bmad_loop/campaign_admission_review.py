"""Validate admission against its issued evidence and the current repository observation."""

import hashlib
import json
import math
from dataclasses import asdict, replace
from pathlib import Path

from .campaign_admission_files import read_json
from .campaign_discovery import (
    INVENTORY_REFRESH_SECONDS,
    CoverageReceipt,
    validate_overlap,
    validate_overlap_evidence,
)
from .campaign_model import CampaignError
from .campaign_stage_prompt import context_artifacts


def _digest(value):
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _authority(unit_spec, pr, workers, workspace, branch, coordinates):
    return _digest([unit_spec, pr, workers, workspace, branch, coordinates])


def validate_original(request, disposition, *, owned_pr):
    coordinates = request["coordinates"]
    prompt = request["prompt"]
    # Stage packets may have a single installed-skill instruction before the JSON.
    packet = json.loads(prompt[prompt.index("{") :])
    context = packet["context"]
    inventory = context["inventory"]
    digest = coordinates["inventory_digest"]
    if (
        packet.get("coordinates") != coordinates
        or context.get("inventory_digest") != digest
        or inventory.get("digest") != digest
        or context.get("owned_pr", {}).get("number") != owned_pr
        or context["unit"]["id"] != disposition.unit_id
    ):
        raise CampaignError("Admission review lacks its exact original inventory and PR")
    if not inventory.get("inline_projection"):
        original = CoverageReceipt(
            **{key: value for key, value in inventory.items() if key != "digest"}
        )
        if original.digest != digest:
            raise CampaignError("Original admission inventory content changed")
        validate_overlap(disposition, original, owned_pr=owned_pr)
        workers = [_digest(row) for row in context["external_ownership"]]
    else:
        # The immutable index binds every original evidence ID and every external
        # owner record, including owners omitted from the inline search view.
        descriptor = context["retrievable_context"]["index"]
        if descriptor not in request.get("context_artifacts", []):
            raise CampaignError("Admission evidence index is not bound to this launch")
        directory = Path(request["execution"]).parent
        (index,) = context_artifacts(directory, [descriptor])
        if index.path.name != "stage-context-index.json":
            raise CampaignError("Admission evidence index has an unexpected identity")
        data = read_json(index.path, limit=index.size_bytes)
        if _digest(data) != index.sha256:
            raise CampaignError("Original admission evidence index changed")
        available = frozenset(
            row["evidence_id"] for row in data["records"] if row["key"] == "available_evidence_ids"
        )
        validate_overlap_evidence(
            disposition,
            inventory_digest=digest,
            complete=inventory["complete"],
            available=available,
            owned_pr=owned_pr,
        )
        rows = [row for row in data["records"] if row["key"] == "external_ownership"]
        if data["layout"].get("external_ownership") != {"kind": "array", "count": len(rows)} or any(
            row["position"] != index for index, row in enumerate(rows)
        ):
            raise CampaignError("Original admission external ownership coverage is incomplete")
        workers = [row["sha256"] for row in rows]
    return {
        "observed_at": inventory["observed_at"],
        "authority": _authority(
            context["unit"],
            context["owned_pr"],
            workers,
            request["workspace"],
            request["branch"],
            {key: value for key, value in coordinates.items() if key != "inventory_digest"},
        ),
    }


def inventory_status(snapshot, receipt, *, now):
    """Require one authenticated, fresh receipt for the original admission subject."""
    answer = receipt.get("validated_admission")
    if not answer:
        return "unvalidated"
    frozen = receipt.get("admission_reviewed_observation") or {}
    unit = snapshot.units.get(answer["unit_id"])
    if not unit or frozen.get("authority") != _authority(
        asdict(unit.spec),
        unit.bindings.get("pr"),
        [_digest(row) for row in snapshot.bindings.get("external_workers", [])],
        unit.bindings.get("workspace"),
        unit.bindings.get("branch"),
        {
            "campaign": snapshot.id,
            "unit": unit.id,
            "stage": "admission_review",
            "base": unit.evidence.get("integrated", {}).get("base", unit.bindings.get("base")),
            "head": unit.bindings.get("head"),
            "source_revision": unit.bindings.get("source_revision", ""),
            "graph_revision": snapshot.graph_revision,
            "config_revision": snapshot.config_revision,
        },
    ):
        return "changed"
    raw = snapshot.bindings["inventory"]
    reviewed_at = frozen.get("observed_at")
    current_at = raw.get("observed_at")
    if (
        raw.get("complete") is not True
        or not all(
            type(value) is int or (type(value) is float and math.isfinite(value))
            for value in (reviewed_at, current_at, now)
        )
        or not reviewed_at <= current_at <= now
        or now - current_at > INVENTORY_REFRESH_SECONDS
    ):
        return "awaiting_inventory"
    try:
        current = CoverageReceipt(**{key: value for key, value in raw.items() if key != "digest"})
    except (TypeError, ValueError):
        return "awaiting_inventory"
    if current.digest != raw.get("digest"):
        return "awaiting_inventory"
    reviewed_digest = answer["inventory_digest"]
    # Substituting the original timestamp must reproduce its exact sealed digest.
    # All other fields, including nested native object revisions, stay bound.
    if (
        current.digest == reviewed_digest
        or replace(current, observed_at=reviewed_at).digest == reviewed_digest
    ):
        return "current"
    return "changed"


def refresh_wait(snapshot, *, now):
    from .campaign_supervisor import Observation

    return Observation(
        "waiting",
        {"admission_inventory_wait": snapshot.bindings["inventory"].get("digest", "")},
        "Retained admission review awaits a fresh complete inventory observation",
        now + 5,
    )
