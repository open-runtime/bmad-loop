"""Planning graphs stay dormant until their reviewed source PR is delivered."""

from __future__ import annotations

import copy
import dataclasses

import pytest

from bmad_loop.campaign_model import (
    CampaignError,
    CampaignSnapshot,
    CampaignSpec,
    CampaignUnit,
    SourceSelector,
    UnitSpec,
    UnitState,
    WorkerClaim,
)
from bmad_loop.campaign_planning_flow import (
    STAGES,
    activate,
    apply_stage,
    context,
    fail_stage,
    next_stage,
    prepare_stage,
)


@pytest.fixture
def snapshot():
    plan = UnitSpec("campaign:plan", "campaign-plan", "planning", "Plan", kind="planning")
    existing = UnitSpec(
        "source:epic:existing",
        "source",
        "epic",
        "Existing",
        acceptance=("Keep",),
        scopes=("src/existing",),
    )
    result = CampaignSnapshot(
        "planning-test",
        "a/b",
        CampaignSpec("Planning", sources=(SourceSelector("stories", path="specs", id="source"),)),
        {
            plan.id: CampaignUnit(plan, UnitState.RUNNING, bindings={"pr": {"number": 10}}),
            existing.id: CampaignUnit(existing, UnitState.READY),
        },
        config_snapshot={"policy": {"limits": {"max_dev_attempts": 3, "max_review_cycles": 2}}},
    )
    return result


def receipt(snapshot, stage, *, new_unit=None, head=None, verdict="approved", findings=None):
    flow = snapshot.units["campaign:plan"].evidence.get("planning_flow", {})
    before = flow.get("head", "brief-head")
    head = head or (before if stage == "planning_review" else stage + "-head")
    coordinates = {
        "campaign": snapshot.id,
        "unit": "campaign:plan",
        "stage": stage,
        "graph_revision": snapshot.graph_revision,
        "config_revision": snapshot.config_revision,
        "head": before,
        "base": "main-head",
        "inventory_digest": "inventory",
    }
    raw = {"status": "done", "schema_version": 1, "coordinates": coordinates}
    artifacts = [{"path": "plans/" + stage + ".md", "sha256": "sha-" + stage}]
    if stage == "decomposition":
        new_unit = new_unit or UnitSpec(
            "source:epic:new",
            "source",
            "epic",
            "New",
            acceptance=("New acceptance",),
            scopes=("src/new",),
        )
        raw.update(
            units=[dataclasses.asdict(new_unit)],
            source_bindings={new_unit.id: {"kind": "stories", "path": "plans/decomposition.md"}},
        )
    if stage == "planning_review":
        raw.update(
            verdict=verdict,
            findings=findings or [],
            graph_digest=context(snapshot, "campaign:plan")["planning_graph_digest"],
        )
    return {
        "head": head,
        "validated": {
            "coordinates": coordinates,
            "result": raw,
            "verdict": verdict if stage == "planning_review" else "",
            "artifacts": artifacts,
        },
    }


def perform(snapshot, stage, **kwargs):
    flow = snapshot.units["campaign:plan"].evidence.get("planning_flow", {})
    launch = stage + "-" + str(flow.get("attempts", {}).get(stage, 0) + 1)
    prepare_stage(snapshot, "campaign:plan", stage, launch_id=launch)
    result = receipt(snapshot, stage, **kwargs)
    apply_stage(snapshot, "campaign:plan", stage, result, launch_id=launch)
    return result


def complete_plan(snapshot):
    for stage in STAGES:
        assert next_stage(snapshot, "campaign:plan").stage == stage
        perform(snapshot, stage)


def test_stages_choose_fresh_planning_review_and_preserve_same_draft(snapshot):
    for stage in STAGES:
        selected = next_stage(snapshot, "campaign:plan")
        assert selected.stage == stage
        assert selected.role == ("planning_review" if stage == "planning_review" else "planning")
        perform(snapshot, stage)
        assert "source:epic:new" not in snapshot.units
    assert next_stage(snapshot, "campaign:plan") is None
    plan = snapshot.units["campaign:plan"]
    assert plan.bindings["pr"] == {"number": 10}
    assert plan.evidence["planning_flow"]["state"] == "approved"
    assert len(plan.evidence["planning_results"]) == 4


def test_new_graph_waits_for_delivery_then_exact_canonical_source_receipt(snapshot):
    complete_plan(snapshot)
    plan = snapshot.units["campaign:plan"]
    sources = {
        "source:epic:new": {
            "path": "plans/decomposition.md",
            "sha256": "sha-decomposition",
            "revision": "canonical-sha",
        }
    }
    with pytest.raises(CampaignError, match="reviewed PR delivery"):
        activate(snapshot, plan.id, canonical_revision="canonical-sha", source_bindings=sources)
    assert "source:epic:new" not in snapshot.units
    plan.state = UnitState.DELIVERED
    bad = copy.deepcopy(sources)
    bad["source:epic:new"]["sha256"] = "changed-after-review"
    with pytest.raises(CampaignError, match="independently reviewed graph"):
        activate(snapshot, plan.id, canonical_revision="canonical-sha", source_bindings=bad)
    assert activate(
        snapshot, plan.id, canonical_revision="canonical-sha", source_bindings=sources
    ) == ("source:epic:new",)
    assert snapshot.units["source:epic:new"].state == UnitState.PENDING
    assert snapshot.graph_revision == 2
    assert activate(
        snapshot, plan.id, canonical_revision="canonical-sha", source_bindings=sources
    ) == ("source:epic:new",)
    assert snapshot.graph_revision == 2


