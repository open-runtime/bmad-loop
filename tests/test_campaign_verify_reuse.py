"""Campaign recovery discards reusable proof before handing authority back."""

import json
import sys

import pytest
from conftest import write_spec, write_sprint
from test_engine import _entries, _reuse_policy, _verify_passes, make_engine, spec_path

from bmad_loop import runsetup, verify
from bmad_loop.campaign_execution import CampaignExecution
from bmad_loop.engine import RunPaused, VerifyReuseToken
from bmad_loop.model import Phase, StoryTask, TokenUsage


@pytest.mark.parametrize("stage", ["dev", "fix"])
def test_campaign_recovery_invalidates_real_green_proof_before_request(
    project, tmp_path, monkeypatch, stage
):
    command = f'"{sys.executable}" -c "print(1)"'
    policy = _reuse_policy(commands=(command,))
    context = CampaignExecution(
        "reuse-campaign",
        "source:project:1-1-a",
        2,
        3,
        1,
        tmp_path / "campaign-worker",
        policy,
        runsetup.resolve_profiles(policy, project.project),
    )
    context.save()
    engine, adapter = make_engine(project, [], policy=policy, campaign_execution=context)
    baseline = verify.rev_parse_head(project.project)
    path = spec_path(project, "1-1-a")
    write_spec(path, "done", baseline)
    write_sprint(project, {"1-1-a": "done"})
    task = StoryTask(
        "1-1-a",
        1,
        phase=Phase.COMMITTING,
        baseline_commit=baseline,
        spec_file=str(path),
        attempt=2,
        review_cycle=3,
        tokens=TokenUsage(input_tokens=123, output_tokens=45),
    )
    engine.state.tasks[task.story_key] = task

    # Mint through the actual upstream verifier/fingerprint path, including a
    # real zero-token command. Assigning a fabricated token could mask a guard
    # that never had any reusable evidence to invalidate.
    outcome, records = engine._verify_commands_with_results(task, stage)
    token = engine._verify_reuse
    assert outcome.ok and isinstance(token, VerifyReuseToken)
    assert token.records == records and token.stage == stage
    assert len(records.results) == 1 and records.results[0].returncode == 0
    assert records.results[0].stdout.strip() == "1"
    engine._env_probes_fresh_root = engine.workspace.root
    before = verify.worktree_fingerprint(project.project)
    task_before = task.to_dict()
    source_before = path.read_bytes(), project.sprint_status.read_bytes()
    observed = []
    recovery_request = CampaignExecution.recovery_request

    def request_after_invalidation(owner, selected, head, *, cause):
        # Inspect inside the outgoing recovery boundary. Engine.run's outer
        # RunPaused cleanup must not make a missing early invalidation pass.
        assert engine._env_probes_fresh_root is None
        assert engine._verify_reuse is None
        assert engine._verify_reuse_void == ("campaign-recovery", None)
        assert selected is task and selected.to_dict() == task_before
        assert head == baseline and verify.worktree_fingerprint(project.project) == before
        observed.append(cause)
        return recovery_request(owner, selected, head, cause=cause)

    monkeypatch.setattr(CampaignExecution, "recovery_request", request_after_invalidation)
    with pytest.raises(RunPaused, match="campaign recovery"):
        engine._campaign_recovery(task, cause="finalization-failed")

    assert observed == ["finalization-failed"]
    request = json.loads((context.directory / "recovery-request.json").read_text())
    assert request["head"] == baseline and request["task"] == task_before
    assert task.to_dict() == task_before
    assert verify.worktree_fingerprint(project.project) == before
    assert (path.read_bytes(), project.sprint_status.read_bytes()) == source_before
    assert not adapter.sessions

    # Even an unchanged tree now owes a fresh review verification pass.
    assert engine._review_verify_gate(task, reuse_eligible=True).ok
    assert _verify_passes(engine) == [(stage, 1), ("review", 2)]
    assert [row["decline"] for row in _entries(engine, "verify-reuse-declined")] == [
        "campaign-recovery"
    ]
    assert not _entries(engine, "verify-pass-reused")
