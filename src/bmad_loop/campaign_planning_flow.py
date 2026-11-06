"""Planning stage progression, bounded repair and delivered graph activation.

Sessions only propose product artifacts and graphs. This module consumes the
worker's deterministically validated receipts after quiescence. A planning draft
PR follows the same publication pipeline as code; only its delivered result can
activate the exact graph independently reviewed in that PR.
"""

from __future__ import annotations

import copy
import dataclasses
import hashlib
import json
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, Mapping

from .campaign_model import (
    CampaignError,
    CampaignSnapshot,
    CampaignUnit,
    Hold,
    UnitSpec,
    UnitState,
    unit_from_dict,
    validate_graph,
)
from .campaign_store import add_event

STAGES = ("requirements", "architecture", "decomposition", "planning_review")


@dataclass(frozen=True)
class PlanningStage:
    stage: str
    role: str
    repair: bool = False
    findings: tuple[dict[str, Any], ...] = ()
    graph_digest: str = ""


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _flow(unit: CampaignUnit) -> dict[str, Any]:
    return copy.deepcopy(
        unit.evidence.get(
            "planning_flow",
            {
                "schema_version": 1,
                "stages": {},
                "history": [],
                "attempts": {},
                "review_cycles": 0,
                "state": "planning",
                "proposal": None,
            },
        )
    )


def _limits(snapshot: CampaignSnapshot) -> tuple[int, int]:
    limits = snapshot.config_snapshot.get("policy", {}).get("limits", {})
    return int(limits.get("max_dev_attempts", 3)), int(limits.get("max_review_cycles", 3))


def next_stage(snapshot: CampaignSnapshot, unit: CampaignUnit | str) -> PlanningStage | None:
    unit = snapshot.units[unit] if isinstance(unit, str) else unit
    if (
        snapshot.phase != "active"
        or unit.spec.kind != "planning"
        or unit.hold
        or unit.state == UnitState.DELIVERED
        or (unit.claim and not unit.claim.quiescent)
    ):
        return None
    if tuple(snapshot.spec.planning.stages) != STAGES:
        raise CampaignError(
            "planning requires requirements, architecture, decomposition and independent planning_review in order"
        )
    state = _flow(unit)
    if state["state"] in {"approved", "activated", "held"}:
        return None
    for stage in STAGES:
        if stage not in state["stages"]:
            proposal = state.get("proposal") or {}
            return PlanningStage(
                stage,
                "planning_review" if stage == "planning_review" else "planning",
                bool(state.get("repair")),
                tuple(state.get("findings", [])),
                proposal.get("digest", ""),
            )
    return None


def context(snapshot: CampaignSnapshot, unit_id: str) -> dict[str, Any]:
    unit = snapshot.units[unit_id]
    state = _flow(unit)
    proposal = state.get("proposal") or {}
    return {
        "planning_graph_digest": proposal.get("digest", ""),
        "prior_stages": state["stages"],
        "required_findings": state.get("findings", []),
        "proposed_graph": proposal.get("units", []),
        "source_bindings": proposal.get("source_bindings", {}),
        "instruction": "Use this same planning draft PR for every planning and repair stage. Decomposition result must include units and source_bindings mapping every new/changed unit to an artifact path and source kind. Planning review must independently inspect all artifacts and return graph_digest equal to planning_graph_digest. Commit substantive stage artifacts before the checkpoint; never rewrite or remove delivered work.",
    }


