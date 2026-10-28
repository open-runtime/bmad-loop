"""open-runtime fork F6: a retry after a stall, timeout, crash or turn error
resumes the previous CLI conversation (`[recovery] max_session_resumes`) instead
of starting over; a failed verify or review always starts fresh.

Engine-level, over `MockAdapter`: the adapter's profile is marked resumable and
each scripted session records the spec it was launched with, so the tests read
the resume decision off `SessionSpec.resume_session_id` and the prompt."""

from __future__ import annotations

import dataclasses
from types import SimpleNamespace

from conftest import dev_effect, git, review_effect, write_sprint
from test_engine import QUIET, make_engine, resume_engine

from bmad_loop import verify
from bmad_loop.adapters.base import SessionResult, SessionSpec
from bmad_loop.journal import load_state, save_state
from bmad_loop.model import Phase, SessionRecord, StoryTask
from bmad_loop.policy import GatesPolicy, Policy, RecoveryPolicy, ReviewPolicy, ScmPolicy
from bmad_loop.verify import rev_parse_head


def _policy(*, resumes=2, rollback=False, **policy_kw) -> Policy:
    return Policy(
        gates=GatesPolicy(mode="none"),
        notify=QUIET,
        scm=ScmPolicy(rollback_on_failure=rollback),
        recovery=RecoveryPolicy(max_session_resumes=resumes),
        **policy_kw,
    )


def _resumable(adapter):
    adapter.profile = SimpleNamespace(resumable=True)
    return adapter


def _stalled(project, session_id, *, commit=True, ended_fields=None):
    """A dev session that did some work (a commit above the baseline) and then
    went idle: the 16.1-dev-1 shape."""

    def effect(spec: SessionSpec) -> SessionResult:
        transcript = project.project / f"{session_id}.jsonl"
        transcript.write_text("{}\n", encoding="utf-8")
        src = project.project / "src.txt"
        src.write_text(src.read_text() + f"partial work in {spec.task_id}\n")
        if commit:
            git(project.project, "add", "src.txt")
            git(project.project, "commit", "-q", "-m", "partial work")
        return SessionResult(
            status="stalled",
            session_id=session_id,
            transcript_path=str(transcript),
            **(ended_fields or {}),
        )

    return effect


def _kinds(engine):
    return [e["kind"] for e in engine.journal.entries()]


def test_a_stalled_dev_session_is_resumed_in_place(project):
    """Attempt 1 stalls with committed work. Upstream (rollback off) paused for
    manual recovery; with resumes on, attempt 2 resumes the same conversation on
    the kept tree, with the plain-text continuation, and the story finishes.

    Ablation: drop the `_resume_candidate(...)` arm of the RETRY branch and the
    run pauses with `rollback-manual-required`."""
    write_sprint(project, {"1-1-a": "ready-for-dev"})
    engine, adapter = make_engine(
        project,
        [
            _stalled(project, "sess-1"),
            dev_effect(project, "1-1-a"),
            review_effect(project, "1-1-a", clean=True),
        ],
        policy=_policy(),
    )
    _resumable(adapter)
    summary = engine.run()

    assert summary.done == 1 and not summary.paused
    dev2 = adapter.sessions[1]
    assert dev2.resume_session_id == "sess-1"
    assert dev2.resume_transcript_path.endswith("sess-1.jsonl")
    assert dev2.prompt.startswith("You are resuming your own earlier bmad-loop dev session")
    assert "do not restart it" in dev2.prompt
    kinds = _kinds(engine)
    assert "session-resume" in kinds and "rollback-manual-required" not in kinds
    row = next(e for e in engine.journal.entries() if e["kind"] == "session-resume")
    assert (row["session_id"], row["ended"], row["resumes"]) == ("sess-1", "stalled", 1)
    # the fresh review launch that follows ends the dev resume chain
    assert load_state(engine.run_dir).tasks["1-1-a"].session_resumes == 0
    assert "partial work in" in (project.project / "src.txt").read_text()


def test_resumes_off_keeps_the_upstream_manual_recovery_pause(project):
    write_sprint(project, {"1-1-a": "ready-for-dev"})
    engine, adapter = make_engine(project, [_stalled(project, "sess-1")], policy=_policy(resumes=0))
    _resumable(adapter)
    summary = engine.run()

    assert summary.paused
    assert "rollback-manual-required" in _kinds(engine)
    assert "session-resume" not in _kinds(engine)


def test_a_profile_that_cannot_resume_starts_fresh(project):
    write_sprint(project, {"1-1-a": "ready-for-dev"})
    engine, adapter = make_engine(project, [_stalled(project, "sess-1")], policy=_policy())
    summary = engine.run()  # MockAdapter has no resumable profile

    assert summary.paused and "session-resume" not in _kinds(engine)


def test_a_failed_verify_starts_fresh(project):
    """A completed session whose product failed verify is a failed approach: the
    retry rolls back and starts a fresh session, as upstream."""
    write_sprint(project, {"1-1-a": "ready-for-dev"})
    engine, adapter = make_engine(
        project,
        [
            dev_effect(project, "1-1-a", final_status="in-progress"),
            dev_effect(project, "1-1-a"),
            review_effect(project, "1-1-a", clean=True),
        ],
        policy=_policy(rollback=True),
    )
    _resumable(adapter)
    summary = engine.run()

    assert summary.done == 1
    assert adapter.sessions[1].resume_session_id is None
    assert "session-resume" not in _kinds(engine)


