"""Finalization compensation retains history, index preimages and concurrent work."""

import contextlib
import dataclasses
import hashlib
import json
import os
import stat

import pytest
from conftest import git

from bmad_loop import finalization_recovery as recovery
from bmad_loop import runs, verify


@pytest.fixture
def finalization(project, tmp_path, monkeypatch):
    repo = project.project
    (repo / "product.txt").write_text("baseline\n")
    git(repo, "add", "product.txt")
    git(repo, "commit", "-qm", "test: baseline")
    baseline = git(repo, "rev-parse", "HEAD")
    git(repo, "checkout", "-qb", "private-finalization")
    (repo / "product.txt").write_text("accepted feature\n")
    git(repo, "commit", "-qam", "test: accepted private feature")
    head = git(repo, "rev-parse", "HEAD")
    (repo / "product.txt").write_text("unfinished worktree contribution\n")
    (repo / "staged.txt").write_text("original staged contribution\n")
    git(repo, "add", "staged.txt")
    (repo / "untracked.txt").write_text("original untracked contribution\n")
    authority = tmp_path / "authority"
    monkeypatch.setattr(runs, "state_root", lambda: authority)
    return repo, baseline, head


def stage_and_move(tx, repo, baseline, head, *, acknowledge=True):
    with tx.staging_index() as private:
        tx.stage_started()
        result = verify._run_git(
            ["git", "-C", str(repo), "add", "-A"],
            repo,
            env={**os.environ, "GIT_INDEX_FILE": str(private)},
        )
        assert result.returncode == 0
        tx.observe_command("add", result.owned_capture)
        tx.publish_staged_index(cleanup_proven=True)
        tx.staged()
        tx.head_move_started(baseline)
        assert tx.record["head_move"] == {
            "expected_old": head,
            "intended_target": baseline,
            "acknowledged": False,
        }
        assert json.loads(tx.path.read_text())["head_move"] == tx.record["head_move"]
        git(repo, "update-ref", "--no-deref", tx.branch_ref, baseline, head)
        if acknowledge:
            tx.head_moved(baseline)


def test_begin_preserves_index_and_recovery_ref_before_staging(finalization):
    repo, baseline, head = finalization
    index = verify._bound_real_index_path(repo)
    before = index.read_bytes()
    with recovery.begin(repo, head, baseline, "finalize") as tx:
        assert tx.state == "prepared"
        assert not tx.path.is_relative_to(repo)
        assert tx.path.parent.parent == runs.state_root() / runs.project_tag(repo) / "_finalization"
        assert tx.index_path == index
        assert git(repo, "rev-parse", tx.record["recovery_ref"]) == head
        assert (tx.path.parent / "index-preimage.bin").read_bytes() == before
        assert tx.record["index_preimage"]["sha256"] == hashlib.sha256(before).hexdigest()
        changed = tx.record
        changed["state"] = "completed"
        assert tx.state == "prepared"
        tx.uncertain("test preserved operation")
    assert recovery.pending(repo)["id"] == tx.id
    assert index.read_bytes() == before


@pytest.mark.parametrize("acknowledge", [False, True])
def test_timeout_after_rewind_compensates_exact_index_without_touching_worktree(
    finalization, acknowledge
):
    repo, baseline, head = finalization
    index = verify._bound_real_index_path(repo)
    before = index.read_bytes()
    if os.name == "posix":
        os.chmod(index, 0o640)
    os.utime(index, ns=(1_000_000_000, 2_000_000_000))
    metadata = index.stat()
    contents = {
        name: (repo / name).read_bytes() for name in ("product.txt", "staged.txt", "untracked.txt")
    }
    with pytest.raises(TimeoutError, match="uncertain commit"):
        with recovery.begin(repo, head, baseline, "finalize") as tx:
            stage_and_move(tx, repo, baseline, head, acknowledge=acknowledge)
            with tx.private_index() if acknowledge else contextlib.nullcontext() as private:
                if acknowledge:
                    tx.commit_environment(private)
                    tx.commit_started()
                    tx.observe_command("commit", capture_result(timed_out=True, returncode=-15))
                tx.uncertain("lost commit or head-move acknowledgement")
                assert tx.restore(cleanup_proven=True), tx.record
            assert tx.record["restoration"] == "restored"
            assert tx.state == "uncertain"
            raise TimeoutError("uncertain commit")
    after = index.stat()
    assert stat.S_IMODE(after.st_mode) == stat.S_IMODE(metadata.st_mode)
    assert after.st_atime_ns == metadata.st_atime_ns
    assert after.st_mtime_ns == metadata.st_mtime_ns
    assert index.read_bytes() == before
    assert git(repo, "rev-parse", "HEAD") == head
    assert git(repo, "rev-parse", tx.record["recovery_ref"]) == head
    assert {name: (repo / name).read_bytes() for name in contents} == contents
    assert git(repo, "show", ":staged.txt") == "original staged contribution"
    assert "?? untracked.txt" in git(repo, "status", "--porcelain")
    assert recovery.pending(repo)["state"] == "uncertain"


