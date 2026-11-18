"""A verification worktree can finish only on its frozen detached commit."""

import pytest
from conftest import git

from bmad_loop.campaign_runtime import CampaignRuntime
from bmad_loop.campaign_store import CampaignStore


@pytest.mark.parametrize("group_key", ["queue_group", "landing_group"])
def test_terminal_verifier_releases_only_its_frozen_detached_checkout(project, tmp_path, group_key):
    repo = project.project
    workspace = tmp_path / "checked-candidate"
    head = git(repo, "rev-parse", "HEAD")
    git(repo, "worktree", "add", "--detach", str(workspace), head)
    runtime = CampaignRuntime(repo, CampaignStore(tmp_path / "state"))
    request = {
        "kind": "queue_verify" if group_key == "queue_group" else "verify",
        "workspace": str(workspace),
        "branch": "story",
        "common_dir": str(repo / ".git"),
        group_key: {"head": head},
    }
    terminal = {"state": "finished", "receipt": {"outcome": "verified"}}
    try:
        assert runtime._observe_git_activity(None, request, terminal) is None
        assert terminal["git_activity_proof"]["observation"]["state"] == "quiescent"
        git(workspace, "commit", "--allow-empty", "-m", "unrelated head")
        wait = runtime._observe_git_activity(None, request, terminal)
        assert wait is not None and wait.state == "waiting"
    finally:
        runtime.close_observers()
