"""Parallel campaign observations retain independent targets in shared Git storage."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event

from conftest import git
from test_campaign_workers import envelope

from bmad_loop import campaign_workers as workers


def test_parallel_integrations_keep_exact_targets_without_shared_ref_updates(
    project, tmp_path, monkeypatch
):
    repo = project.project
    git(repo, "branch", "-M", "main")
    remote = tmp_path / "remote.git"
    git(tmp_path, "clone", "--bare", str(repo), str(remote))
    git(remote, "config", "user.name", "Campaign Test")
    git(remote, "config", "user.email", "campaign@example.invalid")
    git(repo, "remote", "add", "origin", str(remote))
    git(repo, "fetch", "origin")
    original_head = git(repo, "rev-parse", "main")
    first, second = tmp_path / "first", tmp_path / "second"
    for workspace in (first, second):
        git(repo, "worktree", "add", "-b", workspace.name, str(workspace), "main")
    context = envelope(repo, tmp_path / "execution")
    observed, release = Event(), Event()
    original_git = workers.git

    def gated_git(workspace, *args):
        result = original_git(workspace, *args)
        if workspace == first and args[0] == "fetch":
            observed.set()
            assert release.wait(30), "second integration did not release the first observer"
        return result

    monkeypatch.setattr(workers, "git", gated_git)

    def integrate(workspace):
        return workers._integrate(
            {
                "workspace": str(workspace),
                "launch_id": workspace.name,
                "remote": "origin",
                "target": "main",
            },
            context,
        )

    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(integrate, first)
        try:
            assert observed.wait(30), "first integration never fetched its target"
            advanced = git(
                remote,
                "commit-tree",
                original_head + "^{tree}",
                "-p",
                original_head,
                "-m",
                "advance",
            )
            git(remote, "update-ref", "refs/heads/main", advanced, original_head)
            second_result = integrate(second)
        finally:
            release.set()
        first_result = pending.result(timeout=30)
    assert first_result["base"] == original_head
    assert second_result["base"] == advanced
    assert first_result["outcome"] == second_result["outcome"] == "integrated"
    assert git(repo, "rev-parse", "refs/remotes/origin/main") == original_head
    for workspace, target in ((first, original_head), (second, advanced)):
        prefix = f"refs/bmad-campaign/{context.campaign_id}/integration/{workspace.name}"
        assert git(repo, "rev-parse", prefix + "/target") == target
        fetch_head = Path(git(workspace, "rev-parse", "--git-path", "FETCH_HEAD"))
        assert not fetch_head.exists()
