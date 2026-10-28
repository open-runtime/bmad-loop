"""open-runtime fork: engine-side loop-recovery patches F1 and F2.

F1 (``[recovery] post_story_on_resume``): a story a ``_finish_inflight`` replay
arm finishes fires ``post_story`` once, before ``_after_story``, like a story
the loop finished. Upstream fired it on the restart arm only, so push-merge never
published a story a resume finished.

F2 (``[recovery] restamp_plan_baseline``): the plan's ``baseline_revision``
follows the recorded baseline when ``resume --accept-baseline`` adopted HEAD
(after the restart arm's rollback) and when ``pre_dev_phase`` moved HEAD under a
plan that still claims the previous recorded baseline (a sync-with-main merge).
"""

from __future__ import annotations

import dataclasses

import pytest
from conftest import (
    committing_crash_state,
    dev_effect,
    git,
    review_effect,
    spec_path,
    write_spec,
    write_sprint,
)
from test_engine import QUIET, _dev_verify_crash_state, make_engine, resume_engine

from bmad_loop import verify
from bmad_loop.adapters.base import SessionResult, SessionSpec
from bmad_loop.frontmatter import auto_dev_baseline_of
from bmad_loop.journal import load_state, save_state
from bmad_loop.model import Phase, StoryTask
from bmad_loop.policy import GatesPolicy, Policy, RecoveryPolicy, ScmPolicy
from bmad_loop.verify import rev_parse_head


def _policy(*, rollback: bool = True, **recovery) -> Policy:
    return Policy(
        gates=GatesPolicy(mode="none"),
        notify=QUIET,
        scm=ScmPolicy(rollback_on_failure=rollback),
        recovery=RecoveryPolicy(**recovery),
    )


def _record_emits(engine) -> list[tuple[str, str | None]]:
    """Wrap ``engine._emit`` so a test sees every stage fired, plugin or not."""
    seen: list[tuple[str, str | None]] = []
    inner = engine._emit

    def recording(stage, task=None, **fields):
        seen.append((stage, task.story_key if task is not None else None))
        return inner(stage, task, **fields)

    engine._emit = recording
    return seen


def _kinds(engine) -> list[str]:
    return [e["kind"] for e in engine.journal.entries()]


# ------------------------------------------------------------------ F1


def test_replay_arm_finish_fires_post_story_once(project):
    """The recorded-session replay arm (`resume-verify`) finishes the story to
    DONE without `_run_story`, so upstream fired no post_story for it. With F1 it
    fires exactly once, journaled `resume-post-story`.

    Ablation: drop the `self._post_story_after_replay(task)` call in
    `_finish_inflight` and `post_story` never fires here."""
    write_sprint(project, {"1-1-a": "ready-for-dev"})
    policy = _policy(rollback=False)
    engine, _ = make_engine(project, [], policy=policy)
    _dev_verify_crash_state(project, engine, "done")

    resumed, adapter = resume_engine(project, engine, [])
    emits = _record_emits(resumed)
    summary = resumed.run()

    assert summary.done == 1 and not summary.paused
    assert adapter.sessions == []  # the record was replayed, nothing re-ran
    assert emits.count(("post_story", "1-1-a")) == 1
    rows = [e for e in resumed.journal.entries() if e["kind"] == "resume-post-story"]
    assert [(r["story_key"], r["phase"]) for r in rows] == [("1-1-a", "done")]
    assert "resume-verify" in _kinds(resumed)


def test_commit_window_finish_fires_post_story_once(project):
    """The COMMITTING arm (`resume-commit`) finishes the commit a dead host left
    and gets post_story once too."""
    write_sprint(project, {"1-1-a": "ready-for-dev"})
    engine, _ = make_engine(project, [], policy=_policy())
    committing_crash_state(project, engine)

    resumed, _ = resume_engine(project, engine, [])
    emits = _record_emits(resumed)
    summary = resumed.run()

    assert summary.done == 1
    assert "resume-commit" in _kinds(resumed)
    assert emits.count(("post_story", "1-1-a")) == 1


def test_restart_arm_still_fires_post_story_once(project):
    """The restart arm goes through `_run_story`, which already fires post_story:
    F1 must not add a second one, and journals no `resume-post-story`."""
    write_sprint(project, {"1-1-a": "ready-for-dev"})
    policy = _policy()
    engine, _ = make_engine(project, [], policy=policy)
    task = StoryTask(story_key="1-1-a", epic=1, phase=Phase.DEV_RUNNING, attempt=1)
    task.baseline_commit = rev_parse_head(project.project)
    task.baseline_untracked = sorted(verify.untracked_files(project.project))
    engine.state.tasks[task.story_key] = task
    engine._save()

    resumed, _ = resume_engine(
        project,
        engine,
        [dev_effect(project, "1-1-a"), review_effect(project, "1-1-a", clean=True)],
    )
    emits = _record_emits(resumed)
    summary = resumed.run()

    assert summary.done == 1
    assert "resume-restart" in _kinds(resumed)
    assert emits.count(("post_story", "1-1-a")) == 1
    assert "resume-post-story" not in _kinds(resumed)


