"""Real Git finalization and engine restart; scripted hooks consume no LLM tokens."""

import json
import os
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest
from conftest import committing_crash_state, git
from test_engine import make_engine, resume_engine

from bmad_loop import finalization_recovery, verify
from bmad_loop.engine import RunStopped
from bmad_loop.journal import load_state
from bmad_loop.model import PauseKind, Phase


def _attempt(project):
    repo = project.project
    baseline = git(repo, "rev-parse", "HEAD")
    (repo / "src.txt").write_text("skill implementation\n")
    git(repo, "add", "src.txt")
    git(repo, "commit", "-qm", "skill implementation")
    original = git(repo, "rev-parse", "HEAD")
    (repo / "src.txt").write_text("staged change\n")
    git(repo, "add", "src.txt")
    index = verify._bound_real_index_path(repo)
    preimage = index.read_bytes()
    (repo / "src.txt").write_text("unstaged change\n")
    (repo / "untracked.txt").write_text("untracked work\n")
    return repo, baseline, original, index, preimage


@pytest.mark.skipif(os.name != "posix", reason="scripted POSIX Git hook")
def test_timed_out_commit_restores_exact_index_and_retains_unknown_operation(project, monkeypatch):
    repo, baseline, original, index, preimage = _attempt(project)
    hook = repo / ".git/hooks/pre-commit"
    hook.write_text(
        "#!/bin/sh\nprintf entered > hook-entered\nsleep 20\nprintf late > late-write\n"
    )
    hook.chmod(0o755)
    real = verify._run_git

    def bounded(cmd, root, **kwargs):
        if cmd[3] == "commit":
            kwargs["timeout_s"] = 2
        return real(cmd, root, **kwargs)

    monkeypatch.setattr(verify, "_run_git", bounded)
    with pytest.raises(verify.GitFinalizationUncertain):
        verify.finalize_commit(repo, baseline, "finalize")
    assert (repo / "hook-entered").read_text() == "entered"  # Timeout premise actually reached.
    assert not (repo / "late-write").exists()
    record = finalization_recovery.pending(repo)
    assert record is not None and record["state"] == "uncertain"
    assert record["restoration"] == "restored"
    assert git(repo, "rev-parse", "HEAD") == original
    assert git(repo, "rev-parse", record["recovery_ref"]) == original
    assert index.read_bytes() == preimage
    assert (repo / "src.txt").read_text() == "unstaged change\n"
    assert (repo / "untracked.txt").read_text() == "untracked work\n"
    calls = []
    monkeypatch.setattr(verify, "_run_git", lambda *a, **k: calls.append(a))
    with pytest.raises(verify.GitFinalizationUncertain):
        verify.finalize_commit(repo, baseline, "do not repeat")
    assert calls == []


def test_lost_commit_ack_preserves_landed_commit_and_blocks_duplicate(project, monkeypatch):
    repo, baseline, original, _index, _preimage = _attempt(project)
    real = verify._run_git
    landed = []

    def lose_ack(cmd, root, **kwargs):
        result = real(cmd, root, **kwargs)
        if cmd[3] == "commit" and result.returncode == 0:
            landed.append(git(repo, "rev-parse", "HEAD"))
            raise verify.GitTimeoutError(
                "lost commit acknowledgement", capture=result.owned_capture
            )
        return result

    monkeypatch.setattr(verify, "_run_git", lose_ack)
    with pytest.raises(verify.GitFinalizationUncertain):
        verify.finalize_commit(repo, baseline, "one commit")
    assert len(landed) == 1 and landed[0] != original
    assert git(repo, "rev-parse", "HEAD") == landed[0]
    record = finalization_recovery.pending(repo)
    assert record is not None and record["state"] == "uncertain"
    assert git(repo, "rev-parse", record["recovery_ref"]) == original
    with pytest.raises(verify.GitFinalizationUncertain):
        verify.finalize_commit(repo, baseline, "no duplicate")
    assert len(landed) == 1


def test_lost_recovery_ref_ack_retains_committing_and_written_bookkeeping(project, monkeypatch):
    engine, _ = make_engine(project, [])
    committing_crash_state(project, engine, operator_actions=["owner review"])
    original = verify.git_bytes
    attempts = []

    def lose_recovery_ref_ack(repo, *args, **kwargs):
        result = original(repo, *args, **kwargs)
        if args[0] == "update-ref" and any(
            isinstance(arg, str) and arg.startswith("refs/bmad-finalization/") for arg in args
        ):
            assert result.returncode == 0
            attempts.append(args)
            raise verify.GitTimeoutError(
                "recovery ref acknowledgement lost", capture=result.owned_capture
            )
        return result

    monkeypatch.setattr(verify, "git_bytes", lose_recovery_ref_ack)
    resumed, _ = resume_engine(project, engine, [])
    summary = resumed.run()
    assert len(attempts) == 1 and summary.paused
    state = load_state(resumed.run_dir)
    assert state.tasks["1-1-a"].phase == Phase.COMMITTING
    assert state.paused_cause.kind == PauseKind.UNKNOWN
    record = finalization_recovery.pending(project.project)
    park_path, _ = record["context"]["park_record"]
    assert Path(park_path["path"]).is_file()