@pytest.mark.parametrize("cleanup", [False, None, "true"])
def test_missing_cleanup_proof_never_restores_ref_or_index(finalization, cleanup):
    repo, baseline, head = finalization
    with recovery.begin(repo, head, baseline, "finalize") as tx:
        stage_and_move(tx, repo, baseline, head)
        staged = tx.index_path.read_bytes()
        assert not tx.restore(cleanup_proven=cleanup)
        assert git(repo, "rev-parse", "HEAD") == baseline
        assert tx.index_path.read_bytes() == staged
        assert tx.state == "uncertain"


@pytest.mark.parametrize("change", ["ref", "index", "branch", "lock"])
def test_concurrent_ref_index_branch_or_lock_is_preserved(finalization, change):
    repo, baseline, head = finalization
    with recovery.begin(repo, head, baseline, "finalize") as tx:
        stage_and_move(tx, repo, baseline, head)
        if change == "ref":
            git(repo, "commit", "-qm", "another or already-landed commit")
        elif change == "index":
            (repo / "concurrent.txt").write_text("new staged work\n")
            git(repo, "add", "concurrent.txt")
        elif change == "branch":
            git(repo, "branch", "other-owner", baseline)
            git(repo, "symbolic-ref", "HEAD", "refs/heads/other-owner")
        else:
            tx.index_path.with_name("index.lock").write_bytes(b"foreign lock")
        current = git(repo, "rev-parse", "HEAD")
        before = tx.index_path.read_bytes()
        assert not tx.restore(cleanup_proven=True), tx.record
        assert git(repo, "rev-parse", "HEAD") == current
        assert tx.index_path.read_bytes() == before
        assert git(repo, "rev-parse", tx.record["recovery_ref"]) == head
        if change == "lock":
            assert tx.index_path.with_name("index.lock").read_bytes() == b"foreign lock"
        if change == "index":
            assert (repo / "concurrent.txt").read_text() == "new staged work\n"


def test_missing_original_index_is_restored_as_absence(finalization):
    repo, baseline, head = finalization
    index = verify._bound_real_index_path(repo)
    index.unlink()
    with recovery.begin(repo, head, baseline, "finalize") as tx:
        assert tx.record["index_preimage"]["exists"] is False
        assert tx.record["index_preimage"]["mode"] is None
        stage_and_move(tx, repo, baseline, head)
        assert index.exists()
        assert tx.restore(cleanup_proven=True), tx.record
        assert not index.exists()
        assert git(repo, "rev-parse", "HEAD") == head
        assert (repo / "untracked.txt").read_text() == "original untracked contribution\n"


def test_uncertain_staging_without_postimage_cannot_adopt_current_index(finalization):
    repo, baseline, head = finalization
    with recovery.begin(repo, head, baseline, "finalize") as tx:
        before = tx.index_path.read_bytes()
        with tx.staging_index() as private:
            tx.stage_started()
            result = verify._run_git(
                ["git", "-C", str(repo), "add", "-A"],
                repo,
                env={**os.environ, "GIT_INDEX_FILE": str(private)},
            )
            assert result.returncode == 0
            tx.uncertain("staging acknowledgement lost")
            assert not tx.restore(cleanup_proven=True)
            assert tx.index_path.read_bytes() == before
            assert tx.record["stage_observation_pending"]


def test_restart_refuses_unresolved_operation_and_retains_all_evidence(finalization):
    repo, baseline, head = finalization
    with recovery.begin(repo, head, baseline, "finalize") as tx:
        with tx.staging_index():
            tx.stage_started()
    saved = json.loads(tx.path.read_text())
    assert saved["state"] == "uncertain"
    with pytest.raises(recovery.FinalizationRecoveryError, match=tx.id):
        with recovery.begin(repo, head, baseline, "new attempt"):
            pytest.fail("unresolved operation was replaced")
    assert json.loads(tx.path.read_text()) == saved
    assert git(repo, "rev-parse", saved["recovery_ref"]) == head
    assert (tx.path.parent / "index-preimage.bin").is_file()


