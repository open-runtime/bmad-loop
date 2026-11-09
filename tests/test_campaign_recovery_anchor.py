"""Real recovery pushes restore only their authenticated owned checkpoints."""

import copy
import dataclasses
import json

import pytest
import test_campaign_finalization as finalization_cases
import test_campaign_internal_recovery as recovery_cases
from conftest import git
from test_campaign_boundary_sync import Gateway, prepared
from test_campaign_takeover import configured

from bmad_loop import campaign_runtime, verify
from bmad_loop.campaign_model import OutboxAction, UnitState, WorkerClaim

finalization = finalization_cases.finalization
handoff = recovery_cases.handoff


def consume(project, tmp_path, monkeypatch, pusher, before, result, request, policy):
    runtime, snapshot, unit = configured(project, tmp_path)
    snapshot.spec = dataclasses.replace(snapshot.spec, remote="campaign-test")
    snapshot.remote_url = git(project.project, "remote", "get-url", "campaign-test")
    unit.spec = dataclasses.replace(unit.spec, selector="1-1-owned", scopes=("owned.py",))
    unit.state = UnitState.RUNNING
    unit.evidence["dev_attempts_spent"] = 0
    unit.bindings.update(
        branch=pusher.branch,
        head=before,
        base=before,
        pr={"number": 2, "node_id": "P2"},
        issue={"number": 1},
    )
    unit.claim = WorkerClaim(unit.id, 1, snapshot.generation, "recovery-worker", role="recovery")
    runtime._pusher = lambda *args: pusher
    runtime.template = lambda *args: type("Template", (), {"policy": policy})()
    # child_main adds the exact terminal Git head after the producer returns.
    result["head"] = verify.rev_parse_head(project.project)
    result["base"] = before
    runtime._gateway = Gateway(result["head"])
    runtime._gateway.pr.update(merged=False)
    runtime._gateway.pr["head"].update(ref=pusher.branch, repo={"full_name": snapshot.repository})
    directory = runtime.store.directory(snapshot.id) / "workers" / unit.claim.launch_id
    directory.mkdir(parents=True)
    (directory / "request.json").write_text(
        json.dumps(
            {
                **request,
                "kind": "recovery",
                "checkpoint_base": before,
                "source": {"kind": "sprint-status"},
            }
        ),
        encoding="utf-8",
    )
    result["launch_id"] = unit.claim.launch_id
    action = OutboxAction(
        "observe",
        "observe-worker",
        unit.id,
        {"launch_id": unit.claim.launch_id},
        snapshot.generation,
    )
    monkeypatch.setattr(
        campaign_runtime,
        "observe",
        lambda *args, **kwargs: {"state": "finished", "receipt": result},
    )
    # These fixtures run the real deterministic producer in-process, without a
    # mux actor. The production child test separately covers actor settlement.
    monkeypatch.setattr("bmad_loop.campaign_finalization.continuation_run_id", lambda request: None)
    observed = runtime._perform_observe_worker(snapshot, action)
    assert observed.state == "confirmed", observed
    runtime._apply_worker(snapshot, unit, action, observed.data)
    return runtime, snapshot, unit


def assert_boundary_ready(runtime, snapshot, unit, head):
    assert unit.bindings["last_pushed_head"] == head
    assert unit.bindings["remote_head_anchor"] == head
    prepared(runtime, snapshot, unit)
    assert unit.evidence["stage_boundary_ready"]
    assert "remote_head_sync" not in unit.evidence


def test_internal_recovery_checkpoint_reaches_normal_next_stage_boundary(
    handoff, tmp_path, monkeypatch
):
    project, context, _, _, pusher, before, _, _ = handoff
    recovery_cases.request(handoff)
    recovered = recovery_cases.invoke(handoff)
    result = {"outcome": "recovered", "recovery": recovered, "head": recovered["head"]}
    runtime, snapshot, unit = consume(
        project,
        tmp_path,
        monkeypatch,
        pusher,
        before,
        result,
        {"workspace": str(project.project), "recovery_action": "retry-1"},
        context.policy,
    )
    assert result["confirmed_owned_checkpoint"]["action_id"] == "recovery:retry-1"
    assert unit.state == UnitState.READY
    assert_boundary_ready(runtime, snapshot, unit, recovered["head"])