def test_engine_unknown_commit_preserves_bookkeeping_and_resume_does_not_replay(
    project, monkeypatch
):
    engine, _ = make_engine(project, [])
    committing_crash_state(project, engine, operator_actions=["owner approval"])
    real = verify._run_git
    commit_calls = []

    def lose_ack(cmd, root, **kwargs):
        result = real(cmd, root, **kwargs)
        if cmd[3] == "commit" and result.returncode == 0:
            commit_calls.append(cmd)
            raise verify.GitTimeoutError("lost acknowledgement", capture=result.owned_capture)
        return result

    monkeypatch.setattr(verify, "_run_git", lose_ack)
    resumed, adapter = resume_engine(project, engine, [])
    summary = resumed.run()
    assert summary.paused and not summary.crashed and summary.done == 0
    state = load_state(resumed.run_dir)
    assert state.tasks["1-1-a"].phase == Phase.COMMITTING
    assert state.paused_cause.kind == PauseKind.UNKNOWN
    record = finalization_recovery.pending(project.project)
    assert record["context"]["story_key"] == "1-1-a"
    assert record["context"]["park_record"] is not None
    preserved_head = git(project.project, "rev-parse", "HEAD")
    before = json.dumps(record, sort_keys=True)
    rerun, adapter2 = resume_engine(project, resumed, [])
    replays = []

    def unexpected_write(kind):
        replays.append(kind)
        pytest.fail("replayed " + kind)

    monkeypatch.setattr(rerun, "_write_park_record", lambda *_: unexpected_write("park"))
    monkeypatch.setattr(rerun, "_close_declared_deferred", lambda *_: unexpected_write("close"))
    monkeypatch.setattr(rerun, "_terminal_plan_write", lambda *_: unexpected_write("plan"))
    summary2 = rerun.run()
    assert summary2.paused and not summary2.crashed
    assert len(commit_calls) == 1 and adapter.sessions == adapter2.sessions == []
    assert replays == []
    assert git(project.project, "rev-parse", "HEAD") == preserved_head
    assert json.dumps(finalization_recovery.pending(project.project), sort_keys=True) == before


def test_interrupt_survives_recovery_record_write_failure(project, monkeypatch):
    repo, baseline, _original, _index, _preimage = _attempt(project)
    real = verify._run_git
    interrupted = KeyboardInterrupt("stop during commit acknowledgement")

    def lose_ack(cmd, root, **kwargs):
        result = real(cmd, root, **kwargs)
        if cmd[3] == "commit" and result.returncode == 0:
            interrupted.owned_capture = result.owned_capture
            raise interrupted
        return result

    def fail_record(*_args, **_kwargs):
        raise OSError("recovery disk unavailable")

    monkeypatch.setattr(verify, "_run_git", lose_ack)
    monkeypatch.setattr(finalization_recovery.Transaction, "uncertain", fail_record)
    with pytest.raises(KeyboardInterrupt) as caught:
        verify.finalize_commit(repo, baseline, "interrupted")
    assert caught.value is interrupted
    assert isinstance(interrupted.finalization_uncertain, verify.GitFinalizationUncertain)
    assert finalization_recovery.pending(repo) is not None


def test_engine_stop_survives_failed_state_save_without_bookkeeping_rollback(project, monkeypatch):
    engine, _ = make_engine(project, [])
    committing_crash_state(project, engine)
    stopped = RunStopped()
    stopped.finalization_uncertain = verify.GitFinalizationUncertain("unknown commit")

    def interrupt(*_args, **_kwargs):
        raise stopped

    def save_fault():
        raise OSError("state storage unavailable")

    monkeypatch.setattr(verify, "finalize_commit", interrupt)
    monkeypatch.setattr(engine, "_save", save_fault)
    monkeypatch.setattr(
        engine, "_restore_deferred_closes", lambda *_: pytest.fail("unsafe close rollback")
    )
    monkeypatch.setattr(
        engine, "_restore_park_record", lambda *_: pytest.fail("unsafe park rollback")
    )
    monkeypatch.setattr(
        engine, "_undo_terminal_plan_write", lambda *_: pytest.fail("unsafe plan rollback")
    )
    with pytest.raises(RunStopped) as caught:
        engine._finalize_commit_phase(engine.state.tasks["1-1-a"])
    assert caught.value is stopped