def test_success_retains_original_history_and_permits_a_distinct_next_operation(finalization):
    repo, baseline, head = finalization
    with recovery.begin(repo, head, baseline, "finalize") as first:
        stage_and_move(first, repo, baseline, head)
        with first.private_index() as private:
            environment = first.commit_environment(private)
            first.commit_started()
            result = verify._run_git(
                ["git", "-C", str(repo), "commit", "-qm", "finalize"],
                repo,
                env=environment,
            )
            assert result.returncode == 0
            first.observe_command("commit", result.owned_capture)
            committed = first.observe_committed()
            first.publish_index(committed, cleanup_proven=True)
            first.completed(committed)
    assert recovery.pending(repo) is None
    assert git(repo, "rev-parse", first.record["recovery_ref"]) == head
    with recovery.begin(repo, committed, committed, "no-op") as second:
        second.completed(committed)
    assert second.id != first.id
    assert first.path.is_file() and second.path.is_file()
    assert (first.path.parent / "index-preimage.bin").is_file()


def test_durable_record_failure_never_yields_staging_authority(finalization, monkeypatch):
    repo, baseline, head = finalization
    before = verify._bound_real_index_path(repo).read_bytes()

    def unavailable(*args, **kwargs):
        raise OSError("durable record unavailable")

    monkeypatch.setattr(recovery, "_write", unavailable)
    with pytest.raises(OSError, match="durable record"):
        with recovery.begin(repo, head, baseline, "finalize"):
            pytest.fail("stage became available without durable evidence")
    assert git(repo, "rev-parse", "HEAD") == head
    assert verify._bound_real_index_path(repo).read_bytes() == before


def test_state_root_inside_worktree_is_refused(finalization, monkeypatch):
    repo, baseline, head = finalization
    monkeypatch.setattr(runs, "state_root", lambda: repo / "unsafe-state")
    with pytest.raises(recovery.FinalizationRecoveryError, match="outside"):
        with recovery.begin(repo, head, baseline, "finalize"):
            pytest.fail("worktree became its own recovery authority")
    assert not (repo / "unsafe-state").exists()


def capture_result(*, timed_out=False, quiescent=True, returncode=1):
    from bmad_loop.owned_capture import ActorEvidence, CaptureOwnership, CaptureResult

    ownership = CaptureOwnership(
        root_pid=123,
        root_identity=12.5,
        root_reaped=True,
        actors=(ActorEvidence(123, 12.5, "dead"),),
        census_count=1,
        census_complete=quiescent,
        problems=(),
        stdout_eof=True,
        stderr_eof=True,
        io_settled=True,
        stdout_captured=True,
        stderr_captured=True,
        cleanup_required=timed_out,
    )
    return CaptureResult(
        returncode,
        b"private stdout bytes",
        b"private stderr bytes",
        timed_out,
        False,
        0,
        True,
        ownership,
    )


def test_private_commit_index_preserves_real_index_on_refusal(finalization):
    repo, baseline, head = finalization
    index = verify._bound_real_index_path(repo)
    original = index.read_bytes()
    hook = repo / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\nexit 1\n")
    hook.chmod(0o755)
    with pytest.raises(verify.GitError, match="acknowledged refusal"):
        with recovery.begin(repo, head, baseline, "finalize") as tx:
            stage_and_move(tx, repo, baseline, head)
            staged = index.read_bytes()
            with tx.private_index() as private:
                assert index.with_name("index.lock").exists()
                assert private.read_bytes() == staged
                environment = tx.commit_environment(private)
                tx.commit_started()
                result = verify._run_git(
                    ["git", "-C", str(repo), "commit", "-m", "refused"],
                    repo,
                    env=environment,
                )
                assert result.returncode != 0 and result.owned_capture.ownership.quiescent
                tx.observe_command("commit", result.owned_capture)
                assert index.read_bytes() == staged
                assert tx.restore(cleanup_proven=True), tx.record
                tx.refused("acknowledged refusal")
                assert tx.state == "refused"
                raise verify.GitError("acknowledged refusal")
    assert recovery.pending(repo) is None
    assert json.loads(tx.path.read_text())["state"] == "refused"
    assert index.read_bytes() == original
    assert git(repo, "rev-parse", "HEAD") == head
    assert not index.with_name("index.lock").exists()
    assert private.exists()
    with recovery.begin(repo, head, baseline, "explicit next attempt") as next_tx:
        next_tx.refused("canceled before staging")


def test_private_success_publishes_index_only_after_acknowledged_commit(finalization):
    repo, baseline, head = finalization
    with recovery.begin(repo, head, baseline, "finalize") as tx:
        stage_and_move(tx, repo, baseline, head)
        with tx.private_index() as private:
            before = tx.index_path.read_bytes()
            environment = tx.commit_environment(private)
            tx.commit_started()
            result = verify._run_git(
                ["git", "-C", str(repo), "commit", "-m", "finalize"],
                repo,
                env=environment,
            )
            assert result.returncode == 0
            tx.observe_command("commit", result.owned_capture)
            current = tx.observe_committed()
            assert tx.index_path.read_bytes() == before
            tx.publish_index(current, cleanup_proven=True)
            assert tx.index_path.read_bytes() == private.read_bytes()
            tx.completed(current)
    assert recovery.pending(repo) is None
    assert git(repo, "rev-parse", tx.record["recovery_ref"]) == head
    assert tx.record["published_index"]["sha256"]


