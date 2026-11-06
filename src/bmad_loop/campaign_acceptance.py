"""Criterion-level proof tied to an executed recipe and exact candidate.

Reviewers propose relevance mappings, never command results. The controller
validates those mappings against independently collected verifier receipts.
Scope reductions are separate, provenance-bearing controller transactions;
creating a follow-up or writing approved prose does not discharge a criterion.
"""

from __future__ import annotations

import hashlib
import json
import platform
from pathlib import Path
from typing import Any

from .campaign_model import CampaignError


def digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def criteria(unit_id: str, texts) -> list[dict[str, Any]]:
    counts = {}
    rows = []
    for text in texts:
        counts[text] = counts.get(text, 0) + 1
        rows.append(
            {
                "id": "ac-" + digest([unit_id, text, counts[text]])[:24],
                "text": text,
                "required": True,
            }
        )
    return rows


def ensure(unit) -> dict[str, Any]:
    """Retain original obligations across unqualified changes to the plan text."""
    ledger = unit.evidence.setdefault(
        "acceptance",
        {"schema_version": 1, "revision": 0, "criteria": [], "amendments": [], "history": []},
    )
    known = {row["id"] for row in ledger["criteria"]}
    for row in criteria(unit.id, unit.spec.acceptance):
        if row["id"] not in known:
            ledger["criteria"].append(
                {**row, "status": "not_run", "reason": "required proof not recorded"}
            )
            ledger["revision"] += 1
    return ledger


def authorize_amendment(
    unit,
    *,
    expected_revision: int,
    before: tuple[str, ...],
    after: tuple[str, ...],
    authority: dict[str, Any],
) -> dict[str, Any]:
    """Apply an already authorized controller decision; never accept model authority.

    Callers must supply a controller-observed owner/delegation decision. A
    delivered planning record alone is not scope-reduction authority. The
    authority and exact previous obligations remain in the ledger.
    """
    ledger = unit.evidence.get("acceptance") or ensure(unit)
    if ledger["revision"] != expected_revision:
        raise CampaignError("acceptance amendment revision changed")
    if authority.get("kind") not in {"owner-decision", "delegated-owner-decision"} or not all(
        isinstance(authority.get(key), str) and authority[key] for key in ("id", "source", "sha256")
    ):
        raise CampaignError("acceptance amendment lacks controller-observed scope authority")
    previous = [row for row in ledger["criteria"] if row["required"]]
    if [row["text"] for row in previous] != list(before) or tuple(unit.spec.acceptance) != after:
        raise CampaignError("acceptance amendment does not name exact old and accepted new scope")
    active = {row["id"] for row in criteria(unit.id, after)}
    for row in previous:
        if row["id"] not in active:
            row.update(required=False, amendment=authority["id"])
    ledger["amendments"].append(
        {
            "before": list(before),
            "after": list(after),
            "authority": dict(authority),
            "revision": ledger["revision"],
        }
    )
    ledger["revision"] += 1
    return ensure(unit)


def provenance(workspace: Path, head: str, recipe, *, config_revision: str) -> dict[str, Any]:
    environment = {
        "system": platform.system(),
        "release": platform.release(),
        "machine": platform.machine(),
        "python": platform.python_version(),
        "cwd": str(workspace.resolve()),
    }
    return {
        "schema_version": 1,
        "head": head,
        "config_revision": config_revision,
        "recipe": recipe,
        "recipe_digest": digest(recipe),
        "environment": environment,
        "environment_digest": digest(environment),
    }


def context(unit) -> dict[str, Any]:
    ledger = ensure(unit)
    return {
        "schema_version": 1,
        "revision": ledger["revision"],
        "criteria": [
            {key: row[key] for key in ("id", "text", "required")}
            for row in ledger["criteria"]
            if row["required"]
        ],
        "verification_launch_id": unit.evidence.get("verification", {}).get("launch_id", ""),
    }