def test_required_review_repair_repeats_affected_stages_in_same_pr_and_is_bounded(snapshot):
    for stage in STAGES[:-1]:
        perform(snapshot, stage)
    findings = [
        {
            "id": "P1",
            "required": True,
            "resolved": False,
            "stage": "architecture",
            "message": "Missing interface",
        }
    ]
    perform(snapshot, "planning_review", verdict="changes_requested", findings=findings)
    selected = next_stage(snapshot, "campaign:plan")
    assert selected.stage == "architecture"
    assert selected.repair
    assert selected.findings == tuple(findings)
    assert snapshot.units["campaign:plan"].bindings["pr"]["number"] == 10
    for stage in ("architecture", "decomposition"):
        perform(snapshot, stage, head="fixed-" + stage)
    perform(snapshot, "planning_review", verdict="changes_requested", findings=findings)
    plan = snapshot.units["campaign:plan"]
    assert plan.hold.kind == "planning_review_exhausted"
    assert next_stage(snapshot, plan) is None
    assert "source:epic:new" not in snapshot.units


@pytest.mark.parametrize(
    "change,expected",
    [
        ("untracked", "confined validated artifact"),
        ("unknownsource", "accepted source"),
        ("delivered", "delivered work"),
        ("live", "live/uncertain"),
        ("cycle", "cycle"),
    ],
)
def test_decomposition_cannot_remove_ownership_source_or_existing_work_guards(
    snapshot, change, expected
):
    perform(snapshot, "requirements")
    perform(snapshot, "architecture")
    new = UnitSpec(
        "source:epic:new",
        "source",
        "epic",
        "New",
        acceptance=("New acceptance",),
        scopes=("src/new",),
    )
    if change in {"delivered", "live"}:
        new = dataclasses.replace(snapshot.units["source:epic:existing"].spec, title="Redefined")
        if change == "delivered":
            snapshot.units[new.id].state = UnitState.DELIVERED
        else:
            snapshot.units[new.id].claim = WorkerClaim(new.id, 1, 1, "live-launch")
    if change == "unknownsource":
        new = dataclasses.replace(new, source="foreign")
    if change == "cycle":
        new = dataclasses.replace(new, after=(new.id,))
    prepare_stage(snapshot, "campaign:plan", "decomposition", launch_id="decomp")
    result = receipt(snapshot, "decomposition", new_unit=new)
    if change == "untracked":
        result["validated"]["result"]["source_bindings"][new.id]["path"] = "../../outside"
    original = copy.deepcopy(snapshot.units[new.id].spec) if new.id in snapshot.units else None
    with pytest.raises(CampaignError, match=expected):
        apply_stage(snapshot, "campaign:plan", "decomposition", result, launch_id="decomp")
    if original:
        assert snapshot.units[new.id].spec == original
    else:
        assert new.id not in snapshot.units


@pytest.mark.parametrize(
    "change,expected",
    [
        ("digest", "proposed source graph"),
        ("head", "committed candidate"),
        ("finding", "waive unresolved"),
    ],
)
def test_independent_review_is_bound_to_exact_graph_and_unchanged_candidate(
    snapshot, change, expected
):
    for stage in STAGES[:-1]:
        perform(snapshot, stage)
    prepare_stage(snapshot, "campaign:plan", "planning_review", launch_id="review")
    result = receipt(snapshot, "planning_review")
    if change == "digest":
        result["validated"]["result"]["graph_digest"] = "old-graph"
    if change == "head":
        result["head"] = "edited-during-review"
    if change == "finding":
        result["validated"]["result"]["findings"] = [{"required": True, "resolved": False}]
    with pytest.raises(CampaignError, match=expected):
        apply_stage(snapshot, "campaign:plan", "planning_review", result, launch_id="review")
    assert next_stage(snapshot, "campaign:plan").stage == "planning_review"


def test_failed_stage_attempts_persist_and_do_not_start_again_after_exhaustion(snapshot):
    for attempt in range(3):
        launch = f"requirements-{attempt}"
        prepare_stage(snapshot, "campaign:plan", "requirements", launch_id=launch)
        fail_stage(
            snapshot,
            "campaign:plan",
            "requirements",
            launch_id=launch,
            reason="Session interrupted",
        )
    plan = snapshot.units["campaign:plan"]
    assert plan.hold.kind == "planning_attempts_exhausted"
    assert plan.evidence["planning_flow"]["attempts"]["requirements"] == 3
    assert next_stage(snapshot, plan) is None


def test_unchanged_delivered_units_stay_immutable_without_becoming_new_source_proposals(snapshot):
    old = snapshot.units["source:epic:existing"]
    old.state = UnitState.DELIVERED
    perform(snapshot, "requirements")
    perform(snapshot, "architecture")
    prepare_stage(snapshot, "campaign:plan", "decomposition", launch_id="decomp")
    result = receipt(snapshot, "decomposition")
    result["validated"]["result"]["units"].append(dataclasses.asdict(old.spec))
    apply_stage(snapshot, "campaign:plan", "decomposition", result, launch_id="decomp")
    assert old.state == UnitState.DELIVERED
    assert (
        old.id
        not in snapshot.units["campaign:plan"].evidence["planning_flow"]["proposal"][
            "source_bindings"
        ]
    )