def test_timeout_capture_survives_compensation_and_cannot_be_refused(finalization):
    repo, baseline, head = finalization
    with recovery.begin(repo, head, baseline, "finalize") as tx:
        stage_and_move(tx, repo, baseline, head)
        with tx.private_index() as private:
            tx.commit_environment(private)
            tx.commit_started()
            tx.observe_command("commit", capture_result(timed_out=True, returncode=-15))
            tx.uncertain("commit timeout")
            assert tx.restore(cleanup_proven=True), tx.record
            with pytest.raises(recovery.FinalizationRecoveryError, match="uncertain command"):
                tx.refused("compensation is not acknowledgement")
    saved = recovery.pending(repo)
    observation = next(row for row in saved["commands"] if row["command"] == "commit")
    assert observation["capture"]["timed_out"] is True
    assert observation["capture"]["ownership"]["quiescent"] is True
    assert observation["capture"]["stdout"] == {
        "bytes": 20,
        "sha256": hashlib.sha256(b"private stdout bytes").hexdigest(),
    }
    assert "private stdout bytes" not in tx.path.read_text()
    assert any(event.get("reason") == "commit timeout" for event in saved["events"])


def test_missing_command_capture_cannot_be_overridden_by_cleanup_boolean(finalization):
    repo, baseline, head = finalization
    with recovery.begin(repo, head, baseline, "finalize") as tx:
        stage_and_move(tx, repo, baseline, head)
        tx.observe_command("commit", None)
        assert not tx.restore(cleanup_proven=True)
        assert git(repo, "rev-parse", "HEAD") == baseline
        assert not tx.record["commands"][-1]["quiescent"]


def test_recovery_ref_needs_quiescent_capture_before_yield(finalization, monkeypatch):
    repo, baseline, head = finalization
    original = verify._run_git

    def uncertain_ref(argv, *args, **kwargs):
        result = original(argv, *args, **kwargs)
        if "update-ref" in argv and any(arg.startswith("refs/bmad-finalization/") for arg in argv):
            result.owned_capture = dataclasses.replace(
                result.owned_capture,
                ownership=dataclasses.replace(
                    result.owned_capture.ownership, census_complete=False
                ),
            )
        return result

    monkeypatch.setattr(verify, "_run_git", uncertain_ref)
    with pytest.raises(recovery.FinalizationRecoveryError, match="recovery-ref creation"):
        with recovery.begin(repo, head, baseline, "finalize"):
            pytest.fail("staging admitted with unresolved reference hook ownership")
    saved = recovery.pending(repo)
    assert saved["state"] == "uncertain"
    assert saved["commands"][0]["quiescent"] is False
    assert git(repo, "rev-parse", saved["recovery_ref"]) == head


@pytest.mark.parametrize(
    "value", [{"bytes": b"raw"}, {"number": float("inf")}, {"map": {1: "lossy key"}}]
)
def test_non_json_context_is_refused_before_state_writes(finalization, value):
    repo, baseline, head = finalization
    root = runs.state_root()
    assert not root.exists()
    with pytest.raises(recovery.FinalizationRecoveryError, match="JSON"):
        with recovery.begin(repo, head, baseline, "finalize", context=value):
            pytest.fail("invalid context became authority")
    assert not root.exists()


def test_context_is_frozen_before_staging_and_never_automatically_applied(finalization):
    repo, baseline, head = finalization
    context = {"run_id": "run-1", "task": "task-1", "undo": {"bytes_base64": "AAH/"}}
    with recovery.begin(repo, head, baseline, "finalize", context=context) as tx:
        context["undo"]["bytes_base64"] = "changed"
        assert tx.record["context"]["undo"]["bytes_base64"] == "AAH/"
        assert json.loads(tx.path.read_text())["context"] == tx.record["context"]
        tx.refused("before mutation")


def test_pending_stat_failure_is_not_absence(finalization, monkeypatch):
    from pathlib import Path

    repo, _, _ = finalization
    original = Path.lstat

    def denied(path, *args, **kwargs):
        if path.name == "active.json":
            raise PermissionError("active record unreadable")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "lstat", denied)
    with pytest.raises(PermissionError, match="unreadable"):
        recovery.pending(repo)


def test_interrupt_survives_recording_failure_before_state_update(finalization, monkeypatch):
    repo, baseline, head = finalization
    interrupt = KeyboardInterrupt("original interrupt")
    with pytest.raises(KeyboardInterrupt) as caught:
        with recovery.begin(repo, head, baseline, "finalize") as tx:

            def unavailable(reason):
                raise OSError("record persistence unavailable")

            monkeypatch.setattr(tx, "uncertain", unavailable)
            raise interrupt
    assert caught.value is interrupt
    assert "recording failed" in " ".join(interrupt.__notes__)
    assert recovery.pending(repo)["id"] == tx.id


