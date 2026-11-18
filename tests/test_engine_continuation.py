"""Retained campaign entry preserves the original task and dirty product."""

import copy
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import git, ticket_row, write_spec, write_sprint
from test_engine import make_engine
from test_tickets_engine import _tree, _write_plan
from test_tickets_engine import make_engine as make_tickets_engine

from bmad_loop.engine import CAMPAIGN_DEVELOPMENT_RESUME, RunPaused
from bmad_loop.model import PauseKind, Phase, SessionRecord, StoryTask, TokenUsage
from bmad_loop.tickets_source import ticket_binding


class DispatchObserved(BaseException):
    """Stop at the actual session-dispatch seam without creating any worker."""


@pytest.mark.parametrize("source", ["sprint-status", "tickets"])
def test_retained_development_keeps_dirty_work_preimages_and_accounting(
    request, monkeypatch, source
):
    if source == "tickets":
        layout = request.getfixturevalue("project_v7")
        root = layout.project
        tree = _tree(layout, in_progress=(ticket_row("1.1", status="in-progress"),))
        engine, adapter = make_tickets_engine(layout, tree)
        # The ordinary picker still refuses an active ticket without owned task
        # history; continuation does not weaken that gate.
        with pytest.raises(RunPaused) as foreign:
            engine._pick_next()
        assert foreign.value.cause.code == "foreign-active"
        assert engine.state.tasks == {}
        task = StoryTask("1.1", 0, ticket=ticket_binding(tree.finds["1.1"]))
        plan = Path(task.ticket.plan)
    else:
        project = request.getfixturevalue("project")
        root = project.project
        write_sprint(project, {"1-1-owned": "in-progress"})
        engine, adapter = make_engine(project, [])
        task = StoryTask("1-1-owned", 1)
        plan = project.implementation_artifacts / "spec-1-1-owned.md"
    product = root / "owned.py"
    product.write_text("original product\n")
    git(root, "add", "owned.py")
    git(root, "commit", "-qm", "original baseline")
    baseline = git(root, "rev-parse", "HEAD")
    if source == "tickets":
        _write_plan(plan, "ready-for-dev", baseline=baseline)
    else:
        write_spec(plan, "ready-for-dev", baseline)
    original_plan = plan.read_bytes()
    if source == "tickets":
        _write_plan(plan, "in-progress", baseline=baseline)
    else:
        write_spec(plan, "in-progress", baseline)
    product.write_text("published partial checkpoint\n")
    git(root, "add", "owned.py")
    git(root, "commit", "-qm", "partial checkpoint")
    checkpoint = git(root, "rev-parse", "HEAD")
    assert checkpoint != baseline
    product.write_text("staged partial work\n")
    git(root, "add", "owned.py")
    product.write_text("unstaged partial work\n")
    (root / "initial-untracked.txt").write_text("original residue\n")
    (root / "partial-untracked.txt").write_text("unfinished work\n")
    task.phase = Phase.DEV_VERIFY
    task.attempt = 3
    task.dev_usage_relaunches = 1
    task.review_cycle = 2
    task.review_usage_relaunches = 1
    task.generation = 4
    task.baseline_commit = baseline
    task.baseline_untracked = ["initial-untracked.txt"]
    task.baseline_artifacts = {"historical-artifact": [17, 19]}
    task.baseline_ledger_digest = "original-ledger-digest"
    task.pre_harvest_ledger = "original ledger"
    task.pre_harvest_ledger_captured = True
    task.post_engine_ledger_digest = "original-post-engine-digest"
    task.harvested_deferrals = [{"id": "retained-finding"}]
    task.dispatched_spec_file = str(plan)
    task.dispatched_spec_snapshot = original_plan
    task.spec_file = str(plan)
    task.plan_review_owed = True
    task.resume_blocked = True
    task.sessions = [SessionRecord("prior-dev", "dev", "crashed", session_id="predecessor")]
    task.tokens = TokenUsage(input_tokens=23, output_tokens=29)
    engine.state.tasks[task.story_key] = task
    engine.campaign_execution = SimpleNamespace(checkpoint_interval_s=600)
    kept = {
        name: copy.deepcopy(getattr(task, name))
        for name in (
            "generation",
            "baseline_commit",
            "baseline_untracked",
            "baseline_artifacts",
            "baseline_ledger_digest",
            "pre_harvest_ledger",
            "pre_harvest_ledger_captured",
            "post_engine_ledger_digest",
            "harvested_deferrals",
            "dispatched_spec_file",
            "dispatched_spec_snapshot",
            "plan_review_owed",
            "sessions",
            "tokens",
            "review_cycle",
            "review_usage_relaunches",
            "dev_usage_relaunches",
        )
    }
    dirty = {
        "head": checkpoint,
        "index": git(root, "show", ":owned.py"),
        "worktree": product.read_bytes(),
        "plan": plan.read_bytes(),
        "untracked": (root / "partial-untracked.txt").read_bytes(),
    }
    dispatched = []
    checked = []

    def validate_retained():
        checked.append("retained authority checked")

    def observe(selected, **kwargs):
        assert selected is task
        assert task.phase == Phase.DEV_RUNNING
        assert task.attempt == 4 and task.charged_attempts == 3
        assert {name: getattr(task, name) for name in kept} == kept
        assert git(root, "rev-parse", "HEAD") == dirty["head"]
        assert git(root, "show", ":owned.py") == dirty["index"]
        assert product.read_bytes() == dirty["worktree"]
        assert plan.read_bytes() == dirty["plan"]
        assert (root / "partial-untracked.txt").read_bytes() == dirty["untracked"]
        assert kwargs["preserve_dispatched_spec_snapshot"] is True
        assert kwargs["commit_engine_ledger"] is False
        assert kwargs["prelaunch_validator"] is not None
        kwargs["prelaunch_validator"]()
        assert str(plan) in kwargs["prompt"]
        assert CAMPAIGN_DEVELOPMENT_RESUME in kwargs["prompt"]
        if source == "tickets":
            assert "ticket 1.1" in kwargs["prompt"]
        dispatched.append(kwargs)
        raise DispatchObserved()

    monkeypatch.setattr(engine, "_run_session", observe)
    with pytest.raises(DispatchObserved):
        engine._continue_campaign_development(task, prelaunch_validator=validate_retained)
    assert len(dispatched) == 1
    assert engine._campaign_development_entry is None
    assert checked == ["retained authority checked"]
    assert adapter.sessions == []
    if source == "tickets":
        assert tree.marks == []


