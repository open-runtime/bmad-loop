"""Accepted commit-window recovery retains history and spends no new dev session."""

import dataclasses
import json
import subprocess
import sys

import pytest
from conftest import git, install_bmad_config, write_spec, write_sprint
from test_engine import make_engine

from bmad_loop import policy as policy_mod
from bmad_loop import runsetup, verify
from bmad_loop.campaign_execution import CampaignExecution
from bmad_loop.campaign_finalization import resume, validate_resume
from bmad_loop.campaign_recovery import CheckpointPusher, RecoveryHold
from bmad_loop.campaign_workers import common_directory, prepare_launch
from bmad_loop.engine import RunPaused
from bmad_loop.journal import load_state
from bmad_loop.model import Phase, StoryTask, TokenUsage


@pytest.fixture
def finalization(project, tmp_path, request):
    repo = project.project
    install_bmad_config(project)
    write_sprint(project, {"1-1-owned": "done"})
    (repo / "owned.py").write_text("original\n")
    verify_mode = getattr(request, "param", "")
    if verify_mode:
        (repo / "verify-retained.py").write_text(
            "from pathlib import Path\nimport sys\n"
            + (
                "Path('owned.py').write_text('unexpected verifier mutation\\n')\n"
                if verify_mode == "mutating"
                else "sys.exit(0 if Path('.bmad-loop/allow-retained').exists() else 1)\n"
            )
        )
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "baseline")
    baseline = verify.rev_parse_head(repo)
    git(repo, "checkout", "-b", "campaign-retained")
    path = project.implementation_artifacts / "1-1-owned.md"
    write_spec(path, "done", baseline)
    (repo / "owned.py").write_text("accepted behavior\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "accepted agent checkpoint")
    checkpoint = verify.rev_parse_head(repo)
    path.write_text(path.read_text() + "\nAccepted review bookkeeping pending finalization.\n")
    remote = tmp_path / "remote.git"
    subprocess.run(
        ["git", "clone", "--bare", str(repo), str(remote)], capture_output=True, check=True
    )
    git(repo, "remote", "add", "campaign-test", str(remote))
    policy_text = '[gates]\nmode="none"\n[review]\nenabled=false\n[notify]\ndesktop=false\nfile=false\n[scm]\nrollback_on_failure=true'
    if verify_mode:
        command = f'"{sys.executable}" verify-retained.py'
        policy_text += "\n[verify]\ncommands = [" + json.dumps(command) + "]\n"
    policy = policy_mod.loads(policy_text)
    context = CampaignExecution(
        "campaign-retained",
        "source:project:1-1-owned",
        1,
        2,
        1,
        tmp_path / "original-launch",
        policy,
        runsetup.resolve_profiles(policy, repo),
    )
    engine, adapter = make_engine(project, [], policy=policy, campaign_execution=context)
    task = StoryTask(
        "1-1-owned",
        1,
        phase=Phase.COMMITTING,
        baseline_commit=baseline,
        spec_file=str(path),
        attempt=2,
        review_cycle=3,
        tokens=TokenUsage(input_tokens=123),
    )
    engine.state.tasks[task.story_key] = task
    original_request = prepare_launch(
        context,
        {
            "launch_id": "original-launch",
            "workspace": str(repo),
            "branch": "campaign-retained",
            "common_dir": str(common_directory(repo)),
            "kind": "execution",
            "run_id": engine.state.run_id,
            "source": {"kind": "sprint-status", "id": "source"},
            "unit": {
                "id": context.unit_id,
                "source": "source",
                "epic": "1",
                "title": "Owned",
                "selector": task.story_key,
            },
        },
    )
    with pytest.raises(RunPaused, match="campaign recovery"):
        engine._campaign_recovery(task, cause="finalization-failed")
    pusher = CheckpointPusher(
        repo, tmp_path / "publication-state", "campaign-test", "campaign-retained"
    )
    assert pusher.push("checkpoint", checkpoint).state == "confirmed"
    child = dataclasses.replace(context, directory=tmp_path / "retained-launch")
    request = {
        "workspace": str(repo),
        "recovery_request": str(original_request.parent / "recovery-request.json"),
        "branch": "campaign-retained",
        "quiescence_receipt": "daemon-and-engine-terminal",
        "publication_state": str(pusher.root),
        "remote": "campaign-test",
        "recovery_action": "retained-1",
        "run_id": "retained-run",
    }
    return project, context, engine, adapter, task, child, request, pusher, checkpoint, baseline


def invoke(case, **kwargs):
    project, context, engine, adapter, task, child, request, *_ = case
    return resume(
        request,
        child,
        make_adapters=lambda *a, **k: {"dev": adapter, "review": adapter, "retro": adapter},
        **kwargs,
    )


def test_retained_finalization_finishes_append_only_without_new_dev_and_keeps_all_counters(
    finalization,
):
    project, context, engine, adapter, task, child, request, pusher, checkpoint, baseline = (
        finalization
    )
    original = (engine.run_dir / "state.json").read_bytes()
    result = invoke(finalization)
    assert result["outcome"] == "locally_complete", result
    assert result["retained_finalization"] is True
    assert result["dev_attempts_spent"] == 2 and result["review_cycles_spent"] == 3
    state = load_state(project.project / ".bmad-loop/runs/retained-run")
    assert state.replay_of == engine.state.run_id
    assert state.tasks[task.story_key].attempt == 2
    assert state.tasks[task.story_key].review_cycle == 3
    assert state.tasks[task.story_key].tokens.input_tokens == 123
    assert (engine.run_dir / "state.json").read_bytes() == original
    assert (project.project / "owned.py").read_text() == "accepted behavior\n"
    head = verify.rev_parse_head(project.project)
    assert head != checkpoint
    assert verify.is_ancestor(project.project, checkpoint, head)
    assert verify.is_ancestor(project.project, baseline, head)
    assert pusher.remote_head() == head and pusher.fenced() is None
    assert result["recovery"]["state"] == "complete"
    assert not adapter.sessions


@pytest.mark.parametrize("change", ["phase", "head", "selector", "config", "quiescence"])
def test_retained_finalization_refuses_unproven_candidate_without_resetting_code(
    finalization, change
):
    project, context, engine, adapter, task, child, request, pusher, checkpoint, baseline = (
        finalization
    )
    if change in {"phase", "selector"}:
        path = context.directory / "recovery-request.json"
        data = json.loads(path.read_text())
        data["task"]["phase" if change == "phase" else "story_key"] = (
            "dev-verify" if change == "phase" else "other"
        )
        path.write_text(json.dumps(data))
    elif change == "head":
        (project.project / "owned.py").write_text("later preserved work\n")
        git(project.project, "add", "owned.py")
        git(project.project, "commit", "-qm", "operator later work")
    elif change == "config":
        finalization = (
            *finalization[:5],
            dataclasses.replace(child, config_revision=2),
            *finalization[6:],
        )
    else:
        request["quiescence_receipt"] = ""
    before = verify.rev_parse_head(project.project)
    with pytest.raises(RecoveryHold):
        invoke(finalization)
    assert verify.rev_parse_head(project.project) == before
    assert verify.is_ancestor(project.project, checkpoint, before)
    assert not adapter.sessions


def test_commit_phase_alone_cannot_bypass_engine_campaign_guard(finalization):
    project, context, engine, adapter, task, *_ = finalization
    assert validate_resume(engine, task) is False
    engine._campaign_retained_permit = True
    assert validate_resume(engine, task) is False
    (context.directory / "recovery-request.json").unlink()
    with pytest.raises(RunPaused, match="campaign recovery"):
        engine._finish_inflight()
    assert not adapter.sessions


@pytest.mark.parametrize("finalization", ["fails-until-repaired"], indirect=True)
def test_fresh_verification_failure_preserves_pending_debt_and_allows_bounded_repair(finalization):
    project, context, engine, adapter, task, child, request, pusher, checkpoint, _ = finalization
    failed = invoke(finalization)
    assert failed["outcome"] == "retained_verification_failed"
    assert failed["retained_finalization_pending"]["original_launch"] == "original-launch"
    assert pusher.remote_head() == checkpoint and pusher.fenced() is None
    assert not adapter.sessions
    (project.project / ".bmad-loop/allow-retained").write_text("environment repaired\n")
    (project.project / "owned.py").write_text("accepted behavior\nverified repair\n")
    git(project.project, "add", "owned.py")
    git(project.project, "commit", "-qm", "authorized retained candidate repair")
    repair_head = verify.rev_parse_head(project.project)
    assert pusher.push("bounded-repair", repair_head).state == "confirmed"
    request["quiescence_head"] = repair_head
    result = invoke(finalization)
    assert result["outcome"] == "locally_complete", result
    assert result["run_id"] == failed["run_id"]
    assert verify.is_ancestor(project.project, checkpoint, verify.rev_parse_head(project.project))
    assert result["dev_attempts_spent"] == task.attempt
    assert not adapter.sessions


@pytest.mark.parametrize("finalization", ["mutating"], indirect=True)
def test_verifier_cannot_silently_change_retained_accepted_code(finalization):
    project, context, engine, adapter, task, child, request, pusher, checkpoint, _ = finalization
    with pytest.raises(RecoveryHold, match="verification changed"):
        invoke(finalization)
    assert verify.rev_parse_head(project.project) == checkpoint
    assert pusher.remote_head() == checkpoint
    assert (project.project / "owned.py").read_text() == "unexpected verifier mutation\n"
    assert not adapter.sessions


def test_commit_then_lost_finish_ack_adopts_the_same_retained_run(finalization, monkeypatch):
    from bmad_loop.campaign_recovery import CampaignRecovery

    project, context, engine, adapter, task, child, request, pusher, checkpoint, _ = finalization
    real_finish = CampaignRecovery.finish
    monkeypatch.setattr(
        CampaignRecovery,
        "finish",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("lost finish acknowledgement")),
    )
    with pytest.raises(RuntimeError, match="lost finish"):
        invoke(finalization)
    after_commit = verify.rev_parse_head(project.project)
    assert verify.is_ancestor(project.project, checkpoint, after_commit)
    monkeypatch.setattr(CampaignRecovery, "finish", real_finish)
    request["quiescence_head"] = after_commit
    result = invoke(finalization)
    assert result["outcome"] == "locally_complete"
    assert result["run_id"] == "retained-run"
    assert verify.rev_parse_head(project.project) == after_commit
    assert pusher.remote_head() == after_commit and pusher.fenced() is None
    assert not adapter.sessions