@pytest.mark.parametrize("observation", ["missing", "unquiescent", "quiescent_timeout"])
def test_private_index_lock_is_retained_until_writer_cleanup_is_proven(finalization, observation):
    repo, baseline, head = finalization
    with recovery.begin(repo, head, baseline, "finalize") as tx:
        stage_and_move(tx, repo, baseline, head)
        with tx.private_index() as private:
            tx.commit_environment(private)
            tx.commit_started()
            capture = (
                None
                if observation == "missing"
                else capture_result(timed_out=True, quiescent=observation == "quiescent_timeout")
            )
            tx.observe_command("commit", capture)
        lock = tx.index_path.with_name("index.lock")
        if observation == "quiescent_timeout":
            assert not lock.exists()
        else:
            assert lock.exists()
            lease = json.loads(lock.read_text())
            assert lease["operation"] == tx.id
            assert tx.record["index_lock"]["identity"] == [lock.stat().st_dev, lock.stat().st_ino]
            assert tx.record["index_lock"]["state"] == "retained"
            assert "uncertain" in tx.record["index_lock"]["reason"]


def test_private_index_lock_survives_capture_recording_failure(finalization, monkeypatch):
    repo, baseline, head = finalization
    with pytest.raises(OSError, match="record failed"):
        with recovery.begin(repo, head, baseline, "finalize") as tx:
            stage_and_move(tx, repo, baseline, head)
            with tx.private_index() as private:
                tx.commit_environment(private)
                tx.commit_started()

                def unavailable(*args, **kwargs):
                    raise OSError("record failed")

                monkeypatch.setattr(recovery, "_write", unavailable)
                tx.observe_command("commit", capture_result(returncode=0))
    lock = tx.index_path.with_name("index.lock")
    assert lock.exists() and json.loads(lock.read_text())["operation"] == tx.id


def test_index_lock_cleanup_failure_does_not_replace_interrupt(finalization, monkeypatch):
    repo, baseline, head = finalization
    interrupt = KeyboardInterrupt("original interruption")
    identity = recovery._identity
    with pytest.raises(KeyboardInterrupt) as caught:
        with recovery.begin(repo, head, baseline, "finalize") as tx:
            stage_and_move(tx, repo, baseline, head)
            with tx.private_index():

                def unavailable(path):
                    if path.name == "index.lock":
                        raise OSError("lock stat unavailable")
                    return identity(path)

                monkeypatch.setattr(recovery, "_identity", unavailable)
                raise interrupt
    assert caught.value is interrupt
    assert "index-lock cleanup failed" in " ".join(interrupt.__notes__)


@pytest.mark.parametrize("blob", ["index-preimage.bin", "index-postimage.bin"])
def test_missing_index_blob_refuses_compensation(finalization, blob):
    repo, baseline, head = finalization
    with recovery.begin(repo, head, baseline, "finalize") as tx:
        stage_and_move(tx, repo, baseline, head)
        before = tx.index_path.read_bytes()
        (tx.path.parent / blob).unlink()
        assert not tx.restore(cleanup_proven=True)
        assert git(repo, "rev-parse", "HEAD") == baseline
        assert tx.index_path.read_bytes() == before


def test_incomplete_completed_record_never_permits_a_new_operation(finalization):
    repo, baseline, head = finalization
    with recovery.begin(repo, head, baseline, "finalize") as tx:
        tx.completed(head)
    record = json.loads(tx.path.read_text())
    record.pop("index_preimage")
    tx.path.write_text(json.dumps(record))
    with pytest.raises(recovery.FinalizationRecoveryError, match="incomplete"):
        with recovery.begin(repo, head, baseline, "must refuse"):
            pytest.fail("incomplete completed evidence was treated as safe absence")


def test_terminal_record_with_unreleased_owned_lock_still_blocks_begin(finalization, monkeypatch):
    repo, baseline, head = finalization
    original_identity = recovery._identity
    with monkeypatch.context() as patch:
        with recovery.begin(repo, head, baseline, "finalize") as tx:
            stage_and_move(tx, repo, baseline, head)
            with tx.private_index() as private:
                environment = tx.commit_environment(private)
                tx.commit_started()
                result = verify._run_git(
                    ["git", "-C", str(repo), "commit", "-qm", "finalize"],
                    repo,
                    env=environment,
                )
                assert result.returncode == 0
                tx.observe_command("commit", result.owned_capture)
                current = tx.observe_committed()
                tx.publish_index(current, cleanup_proven=True)
                tx.completed(current)
                assert tx.record["index_lock"]["state"] == "held"
                # Model a crash before cleanup by preventing the test's normal
                # unwind from finding its owned inode. Actual lease stays intact.
                patch.setattr(
                    recovery,
                    "_identity",
                    lambda path: [-1, -1] if path.name == "index.lock" else original_identity(path),
                )
    lock = tx.index_path.with_name("index.lock")
    assert lock.exists()
    assert original_identity(lock) == tx.record["index_lock"]["identity"]
    assert recovery.pending(repo)["id"] == tx.id
    pointer = (tx.path.parent.parent / "active.json").read_bytes()
    with pytest.raises(recovery.FinalizationRecoveryError, match=tx.id):
        with recovery.begin(repo, current, current, "must not replace crash record"):
            pytest.fail("terminal marker hid the unreleased index lease")
    assert (tx.path.parent.parent / "active.json").read_bytes() == pointer


