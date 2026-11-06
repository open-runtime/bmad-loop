"""Development identity survives mixed-role telemetry without guessing models."""

import dataclasses
import json

import pytest
from test_campaign_publication import publishable as publishable

from bmad_loop.campaign_model import (
    CampaignSnapshot,
    CampaignSpec,
    CampaignUnit,
    OutboxAction,
    UnitSpec,
    UnitState,
    WorkerClaim,
)
from bmad_loop.campaign_runtime import CampaignRuntime
from bmad_loop.campaign_store import CampaignStore


def session(role, models, *, status="completed", label=""):
    return {
        "role": role,
        "status": status,
        "models": models,
        "label": label,
        "session_id": role + str(models),
        "task_id": "story",
        "model": "gpt-6.1-sol",
    }


def finished(project, tmp_path, sessions):
    unit = CampaignUnit(UnitSpec("unit", "source", "epic", "Unit"), UnitState.RUNNING)
    unit.claim = WorkerClaim(unit.id, 1, 0, "execution", role="execution", writes=True)
    snapshot = CampaignSnapshot(
        "attribution",
        "org/repo",
        CampaignSpec("attribution", units=(unit.spec,)),
        {unit.id: unit},
        common_dir=str(project.project / ".git"),
        root=str(project.project),
    )
    runtime = CampaignRuntime(project.project, CampaignStore(tmp_path / "state"))
    receipt = {
        "outcome": "locally_complete",
        "head": "h" * 40,
        "launch_id": "execution",
        "sessions": sessions,
    }
    action = OutboxAction("observe", "observe-worker", unit.id, {"launch_id": "execution"}, 0)
    runtime._apply_worker(snapshot, unit, action, {"state": "finished", "receipt": receipt})
    return runtime, snapshot, unit


def test_mixed_development_and_clean_followup_review_retains_independent_publication(
    project, tmp_path, publishable
):
    runtime, snapshot, unit = finished(
        project,
        tmp_path,
        [session("dev", ["gpt-6.1-sol"]), session("review", ["claude-fable-5-1"])],
    )
    assert unit.evidence["developer_family"] == "openai"
    controller, candidate, evidence, observation = publishable
    evidence = dataclasses.replace(evidence, developer_family=unit.evidence["developer_family"])
    assert controller.evaluate(candidate, evidence, observation, independent_families=True).ready
    same_family = dataclasses.replace(evidence, reviewer_family="openai")
    assert not controller.evaluate(
        candidate, same_family, observation, independent_families=True
    ).ready


@pytest.mark.parametrize(
    "sessions,expected",
    [
        (
            [
                session("dev", ["claude-fable-5-1"]),
                session("dev", ["gpt-6.1-sol"]),
                session("review", ["claude-fable-5-1"]),
            ],
            "openai",
        ),
        (
            [
                session("dev", ["gpt-6.1-sol"]),
                session("dev", ["claude-fable-5-1"], label="plugin.audit"),
            ],
            "openai",
        ),
        (
            [
                session("dev", ["gpt-6.1-sol"]),
                session("dev", ["claude-fable-5-1"], status="crashed"),
            ],
            "openai",
        ),
        ([session("dev", []), session("review", ["claude-fable-5-1"])], ""),
        ([session("dev", ["unrecognized-model"]), session("review", ["gpt-6-astra"])], ""),
        ([session("review", ["claude-fable-5-1"])], ""),
        ([session("dev", ["gpt-6.1-sol", "claude-fable-5-1"])], "openai"),
    ],
)
def test_attribution_uses_latest_completed_primary_development_observation(
    project, tmp_path, sessions, expected
):
    _, snapshot, unit = finished(project, tmp_path, sessions)
    assert unit.evidence["developer_family"] == expected
    if not expected:
        assert any(
            event["kind"] == "development-attribution-unavailable" for event in snapshot.events
        )


