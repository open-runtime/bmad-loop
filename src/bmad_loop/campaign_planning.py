"""Bounded coding-session stage packets and deterministic planning validation.

The model proposes artifacts.  The supervisor validates identities, confined
paths and graph changes after the normal coding-session completion boundary.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
from pathlib import Path
from typing import Any

from .campaign_model import CampaignError, CampaignSnapshot, UnitSpec, spec_from_dict


@dataclasses.dataclass(frozen=True)
class StageCoordinates:
    campaign: str
    unit: str
    stage: str
    graph_revision: int
    config_revision: int
    base: str
    head: str
    inventory_digest: str
    source_revision: str = ""


def stage_packet(coordinates: StageCoordinates, *, context: dict[str, Any], skill: str = "") -> str:
    instruction = {
        "requirements": "Derive requirements and acceptance criteria from the approved brief and repository inventory.",
        "architecture": "Specify interfaces, ownership boundaries, dependencies and verification without duplicating existing work.",
        "decomposition": "Propose qualified executable units with acceptance criteria, dependencies and explicit ownership scopes.",
        "planning_review": "Independently review the complete proposed plan, coverage, exclusions, overlap evidence and dependency graph.",
        "publication_review": "Review the complete PR diff against the current target, acceptance criteria, private source artifacts and all required feedback. Read every source artifact at its review_path and return its exact source_artifact_digest. Do not modify files.",
        "conflict_repair": "Resolve the merge conflicts using both preserved heads and their intent. Record each conflict disposition and verification covering both sides. Preserve important work from both branches.",
        "closure": "Verify the entire epic is delivered, perform its required retrospective and record the canonical closure artifact.",
        "triage": "Triage the exact incoming GitHub object versions against existing work. Required review findings repair the same PR.",
        "admission_review": "Compare this unit's acceptance criteria with existing issues, PRs, branches and known worker ownership. Read relevant diffs and commits without executing branch code. Return overlap with unit_id, disposition, inventory_digest, evidence_ids, acceptance_coverage, remaining, exclusions, reason and adopted_pr. Disposition is new_work, already_delivered, covered_by_active_work, continue_existing_work, reuse_partial_work or unresolved_overlap. Do not claim already delivered without verified merge/source evidence, or adopt another actor's branch.",
        "escalation": "Diagnose the typed failure and propose one bounded action within existing authority; never waive a gate.",
        "decision_review": "Determine whether existing authoritative evidence answers or delegates this exact decision. Do not create approval authority.",
    }.get(coordinates.stage)
    if instruction is None:
        raise CampaignError(f"unknown campaign stage {coordinates.stage}")
    body = {
        "schema_version": 1,
        "coordinates": dataclasses.asdict(coordinates),
        "instruction": instruction,
        "context": context,
        "result_contract": {
            "status": "done",
            "schema_version": 1,
            "coordinates": dataclasses.asdict(coordinates),
            "artifacts": ["repository-relative durable product artifact"],
            "verdict": "approved|changes_requested|hold",
            "findings": [],
            "source_artifact_digest": coordinates.source_revision,
            "units": "decomposition only: campaign unit objects",
        },
    }
    prefix = f"Use the installed {skill} skill.\n" if skill else ""
    return prefix + json.dumps(body, ensure_ascii=False, indent=2)


def validate_result(
    raw: dict[str, Any],
    coordinates: StageCoordinates,
    workspace: Path,
    *,
    snapshot: CampaignSnapshot | None = None,
) -> dict[str, Any]:
    if raw.get("schema_version") != 1 or raw.get("coordinates") != dataclasses.asdict(coordinates):
        raise CampaignError("stage result identity/revision/candidate mismatch")
    if raw.get("status") != "done":
        raise CampaignError("stage result is not complete")
    paths = raw.get("artifacts", [])
    if not isinstance(paths, list) or not all(isinstance(path, str) for path in paths):
        raise CampaignError("stage artifact paths must be a list of strings")
    receipts = []
    for relative in paths:
        path = workspace / relative
        if (
            Path(relative).is_absolute()
            or ".." in Path(relative).parts
            or path.is_symlink()
            or not path.resolve().is_relative_to(workspace.resolve())
            or not path.is_file()
            or path.stat().st_size > 8 * 1024 * 1024
        ):
            raise CampaignError(
                "stage artifact is missing, redirected, unbounded or outside the checkout"
            )
        data = path.read_bytes()
        if not data.strip():
            raise CampaignError("stage artifact is empty")
        receipts.append({"path": relative, "sha256": hashlib.sha256(data).hexdigest()})
    if coordinates.stage in {"requirements", "architecture", "decomposition"} and not receipts:
        raise CampaignError("planning stage requires durable product artifacts")
    if coordinates.stage in {"planning_review", "publication_review"}:
        if raw.get("verdict") not in {"approved", "changes_requested", "hold"}:
            raise CampaignError("review verdict missing")
        findings = raw.get("findings")
        if not isinstance(findings, list):
            raise CampaignError("review findings missing")
        if raw["verdict"] == "approved" and any(
            f.get("required", True) and not f.get("resolved", False) for f in findings
        ):
            raise CampaignError("approved review still has unresolved required findings")
        if (
            coordinates.stage == "publication_review"
            and coordinates.source_revision
            and raw.get("source_artifact_digest") != coordinates.source_revision
        ):
            raise CampaignError(
                "publication review does not cover the exact private source artifacts"
            )
    if coordinates.stage == "decomposition":
        proposed = raw.get("units")
        if not isinstance(proposed, list) or not proposed:
            raise CampaignError("decomposition requires executable units")
        combined = (
            {key: dataclasses.asdict(unit.spec) for key, unit in snapshot.units.items()}
            if snapshot
            else {}
        )
        for item in proposed:
            if not isinstance(item, dict) or not isinstance(item.get("id"), str):
                raise CampaignError("proposed unit requires an identity")
            combined[item["id"]] = item
        base = {"schema_version": 1, "name": "validated-plan", "units": list(combined.values())}
        graph = spec_from_dict(base)
        proposed_ids = {item["id"] for item in proposed}
        if any(
            not unit.acceptance or not unit.scopes
            for unit in graph.units
            if unit.id in proposed_ids
        ):
            raise CampaignError(
                "every proposed unit requires acceptance criteria and ownership scopes"
            )
        if snapshot is not None:
            by_id = {unit.id: unit for unit in graph.units}
            for key, unit in snapshot.units.items():
                if unit.state.value == "delivered" and by_id.get(key) != unit.spec:
                    raise CampaignError("planning revision removed or redefined delivered work")
    return {
        "coordinates": dataclasses.asdict(coordinates),
        "artifacts": receipts,
        "verdict": raw.get("verdict", ""),
        "result": raw,
    }


def execution_spec(
    unit: UnitSpec, *, campaign_id: str, graph_revision: int, source: dict[str, Any]
) -> str:
    """A substantive first commit allows the draft PR to precede all coding."""
    lines = [
        f"# {unit.title}",
        "",
        f"Campaign: {campaign_id}",
        f"Unit: {unit.id}",
        f"Plan revision: {graph_revision}",
        "",
        "Acceptance criteria",
        "",
    ]
    lines.extend(f"- {criterion}" for criterion in unit.acceptance)
    if not unit.acceptance:
        lines.append(
            "- Satisfy the selected BMAD source specification and its deterministic verification gates."
        )
    lines.extend(["", "Dependencies", "", *(f"- {key}" for key in unit.after), "", "Ownership", ""])
    lines.extend(f"- {scope}" for scope in unit.scopes)
    lines.extend(
        [
            "",
            "Source binding",
            "",
            "```json",
            json.dumps(source, sort_keys=True, indent=2),
            "```",
            "",
        ]
    )
    return "\n".join(lines)
