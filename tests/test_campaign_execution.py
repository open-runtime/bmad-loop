"""Campaign worker safety using the real legacy engine seams and sandbox Git."""

from __future__ import annotations

import dataclasses
import json

import pytest
from conftest import git, write_sprint
from test_engine import make_engine

from bmad_loop import policy as policy_mod
from bmad_loop import runsetup, verify
from bmad_loop.campaign_execution import (
    CampaignExecution,
    CampaignExecutionError,
    legacy_control_refusal,
    worker_policy,
)
from bmad_loop.engine import RunPaused
from bmad_loop.model import Phase, StoryTask


def test_campaign_checkpoint_prompt_preserves_final_workflow_completion_contract(project, tmp_path):
    from bmad_loop.adapters.base import SessionResult

    write_sprint(project, {"1-1-a": "ready-for-dev"})
    context = dataclasses.replace(envelope(project, tmp_path), checkpoint_interval_s=13.0)
    engine, adapter = make_engine(
        project, [SessionResult(status="completed")], campaign_execution=context
    )
    task = StoryTask(story_key="1-1-a", epic=1, phase=Phase.DEV_RUNNING)
    engine.state.tasks[task.story_key] = task
    engine._run_session(
        task, role="review", prompt="Inspect selected documents.", seq=1, label="docs"
    )
    prompt = adapter.sessions[0].prompt
    assert "every 13 seconds" in prompt
    assert prompt.index("Campaign backup contract") < prompt.index("## Completion signal")
    assert prompt.endswith("stalled and its work may be discarded.")


def envelope(project, tmp_path, policy=None):
    pol = policy or policy_mod.Policy()
    context = CampaignExecution(
        "campaign-1",
        "source:initiative:1.1",
        1,
        2,
        1,
        tmp_path / "worker",
        pol,
        runsetup.resolve_profiles(pol, project.project),
    )
    context.save()
    return context


def test_full_configuration_round_trip_and_tamper_refusal(project, tmp_path):
    pol = policy_mod.loads(
        '[adapter]\nmodel="frozen"\n[limits]\nmax_review_cycles=7\n[recovery]\nreload_policy="story-boundary"'
    )
    original = envelope(project, tmp_path, pol)
    path, digest = original.save()
    restored = CampaignExecution.load(path, digest)
    assert restored == original
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["policy"]["limits"]["max_review_cycles"] = 99
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(CampaignExecutionError, match="digest mismatch"):
        CampaignExecution.load(path, digest)


def test_effective_policy_disables_reload_and_automatic_cleanup(project, tmp_path):
    pol = policy_mod.loads('[recovery]\nreload_policy="story-boundary"')
    frozen = worker_policy(pol)
    assert frozen.recovery.reload_policy == "off"
    assert frozen.cleanup.auto_clean_on_finish is False
    assert frozen.gates.retrospective == "never"
    assert pol.recovery.reload_policy == "story-boundary"


def test_first_and_resumed_boundary_never_reads_editable_policy(project, tmp_path, monkeypatch):
    # Keep reload ON deliberately: the independent early guard must protect
    # even a caller that forgot to apply worker_policy.
    pol = policy_mod.loads('[recovery]\nreload_policy="story-boundary"')
    context = envelope(project, tmp_path, pol)
    engine, _ = make_engine(project, [], policy=pol, campaign_execution=context)
    original_digest = runsetup.config_digest(pol, project.project)
    changed = dataclasses.replace(pol, limits=dataclasses.replace(pol.limits, max_review_cycles=99))
    assert runsetup.config_digest(changed, project.project) == original_digest

    def editable_read(*args, **kwargs):
        pytest.fail("campaign worker attempted live policy reload")

    monkeypatch.setattr(policy_mod, "load", editable_read)
    engine._maybe_reload_policy()
    saved, digest = context.save()
    resumed, _ = make_engine(
        project, [], policy=pol, campaign_execution=CampaignExecution.load(saved, digest)
    )
    resumed._maybe_reload_policy()
    assert engine.policy.limits.max_review_cycles == pol.limits.max_review_cycles
    assert resumed.policy == pol


def test_rejected_attempt_keeps_published_head_and_ledger_snapshot(project, tmp_path, monkeypatch):
    write_sprint(project, {"1-1-a": "ready-for-dev"})
    pol = policy_mod.loads("[scm]\nrollback_on_failure=true")
    context = envelope(project, tmp_path, pol)
    engine, _ = make_engine(project, [], policy=pol, campaign_execution=context)
    baseline = verify.rev_parse_head(project.project)
    (project.project / "feature.txt").write_text("checkpoint\n", encoding="utf-8")
    git(project.project, "add", "feature.txt")
    git(project.project, "commit", "-qm", "checkpoint")
    checkpoint = verify.rev_parse_head(project.project)
    # A remote/push receipt names this immutable checkpoint; recovery must not
    # move the branch behind it, irrespective of inherited rollback=true.
    task = StoryTask("1-1-a", 1, phase=Phase.DEV_VERIFY, baseline_commit=baseline)
    engine.state.tasks[task.story_key] = task
    monkeypatch.setattr(
        engine,
        "_restore_persisted_ledger",
        lambda *a, **k: pytest.fail("finally restored ledger after handoff"),
    )
    monkeypatch.setattr(
        engine,
        "_disarm_ledger_snapshot",
        lambda *a, **k: pytest.fail("finally disarmed recovery preimage"),
    )
    with pytest.raises(RunPaused, match="campaign recovery"):
        engine._roll_back_rejected_attempt(task, replayed=False)
    assert verify.rev_parse_head(project.project) == checkpoint
    assert verify.is_ancestor(project.project, baseline, checkpoint)
    request = json.loads((context.directory / "recovery-request.json").read_text(encoding="utf-8"))
    assert request["head"] == checkpoint
    assert request["task"]["baseline_commit"] == baseline


@pytest.mark.parametrize("entry", ["rollback_or_pause", "safe_reset"])
def test_flow_guard_precedes_normalization_and_destructive_reset(
    project, tmp_path, monkeypatch, entry
):
    pol = policy_mod.loads("[scm]\nrollback_on_failure=true")
    context = envelope(project, tmp_path, pol)
    engine, _ = make_engine(project, [], policy=pol, campaign_execution=context)
    task = StoryTask("1-1-a", 1, baseline_commit=verify.rev_parse_head(project.project))
    monkeypatch.setattr(
        verify, "safe_rollback", lambda *a, **k: pytest.fail("legacy destructive reset")
    )
    with pytest.raises(RunPaused, match="campaign recovery"):
        getattr(engine._recovery_flow, entry)(task)
    assert (context.directory / "recovery-request.json").is_file()


def test_legacy_controls_refuse_campaign_child(tmp_path):
    assert legacy_control_refusal(tmp_path) is None
    (tmp_path / "campaign-worker.json").write_text("{}", encoding="utf-8")
    assert "campaign resume/reconcile" in legacy_control_refusal(tmp_path)
