"""Admission consumes only a valid review of the current repository inventory."""

import dataclasses

import pytest
from test_campaign_boundary_sync import completed, setup

from bmad_loop import campaign_boundary_sync as boundary
from bmad_loop.campaign_discovery import CoverageReceipt, OverlapDisposition, evidence_ids
from bmad_loop.campaign_model import Hold, UnitState
from bmad_loop.campaign_planning import StageCoordinates, stage_packet
from bmad_loop.campaign_stage_prompt import prepare_context


def review_case(project, tmp_path, *, large=False):
    runtime, snapshot, unit = setup(project, tmp_path)
    unit.spec = dataclasses.replace(
        unit.spec, kind="story", acceptance=("Keep the public contract",)
    )
    after = completed(runtime, snapshot, unit)
    issue = {"number": 11, "updated_at": "old", "body": "x" * (70_000 if large else 1)}
    inventory = CoverageReceipt(
        1, snapshot.repository, snapshot.common_dir, 20, (), True, (), (), (issue,), (), (), (), ()
    )
    original = {**inventory.to_dict(), "digest": inventory.digest}
    current = dataclasses.replace(inventory, issues=({**issue, "updated_at": "new"},))
    moved = {**current.to_dict(), "digest": current.digest}
    snapshot.bindings["inventory"] = original
    coordinates = StageCoordinates(
        snapshot.id,
        unit.id,
        "admission_review",
        snapshot.graph_revision,
        snapshot.config_revision,
        unit.bindings["head"],
        unit.bindings["head"],
        inventory.digest,
    )
    directory = tmp_path / "admission-inputs"
    ctx, files = prepare_context(
        directory,
        {
            "unit": dataclasses.asdict(unit.spec),
            "inventory": original,
            "inventory_digest": inventory.digest,
            "owned_pr": unit.bindings["pr"],
            "available_evidence_ids": sorted(evidence_ids(inventory)),
        },
    )
    assert bool(files) == large
    debt = unit.evidence["stage_sync_debt"]
    debt["request"].update(
        coordinates=dataclasses.asdict(coordinates),
        workspace=str(project.project),
        execution=str(directory / "execution.json"),
        context_artifacts=files,
        prompt=stage_packet(coordinates, context=ctx, compact=bool(files)),
    )
    disposition = OverlapDisposition(
        unit.id,
        "new_work",
        coordinates.inventory_digest,
        ("issue:11@old",),
        unit.spec.acceptance,
        (),
        (),
        "The observed issue addresses a separate obligation.",
    )
    debt["receipt"].update(
        stage="admission_review",
        artifact={
            "schema_version": 1,
            "status": "done",
            "coordinates": dataclasses.asdict(coordinates),
            "artifacts": [],
            "overlap": dataclasses.asdict(disposition),
        },
    )
    return runtime, snapshot, unit, after, original, moved


@pytest.mark.parametrize("large", [False, True])
@pytest.mark.parametrize("movement", ["before-observation", "before-apply", "unchanged"])
def test_current_review_is_accepted_and_valid_superseded_review_is_retained(
    project,
    tmp_path,
    large,
    movement,
):
    runtime, snapshot, unit, action, original, moved = review_case(project, tmp_path, large=large)
    if movement == "before-observation":
        snapshot.bindings["inventory"] = moved
    data = boundary.after_effect(runtime, snapshot, action).data
    if movement == "before-apply":
        snapshot.bindings["inventory"] = moved
    boundary.after_apply(runtime, snapshot, action, data)
    assert unit.state == UnitState.READY and unit.hold is None
    if movement == "unchanged":
        assert unit.evidence["admission"]["inventory_digest"] == original["digest"]
    else:
        history = unit.evidence["stage_input_invalidations"]
        assert (
            len(history) == 1
            and history[0]["reason"] == "Repository inventory changed during admission review"
        )
        assert (
            history[0]["receipt"]["artifact"]["overlap"]["inventory_digest"] == original["digest"]
        )
        assert "admission" not in unit.evidence


@pytest.mark.parametrize("change", ["forged-revision", "invented-citation", "unknown-verdict"])
def test_invalid_original_answer_stays_held_after_inventory_moves(project, tmp_path, change):
    runtime, snapshot, unit, action, _, moved = review_case(project, tmp_path, large=True)
    raw = unit.evidence["stage_sync_debt"]["receipt"]["artifact"]["overlap"]
    if change == "forged-revision":
        raw["inventory_digest"] = moved["digest"]
    elif change == "invented-citation":
        raw["evidence_ids"] = ["issue:11@new"]
    else:
        raw["disposition"] = "automatically_approve"
    snapshot.bindings["inventory"] = moved
    data = boundary.after_effect(runtime, snapshot, action).data
    boundary.after_apply(runtime, snapshot, action, data)
    assert unit.hold.kind == "invalid_stage_result"
    assert "admission" not in unit.evidence
    assert not unit.evidence.get("stage_input_invalidations")


def test_repeated_inventory_change_preserves_owner_hold_and_bounds_rechecks(project, tmp_path):
    runtime, snapshot, unit, action, _, moved = review_case(project, tmp_path)
    prior = {"reason": "Previous inventory changed"}
    unit.evidence["stage_input_invalidations"] = [prior, prior]
    snapshot.bindings["inventory"] = moved
    data = boundary.after_effect(runtime, snapshot, action).data
    unit.hold = owner = Hold("operator", "Pause here", id="owner-stop")
    boundary.after_apply(runtime, snapshot, action, data)
    assert unit.hold == owner
    assert unit.evidence["pending_failure_hold"]["kind"] == "github_inputs_churning"
    assert len(unit.evidence["stage_input_invalidations"]) == 3
    assert "admission" not in unit.evidence