def prepare_stage(snapshot: CampaignSnapshot, unit_id: str, stage: str, *, launch_id: str) -> None:
    unit = snapshot.units[unit_id]
    state = _flow(unit)
    prior = [row for row in state["history"] if row.get("launch_id") == launch_id]
    if prior:
        if prior[0]["stage"] != stage:
            raise CampaignError("planning launch ID reused for another stage")
        return
    expected = next_stage(snapshot, unit)
    if not expected or stage != expected.stage:
        raise CampaignError("planning stage launch does not match its next required stage")
    dev_limit, review_limit = _limits(snapshot)
    count = state["attempts"].get(stage, 0)
    limit = review_limit if stage == "planning_review" else dev_limit
    if count >= limit:
        unit.hold = Hold(
            "planning_attempts_exhausted",
            f"{stage} exhausted its lifetime attempt budget",
            id=launch_id,
            actor="planning",
        )
        state["state"] = "held"
    else:
        state["attempts"][stage] = count + 1
        state["history"].append(
            {
                "launch_id": launch_id,
                "stage": stage,
                "state": "prepared",
                "graph_revision": snapshot.graph_revision,
                "config_revision": snapshot.config_revision,
            }
        )
    unit.evidence["planning_flow"] = state
    add_event(snapshot, "planning-stage-prepared", unit=unit_id, stage=stage, launch=launch_id)


def fail_stage(
    snapshot: CampaignSnapshot, unit_id: str, stage: str, *, launch_id: str, reason: str
) -> None:
    unit = snapshot.units[unit_id]
    state = _flow(unit)
    row = [row for row in state["history"] if row.get("launch_id") == launch_id]
    if not row or row[0]["stage"] != stage:
        raise CampaignError("failed planning stage does not own its launch")
    row[0].update(state="failed", reason=reason)
    limit = _limits(snapshot)[1 if stage == "planning_review" else 0]
    if state["attempts"][stage] >= limit:
        state["state"] = "held"
        unit.hold = Hold("planning_attempts_exhausted", reason, id=launch_id, actor="planning")
    unit.evidence["planning_flow"] = state
    add_event(
        snapshot,
        "planning-stage-failed",
        unit=unit_id,
        stage=stage,
        launch=launch_id,
        reason=reason,
    )


def _graph(
    snapshot: CampaignSnapshot,
    unit_id: str,
    raw: Mapping[str, Any],
    artifacts: list[dict[str, Any]],
) -> dict[str, Any]:
    values = raw.get("units")
    if not isinstance(values, list) or not values:
        raise CampaignError("decomposition requires a proposed unit graph")
    proposed = tuple(unit_from_dict(value) for value in values)
    by_id = {unit.id: unit for unit in proposed}
    if len(by_id) != len(proposed) or unit_id in by_id:
        raise CampaignError("decomposition duplicates identities or redefines its planning control")
    combined = {key: unit.spec for key, unit in snapshot.units.items()}
    source_ids = {source.id for source in snapshot.spec.sources}
    changed: list[UnitSpec] = []
    for unit in proposed:
        old = snapshot.units.get(unit.id)
        if old and old.spec == unit:
            continue
        if old and old.state == UnitState.DELIVERED:
            raise CampaignError("planning cannot redefine delivered work")
        if old and old.claim and not old.claim.quiescent:
            raise CampaignError("planning cannot replace a live/uncertain unit contract")
        if not unit.acceptance or not unit.scopes or unit.source not in source_ids:
            raise CampaignError(
                "every changed unit requires an accepted source, acceptance and ownership"
            )
        changed.append(unit)
        combined[unit.id] = unit
    validate_graph(list(combined.values()))
    raw_sources = raw.get("source_bindings")
    if not isinstance(raw_sources, dict):
        raise CampaignError("decomposition must bind proposed units to source artifacts")
    artifact_map = {artifact["path"]: artifact["sha256"] for artifact in artifacts}
    mirror_map = {
        artifact["path"]: artifact.get("review_path", artifact["path"]) for artifact in artifacts
    }
    sources: dict[str, dict[str, Any]] = {}
    selectors = {source.id: source for source in snapshot.spec.sources}
    for unit in changed:
        source = raw_sources.get(unit.id)
        if not isinstance(source, dict):
            raise CampaignError("changed unit has no canonical source artifact proposal")
        path = source.get("path")
        if (
            not isinstance(path, str)
            or path not in artifact_map
            or path.startswith("/")
            or ".." in PurePosixPath(path).parts
            or "\\" in path
        ):
            raise CampaignError("proposed source path is not a confined validated artifact")
        kind = selectors[unit.source].kind
        if source.get("kind") != kind:
            raise CampaignError("proposed source kind differs from its frozen selector")
        sources[unit.id] = {
            **source,
            "path": path,
            "sha256": artifact_map[path],
            "kind": kind,
            "review_path": mirror_map[path],
        }
    content = {"units": [dataclasses.asdict(unit) for unit in proposed], "source_bindings": sources}
    return {
        **content,
        "digest": _digest(content),
        "graph_revision": snapshot.graph_revision,
        "config_revision": snapshot.config_revision,
        "state": "proposed",
    }


