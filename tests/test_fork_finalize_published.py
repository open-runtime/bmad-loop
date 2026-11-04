"""Finalization preserves the epic's published ancestry and normal push path."""

from __future__ import annotations

import dataclasses

import pytest
from conftest import committing_crash_state, git
from test_engine import make_engine, resume_engine
from test_fork_finalize_target import _commit, _snapshot, _stage_and_dirty

from bmad_loop import verify
from bmad_loop.journal import load_state
from bmad_loop.model import Phase
from bmad_loop.policy import PluginsPolicy


def _published_epic(project, tmp_path, history):
    repo = project.project
    fork = git(repo, "rev-parse", "HEAD")
    git(repo, "checkout", "-q", "-b", "epic")
    if history == "earlier-stories":
        _commit(repo, "story-one.txt", "earlier unmerged story one\n")
        _commit(repo, "story-two.txt", "earlier unmerged story two\n")
    # Clone the fixture's actual history as a local receiving remote. A normal
    # push to this repo must accept the final branch without force or a lease.
    remote = tmp_path / "receiver.git"
    git(repo, "clone", "--bare", str(repo), str(remote))
    git(repo, "remote", "add", "origin", str(remote))
    git(repo, "fetch", "origin")
    git(repo, "branch", "--set-upstream-to", "origin/epic", "epic")
    baseline = (
        _commit(repo, "checkpoint.txt", "recorded story baseline\n")
        if history == "earlier-stories"
        else fork
    )
    _commit(repo, "dev.txt", "current story development\n")
    if history == "published-attempt":
        git(repo, "push", "origin", "epic")
    else:
        git(repo, "checkout", "-q", "main")
        _commit(repo, "main-one.txt", "published main one\n")
        _commit(repo, "main-two.txt", "published main two\n")
        git(repo, "push", "origin", "main")
        git(repo, "checkout", "-q", "epic")
        git(repo, "merge", "--no-ff", "-m", "sync main into epic", "main")
    _commit(repo, "review.txt", "current story review delta\n")
    return repo, remote, baseline


@pytest.mark.parametrize("history", ["earlier-stories", "published-attempt", "main-merge"])
@pytest.mark.parametrize("bookkeeping", [False, True])
def test_finalize_published_epic_keeps_history_and_plain_push(
    project, tmp_path, history, bookkeeping
):
    """Published epic commits and sync merges retain their exact identities.

    Removing either preservation rule rewrites ancestors or makes the ordinary
    fast-forward push fail, even when the resulting file tree looks correct.
    """
    repo, remote, baseline = _published_epic(project, tmp_path, history)
    head = git(repo, "rev-parse", "HEAD")
    published = git(repo, "rev-parse", "refs/remotes/origin/epic")
    target = git(repo, "rev-parse", "refs/remotes/origin/main")
    ancestry = git(repo, "rev-list", "HEAD").splitlines()
    commit_objects = {sha: git(repo, "cat-file", "commit", sha) for sha in ancestry}
    if bookkeeping:
        (repo / "bookkeeping.txt").write_text("story done\n", encoding="utf-8")
    git(repo, "add", "-A")
    expected_tree = git(repo, "write-tree")
    plans = []

    finalized = verify.finalize_commit(
        repo, baseline, "finalized current story", on_plan=plans.append
    )

    # Exercise the actual receiving-side fast-forward rule, not a mock or an
    # ancestry approximation. The push uses no force option.
    git(repo, "push", "origin", "epic")
    assert finalized == git(repo, "rev-parse", "HEAD")
    assert git(remote, "rev-parse", "refs/heads/epic") == finalized
    assert git(repo, "rev-parse", "HEAD^{tree}") == expected_tree
    final_ancestry = git(repo, "rev-list", "HEAD").splitlines()
    assert all(sha in final_ancestry for sha in ancestry)
    assert published in final_ancestry and target in final_ancestry
    assert {sha: git(repo, "cat-file", "commit", sha) for sha in ancestry} == commit_objects
    if bookkeeping:
        assert git(repo, "rev-parse", "HEAD^") == head
        assert git(repo, "diff", "--name-only", head, "HEAD") == "bookkeeping.txt"
    else:
        assert finalized == head
    assert git(repo, "status", "--porcelain") == ""
    [plan] = plans
    assert plan.baseline == head and not plan.squash
    assert plan.strategy == ("merge-history" if history == "main-merge" else "published-branch")


def test_finalize_diverged_published_branch_refuses_before_mutation(project, tmp_path):
    """A newer conflicting published tip must be synchronized before finalizing.

    Ablation: remove the published-tip containment refusal; this raises no
    GitError and may make a story commit on the still-diverged branch.
    """
    repo, _remote, baseline = _published_epic(project, tmp_path, "published-attempt")
    git(repo, "checkout", "-q", "-b", "rival", "origin/epic")
    rival = _commit(repo, "rival.txt", "another published writer\n")
    git(repo, "push", "origin", "HEAD:epic")
    git(repo, "checkout", "-q", "epic")
    assert git(repo, "rev-parse", "origin/epic") == rival
    before = _stage_and_dirty(repo)

    with pytest.raises(verify.GitError, match="(?i)published branch.*not contained"):
        verify.finalize_commit(repo, baseline, "must not finalize")

    assert _snapshot(repo) == before


