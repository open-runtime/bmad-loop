"""Validate a late admission answer against the inventory its reviewer actually received."""

import hashlib
import json
from pathlib import Path

from .campaign_admission_files import read_json
from .campaign_discovery import CoverageReceipt, validate_overlap, validate_overlap_evidence
from .campaign_model import CampaignError
from .campaign_stage_prompt import context_artifacts


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
    ):
        raise CampaignError("Admission review lacks its exact original inventory and PR")
    if not inventory.get("inline_projection"):
        original = CoverageReceipt(
            **{key: value for key, value in inventory.items() if key != "digest"}
        )
        if original.digest != digest:
            raise CampaignError("Original admission inventory content changed")
        validate_overlap(disposition, original, owned_pr=owned_pr)
        return
    # Large prompts carry only a search view. The controller's original
    # immutable index records every available evidence ID directly.
    descriptor = context["retrievable_context"]["index"]
    if descriptor not in request.get("context_artifacts", []):
        raise CampaignError("Admission evidence index is not bound to this launch")
    directory = Path(request["execution"]).parent
    (index,) = context_artifacts(directory, [descriptor])
    if index.path.name != "stage-context-index.json":
        raise CampaignError("Admission evidence index has an unexpected identity")
    data = read_json(index.path, limit=index.size_bytes)
    encoded = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    if hashlib.sha256(encoded).hexdigest() != index.sha256:
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