def test_raw_git_exception_output_is_not_persisted_in_record_or_events(finalization, monkeypatch):
    repo, baseline, head = finalization
    canary = "ghp_CANARY_do_not_persist_raw_git_output_0123456789"
    with pytest.raises(verify.GitError, match="CANARY"):
        with recovery.begin(repo, head, baseline, "finalize") as tx:
            raise verify.GitError(canary)
    assert canary not in tx.path.read_text()
    assert recovery.pending(repo)["reason"] == "GitError"


def test_restore_probe_error_diagnostic_never_copies_raw_git_output(finalization, monkeypatch):
    repo, baseline, head = finalization
    canary = "provider-secret-CANARY-from-git-stderr"
    with recovery.begin(repo, head, baseline, "finalize") as tx:
        stage_and_move(tx, repo, baseline, head)

        def failed(*args, **kwargs):
            raise verify.GitError(canary)

        monkeypatch.setattr(verify, "prepared_ref_update", failed)
        assert not tx.restore(cleanup_proven=True)
        assert canary not in tx.path.read_text()
        assert "GitError" in tx.record["reason"]


def test_concurrent_ref_move_without_index_change_cannot_be_compensated(finalization):
    repo, baseline, head = finalization
    with recovery.begin(repo, head, baseline, "finalize") as tx:
        stage_and_move(tx, repo, baseline, head)
        before = tx.index_path.read_bytes()
        tree = git(repo, "rev-parse", baseline + "^{tree}")
        foreign = git(repo, "commit-tree", tree, "-p", baseline, "-m", "concurrent owner commit")
        git(repo, "update-ref", tx.branch_ref, foreign, baseline)
        assert tx.index_path.read_bytes() == before
        assert not tx.restore(cleanup_proven=True)
        assert git(repo, "rev-parse", "HEAD") == foreign
        assert tx.index_path.read_bytes() == before


def test_private_index_cleanup_never_removes_peer_replacement_lock(finalization):
    repo, baseline, head = finalization
    with recovery.begin(repo, head, baseline, "finalize") as tx:
        stage_and_move(tx, repo, baseline, head)
        with tx.private_index():
            lock = tx.index_path.with_name("index.lock")
            original_identity = [lock.stat().st_dev, lock.stat().st_ino]
            lock.unlink()
            lock.write_bytes(b"peer replacement lock")
            assert [lock.stat().st_dev, lock.stat().st_ino] != original_identity
        assert lock.read_bytes() == b"peer replacement lock"


def test_cooperating_stage_writer_cannot_enter_between_preimage_check_and_add(finalization):
    repo, baseline, head = finalization
    (repo / "rival.txt").write_text("rival content\n")
    blob = git(repo, "hash-object", "-w", str(repo / "rival.txt"))
    with recovery.begin(repo, head, baseline, "finalize") as tx:
        before = tx.index_path.read_bytes()
        with tx.staging_index() as private:
            tx.stage_started()
            rival = verify.git_bytes(
                repo, "update-index", "--cacheinfo", "100644," + blob + ",staged.txt"
            )
            assert rival.returncode != 0 and b"index.lock" in rival.stderr
            assert tx.index_path.read_bytes() == before
            added = verify._run_git(
                ["git", "-C", str(repo), "add", "-A"],
                repo,
                env={**os.environ, "GIT_INDEX_FILE": str(private)},
            )
            assert added.returncode == 0
            tx.observe_command("add", added.owned_capture)
            assert tx.index_path.read_bytes() == before
            tx.publish_staged_index(cleanup_proven=True)
            tx.staged()
            assert tx.restore(cleanup_proven=True), tx.record
            tx.refused("validator refused before commit")
        assert tx.index_path.read_bytes() == before


