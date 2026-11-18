"""Typed pause producers survive engine persistence without interpreting prose."""

import dataclasses

import pytest
from conftest import ticket_row, write_sprint
from test_engine import make_engine
from test_tickets_engine import _tree
from test_tickets_engine import make_engine as make_tickets_engine

from bmad_loop import campaign_hold
from bmad_loop.adapters.base import SessionResult
from bmad_loop.checkpoints import CheckpointFlags, CheckpointFlowMixin
from bmad_loop.engine import RunPaused, _session_pause_cause
from bmad_loop.journal import load_state
from bmad_loop.model import PAUSE_ESCALATION, PauseCause, PauseKind, Phase, StoryTask
from bmad_loop.tickets_source import halt_pause_cause
from bmad_loop.tickettree import TicketsCrashed


def test_run_paused_preserves_legacy_positional_arguments_without_guessing():
    pause = RunPaused("authentication failed; quota reset requested", "escalation", "story")
    assert str(pause) == pause.reason
    assert pause.stage == "escalation" and pause.story_key == "story"
    assert pause.cause is None


@pytest.mark.parametrize("kind", list(PauseKind))
def test_engine_persists_explicit_pause_cause(project, monkeypatch, kind):
    write_sprint(project, {"1-1-owned": "ready-for-dev"})
    engine, adapter = make_engine(project, [])
    cause = PauseCause(kind, "scripted.producer", "explicit_code")

    def pause():
        raise RunPaused("A contextual explanation", PAUSE_ESCALATION, "1-1-owned", cause=cause)

    monkeypatch.setattr(engine, "_loop", pause)
    summary = engine.run()
    assert summary.paused
    assert load_state(engine.run_dir).paused_cause == cause
    assert adapter.sessions == []


@pytest.mark.parametrize(
    "result,kind,code",
    [
        (
            SessionResult(
                status="crashed", env_fault=True, native_binding={"outcome": "authentication"}
            ),
            PauseKind.AUTHENTICATION_FAILURE,
            "authentication",
        ),
        (
            SessionResult(status="crashed", env_fault=True, transport_fault="transport"),
            PauseKind.TRANSPORT_FAILURE,
            "transport",
        ),
        (
            SessionResult(status="stalled", env_fault=True, usage_limited="scripted account limit"),
            PauseKind.QUOTA_WAIT,
            "usage_limit",
        ),
        (
            SessionResult(status="crashed", env_fault=True, transport_fault="environment"),
            PauseKind.TECHNICAL_FAILURE,
            "environment",
        ),
        (
            SessionResult(status="stalled", parked=True, parked_evidence="scripted human prompt"),
            PauseKind.OWNER_DECISION,
            "human_prompt",
        ),
    ],
)
def test_current_session_fields_produce_distinct_persisted_pauses(project, result, kind, code):
    write_sprint(project, {"1-1-owned": "ready-for-dev"})
    engine, adapter = make_engine(project, [result])
    summary = engine.run()
    assert summary.paused
    saved = load_state(engine.run_dir)
    assert saved.paused_cause == PauseCause(kind, "engine.session", code)
    assert len(adapter.sessions) == 1
    assert saved.tasks["1-1-owned"].attempt == 1


def test_session_prose_does_not_supply_missing_failure_authority():
    result = SessionResult(
        status="completed",
        result_json={"summary": "authentication failure; transport quota owner approval"},
    )
    assert _session_pause_cause(result, None) is None
    assert _session_pause_cause(None, None) is None
    assert halt_pause_cause("quota requested by text").kind == PauseKind.UNKNOWN


@pytest.mark.parametrize(
    "producer,code",
    [
        ("_pause_plan_checkpoint", "plan_checkpoint"),
        ("_pause_plan_review_owed", "plan_review_owed"),
    ],
)
def test_actual_plan_checkpoint_producer_is_preserved_by_engine_run(
    project, monkeypatch, producer, code
):
    write_sprint(project, {"1-1-owned": "ready-for-dev"})
    engine, adapter = make_engine(project, [])
    task = StoryTask("1-1-owned", 1, phase=Phase.DEV_VERIFY, attempt=2, plan_review_owed=True)
    engine.state.tasks[task.story_key] = task

    def pause():
        getattr(CheckpointFlowMixin, producer)(engine, task)

    monkeypatch.setattr(engine, "_loop", pause)
    assert engine.run().paused
    saved = load_state(engine.run_dir)
    assert saved.paused_cause == PauseCause(PauseKind.SOURCE_CHECKPOINT, "source.checkpoint", code)
    assert saved.tasks[task.story_key].phase == Phase.DEV_VERIFY
    assert saved.tasks[task.story_key].attempt == 2
    assert adapter.sessions == []


def test_actual_story_checkpoint_producer_is_preserved_by_engine_run(project, monkeypatch):
    write_sprint(project, {"1-1-owned": "done", "1-2-next": "ready-for-dev"})
    engine, adapter = make_engine(project, [])
    task = StoryTask("1-1-owned", 1, phase=Phase.DONE, commit_sha="accepted-commit", attempt=2)
    engine.state.tasks[task.story_key] = task
    monkeypatch.setattr(
        engine, "_entry_checkpoint_flags", lambda task: CheckpointFlags(False, True), raising=False
    )
    monkeypatch.setattr(engine, "_schedule_complete", lambda: False, raising=False)
    monkeypatch.setattr(engine, "_max_stories_reached", lambda: False, raising=False)
    monkeypatch.setattr(engine, "_loop", lambda: CheckpointFlowMixin._after_story(engine, task))
    assert engine.run().paused
    saved = load_state(engine.run_dir)
    assert saved.paused_cause == PauseCause(
        PauseKind.SOURCE_CHECKPOINT, "source.checkpoint", "story_checkpoint"
    )
    assert saved.tasks[task.story_key].phase == Phase.DONE
    assert saved.tasks[task.story_key].commit_sha == "accepted-commit"
    assert adapter.sessions == []


@pytest.mark.parametrize("fault", [False, True])
def test_ticket_checkpoint_and_tree_fault_have_distinct_causes(project_v7, fault):
    row = ticket_row("1.1", refined=True, plan_checkpoint=True, file="story-cart-model.md")
    tree = _tree(project_v7, ready_to_start=(row,))
    if fault:
        tree.faults["find"] = TicketsCrashed("scripted read failed; no authority inferred")
    engine, adapter = make_tickets_engine(project_v7, tree)
    assert engine.run().paused
    saved = load_state(engine.run_dir)
    assert saved.paused_cause == PauseCause(
        PauseKind.TECHNICAL_FAILURE if fault else PauseKind.SOURCE_CHECKPOINT,
        "tickets.pick",
        "tree-fault" if fault else "plan-checkpoint",
    )
    assert saved.tasks == {} and adapter.sessions == []


def test_explicit_campaign_operator_hold_has_typed_owner_cause(project):
    write_sprint(project, {"1-1-owned": "ready-for-dev"})
    engine, adapter = make_engine(project, [])
    engine.campaign_execution = object()
    task = StoryTask("1-1-owned", 1, phase=Phase.DEV_RUNNING)
    before = dataclasses.asdict(task)
    token = campaign_hold.install(lambda: True)
    try:
        with pytest.raises(RunPaused) as paused:
            engine._run_session(task, "dev", "retained prompt", 1)
    finally:
        campaign_hold.reset(token)
    assert paused.value.cause == PauseCause(
        PauseKind.OWNER_DECISION, "engine.campaign", "campaign_operator_hold"
    )
    assert dataclasses.asdict(task) == before
    assert adapter.sessions == []