def test_retained_development_missing_source_pauses_before_spending_attempt(project):
    engine, adapter = make_engine(project, [])
    task = StoryTask("1-1-owned", 1, phase=Phase.DEV_VERIFY, attempt=2)
    engine.state.tasks[task.story_key] = task
    engine.campaign_execution = SimpleNamespace(checkpoint_interval_s=600)
    with pytest.raises(RunPaused) as paused:
        engine._continue_campaign_development(task)
    assert paused.value.cause.kind == PauseKind.UNKNOWN
    assert paused.value.cause.code == "retained_source_unavailable"
    assert task.attempt == 2 and task.phase == Phase.DEV_VERIFY
    assert engine._campaign_development_entry is None
    assert adapter.sessions == []


def test_retained_prelaunch_refusal_preserves_changed_work_as_unknown_pause(project, monkeypatch):
    from bmad_loop.campaign_model import CampaignError

    engine, adapter = make_engine(project, [])
    task = StoryTask("1-1-owned", 1, phase=Phase.DEV_VERIFY, attempt=2)
    engine.state.tasks[task.story_key] = task
    engine.campaign_execution = SimpleNamespace(checkpoint_interval_s=600)
    changed = project.project / "owner-edit.txt"
    checked = []

    def validate():
        checked.append(True)
        raise CampaignError("source fingerprint changed after the permit was consumed")

    def enter(selected):
        assert selected is task
        changed.write_text("preserve concurrent owner work\n")
        engine._campaign_development_prelaunch()
        pytest.fail("changed continuation reached launch")

    monkeypatch.setattr(engine, "_drive_story", enter)
    with pytest.raises(RunPaused) as paused:
        engine._continue_campaign_development(task, prelaunch_validator=validate)
    assert paused.value.cause.kind == PauseKind.UNKNOWN
    assert paused.value.cause.code == "continuation_authority_changed"
    assert changed.read_text() == "preserve concurrent owner work\n"
    assert checked == [True]
    assert task.attempt == 2 and task.phase == Phase.DEV_VERIFY
    assert engine._campaign_development_entry is None
    assert engine._campaign_development_prelaunch is None
    assert adapter.sessions == []
