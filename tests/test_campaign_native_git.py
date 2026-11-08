"""Two real sandbox writers cannot expose missing objects to concurrent fetch."""

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest
from conftest import git

from bmad_loop.campaign_execution_sandbox import command
from bmad_loop.campaign_model import CampaignError
from bmad_loop.campaign_native_git import publish


@pytest.fixture
def private_view(project, tmp_path):
    from bmad_loop.campaign_execution_sandbox import _object_bank
    from bmad_loop.campaign_native_git import environment, prepare

    workspace = tmp_path / "private-worker"
    git(project.project, "worktree", "add", "-b", "native-private", str(workspace))
    baseline = git(workspace, "rev-parse", "HEAD").strip()
    directory = tmp_path / "private-job"
    directory.mkdir()
    bank, objects = _object_bank(
        {"git_branch": "native-private", "config_digest": "test"}, directory, workspace
    )
    record = prepare(bank, objects, workspace, directory, "native-private")
    env = environment(record)

    def native(*args):
        result = subprocess.run(
            ["git", "-C", str(workspace), *args], env=env, capture_output=True, text=True
        )
        assert result.returncode == 0, result.stderr
        return result.stdout.strip()

    return workspace, baseline, directory, native


def test_rewritten_native_history_cannot_replace_published_checkpoint(private_view):
    workspace, baseline, directory, native = private_view
    (workspace / "src.txt").write_text("first")
    native("add", "src.txt")
    native("commit", "-qm", "first")
    head = publish(workspace, baseline, directory=directory)
    native("reset", "--hard", baseline)
    with pytest.raises(CampaignError, match="diverged"):
        publish(workspace, baseline, directory=directory, final=True)
    assert git(workspace, "rev-parse", "HEAD").strip() == head


def test_private_index_flags_cannot_hide_owner_changes(private_view):
    workspace, baseline, directory, native = private_view
    native("update-index", "--assume-unchanged", "src.txt")
    (workspace / "src.txt").write_text("hidden native modification")
    with pytest.raises(CampaignError, match="index hides paths"):
        publish(workspace, baseline, directory=directory, final=True)
    assert git(workspace, "rev-parse", "HEAD").strip() == baseline
    assert (workspace / "src.txt").read_text() == "hidden native modification"


def test_concurrent_shared_ref_update_is_not_overwritten(private_view, monkeypatch):
    from bmad_loop import campaign_native_git

    workspace, baseline, directory, native = private_view
    (workspace / "src.txt").write_text("native")
    native("add", "src.txt")
    native("commit", "-qm", "native")
    concurrent = git(
        workspace,
        "commit-tree",
        baseline + "^{tree}",
        "-p",
        baseline,
        "-m",
        "concurrent owner update",
    ).strip()
    real = campaign_native_git._git

    def racing(repo, *args, **kwargs):
        if args[0] == "update-ref":
            git(repo, "update-ref", args[1], concurrent, args[3])
        return real(repo, *args, **kwargs)

    monkeypatch.setattr(campaign_native_git, "_git", racing)
    with pytest.raises(CampaignError, match="cannot lock ref"):
        publish(workspace, baseline, directory=directory, final=True)
    assert git(workspace, "rev-parse", "HEAD").strip() == concurrent


def test_ref_index_crash_reconciles_exact_pending_tree_without_losing_staging(
    private_view, monkeypatch
):
    from bmad_loop import campaign_native_git

    workspace, baseline, directory, native = private_view
    (workspace / "src.txt").write_text("committed")
    native("add", "src.txt")
    native("commit", "-qm", "committed")
    head = native("rev-parse", "HEAD")
    (workspace / "src.txt").write_text("staged after commit")
    native("add", "src.txt")
    real = campaign_native_git._git

    def crash(repo, *args, **kwargs):
        if args[0] == "read-tree":
            raise CampaignError("simulated controller death after CAS")
        return real(repo, *args, **kwargs)

    monkeypatch.setattr(campaign_native_git, "_git", crash)
    with pytest.raises(CampaignError, match="controller death"):
        publish(workspace, baseline, directory=directory, final=True)
    assert git(workspace, "rev-parse", "HEAD").strip() == head
    monkeypatch.setattr(campaign_native_git, "_git", real)
    # Recovery runs through the live observer too; it cannot rely on a second
    # finalization call to reconstruct the lost canonical index.
    assert publish(workspace, baseline, directory=directory) == head
    assert git(workspace, "show", ":src.txt") == "staged after commit"
    assert (workspace / "src.txt").read_text() == "staged after commit"