def test_post_story_on_resume_off_keeps_the_upstream_silence(project):
    """`post_story_on_resume = false` restores upstream: the replay arm fires no
    post_story at all."""
    write_sprint(project, {"1-1-a": "ready-for-dev"})
    policy = _policy(rollback=False, post_story_on_resume=False)
    engine, _ = make_engine(project, [], policy=policy)
    _dev_verify_crash_state(project, engine, "done")

    resumed, _ = resume_engine(project, engine, [])
    emits = _record_emits(resumed)
    summary = resumed.run()

    assert summary.done == 1
    assert ("post_story", "1-1-a") not in emits
    assert "resume-post-story" not in _kinds(resumed)


def test_post_story_fires_before_after_story(project):
    """Same order as `_loop`: post_story, then `_after_story` (the stories-mode
    done checkpoint hangs off the latter)."""
    write_sprint(project, {"1-1-a": "ready-for-dev"})
    engine, _ = make_engine(project, [], policy=_policy(rollback=False))
    _dev_verify_crash_state(project, engine, "done")

    resumed, _ = resume_engine(project, engine, [])
    order: list[str] = []
    inner_emit = resumed._emit

    def emit(stage, task=None, **fields):
        if stage == "post_story":
            order.append("post_story")
        return inner_emit(stage, task, **fields)

    inner_after = resumed._after_story

    def after_story(task):
        order.append("after_story")
        return inner_after(task)

    resumed._emit = emit
    resumed._after_story = after_story
    resumed.run()

    assert order[:2] == ["post_story", "after_story"]


# ------------------------------------------------------------------ F2


def _accept_with_recorded_plan(project, *, restamp: bool):
    """A DEV_RUNNING task at baseline B whose plan claims B, a human commit C made
    while the run was down, then `resume --accept-baseline`. The dev session
    preserves whatever baseline the plan claims when it starts (the skill's
    step-03 "preserve it when resuming"), so the gate passes only if the plan was
    re-stamped to the adopted baseline."""
    write_sprint(project, {"1-1-a": "ready-for-dev"})
    policy = _policy(restamp_plan_baseline=restamp)
    engine, _ = make_engine(project, [], policy=policy)
    repo = project.project
    baseline = rev_parse_head(repo)
    sp = spec_path(project, "1-1-a")
    write_spec(sp, "in-progress", baseline)
    task = StoryTask(story_key="1-1-a", epic=1, phase=Phase.DEV_RUNNING, attempt=1)
    task.baseline_commit = baseline
    task.baseline_untracked = sorted(verify.untracked_files(repo))
    task.spec_file = str(sp)
    engine.state.tasks[task.story_key] = task
    engine._save()
    (repo / "human.txt").write_text("made while the run was down\n")
    git(repo, "add", "human.txt")
    git(repo, "commit", "-q", "-m", "human work")
    human = rev_parse_head(repo)
    state = load_state(engine.run_dir)
    state.accept_baseline = True
    save_state(engine.run_dir, state)

    claimed_at_launch: list[str] = []

    def preserving_dev(spec: SessionSpec) -> SessionResult:
        claim = auto_dev_baseline_of(verify.read_frontmatter(sp))
        claimed_at_launch.append(claim)
        result = dev_effect(project, "1-1-a")(spec)
        # the real skill preserves the claim it found when resuming (step-03)
        write_spec(sp, "done", claim)
        return dataclasses.replace(
            result, result_json={**(result.result_json or {}), "baseline_commit": claim}
        )

    resumed, _ = resume_engine(
        project,
        engine,
        [preserving_dev, review_effect(project, "1-1-a", clean=True)],
        policy=policy,
    )
    return resumed, resumed.run(), baseline, human, claimed_at_launch


def test_accept_baseline_restamps_the_plan_after_the_rollback(project):
    """12.1's sequence, leg 1: `--accept-baseline` adopts HEAD C, the plan still
    claims B, and the attempt-2 session preserves the claim it finds. F2 re-stamps
    the plan to C after the restart arm's rollback, so the session finds C and the
    baseline gate passes; the story is done, not deferred.

    Ablation: drop the `_restamp_plan_baseline(..., site="accept-baseline")` call
    and the session launches on B, the gate rejects the stale claim and the story
    never reaches done."""
    resumed, summary, baseline, human, claimed = _accept_with_recorded_plan(project, restamp=True)

    assert summary.done == 1 and not summary.paused and summary.deferred == 0
    assert claimed == [human]
    rows = [e for e in resumed.journal.entries() if e["kind"] == "plan-baseline-restamped"]
    assert [(r["site"], r["overwritten"], r["baseline"]) for r in rows] == [
        ("accept-baseline", baseline, human)
    ]
    assert "story-deferred" not in _kinds(resumed)


def test_accept_baseline_without_restamp_keeps_the_upstream_gate_failure(project):
    """`restamp_plan_baseline = false`: upstream behaviour — the session launches
    on the stale claim and the baseline gate rejects the attempt."""
    resumed, summary, baseline, human, claimed = _accept_with_recorded_plan(project, restamp=False)

    assert claimed[0] == baseline
    assert summary.done == 0
    assert "plan-baseline-restamped" not in _kinds(resumed)