def test_staging_refuses_index_change_before_acquiring_lease(finalization):
    repo, baseline, head = finalization
    with recovery.begin(repo, head, baseline, "finalize") as tx:
        (repo / "rival.txt").write_text("new staged contribution\n")
        git(repo, "add", "rival.txt")
        before = tx.index_path.read_bytes()
        with pytest.raises(recovery.FinalizationRecoveryError, match="index changed"):
            with tx.staging_index():
                pytest.fail("private add admitted on an obsolete index preimage")
        assert tx.index_path.read_bytes() == before
        assert git(repo, "show", ":rival.txt") == "new staged contribution"


@pytest.mark.parametrize("returncode", [-15, None])
def test_signal_or_missing_exit_is_not_a_definitive_refusal(finalization, returncode):
    repo, baseline, head = finalization
    with recovery.begin(repo, head, baseline, "finalize") as tx:
        stage_and_move(tx, repo, baseline, head)
        with tx.private_index() as private:
            tx.commit_environment(private)
            tx.commit_started()
            tx.observe_command("commit", capture_result(returncode=returncode))
            assert tx.restore(cleanup_proven=True), tx.record
            with pytest.raises(recovery.FinalizationRecoveryError, match="uncertain command"):
                tx.refused("signal is not a Git refusal")
    assert recovery.pending(repo)["state"] == "uncertain"


def test_operation_lock_cleanup_cannot_mask_original_interrupt(finalization, monkeypatch):
    repo, baseline, head = finalization
    interrupt = KeyboardInterrupt("original interruption")

    @contextlib.contextmanager
    def broken_release(*args, **kwargs):
        try:
            yield
        finally:
            raise OSError("operation lock cleanup failed")

    monkeypatch.setattr(recovery, "file_lock", broken_release)
    with pytest.raises(KeyboardInterrupt) as caught:
        with recovery.begin(repo, head, baseline, "finalize"):
            raise interrupt
    assert caught.value is interrupt
    assert "operation-lock cleanup failed" in " ".join(interrupt.__notes__)


@pytest.mark.parametrize(
    "foreign_kind", ["different_tree", "empty_descendant", "same_tree_sibling"]
)
def test_observed_commit_is_operation_bound_and_pinned_before_foreign_head_refusal(
    finalization, foreign_kind
):
    repo, baseline, head = finalization
    with recovery.begin(repo, head, baseline, "finalize") as tx:
        stage_and_move(tx, repo, baseline, head)
        with tx.private_index() as private:
            environment = tx.commit_environment(private)
            tx.commit_started()
            committed = verify._run_git(
                ["git", "-C", str(repo), "commit", "-qm", "owned commit"], repo, env=environment
            )
            assert committed.returncode == 0
            tx.observe_command("commit", committed.owned_capture)
            own = git(repo, "rev-parse", "HEAD")
            own_tree = git(repo, "rev-parse", own + "^{tree}")
            tree = (
                git(repo, "rev-parse", baseline + "^{tree}")
                if foreign_kind == "different_tree"
                else own_tree
            )
            parent = baseline if foreign_kind == "same_tree_sibling" else own
            foreign = git(repo, "commit-tree", tree, "-p", parent, "-m", "foreign peer commit")
            git(repo, "update-ref", tx.branch_ref, foreign, own)
            before = tx.index_path.read_bytes()
            with pytest.raises(recovery.FinalizationRecoveryError, match="current HEAD differs"):
                tx.observe_committed()
            evidence = tx.record["commit_evidence"]
            assert evidence["head"] == own and evidence["tree"] == own_tree
            assert evidence["parents"] == [baseline] and evidence["validated"]
            assert git(repo, "rev-parse", evidence["recovery_ref"]) == own
            with pytest.raises(recovery.FinalizationRecoveryError, match="exact pinned"):
                tx.publish_index(foreign, cleanup_proven=True)
            assert git(repo, "rev-parse", "HEAD") == foreign
            assert tx.index_path.read_bytes() == before
    assert recovery.pending(repo)["id"] == tx.id


@pytest.mark.parametrize("count", ["", "0", "2"])
def test_commit_environment_preserves_scoped_configuration_without_host_writes(
    finalization, monkeypatch, count
):
    repo, baseline, head = finalization
    with recovery.begin(repo, head, baseline, "finalize") as tx:
        stage_and_move(tx, repo, baseline, head)
        with tx.private_index() as private:
            monkeypatch.setenv("GIT_CONFIG_COUNT", count)
            if count == "2":
                monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.logAllRefUpdates")
                monkeypatch.setenv("GIT_CONFIG_VALUE_0", "false")
                monkeypatch.setenv("GIT_CONFIG_KEY_1", "example.preserved")
                monkeypatch.setenv("GIT_CONFIG_VALUE_1", "kept value")
            before = dict(os.environ)
            env = tx.commit_environment(private)
            n = int(count or "0")
            assert env["GIT_CONFIG_COUNT"] == str(n + 1)
            assert env[f"GIT_CONFIG_KEY_{n}"] == "core.logAllRefUpdates"
            assert env[f"GIT_CONFIG_VALUE_{n}"] == "true"
            assert env["GIT_NO_REPLACE_OBJECTS"] == "1"
            assert env["GIT_REFLOG_ACTION"] == "bmad-finalization-" + tx.id
            if n:
                assert env["GIT_CONFIG_KEY_0"] == before["GIT_CONFIG_KEY_0"]
                assert env["GIT_CONFIG_VALUE_1"] == "kept value"
            assert dict(os.environ) == before