def test_finalize_published_ref_probe_fault_refuses_before_mutation(project, tmp_path, monkeypatch):
    """A failed listing cannot masquerade as an unpublished branch.

    Ablation: bypass the publication-ref probe; this expected GitError disappears.
    """
    repo, _remote, baseline = _published_epic(project, tmp_path, "published-attempt")
    before = _stage_and_dirty(repo)
    run_git = verify._run_git
    faults = []

    def fail_published_ref(cmd, root, **kwargs):
        if "for-each-ref" in cmd and cmd[-1] == "refs/remotes/origin/epic":
            result = run_git([*cmd, "--unknown-publication-probe-option"], root, **kwargs)
            faults.append(result.returncode)
            return result
        return run_git(cmd, root, **kwargs)

    monkeypatch.setattr(verify, "_run_git", fail_published_ref)
    with pytest.raises(verify.GitError, match="(?i)for-each-ref"):
        verify.finalize_commit(repo, baseline, "must not finalize")

    assert faults and all(code > 1 for code in faults)
    assert _snapshot(repo) == before


def test_finalize_absent_published_ref_allows_first_push(project, tmp_path):
    repo, remote, baseline = _published_epic(project, tmp_path, "published-attempt")
    git(remote, "symbolic-ref", "HEAD", "refs/heads/main")
    git(repo, "push", "origin", ":epic")
    assert git(repo, "for-each-ref", "--format=%(refname)", "refs/remotes/origin/epic") == ""
    tree = git(repo, "rev-parse", "HEAD^{tree}")

    finalized = verify.finalize_commit(repo, baseline, "first publication")

    git(repo, "push", "origin", "epic")
    assert git(remote, "rev-parse", "refs/heads/epic") == finalized
    assert git(repo, "rev-parse", "HEAD^") == baseline
    assert git(repo, "rev-parse", "HEAD^{tree}") == tree


def test_finalize_stale_unit_branch_uses_current_published_branch(project, tmp_path):
    repo, remote, baseline = _published_epic(project, tmp_path, "published-attempt")
    head = git(repo, "rev-parse", "HEAD")

    finalized = verify.finalize_commit(
        repo, baseline, "finalized story", publication_branch="removed-unit"
    )

    git(repo, "push", "origin", "epic")
    assert finalized == git(repo, "rev-parse", "HEAD") == head
    assert git(remote, "rev-parse", "refs/heads/epic") == head


def test_finalize_uses_plugin_publication_remote_instead_of_tracking_upstream(project, tmp_path):
    repo = project.project
    git(repo, "checkout", "-q", "-b", "epic")
    engine, _ = make_engine(project, [])
    engine.policy = dataclasses.replace(
        engine.policy,
        plugins=PluginsPolicy(settings={"push-merge": {"remote": "publishing"}}),
    )
    baseline = committing_crash_state(project, engine)
    _commit(repo, "published.txt", "published attempt with verified bookkeeping\n")
    remote = tmp_path / "receiver.git"
    git(repo, "clone", "--bare", str(repo), str(remote))
    git(repo, "remote", "add", "publishing", str(remote))
    git(repo, "fetch", "publishing")
    # The configured upstream deliberately differs from push-merge's contract.
    git(repo, "remote", "add", "origin", str(remote))
    git(repo, "fetch", "origin")
    git(repo, "update-ref", "refs/remotes/origin/epic", baseline)
    git(repo, "branch", "--set-upstream-to", "origin/main", "epic")
    _commit(repo, "review.txt", "retained unpublished review\n")
    head = git(repo, "rev-parse", "HEAD")

    engine._finalize_commit_phase(engine.state.tasks["1-1-a"])

    final = load_state(engine.run_dir).tasks["1-1-a"]
    assert final.phase == Phase.DONE and final.baseline_commit == baseline
    assert final.commit_sha == git(repo, "rev-parse", "HEAD") == head
    git(repo, "push", "publishing", "epic")
    assert git(remote, "rev-parse", "refs/heads/epic") == head
    [event] = [e for e in engine.journal.entries() if e["kind"] == "finalization-strategy"]
    assert event["strategy"] == "published-branch"
    assert event["baseline"] is None and event["commit"] == head
    assert event["target_branch"] == "refs/remotes/publishing/main"


def test_native_resume_preserves_legacy_orphaned_baseline_after_squash(project):
    """A pre-upgrade squash can have removed the recorded private baseline."""
    repo = project.project
    git(repo, "checkout", "-q", "-b", "epic")
    _commit(repo, "seed.txt", "private historical baseline\n")
    engine, _ = make_engine(project, [])
    baseline = committing_crash_state(project, engine)
    _commit(repo, "dev.txt", "verified development and bookkeeping\n")
    git(repo, "checkout", "-q", "main")
    target = _commit(repo, "shared.txt", "published main update\n")
    git(repo, "update-ref", "refs/remotes/origin/main", target)
    git(repo, "checkout", "-q", "epic")
    git(repo, "merge", "--no-ff", "-m", "sync main", "main")
    # Reproduce 0.13.3's completed squash before its DONE save, independently of
    # the current finalizer that must now preserve this durable COMMITTING state.
    git(repo, "reset", "--soft", target)
    git(repo, "commit", "-q", "-m", "legacy finalized story")
    head = git(repo, "rev-parse", "HEAD")
    tree = git(repo, "rev-parse", "HEAD^{tree}")
    assert git(repo, "merge-base", baseline, head) != baseline

    resumed, adapter = resume_engine(project, engine, [])
    resumed._finish_inflight()

    final = load_state(resumed.run_dir).tasks["1-1-a"]
    assert final.phase == Phase.DONE and final.baseline_commit == baseline
    assert final.commit_sha == git(repo, "rev-parse", "HEAD") == head
    assert git(repo, "rev-parse", "HEAD^") == target
    assert git(repo, "rev-parse", "HEAD^{tree}") == tree
    assert adapter.sessions == []
    [event] = [e for e in resumed.journal.entries() if e["kind"] == "finalization-strategy"]
    assert event["strategy"] == "historical-baseline"
    assert event["baseline"] is None and event["commit"] == head
