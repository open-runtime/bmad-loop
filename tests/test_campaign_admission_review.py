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
    unit.bindings.setdefault("base", unit.bindings["head"])
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
            "external_ownership": [],
            "available_evidence_ids": sorted(evidence_ids(inventory)),
        },
    )
    assert bool(files) == large
    debt = unit.evidence["stage_sync_debt"]
    debt["request"].update(
        coordinates=dataclasses.asdict(coordinates),
        workspace=str(project.project),
        branch=unit.bindings["branch"],
        pr_number=unit.bindings["pr"]["number"],
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


def refreshed_inventory(original, observed_at, **changes):
    fields = {key: value for key, value in original.items() if key != "digest"}
    receipt = CoverageReceipt(**{**fields, "observed_at": observed_at, **changes})
    return {**receipt.to_dict(), "digest": receipt.digest}


@pytest.mark.parametrize("large", [False, True])
def test_successive_timestamp_only_refreshes_keep_the_review_and_its_attempt_budget(
    project, tmp_path, large
):
    runtime, snapshot, unit, action, original, _ = review_case(project, tmp_path, large=large)
    runtime.clock = lambda: 101
    reviewed = unit.evidence["stage_sync_debt"]["receipt"]["artifact"]["overlap"].copy()
    count = unit.attempt
    snapshot.bindings["inventory"] = refreshed_inventory(original, 99)
    data = boundary.after_effect(runtime, snapshot, action).data
    # A further refresh may be committed while the previous observation is in flight.
    current = refreshed_inventory(original, 100)
    snapshot.bindings["inventory"] = current
    boundary.after_apply(runtime, snapshot, action, data)
    assert unit.state == UnitState.READY and unit.hold is None
    assert unit.evidence["admission"] == reviewed
    assert unit.evidence["admission_inventory_refresh"] == {
        "reviewed_digest": original["digest"],
        "reviewed_at": original["observed_at"],
        "current_digest": current["digest"],
        "current_observed_at": current["observed_at"],
    }
    assert not unit.evidence.get("stage_input_invalidations") and unit.attempt == count


@pytest.mark.parametrize("change", ["scope", "object", "second-change"])
def test_only_a_fresh_complete_timestamp_only_inventory_may_satisfy_admission(
    project, tmp_path, change
):
    runtime, snapshot, unit, action, original, moved = review_case(project, tmp_path)
    runtime.clock = lambda: 101
    changes = {
        "scope": {"scopes": ["new/"]},
        "object": {"issues": moved["issues"]},
    }.get(change, {})
    snapshot.bindings["inventory"] = refreshed_inventory(original, 100, **changes)
    data = boundary.after_effect(runtime, snapshot, action).data
    if change == "second-change":
        snapshot.bindings["inventory"] = moved
    boundary.after_apply(runtime, snapshot, action, data)
    assert len(unit.evidence["stage_input_invalidations"]) == 1
    assert "admission" not in unit.evidence and "admission_inventory_refresh" not in unit.evidence


@pytest.mark.parametrize(
    "change", ["expired-exact", "expired-refresh", "older", "future", "incomplete", "changed-bytes"]
)
def test_unusable_observation_waits_for_refresh_without_spending_another_review(
    project, tmp_path, change
):
    runtime, snapshot, unit, action, original, _ = review_case(project, tmp_path)
    clock = 300 if change.startswith("expired") else 101
    runtime.clock = lambda: clock
    stamp = {"expired-exact": 20, "older": 19, "future": 102}.get(change, 100)
    unusable = refreshed_inventory(original, stamp)
    if change == "incomplete":
        unusable = {**original, "complete": False}  # old digest cannot authenticate new content
    elif change == "changed-bytes":
        unusable = {**original, "scopes": ["changed/"]}
    snapshot.bindings["inventory"] = unusable
    waiting = boundary.after_effect(runtime, snapshot, action)
    assert (
        waiting.state == "waiting"
        and waiting.data["admission_inventory_wait"] == unusable["digest"]
    )
    assert unit.claim.quiescent and "stage_sync_debt" in unit.evidence
    snapshot.bindings["inventory"] = refreshed_inventory(original, clock)
    completed = boundary.after_effect(runtime, snapshot, action)
    boundary.after_apply(runtime, snapshot, action, completed.data)
    assert (
        unit.state == UnitState.READY
        and unit.evidence["admission"]["inventory_digest"] == original["digest"]
    )
    assert not unit.evidence.get("stage_input_invalidations")


@pytest.mark.parametrize("at_apply", [False, True])
@pytest.mark.parametrize("large", [False, True])
@pytest.mark.parametrize("change", ["owner", "pr"])
def test_external_ownership_and_pr_are_still_part_of_a_timestamp_refresh(
    project, tmp_path, at_apply, large, change
):
    runtime, snapshot, unit, action, original, _ = review_case(project, tmp_path, large=large)
    snapshot.bindings["inventory"] = refreshed_inventory(original, 99)

    def alter():
        if change == "owner":
            snapshot.bindings["external_workers"] = [
                {
                    "kind": "process",
                    "details": {"worktree_path": unit.bindings["workspace"]},
                    "liveness": "alive",
                }
            ]
        else:
            unit.bindings["pr"] = {**unit.bindings["pr"], "node_id": "other-native"}

    if not at_apply:
        alter()
    data = boundary.after_effect(runtime, snapshot, action).data
    if at_apply:
        alter()
    boundary.after_apply(runtime, snapshot, action, data)
    assert "admission" not in unit.evidence
    if change == "pr" and not at_apply:
        assert unit.hold.kind == "ownership_uncertain"
        assert "stage_sync_debt" in unit.evidence
    else:
        assert len(unit.evidence["stage_input_invalidations"]) == 1


def test_unusable_commit_time_receipt_retains_sync_debt_until_a_real_refresh(project, tmp_path):
    from bmad_loop.campaign_supervisor import ObservationStale

    runtime, snapshot, unit, action, original, _ = review_case(project, tmp_path)
    data = boundary.after_effect(runtime, snapshot, action).data
    snapshot.bindings["inventory"] = {**original, "complete": False, "observed_at": 1_000_000}
    with pytest.raises(ObservationStale, match="fresh complete inventory"):
        boundary.after_apply(runtime, snapshot, action, data)
    assert data["admission_inventory_wait"] == original["digest"]
    assert unit.claim.quiescent and "stage_sync_debt" in unit.evidence
    assert not unit.evidence.get("stage_input_invalidations")
    assert any(p.kind == "inventory" for p in runtime.proposals(snapshot))
    snapshot.bindings["inventory"] = refreshed_inventory(original, runtime.clock())
    fresh = boundary.after_effect(runtime, snapshot, action)
    boundary.after_apply(runtime, snapshot, action, fresh.data)
    assert unit.evidence["admission"]["inventory_digest"] == original["digest"]
    assert "stage_sync_debt" not in unit.evidence


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