def test_exact_final_session_does_not_replay_prior_session_staging(private_view, tmp_path):
    from bmad_loop.campaign_execution_sandbox import _object_bank
    from bmad_loop.campaign_native_git import environment, prepare

    workspace, baseline, first, native = private_view
    (workspace / "src.txt").write_text("first")
    native("add", "src.txt")
    native("commit", "-qm", "first")
    publish(workspace, baseline, directory=first, final=True)
    second = tmp_path / "second-job"
    second.mkdir()
    bank, objects = _object_bank(
        {"git_branch": "native-private", "config_digest": "test"}, second, workspace
    )
    record = prepare(bank, objects, workspace, second, "native-private")
    env = environment(record)
    (workspace / "src.txt").write_text("second")
    for args in (("add", "src.txt"), ("commit", "-qm", "second")):
        result = subprocess.run(
            ["git", "-C", str(workspace), *args], env=env, capture_output=True, text=True
        )
        assert result.returncode == 0, result.stderr
    head = publish(workspace, baseline, directory=second, final=True)
    assert git(workspace, "show", "HEAD:src.txt") == "second"
    assert git(workspace, "diff", "--cached", "--name-only") == ""
    assert publish(workspace, baseline) == head
    assert git(workspace, "show", ":src.txt") == "second"


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS scoped native writer")
def test_two_native_writers_keep_refs_private_during_fetch_and_preserve_staging(project, tmp_path):
    root = project.project
    baseline = git(root, "rev-parse", "HEAD").strip()
    remote = tmp_path / "remote.git"
    git(root, "clone", "--bare", str(root), str(remote))
    git(root, "remote", "add", "private-test", str(remote))
    script = """import subprocess,sys,time
from pathlib import Path
def git(*args):return subprocess.check_output(['git',*args],text=True).strip()
Path('src/owned.txt').write_text('accepted')
git('add','src/owned.txt');git('commit','-qm','native owned commit')
Path('src/staged.txt').write_text('staged intention');git('add','src/staged.txt')
Path('src/unstaged.txt').write_text('unstaged intention')
Path('src/ready').write_text(git('rev-parse','HEAD'))
deadline=time.monotonic()+30
while not Path(sys.argv[1]).exists():
 if time.monotonic()>deadline:raise RuntimeError('controller did not release owned fixture')
 time.sleep(.01)
"""
    workers = []
    try:
        for index in range(2):
            workspace = tmp_path / f"worker-{index}"
            git(root, "worktree", "add", "-b", f"bmad-private-{index}", str(workspace))
            (workspace / "src").mkdir()
            directory = tmp_path / f"job-{index}"
            directory.mkdir()
            grant = directory / "grants.json"
            grant.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "workspace": str(workspace),
                        "scopes": ["src"],
                        "config_digest": "test",
                        "git_branch": f"bmad-private-{index}",
                        "protected_branches": ["main"],
                    }
                )
            )
            release = directory / "release"
            argv, cwd, env = command(
                grant, [str(Path(sys.executable).resolve()), "-c", script, str(release)]
            )
            process = subprocess.Popen(
                argv, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
            )
            workers.append((workspace, directory, release, process))
        deadline = time.monotonic() + 15
        for workspace, _, _, process in workers:
            while not (workspace / "src/ready").exists():
                assert process.poll() is None, process.communicate()
                assert time.monotonic() < deadline, "native fixture did not commit"
                time.sleep(0.01)
        for _ in range(4):
            git(root, "fetch", "private-test", "main")
            git(root, "fsck", "--connectivity-only")
        for workspace, directory, _, _ in workers:
            assert git(workspace, "rev-parse", "HEAD").strip() == baseline
            candidate = (workspace / "src/ready").read_text()
            assert publish(workspace, baseline, directory=directory) == candidate
            git(root, "fetch", "private-test", "main")
            assert git(workspace, "diff", "--cached", "--name-only") == ""
        for workspace, directory, release, process in workers:
            release.touch()
            output, error = process.communicate(timeout=10)
            assert process.returncode == 0, (output, error)
            publish(workspace, baseline, directory=directory, final=True)
            assert git(workspace, "diff", "--cached", "--name-only").strip() == "src/staged.txt"
            assert (workspace / "src/unstaged.txt").read_text() == "unstaged intention"
            assert (
                git(root, "show", git(workspace, "rev-parse", "HEAD").strip() + ":src/owned.txt")
                == "accepted"
            )
        git(root, "fsck", "--connectivity-only")
    finally:
        for _, _, release, process in workers:
            release.touch()
            if process.poll() is None:
                process.communicate(timeout=10)