def apply_stage(
    snapshot: CampaignSnapshot,
    unit_id: str,
    stage: str,
    receipt: Mapping[str, Any],
    *,
    launch_id: str,
    committed_head: str = "",
) -> None:
    unit = snapshot.units[unit_id]
    if (
        unit.spec.kind != "planning"
        or unit.state == UnitState.DELIVERED
        or (unit.claim and not unit.claim.quiescent)
    ):
        raise CampaignError("planning result cannot apply over a live actor or delivered control")
    state = _flow(unit)
    launches = [row for row in state["history"] if row.get("launch_id") == launch_id]
    if not launches or launches[0]["stage"] != stage or launches[0]["state"] != "prepared":
        raise CampaignError("planning result does not match its owned prepared launch")
    validated = receipt.get("validated", receipt)
    raw = validated.get("result", {})
    coordinates = validated.get("coordinates", {})
    if (
        coordinates.get("campaign") != snapshot.id
        or coordinates.get("unit") != unit_id
        or coordinates.get("stage") != stage
        or coordinates.get("graph_revision") != snapshot.graph_revision
        or coordinates.get("config_revision") != snapshot.config_revision
    ):
        raise CampaignError("planning result candidate/graph/configuration identity changed")
    if raw.get("status") != "done" or raw.get("schema_version") != 1:
        raise CampaignError("planning result is incomplete")
    head = committed_head or str(receipt.get("head", ""))
    if not head:
        raise CampaignError("planning checkpoint has no committed head")
    artifacts = validated.get("artifacts", [])
    if stage != "planning_review" and (
        not artifacts or any(not row.get("path") or not row.get("sha256") for row in artifacts)
    ):
        raise CampaignError("planning stage has no independently verified durable artifacts")
    if stage == "planning_review":
        proposal = state.get("proposal")
        if not proposal or raw.get("graph_digest") != proposal["digest"]:
            raise CampaignError(
                "independent planning review does not cover the proposed source graph"
            )
        if coordinates.get("head") != state.get("head") or head != coordinates.get("head"):
            raise CampaignError(
                "planning review changed or did not inspect its committed candidate"
            )
        verdict = validated.get("verdict")
        findings = raw.get("findings")
        if verdict not in {"approved", "changes_requested", "hold"} or not isinstance(
            findings, list
        ):
            raise CampaignError("planning review has no typed verdict/findings")
        required = [
            finding
            for finding in findings
            if finding.get("required", True) and not finding.get("resolved", False)
        ]
        state["review_cycles"] += 1
        if verdict == "approved" and required:
            raise CampaignError("planning review cannot waive unresolved required findings")
        if verdict == "approved":
            state["state"] = "approved"
            state["proposal"]["state"] = "reviewed"
            state["proposal"]["reviewed_head"] = head
            state["stages"][stage] = dict(receipt)
            state["findings"] = []
        elif verdict == "hold":
            state["state"] = "held"
            unit.hold = Hold(
                "planning_decision",
                "Planning review requires an unresolved decision",
                id=launch_id,
                actor="planning_review",
                evidence={"findings": findings},
            )
        else:
            signature = _digest([required, head])
            if (
                state.get("last_review_failure") == signature
                or state["review_cycles"] >= _limits(snapshot)[1]
            ):
                state["state"] = "held"
                unit.hold = Hold(
                    "planning_review_exhausted",
                    "Planning review did not converge within its lifetime budget",
                    id=launch_id,
                    actor="planning_review",
                    evidence={"findings": findings},
                )
            else:
                hints = [
                    finding.get("stage")
                    for finding in required
                    if finding.get("stage") in STAGES[:-1]
                ]
                repair_stage = min(hints, key=STAGES.index) if hints else "decomposition"
                for name in STAGES[STAGES.index(repair_stage) :]:
                    state["stages"].pop(name, None)
                state["state"] = "planning"
                state["repair"] = True
                state["findings"] = required or findings
                state["last_review_failure"] = signature
    else:
        if stage == "decomposition":
            state["proposal"] = _graph(snapshot, unit_id, raw, artifacts)
        state["stages"][stage] = dict(receipt)
        state["head"] = head
    launches[0].update(state="completed", head=head)
    unit.evidence["planning_flow"] = state
    unit.evidence["planning_stages"] = list(state["stages"])
    unit.evidence["planning_results"] = list(state["stages"].values())
    add_event(
        snapshot,
        "planning-stage-accepted",
        unit=unit_id,
        stage=stage,
        launch=launch_id,
        head=head,
        verdict=validated.get("verdict", ""),
    )