def test_resume_recovers_missing_family_and_invalidates_same_family_review(project, tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    from bmad_loop.campaign_supervisor import CampaignSupervisor

    runtime, snapshot, unit = finished(
        project,
        tmp_path,
        [session("dev", ["gpt-6.1-sol"]), session("review", ["claude-fable-5-1"])],
    )
    unit.evidence["developer_family"] = ""
    unit.evidence.pop("developer_attribution", None)
    unit.state = UnitState.READY_TO_PUBLISH
    unit.evidence["review"] = {"provider_family": "openai", "launch_id": "old-review"}
    snapshot = runtime.store.create(snapshot)
    with ThreadPoolExecutor() as executor:
        result = CampaignSupervisor(runtime.store, runtime)._reconciled(snapshot.id, executor)
    assert result.units[unit.id].evidence["developer_family"] == "openai"
    assert "review" not in result.units[unit.id].evidence


@pytest.mark.parametrize("later_writer", [False, True])
def test_resume_preserves_explicit_later_writer_or_missing_writer_observation(
    project, tmp_path, later_writer
):
    runtime, snapshot, unit = finished(
        project,
        tmp_path,
        [session("dev", ["gpt-6.1-sol"]), session("review", ["claude-fable-5-1"])],
    )
    unit.evidence.pop("developer_attribution", None)
    unit.evidence["developer_family"] = "anthropic" if later_writer else ""
    unit.evidence["worker_history"].append(
        {
            "outcome": "stage_complete",
            "result_contract": "repair-v1",
            "provider_family": "anthropic" if later_writer else "",
        }
    )
    assert not runtime.prepare_resume(snapshot)
    assert unit.evidence["developer_family"] == ("anthropic" if later_writer else "")


def test_resume_does_not_rewrite_issued_merge_state(project, tmp_path):
    runtime, snapshot, unit = finished(project, tmp_path, [session("dev", ["gpt-6.1-sol"])])
    unit.evidence.pop("developer_attribution", None)
    unit.evidence["developer_family"] = ""
    unit.state = UnitState.MERGE_REQUESTED
    intent = OutboxAction("merge", "github", unit.id, {"purpose": "merge"}, 0, state="sent_unknown")
    snapshot.outbox.append(intent)
    assert not runtime.prepare_resume(snapshot)
    assert unit.state == UnitState.MERGE_REQUESTED and intent.state == "sent_unknown"


@pytest.mark.parametrize("deferred", [False, True])
@pytest.mark.parametrize("reviewer_family", ["anthropic", "openai"])
def test_resumed_live_review_restores_attribution_only_after_accepted_result(
    project, tmp_path, deferred, reviewer_family
):
    from test_campaign_boundary_sync import setup

    from bmad_loop import campaign_boundary_sync as boundary

    runtime, snapshot, unit = setup(project, tmp_path)
    unit.spec = dataclasses.replace(unit.spec, kind="epic")
    unit.state = UnitState.REVIEWING
    unit.claim = WorkerClaim(unit.id, 1, 0, "live-review", role="publication_review")
    unit.evidence["developer_family"] = ""
    unit.evidence["worker_history"] = [
        {
            "outcome": "locally_complete",
            "launch_id": "old-execution",
            "sessions": [
                session("dev", ["gpt-6.1-sol"]),
                session("review", ["claude-fable-5-1"]),
            ],
        }
    ]
    assert not runtime.prepare_resume(snapshot)
    assert unit.evidence["developer_family"] == ""
    receipt = {
        "outcome": "stage_complete",
        "launch_id": unit.claim.launch_id,
        "head": unit.bindings["head"],
        "stage": "publication_review",
        "provider_family": reviewer_family,
        "validated": {"verdict": "approved"},
    }
    if deferred:
        before = boundary.collect(runtime, snapshot, unit, action_id="before")
        directory = runtime.store.directory(snapshot.id) / "workers" / unit.claim.launch_id
        directory.mkdir(parents=True)
        (directory / "request.json").write_text(
            json.dumps(
                {
                    "stage_boundary": {"inputs": before},
                    "coordinates": {"head": receipt["head"]},
                }
            )
        )
    action = OutboxAction(
        "observe", "observe-worker", unit.id, {"launch_id": unit.claim.launch_id}, 0
    )
    runtime._apply_worker(snapshot, unit, action, {"state": "finished", "receipt": receipt})
    if deferred:
        assert unit.claim.quiescent and unit.evidence["developer_family"] == ""
        boundary.after_apply(
            runtime,
            snapshot,
            OutboxAction("after", "stage-boundary-after", unit.id, {}, 0),
            {
                "launch_id": unit.claim.launch_id,
                "receipt": receipt,
                "inputs": boundary.collect(runtime, snapshot, unit, action_id="after"),
                "changed": False,
                "validated": True,
            },
        )
        assert "stage_sync_debt" not in unit.evidence
    assert unit.evidence["developer_family"] == "openai"
    if reviewer_family == "anthropic":
        assert unit.state == UnitState.READY_TO_PUBLISH
        assert unit.evidence["review"] == receipt
    else:
        assert unit.state == UnitState.SYNCING
        assert "review" not in unit.evidence
    assert not runtime.prepare_resume(snapshot)


@pytest.mark.parametrize("writer_family", ["", "anthropic"])
def test_completed_live_review_preserves_later_writer_attribution(project, tmp_path, writer_family):
    runtime, snapshot, unit = finished(project, tmp_path, [session("dev", ["gpt-6.1-sol"])])
    unit.evidence.pop("developer_attribution")
    unit.evidence["developer_family"] = writer_family
    unit.evidence["worker_history"].append(
        {
            "outcome": "stage_complete",
            "result_contract": "repair-v1",
            "provider_family": writer_family,
        }
    )
    unit.state = UnitState.REVIEWING
    unit.claim = WorkerClaim(unit.id, 1, 0, "live-review", role="publication_review")
    receipt = {
        "outcome": "stage_complete",
        "launch_id": unit.claim.launch_id,
        "head": unit.bindings["head"],
        "stage": "publication_review",
        "provider_family": "openai",
        "validated": {"verdict": "approved"},
    }
    action = OutboxAction(
        "observe", "observe-worker", unit.id, {"launch_id": unit.claim.launch_id}, 0
    )
    runtime._apply_worker(snapshot, unit, action, {"state": "finished", "receipt": receipt})
    assert unit.evidence["developer_family"] == writer_family
    assert "developer_attribution" not in unit.evidence