def _restart_with_pre_dev_merge(project, *, plan_claim: str | None):
    """A DEV_RUNNING task at baseline B whose plan claims ``plan_claim`` (B when
    None), restarted without --accept-baseline; `pre_dev_phase` then commits on
    top (sync-with-main merging main) before the dev session launches."""
    write_sprint(project, {"1-1-a": "ready-for-dev"})
    policy = _policy()
    engine, _ = make_engine(project, [], policy=policy)
    repo = project.project
    baseline = rev_parse_head(repo)
    sp = spec_path(project, "1-1-a")
    write_spec(sp, "in-progress", plan_claim or baseline)
    task = StoryTask(story_key="1-1-a", epic=1, phase=Phase.DEV_RUNNING, attempt=1)
    task.baseline_commit = baseline
    task.baseline_untracked = sorted(verify.untracked_files(repo))
    task.spec_file = str(sp)
    engine.state.tasks[task.story_key] = task
    engine._save()

    claimed_at_launch: list[str] = []

    def preserving_dev(spec: SessionSpec) -> SessionResult:
        claim = auto_dev_baseline_of(verify.read_frontmatter(sp))
        claimed_at_launch.append(claim)
        result = dev_effect(project, "1-1-a")(spec)
        write_spec(sp, "done", claim)
        return dataclasses.replace(
            result, result_json={**(result.result_json or {}), "baseline_commit": claim}
        )

    resumed, _ = resume_engine(
        project,
        engine,
        [preserving_dev, review_effect(project, "1-1-a", clean=True)],
        policy=policy,
    )
    inner = resumed._emit
    merged: list[str] = []

    def emit(stage, task=None, **fields):
        if stage == "pre_dev_phase" and not merged:
            (repo / "main.txt").write_text("merged from main\n")
            git(repo, "add", "main.txt")
            git(repo, "commit", "-q", "-m", "sync with main")
            merged.append(rev_parse_head(repo))
        return inner(stage, task, **fields)

    resumed._emit = emit
    return resumed, resumed.run(), baseline, merged, claimed_at_launch


def test_pre_dev_merge_restamps_a_plan_claiming_the_previous_baseline(project):
    """Leg 2: the plan claims the previous recorded baseline B and `pre_dev_phase`
    merged main (HEAD M). F2 re-stamps the plan to M before the session launches,
    so the preserving session passes the gate."""
    resumed, summary, baseline, merged, claimed = _restart_with_pre_dev_merge(
        project, plan_claim=None
    )

    assert summary.done == 1 and summary.deferred == 0
    assert claimed == merged
    rows = [e for e in resumed.journal.entries() if e["kind"] == "plan-baseline-restamped"]
    assert [(r["site"], r["overwritten"], r["baseline"]) for r in rows] == [
        ("pre-dev-merge", baseline, merged[0])
    ]


def test_pre_dev_merge_leaves_a_foreign_claim_for_the_gate(project):
    """A plan claiming something the run never recorded is left alone: the gate
    keeps its independent signal (runs.rearm_escalation's reasoning)."""
    foreign = "0" * 40
    resumed, summary, _baseline, _merged, claimed = _restart_with_pre_dev_merge(
        project, plan_claim=foreign
    )

    assert claimed[0] == foreign
    assert "plan-baseline-restamped" not in _kinds(resumed)


def test_restamp_skips_a_missing_plan_with_a_journal_row(project):
    """A recorded plan that vanished is journaled, never a pause or a crash."""
    write_sprint(project, {"1-1-a": "ready-for-dev"})
    engine, _ = make_engine(project, [], policy=_policy())
    task = StoryTask(story_key="1-1-a", epic=1, phase=Phase.DEV_RUNNING, attempt=1)
    task.spec_file = str(spec_path(project, "1-1-a"))
    engine.state.tasks[task.story_key] = task

    engine._restamp_plan_baseline(task, "a" * 40, site="accept-baseline")

    rows = [e for e in engine.journal.entries() if e["kind"] == "plan-baseline-restamp-skipped"]
    assert [(r["site"], r["reason"]) for r in rows] == [("accept-baseline", "plan-missing")]


@pytest.mark.parametrize("restamp", [True, False])
def test_restamp_is_a_no_op_when_the_plan_already_claims_the_baseline(project, restamp):
    write_sprint(project, {"1-1-a": "ready-for-dev"})
    engine, _ = make_engine(project, [], policy=_policy(restamp_plan_baseline=restamp))
    sp = spec_path(project, "1-1-a")
    head = rev_parse_head(project.project)
    write_spec(sp, "in-progress", head)
    before = sp.read_bytes()
    task = StoryTask(story_key="1-1-a", epic=1, phase=Phase.DEV_RUNNING, attempt=1)
    task.spec_file = str(sp)

    engine._restamp_plan_baseline(task, head, site="accept-baseline")

    assert sp.read_bytes() == before
    assert "plan-baseline-restamped" not in _kinds(engine)