def activate(
    snapshot: CampaignSnapshot,
    unit_id: str,
    *,
    canonical_revision: str,
    source_bindings: Mapping[str, Mapping[str, Any]],
) -> tuple[str, ...]:
    unit = snapshot.units[unit_id]
    state = _flow(unit)
    if state["state"] == "activated":
        return tuple(state.get("activated_units", []))
    proposal = state.get("proposal")
    if (
        unit.state != UnitState.DELIVERED
        or not unit.bindings.get("pr")
        or state["state"] != "approved"
        or not proposal
        or proposal["state"] != "reviewed"
    ):
        raise CampaignError(
            "planning graph waits for reviewed PR delivery and source reconciliation"
        )
    if not canonical_revision or proposal["config_revision"] != snapshot.config_revision:
        raise CampaignError("planning graph canonical/configuration revision is missing or stale")
    changed = [
        unit_from_dict(value)
        for value in proposal["units"]
        if value["id"] not in snapshot.units
        or unit_from_dict(value) != snapshot.units[value["id"]].spec
    ]
    if set(source_bindings) != {value.id for value in changed}:
        raise CampaignError("planning graph needs exact canonical source publication bindings")
    for value in changed:
        old = snapshot.units.get(value.id)
        if old and (old.state == UnitState.DELIVERED or (old.claim and not old.claim.quiescent)):
            raise CampaignError("planning activation cannot replace delivered or live work")
        expected = proposal["source_bindings"][value.id]
        observed = source_bindings[value.id]
        artifact_path = observed.get("artifact_path", observed.get("path"))
        source_revision = observed.get("revision")
        separately_published = (
            observed.get("code_revision") == canonical_revision
            and observed.get("remote_publication_confirmed") is True
        )
        if (
            artifact_path != expected["path"]
            or observed.get("sha256") != expected["sha256"]
            or not observed.get("path")
            or not source_revision
            or (source_revision != canonical_revision and not separately_published)
        ):
            raise CampaignError(
                "canonical source artifact does not match the independently reviewed graph"
            )
    combined = {key: member.spec for key, member in snapshot.units.items()}
    combined.update({value.id: value for value in changed})
    validate_graph(list(combined.values()))
    source_observations = snapshot.bindings.setdefault("source_observations", {})
    for value in changed:
        if value.id in snapshot.units:
            snapshot.units[value.id].spec = value
        else:
            snapshot.units[value.id] = CampaignUnit(value)
        previous = source_observations.get(value.id, {})
        source_observations[value.id] = {
            **dict(source_bindings[value.id]),
            **({"tracker_id": previous["tracker_id"]} if previous.get("tracker_id") else {}),
        }
        snapshot.units[value.id].bindings["planning_unit"] = unit_id
    if changed:
        snapshot.graph_revision += 1
    state["state"] = "activated"
    state["activated_units"] = [value.id for value in changed]
    state["canonical_revision"] = canonical_revision
    unit.evidence["planning_flow"] = state
    add_event(
        snapshot,
        "planning-graph-activated",
        unit=unit_id,
        graph_revision=snapshot.graph_revision,
        units=state["activated_units"],
    )
    return tuple(state["activated_units"])