def evaluate(unit, candidate, *, review=None) -> dict[str, Any]:
    """Return proof states; missing, failed and stale evidence can never pass."""
    ledger = ensure(unit)
    verification = unit.evidence.get("verification", {})
    review = review if review is not None else unit.evidence.get("review", {})
    mapping = review.get("artifact", {}).get("acceptance_evidence", [])
    if not isinstance(mapping, list):
        raise CampaignError("acceptance_evidence must be a list")
    by_id = {}
    known = {row["id"] for row in ledger["criteria"]}
    for row in mapping:
        if (
            not isinstance(row, dict)
            or row.get("criterion_id") not in known
            or row["criterion_id"] in by_id
        ):
            raise CampaignError("acceptance evidence has unknown or duplicate criterion identity")
        by_id[row["criterion_id"]] = row
    proof = verification.get("proof", {})
    commands = verification.get("commands", [])
    recorded_recipe = [row.get("argv", row.get("command")) for row in commands]
    coordinates = review.get("validated", {}).get("coordinates", {})
    fresh = (
        verification.get("head") == candidate.head
        and proof.get("head") == candidate.head
        and proof.get("config_revision") == candidate.config_revision
        and proof.get("recipe_digest") == digest(proof.get("recipe"))
        and proof.get("recipe") == recorded_recipe
        and bool(proof.get("environment"))
        and proof.get("environment_digest") == digest(proof.get("environment"))
        and review.get("head") == candidate.head
        and all(
            coordinates.get(key) == value
            for key, value in (
                ("head", candidate.head),
                ("base", candidate.base),
                ("graph_revision", candidate.graph_revision),
                ("source_revision", candidate.source_revision),
            )
        )
        and str(coordinates.get("config_revision")) == candidate.config_revision
    )
    rows = []
    for criterion in ledger["criteria"]:
        row = {
            **criterion,
            "status": "not_run",
            "reason": "required proof not recorded",
            "evidence": [],
        }
        claim = by_id.get(row["id"])
        if claim:
            indexes = claim.get("command_indexes", [])
            if (
                not isinstance(indexes, list)
                or any(
                    type(index) is not int or index < 0 or index >= len(commands)
                    for index in indexes
                )
                or len(set(indexes)) != len(indexes)
            ):
                raise CampaignError("criterion cites missing or duplicate verification commands")
            if claim.get("status") not in {"passed", "failed", "not_run"}:
                raise CampaignError("criterion has an unsupported evidence status")
            if claim.get("verification_launch_id") != verification.get(
                "launch_id"
            ) or not verification.get("launch_id"):
                raise CampaignError("criterion cites a different verification attempt")
            if claim["status"] == "not_run" or not indexes:
                row["reason"] = "review reports missing executable proof"
            elif not fresh:
                row.update(
                    status="stale",
                    reason="verification environment, recipe, configuration or candidate is stale",
                )
            elif any(
                commands[index].get("returncode") != 0
                or commands[index].get("timed_out")
                or commands[index].get("interrupted")
                or commands[index].get("spawn_error")
                for index in indexes
            ):
                row.update(
                    status="failed", reason="cited verification command failed or did not execute"
                )
            elif claim["status"] == "failed":
                row.update(status="failed", reason="review found criterion unsatisfied")
            elif (
                verification.get("outcome") == "verified"
                and review.get("validated", {}).get("verdict") == "approved"
                and isinstance(claim.get("reason"), str)
                and claim["reason"].strip()
            ):
                row.update(
                    status="passed",
                    reason=claim["reason"],
                    evidence=[
                        {
                            "verification_launch_id": verification["launch_id"],
                            "command_index": index,
                            "command_digest": digest(commands[index]),
                            "head": candidate.head,
                            "recipe_digest": proof["recipe_digest"],
                            "environment_digest": proof["environment_digest"],
                            "review_launch_id": review.get("launch_id", ""),
                        }
                        for index in indexes
                    ],
                )
        rows.append(row)
    return {
        "schema_version": 1,
        "revision": ledger["revision"],
        "candidate": {
            "head": candidate.head,
            "base": candidate.base,
            "graph_revision": candidate.graph_revision,
            "config_revision": candidate.config_revision,
        },
        "criteria": rows,
        "complete": all(not row["required"] or row["status"] == "passed" for row in rows),
    }


def record(unit, candidate, *, review=None) -> dict[str, Any]:
    result = evaluate(unit, candidate, review=review)
    ledger = ensure(unit)
    if ledger.get("current") and ledger["current"] != result:
        ledger["history"].append(ledger["current"])
    ledger["current"] = result
    return result