def test_retained_actor_lookup_uses_stable_run_and_stop_and_quiescence_target_that_run(
    finalization, tmp_path, monkeypatch
):
    from types import SimpleNamespace

    from bmad_loop import campaign_runtime, runs
    from bmad_loop.adapters import multiplexer
    from bmad_loop.campaign_finalization import continuation_run_id
    from bmad_loop.campaign_model import (
        CampaignSnapshot,
        CampaignSpec,
        CampaignUnit,
        UnitSpec,
        WorkerClaim,
    )
    from bmad_loop.campaign_store import CampaignStore

    project, context, engine, adapter, task, child, request, *_ = finalization
    completed = invoke(finalization)
    request.update(kind="recovery", campaign_id=context.campaign_id, unit_id=context.unit_id)
    request["run_id"] = "later-claim-run-id"
    assert continuation_run_id(request) == completed["run_id"]
    unit_spec = UnitSpec(context.unit_id, "source", "1", "Unit", selector=task.story_key)
    claim = WorkerClaim(
        unit_spec.id,
        3,
        2,
        "recovery-launch",
        workspace=str(project.project),
        branch="campaign-retained",
        run_id="later-claim-run-id",
        role="recovery",
    )
    unit = CampaignUnit(
        unit_spec, claim=claim, bindings={"workspace": str(project.project), "branch": claim.branch}
    )
    snapshot = CampaignSnapshot(
        "campaign-retained",
        "a/b",
        CampaignSpec("Retained", units=(unit_spec,)),
        {unit.id: unit},
        generation=2,
    )
    store = CampaignStore(tmp_path / "runtime-state")
    directory = store.directory(snapshot.id) / "workers" / claim.launch_id
    directory.mkdir(parents=True)
    (directory / "request.json").write_text(json.dumps(request))
    (directory / "startup.json").write_text(
        json.dumps({"launch_id": claim.launch_id, "generation": claim.generation})
    )
    runtime = campaign_runtime.CampaignRuntime(project.project, store)
    monkeypatch.setattr(runtime, "template", lambda _: context)
    monkeypatch.setattr(campaign_runtime, "observe", lambda *a, **k: {"state": "interrupted"})
    seen = []
    mux = SimpleNamespace(
        list_window_ids=lambda session: seen.append(session) or ["still-running-pre-commit-cli"]
    )
    monkeypatch.setattr(multiplexer, "configure_multiplexer", lambda *a, **k: None)
    monkeypatch.setattr(multiplexer, "get_multiplexer", lambda: mux)
    action = SimpleNamespace(id="observe", unit=unit.id, payload={"launch_id": claim.launch_id})
    blocked = runtime._perform_observe_worker(snapshot, action)
    assert blocked.state == "unknown" and "coding windows remain" in blocked.reason
    assert seen == [runs.session_name(completed["run_id"])]
    runtime._perform_terminate_worker(snapshot, action)
    stable_run = project.project / runs.RUNS_DIR / completed["run_id"]
    assert runs.read_stop_request_mode(stable_run) == "hard"
    assert not (project.project / runs.RUNS_DIR / "later-claim-run-id").exists()
    mux.list_window_ids = lambda session: seen.append(session) or []
    terminal = runtime._perform_observe_worker(snapshot, action)
    assert terminal.state == "confirmed" and terminal.data["quiescence_confirmed"] is True