@pytest.mark.skipif(os.name != "posix", reason="scripted POSIX Git hook")
@pytest.mark.parametrize("fault", ["unknown-cleanup", "compensation-refused"])
def test_unknown_commit_record_excludes_raw_hook_diagnostics(project, monkeypatch, fault):
    repo, baseline, _original, _index, _preimage = _attempt(project)
    canary = "TOKEN=ghp_SYNTHETIC_CANARY_DO_NOT_PERSIST_12345"
    hook = repo / ".git/hooks/pre-commit"
    hook.write_text(f"#!/bin/sh\nprintf '%s' '{canary}' >&2\nexit 1\n")
    hook.chmod(0o755)
    real = verify._run_git
    observed = []

    def commit_diagnostic(cmd, root, **kwargs):
        result = real(cmd, root, **kwargs)
        if cmd[3] == "commit":
            assert result.returncode == 1 and result.stderr == canary
            observed.append(True)
            if fault == "unknown-cleanup":
                result.owned_capture = replace(
                    result.owned_capture,
                    ownership=replace(result.owned_capture.ownership, census_complete=False),
                )
        return result

    monkeypatch.setattr(verify, "_run_git", commit_diagnostic)
    if fault == "compensation-refused":
        monkeypatch.setattr(finalization_recovery.Transaction, "restore", lambda *a, **k: False)
    with pytest.raises(verify.GitFinalizationUncertain) as caught:
        verify.finalize_commit(repo, baseline, "rejected commit")
    assert observed == [True]
    record = finalization_recovery.pending(repo)
    assert record is not None and record["state"] == "uncertain"
    assert canary not in caught.value.record_path.read_text()


def test_cooperating_index_writer_during_stage_cannot_lose_staged_only_work(project, monkeypatch):
    repo = project.project
    baseline = git(repo, "rev-parse", "HEAD")
    original_index = verify._bound_real_index_path(repo).read_bytes()
    original_content = (repo / "src.txt").read_bytes()
    peer_content = b"peer staged-only work absent from checkout\n"
    peer_oid = (
        subprocess.run(
            ["git", "-C", str(repo), "hash-object", "-w", "--stdin"],
            input=peer_content,
            capture_output=True,
            check=True,
            timeout=10,
        )
        .stdout.decode()
        .strip()
    )
    real = verify._run_git
    attempts = []

    def peer_before_add(cmd, root, **kwargs):
        if cmd[3:] == ["add", "-A"]:
            attempts.append(
                subprocess.run(
                    [
                        "git",
                        "-C",
                        str(repo),
                        "update-index",
                        "--cacheinfo",
                        f"100644,{peer_oid},src.txt",
                    ],
                    capture_output=True,
                    timeout=10,
                )
            )
        return real(cmd, root, **kwargs)

    def reject():
        raise RuntimeError("staged validation refused")

    monkeypatch.setattr(verify, "_run_git", peer_before_add)
    with pytest.raises((RuntimeError, verify.GitFinalizationUncertain)):
        verify.finalize_commit(repo, baseline, "refused", staged_validator=reject)
    assert len(attempts) == 1
    if attempts[0].returncode == 0:
        # If a writer completed before we acquired authority, its staged-only
        # content must survive. In the locked window Git instead refuses it.
        assert git(repo, "rev-parse", ":src.txt") == peer_oid
    else:
        assert b"index.lock" in attempts[0].stderr
        assert verify._bound_real_index_path(repo).read_bytes() == original_index
    assert (repo / "src.txt").read_bytes() == original_content


@pytest.mark.skipif(os.name != "posix", reason="scripted POSIX Git hook")
@pytest.mark.parametrize("returncode", [1, -15])
def test_signaled_commit_stays_unknown_beside_definitive_refusal_control(
    project, monkeypatch, returncode
):
    repo, baseline, original, _index, preimage = _attempt(project)
    hook = repo / ".git/hooks/pre-commit"
    hook.write_text("#!/bin/sh\nexit 1\n")
    hook.chmod(0o755)
    real = verify._run_git

    def observed_exit(cmd, root, **kwargs):
        result = real(cmd, root, **kwargs)
        if cmd[3] == "commit":
            assert result.returncode == 1
            result.returncode = returncode
            result.owned_capture = replace(result.owned_capture, returncode=returncode)
        return result

    monkeypatch.setattr(verify, "_run_git", observed_exit)
    expected = verify.GitFinalizationUncertain if returncode < 0 else verify.GitError
    with pytest.raises(expected):
        verify.finalize_commit(repo, baseline, "refused or signaled")
    record = finalization_recovery.pending(repo)
    assert (record is not None) is (returncode < 0)
    assert git(repo, "rev-parse", "HEAD") == original
    assert verify._bound_real_index_path(repo).read_bytes() == preimage


@pytest.mark.parametrize("kind", [KeyboardInterrupt, SystemExit, RunStopped])
def test_stop_does_not_launch_git_compensation_after_owned_cleanup(project, monkeypatch, kind):
    repo, baseline, _original, _index, _preimage = _attempt(project)
    real = verify._run_git
    stopped = kind()
    stopping = False
    after_stop = []

    def stop_after_commit(cmd, root, **kwargs):
        nonlocal stopping
        if stopping:
            after_stop.append(cmd)
            pytest.fail("Git launched after requested stop")
        result = real(cmd, root, **kwargs)
        if cmd[3] == "commit" and result.returncode == 0:
            stopping = True
            stopped.owned_capture = result.owned_capture
            raise stopped
        return result

    monkeypatch.setattr(verify, "_run_git", stop_after_commit)
    with pytest.raises(kind) as caught:
        verify.finalize_commit(
            repo,
            baseline,
            "interrupted",
            interruption_types=(KeyboardInterrupt, SystemExit, RunStopped),
        )
    assert caught.value is stopped
    assert stopping and after_stop == []
    assert finalization_recovery.pending(repo) is not None