def test_the_resume_chain_is_capped(project):
    """After `max_session_resumes` resumes in a row the next retry is fresh (and,
    with rollback off and work committed, pauses for manual recovery)."""
    write_sprint(project, {"1-1-a": "ready-for-dev"})
    policy = _policy(resumes=1)
    policy = dataclasses.replace(
        policy, limits=dataclasses.replace(policy.limits, max_dev_attempts=5)
    )
    engine, adapter = make_engine(
        project,
        [_stalled(project, "sess-1"), _stalled(project, "sess-1")],
        policy=policy,
    )
    _resumable(adapter)
    summary = engine.run()

    assert [s.resume_session_id for s in adapter.sessions] == [None, "sess-1"]
    assert summary.paused and "rollback-manual-required" in _kinds(engine)


def test_a_parked_session_is_not_resumed(project):
    """A session parked on a human question pauses; its conversation is not
    resumed by a retry (the answer comes through resolve)."""
    write_sprint(project, {"1-1-a": "ready-for-dev"})
    engine, adapter = make_engine(
        project,
        [
            _stalled(
                project,
                "sess-1",
                ended_fields={"parked": True, "parked_evidence": "question: Should I?"},
            )
        ],
        policy=_policy(),
    )
    _resumable(adapter)
    summary = engine.run()

    assert summary.paused and "session-resume" not in _kinds(engine)
    record = load_state(engine.run_dir).tasks["1-1-a"].sessions[0]
    assert record.ended == "parked"


def test_a_review_retry_resumes_the_stalled_review(project):
    write_sprint(project, {"1-1-a": "ready-for-dev"})

    def stalled_review(spec: SessionSpec) -> SessionResult:
        transcript = project.project / "rev-1.jsonl"
        transcript.write_text("{}\n", encoding="utf-8")
        return SessionResult(status="stalled", session_id="rev-1", transcript_path=str(transcript))

    policy = _policy(review=ReviewPolicy(enabled=True, trigger="always"))
    engine, adapter = make_engine(
        project,
        [dev_effect(project, "1-1-a"), stalled_review, review_effect(project, "1-1-a", clean=True)],
        policy=policy,
    )
    _resumable(adapter)
    summary = engine.run()

    assert summary.done == 1
    assert [s.role for s in adapter.sessions] == ["dev", "review", "review"]
    assert adapter.sessions[2].resume_session_id == "rev-1"
    assert adapter.sessions[2].prompt.startswith(
        "You are resuming your own earlier bmad-loop review session"
    )


def _restart_after_stall(project, *, accept: bool, dirty: bool):
    """Attempt 1 stalled (recorded), the run paused, and is resumed later. With
    the tree clean at the accepted baseline the restart resumes the
    conversation; with uncommitted work a rollback resets the tree first."""
    write_sprint(project, {"1-1-a": "ready-for-dev"})
    policy = _policy(rollback=True)
    engine, adapter = make_engine(project, [], policy=policy)
    repo = project.project
    task = StoryTask(story_key="1-1-a", epic=1, phase=Phase.DEV_RUNNING, attempt=1)
    task.baseline_commit = rev_parse_head(repo)
    task.baseline_untracked = sorted(verify.untracked_files(repo))
    transcript = repo / "sess-1.jsonl"
    transcript.write_text("{}\n", encoding="utf-8")
    task.record_session(
        SessionRecord(
            task_id="1-1-a-dev-1",
            role="dev",
            status="stalled",
            adapter=policy.adapter.resolved("dev").name,
            session_id="sess-1",
            transcript_path=str(transcript),
        )
    )
    engine.state.tasks[task.story_key] = task
    engine._save()
    src = repo / "src.txt"
    src.write_text(src.read_text() + "kept work\n")
    git(repo, "add", "src.txt")
    git(repo, "commit", "-q", "-m", "wip: keep the stopped attempt's work")
    if dirty:
        src.write_text(src.read_text() + "uncommitted\n")
    state = load_state(engine.run_dir)
    state.accept_baseline = accept
    save_state(engine.run_dir, state)
    resumed, adapter = resume_engine(
        project,
        engine,
        [dev_effect(project, "1-1-a"), review_effect(project, "1-1-a", clean=True)],
        policy=policy,
    )
    _resumable(adapter)
    return resumed, adapter, resumed.run()


def test_a_restart_on_a_kept_tree_resumes_the_conversation(project):
    """The supervisor's keep-work path: WIP commit, `resume --accept-baseline`.
    Nothing is reset, so the restarted attempt resumes the stalled session."""
    resumed, adapter, summary = _restart_after_stall(project, accept=True, dirty=False)

    assert summary.done == 1
    assert adapter.sessions[0].resume_session_id == "sess-1"


def test_a_restart_that_reset_the_tree_starts_fresh(project):
    """A rollback that reset the attempt's changes blocks the resume: the
    conversation would remember edits that are gone."""
    resumed, adapter, summary = _restart_after_stall(project, accept=False, dirty=True)

    assert adapter.sessions[0].resume_session_id is None
    assert "session-resume" not in _kinds(resumed)
