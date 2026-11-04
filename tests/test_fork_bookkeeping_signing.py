"""Bookkeeping honors checkout signing while repository hooks stay disabled."""

from __future__ import annotations

import copy
import dataclasses
import shutil
import subprocess
from pathlib import Path

import pytest
from conftest import committing_crash_state, git, write_ledger
from test_engine import _bookkeeping_engine, _bookkeeping_rows, make_engine, resume_engine
from test_verify import _HOOK_NAMES, _install_marker_hooks

from bmad_loop import verify
from bmad_loop.journal import load_state
from bmad_loop.model import Phase
from bmad_loop.policy import ReviewPolicy


@pytest.fixture
def ssh_signing(project, tmp_path, monkeypatch):
    """Use only an ephemeral unencrypted key and this sandbox's local config."""
    signer = shutil.which("ssh-keygen")
    if signer is None:
        pytest.skip("SSH signing requires ssh-keygen")
    monkeypatch.delenv("SSH_AUTH_SOCK", raising=False)
    key = tmp_path / "test-signing-key"
    subprocess.run(
        [signer, "-q", "-t", "ed25519", "-N", "", "-C", "test@test", "-f", str(key)],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    allowed = tmp_path / "allowed-signers"
    allowed.write_text(
        f"test@test {Path(str(key) + '.pub').read_text(encoding='utf-8')}", encoding="utf-8"
    )
    for name, value in (
        ("gpg.format", "ssh"),
        ("gpg.ssh.program", signer),
        ("gpg.ssh.allowedSignersFile", str(allowed)),
        ("user.signingkey", str(key)),
        ("commit.gpgsign", "true"),
    ):
        git(project.project, "config", "--local", name, value)
    return project.project


def _assert_signed(repo, revision):
    assert "\ngpgsig -----BEGIN SSH SIGNATURE-----\n" in git(repo, "cat-file", "commit", revision)
    git(repo, "verify-commit", revision)
    assert git(repo, "log", "-1", "--format=%G?", revision) == "G"


def test_hookless_commit_paths_keeps_ssh_signature_and_disables_every_hook(ssh_signing, tmp_path):
    repo = ssh_signing
    hooks = tmp_path / "configured-hooks"
    _install_marker_hooks(hooks)
    git(repo, "config", "core.hooksPath", hooks.as_posix())
    (repo / "src.txt").write_text("signed bookkeeping\n", encoding="utf-8")

    signed = verify.commit_paths(
        repo, "chore: signed bookkeeping", [repo / "src.txt"], run_hooks=False
    )

    assert signed == git(repo, "rev-parse", "HEAD")
    _assert_signed(repo, signed)
    assert [name for name in _HOOK_NAMES if (repo / f"hook-{name}").exists()] == []
    # Positive hook control proves the same configured hooks are executable.
    (repo / "src.txt").write_text("ordinary signed commit\n", encoding="utf-8")
    ordinary = verify.commit_paths(repo, "chore: hooks live", [repo / "src.txt"])
    _assert_signed(repo, ordinary)
    assert [name for name in _HOOK_NAMES if (repo / f"hook-{name}").exists()] == list(_HOOK_NAMES)


def test_engine_bookkeeping_records_verified_ssh_signed_commit(project, ssh_signing):
    write_ledger(project, {"DW-1": "open"}, commit=False)
    engine, task = _bookkeeping_engine(project)

    engine._commit_engine_ledger(task, site="review")

    [event] = _bookkeeping_rows(engine)
    assert event["kind"] == "engine-ledger-commit"
    head = git(ssh_signing, "rev-parse", "HEAD")
    assert event["commit"] == head
    _assert_signed(ssh_signing, head)
    saved = load_state(engine.run_dir).tasks[task.story_key]
    assert saved.bookkeeping_for_baseline()["shas"] == [head]
    assert git(ssh_signing, "status", "--porcelain", "--", str(project.deferred_work)) == ""


def test_engine_signer_failure_keeps_prior_commit_and_never_falls_back_unsigned(
    project, ssh_signing, tmp_path
):
    """Restoring the signing override makes the failure look like a success."""
    write_ledger(project, {"DW-1": "open"}, commit=False)
    engine, task = _bookkeeping_engine(project)
    engine._commit_engine_ledger(task, site="dev")
    previous = copy.deepcopy(task.bookkeeping_ledger)
    head = git(ssh_signing, "rev-parse", "HEAD")
    with project.deferred_work.open("a", encoding="utf-8") as ledger:
        ledger.write("\n### DW-2: next finding\n\nstatus: open\n")
    git(ssh_signing, "config", "user.signingkey", str(tmp_path / "missing-signing-key"))

    engine._commit_engine_ledger(task, site="review")

    assert git(ssh_signing, "rev-parse", "HEAD") == head
    assert task.bookkeeping_ledger == previous
    assert load_state(engine.run_dir).tasks[task.story_key].bookkeeping_ledger == previous
    assert git(ssh_signing, "status", "--porcelain", "--", str(project.deferred_work))
    assert [row["kind"] for row in _bookkeeping_rows(engine)] == [
        "engine-ledger-commit",
        "engine-ledger-commit-failed",
    ]
    assert "git commit failed" in _bookkeeping_rows(engine)[-1]["error"]


def test_hookless_commit_paths_respects_explicit_unsigned_configuration(ssh_signing, tmp_path):
    repo = ssh_signing
    git(repo, "config", "commit.gpgsign", "false")
    git(repo, "config", "user.signingkey", str(tmp_path / "missing-signing-key"))
    (repo / "src.txt").write_text("explicitly unsigned bookkeeping\n", encoding="utf-8")

    unsigned = verify.commit_paths(
        repo, "chore: unsigned by configuration", [repo / "src.txt"], run_hooks=False
    )

    assert unsigned == git(repo, "rev-parse", "HEAD")
    assert "\ngpgsig " not in git(repo, "cat-file", "commit", unsigned)
    assert git(repo, "log", "-1", "--format=%G?", unsigned) == "N"


@pytest.mark.parametrize("resume", ["committing", "reverify"])
def test_native_resume_finalization_keeps_configured_ssh_signing(project, ssh_signing, resume):
    engine, _ = make_engine(project, [])
    engine.policy = dataclasses.replace(engine.policy, review=ReviewPolicy(enabled=False))
    baseline = committing_crash_state(project, engine)
    if resume == "reverify":
        task = engine.state.tasks["1-1-a"]
        task.phase = Phase.DEV_VERIFY
        task.reverify_from = "deferred"
        engine._save()

    resumed, adapter = resume_engine(project, engine, [])
    resumed._finish_inflight()

    final = load_state(resumed.run_dir).tasks["1-1-a"]
    assert final.phase == Phase.DONE and final.baseline_commit == baseline
    assert final.commit_sha == git(ssh_signing, "rev-parse", "HEAD")
    _assert_signed(ssh_signing, final.commit_sha)
    assert adapter.sessions == []