@pytest.mark.parametrize("count", ["-1", "invalid", "1", "9999"])
def test_invalid_commit_environment_refuses_before_commit_intent(finalization, monkeypatch, count):
    repo, baseline, head = finalization
    with recovery.begin(repo, head, baseline, "finalize") as tx:
        stage_and_move(tx, repo, baseline, head)
        with tx.private_index() as private:
            monkeypatch.setenv("GIT_CONFIG_COUNT", count)
            monkeypatch.delenv("GIT_CONFIG_KEY_0", raising=False)
            monkeypatch.delenv("GIT_CONFIG_VALUE_0", raising=False)
            with pytest.raises(recovery.FinalizationRecoveryError, match="configuration"):
                tx.commit_environment(private)
            assert tx.state == "head-moved"
            assert "commit_environment" not in tx.record


@pytest.mark.parametrize("defect", ["parent", "private_tree", "missing_nonce", "ambiguous_nonce"])
def test_commit_observation_refuses_invalid_parent_tree_or_nonce(finalization, defect):
    repo, baseline, head = finalization
    with recovery.begin(repo, head, baseline, "finalize") as tx:
        stage_and_move(tx, repo, baseline, head)
        with tx.private_index() as private:
            environment = tx.commit_environment(private)
            nonce = environment["GIT_REFLOG_ACTION"]
            tx.commit_started()
            if defect == "parent":
                tree = git(repo, "rev-parse", baseline + "^{tree}")
                foreign_parent = git(repo, "commit-tree", tree, "-p", baseline, "-m", "peer base")
                git(repo, "update-ref", tx.branch_ref, foreign_parent, baseline)
            if defect == "missing_nonce":
                environment = {**environment, "GIT_REFLOG_ACTION": "another-operation"}
            result = verify._run_git(
                ["git", "-C", str(repo), "commit", "-qm", "owned product"], repo, env=environment
            )
            assert result.returncode == 0 and result.owned_capture.ownership.quiescent
            tx.observe_command("commit", result.owned_capture)
            own = git(repo, "rev-parse", "HEAD")
            own_tree = git(repo, "rev-parse", own + "^{tree}")
            parents = git(repo, "show", "-s", "--format=%P", own).split()
            if defect == "ambiguous_nonce":
                second = verify._run_git(
                    ["git", "-C", str(repo), "commit", "--allow-empty", "-qm", "same action again"],
                    repo,
                    env=environment,
                )
                assert second.returncode == 0 and second.owned_capture.ownership.quiescent
                tx.observe_command("commit", second.owned_capture)
            elif defect == "private_tree":
                blob = git(repo, "rev-parse", baseline + ":product.txt")
                changed = verify.git_bytes(
                    repo,
                    "update-index",
                    "--cacheinfo",
                    "100644," + blob + ",product.txt",
                    env={**verify._bound_git_env(), "GIT_INDEX_FILE": str(private)},
                )
                assert changed.returncode == 0
            private_tree = verify.git_bytes(
                repo, "write-tree", env={**verify._bound_git_env(), "GIT_INDEX_FILE": str(private)}
            )
            assert private_tree.returncode == 0
            matches = [
                line
                for line in git(
                    repo, "reflog", "show", "--max-count=256", "--format=%gs", tx.branch_ref
                ).splitlines()
                if line.startswith(nonce + ": ")
            ]
            assert len(matches) == (
                0 if defect == "missing_nonce" else 2 if defect == "ambiguous_nonce" else 1
            )
            assert (parents == [baseline]) is (defect != "parent")
            assert (private_tree.stdout.decode().strip() == own_tree) is (defect != "private_tree")
            current = git(repo, "rev-parse", "HEAD")
            before = tx.index_path.read_bytes()
            reason = (
                "expected parent or private index tree"
                if defect in {"parent", "private_tree"}
                else "missing or ambiguous"
            )
            with pytest.raises(recovery.FinalizationRecoveryError, match=reason):
                tx.observe_committed()
            assert git(repo, "rev-parse", "HEAD") == current
            assert tx.index_path.read_bytes() == before
            if defect in {"parent", "private_tree"}:
                evidence = tx.record["commit_evidence"]
                assert evidence["head"] == own and evidence["pinned"]
                assert not evidence["validated"]
                assert git(repo, "rev-parse", evidence["recovery_ref"]) == own
