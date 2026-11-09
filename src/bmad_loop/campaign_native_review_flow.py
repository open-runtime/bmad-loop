"""Optional native main/custom reviews supplement independent publication review.

The pair is a separate read-only stage, with exact-candidate evidence. It never
supplies the independent reviewer identity or discharges acceptance criteria.
"""

from __future__ import annotations

from .campaign_model import CampaignError


def required(snapshot) -> bool:
    return "native_review_pair" in dict(snapshot.spec.roles)


def current(snapshot, unit) -> bool:
    receipt = unit.evidence.get("native_review_pair", {})
    integrated = unit.evidence.get("integrated", {})
    coordinates = receipt.get("validated", {}).get("coordinates", {})
    proof = receipt.get("validated_native_review_pair", {})
    expected = {
        "campaign": snapshot.id,
        "unit": unit.id,
        "stage": "native_review_pair",
        "graph_revision": snapshot.graph_revision,
        "config_revision": snapshot.config_revision,
        "head": integrated.get("head"),
        "base": integrated.get("base"),
        "source_revision": unit.bindings.get("source_revision", ""),
    }
    return bool(
        integrated.get("head")
        and integrated.get("base")
        and receipt.get("launch_id")
        and receipt.get("head") == integrated["head"]
        and all(coordinates.get(key) == value for key, value in expected.items())
        and proof.get("target") == {"head": integrated["head"], "base": integrated["base"]}
        and proof.get("graph_revision") == snapshot.graph_revision
        and proof.get("config_revision") == snapshot.config_revision
        and proof.get("native_binding")
        and proof.get("native_review_evidence")
    )


def accepted(snapshot, unit) -> bool:
    return not required(snapshot) or (
        current(snapshot, unit)
        and unit.evidence["native_review_pair"].get("validated", {}).get("verdict") == "approved"
    )


def require(snapshot, unit) -> None:
    if not accepted(snapshot, unit):
        raise CampaignError(
            "Required native main/custom review pair lacks current approved evidence"
        )