@pytest.mark.parametrize("finalization", ["", "fails-until-repaired"], indirect=True)
def test_retained_finalization_checkpoint_reaches_normal_boundary(
    finalization, tmp_path, monkeypatch
):
    project, context, _, _, _, _, request, pusher, before, _ = finalization
    result = finalization_cases.invoke(finalization)
    runtime, snapshot, unit = consume(
        project,
        tmp_path,
        monkeypatch,
        pusher,
        before,
        result,
        request,
        context.policy,
    )
    assert result["confirmed_owned_checkpoint"]["head"] == result["head"]
    if result["outcome"] == "retained_verification_failed":
        assert unit.hold.kind == "verification_failed"
        assert result["retained_checkpoint"]["state"] == "confirmed"
    else:
        assert unit.state == UnitState.LOCALLY_COMPLETE
    assert_boundary_ready(runtime, snapshot, unit, result["head"])


@pytest.mark.parametrize(
    "fault", ["missing", "uncertain", "wrong_action", "wrong_head", "incomplete", "no_receipt"]
)
def test_recovery_anchor_refuses_nonmatching_publication_proof(
    handoff, tmp_path, monkeypatch, fault
):
    project, context, _, _, pusher, before, _, _ = handoff
    recovery_cases.request(handoff)
    recovered = recovery_cases.invoke(handoff)
    result = {
        "outcome": "recovered",
        "recovery": copy.deepcopy(recovered),
        "head": recovered["head"],
    }
    if fault == "wrong_action":
        assert pusher.push("recovery:foreign-action", recovered["head"]).state == "confirmed"
    rows = list(pusher.observations())
    row = next(row for row in rows if row.action_id == "recovery:retry-1")
    if fault == "missing":
        rows.remove(row)
    elif fault == "uncertain":
        rows[rows.index(row)] = dataclasses.replace(row, state="sent_unknown")
    elif fault == "wrong_action":
        result["recovery"]["action_id"] = "foreign-action"
    elif fault == "wrong_head":
        rows[rows.index(row)] = dataclasses.replace(row, head=before)
    elif fault == "incomplete":
        result["recovery"]["state"] = "committed"
    else:
        result.pop("recovery")
    monkeypatch.setattr(pusher, "observations", lambda: rows)
    # A terminal child cannot authenticate its own arbitrary marker.
    result["confirmed_owned_checkpoint"] = dataclasses.asdict(row)
    runtime, snapshot, unit = consume(
        project,
        tmp_path,
        monkeypatch,
        pusher,
        before,
        result,
        {"workspace": str(project.project), "recovery_action": "retry-1"},
        context.policy,
    )
    assert unit.hold.kind == "ownership_uncertain"
    assert "confirmed_owned_checkpoint" not in result
    assert "remote_head_anchor" not in unit.bindings
    assert "last_pushed_head" not in unit.bindings
    assert unit.claim.quiescent


def test_confirmed_recovery_does_not_adopt_later_tip_or_erase_external_debt(
    handoff, tmp_path, monkeypatch
):
    project, context, _, _, pusher, before, _, remote = handoff
    recovery_cases.request(handoff)
    recovered = recovery_cases.invoke(handoff)
    head = recovered["head"]
    # Native readback may advance after the confirmed checkpoint. Its SHA is
    # not the checkpoint we authenticated, even if all its parents are known.
    later = git(remote, "commit-tree", head + "^{tree}", "-p", head, "-m", "external follow-up")
    git(remote, "update-ref", "refs/heads/" + pusher.branch, later, head)
    assert pusher.reconcile("recovery:retry-1").remote_head == later
    result = {"outcome": "recovered", "recovery": recovered, "head": head}
    runtime, snapshot, unit = consume(
        project,
        tmp_path,
        monkeypatch,
        pusher,
        before,
        result,
        {"workspace": str(project.project), "recovery_action": "retry-1"},
        context.policy,
    )
    assert unit.bindings["remote_head_anchor"] == head != later
    unit.evidence["remote_head_sync"] = {"head": later, "id": "preserved-debt"}
    unit.state = UnitState.RUNNING
    action = OutboxAction(
        "repeat",
        "observe-worker",
        unit.id,
        {"launch_id": unit.claim.launch_id},
        snapshot.generation,
    )
    runtime._apply_worker(snapshot, unit, action, {"state": "finished", "receipt": result})
    assert unit.evidence["remote_head_sync"] == {"head": later, "id": "preserved-debt"}
    assert unit.bindings["remote_head_anchor"] == head
