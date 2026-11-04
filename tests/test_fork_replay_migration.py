"""Saved session replay follows a moved project without rewriting its history."""

from __future__ import annotations

import copy
import dataclasses
from pathlib import Path

import pytest
from conftest import _file_exists_cmd, ticket_row
from test_tickets_engine import _bound, _kinds, _policy, _tree, _write_plan, make_engine

from bmad_loop import verify
from bmad_loop.journal import load_state
from bmad_loop.model import Phase, SessionRecord
from bmad_loop.policy import VerifyPolicy


def _replay_case(project_v7, tmp_path, claim):
    tree = _tree(project_v7, ready_to_start=(ticket_row("1.1"),))
    marker = tmp_path / "verification-ready"
    marker.write_text("ready\n", encoding="utf-8")
    policy = dataclasses.replace(
        _policy(), verify=VerifyPolicy(commands=(_file_exists_cmd(marker),))
    )
    engine, adapter = make_engine(project_v7, tree, policy=policy)
    task = _bound(engine, tree)
    task.attempt = 1
    task.phase = Phase.DEV_VERIFY
    task.reverify_from = "deferred"
    task.baseline_commit = verify.rev_parse_head(project_v7.project)
    live = Path(tree.finds["1.1"].plan)
    relative = live.relative_to(project_v7.project)
    _write_plan(live, "built", baseline=task.baseline_commit)
    (project_v7.project / "src.txt").write_bytes(b"retained product\n")
    launched = project_v7.project if claim == "unmoved" else tmp_path / "old-machine" / "checkout"
    engine.state.project = str(launched)
    assert task.ticket is not None
    task.ticket = dataclasses.replace(task.ticket, plan=str(launched / relative))
    task.spec_file = str(live)
    task.dispatched_spec_file = str(live)
    if claim == "relative":
        reported = str(relative)
    elif claim == "wrong-plan":
        other = relative.with_name("other-plan.md")
        _write_plan(project_v7.project / other, "built", baseline=task.baseline_commit)
        reported = str(launched / other)
    elif claim == "external":
        reported = str(tmp_path / "external" / relative)
    elif claim == "traversal":
        reported = str(launched / ".." / launched.name / relative)
    else:
        reported = str(launched / relative)
    result = {
        "workflow": "auto-dev",
        "status": "built",
        "spec_file": reported,
        "extra": {"observations": ["original"]},
    }
    record = SessionRecord(task_id="1.1-dev-1", role="dev", status="completed", result_json=result)
    task.sessions.append(record)
    engine._save()
    return engine, adapter, task, live, record


@pytest.mark.parametrize("claim", ["moved", "relative", "unmoved"])
def test_reverify_moved_ticket_reaches_commands_without_rewriting_history(
    project_v7, tmp_path, monkeypatch, claim
):
    """The saved result is copied/rebased before the real ticket identity gate.

    The command verifier and acceptance continuation must both run. Removing
    replay rebasing fails the moved row; a shallow or in-place copy corrupts
    the original nested payload even for relative/unmoved positive controls.
    """
    engine, adapter, task, live, record = _replay_case(project_v7, tmp_path, claim)
    history = copy.deepcopy(record.to_dict())
    launch_project = engine.state.project
    baseline = task.baseline_commit
    seen = []
    accepted = []
    refused = []

    def observe_replay(_task, payload):
        seen.append(payload)
        payload["extra"]["observations"].append("replayed")

    monkeypatch.setattr(engine, "_post_dev_state_sync", observe_replay)
    monkeypatch.setattr(engine, "_review_and_commit", lambda unit: accepted.append(unit.story_key))
    monkeypatch.setattr(engine, "_defer", lambda _task, reason: refused.append(reason))

    engine._resume_reverify(task)
    engine._save()

    assert accepted == ["1.1"], refused
    [decision] = _kinds(engine, "reverify-decision")
    assert decision["action"] == "proceed" and decision["verification_sequence"] is not None
    commands = _kinds(engine, "verify-command-result")
    assert [(row["verification_stage"], row["returncode"]) for row in commands] == [("dev", 0)]
    assert seen[0]["spec_file"] == (
        history["result_json"]["spec_file"] if claim == "relative" else str(live)
    )
    assert record.to_dict() == history
    saved = load_state(engine.run_dir)
    assert saved.tasks["1.1"].sessions[0].to_dict() == history
    assert saved.project == launch_project and task.baseline_commit == baseline
    assert adapter.sessions == []


@pytest.mark.parametrize("claim", ["wrong-plan", "external", "traversal"])
def test_reverify_migration_still_rejects_mismatching_ticket_plan(
    project_v7, tmp_path, monkeypatch, claim
):
    """Root mapping grants no authority to a different or non-project plan.

    Ablation: bypass the claimed-path identity gate in verify_dev_tickets;
    these rows must proceed and fail their expected-defer assertions.
    """
    engine, adapter, task, live, record = _replay_case(project_v7, tmp_path, claim)
    history = copy.deepcopy(record.to_dict())
    accepted = []
    refused = []
    monkeypatch.setattr(engine, "_review_and_commit", lambda unit: accepted.append(unit.story_key))
    monkeypatch.setattr(engine, "_defer", lambda _task, reason: refused.append(reason))

    engine._resume_reverify(task)
    engine._save()

    [decision] = _kinds(engine, "reverify-decision")
    assert decision["action"] == "defer"
    assert (
        "dev result names" in decision["reason"] and f"bound to plan {live}" in decision["reason"]
    )
    assert decision["verification_sequence"] is None
    assert len(refused) == 1 and accepted == []
    assert _kinds(engine, "verify-command-result") == []
    assert record.to_dict() == history
    assert load_state(engine.run_dir).tasks["1.1"].sessions[0].to_dict() == history
    assert adapter.sessions == []


@pytest.mark.parametrize("replay", ["dev", "review", "salvage"])
def test_saved_session_replay_rebases_copy_before_ticket_validation(project_v7, tmp_path, replay):
    """Crash replay and pending review salvage use the same moved result view."""
    engine, adapter, task, live, record = _replay_case(project_v7, tmp_path, "moved")
    task.reverify_from = ""
    task.phase = Phase.DEV_RUNNING if replay == "dev" else Phase.REVIEW_RUNNING
    if replay != "dev":
        task.review_cycle = 1
        record.task_id = "1.1-review-1"
        record.role = "review"
    if replay == "salvage":
        record.status = "timeout"
        task.salvage_refile_pending = True
    history = copy.deepcopy(record.to_dict())

    resumed = (
        engine._pending_salvage_session(task)
        if replay == "salvage"
        else engine._resumable_session(task)
    )

    assert resumed is not None
    role, result = resumed
    assert role == ("dev" if replay == "dev" else "review")
    assert result.result_json is not None and result.result_json["spec_file"] == str(live)
    outcome = engine._verify_dev_artifacts(task, result.result_json)
    assert outcome.ok, outcome.reason
    result.result_json["extra"]["observations"].append("replayed")
    engine._save()
    assert record.to_dict() == history
    assert load_state(engine.run_dir).tasks["1.1"].sessions[0].to_dict() == history
    assert adapter.sessions == []
