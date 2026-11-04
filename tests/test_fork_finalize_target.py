"""Finalization must preserve the history already published to its target."""

from __future__ import annotations

import dataclasses

import pytest
from conftest import committing_crash_state, git
from test_engine import make_engine, resume_engine
from test_plugin_loader import write_plugin

from bmad_loop import verify
from bmad_loop.journal import load_state
from bmad_loop.model import Phase
from bmad_loop.policy import PluginsPolicy, ReviewPolicy


def _commit(repo, name, content):
    (repo / name).write_text(content, encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", content.strip())
    return git(repo, "rev-parse", "HEAD")


def _history(project, published):
    repo = project.project
    baseline = git(repo, "rev-parse", "HEAD")
    git(repo, "checkout", "-q", "-b", "epic")
    _commit(repo, "shared.txt", "published first\n")
    prefix = _commit(repo, "shared.txt", "published second\n")
    if published != "all":
        _commit(repo, "src.txt", "private dev\n")
        _commit(repo, "src.txt", "private review\n")
    head = git(repo, "rev-parse", "HEAD")
    target = baseline if published == "none" else prefix
    git(repo, "update-ref", "refs/remotes/origin/main", target)
    return repo, baseline, prefix, head


@pytest.mark.parametrize("published", ["all", "partial", "none"])
def test_finalize_preserves_target_reachable_history(project, published):
    """Removing target adoption rewrites the all/partial published prefixes."""
    repo, baseline, prefix, head = _history(project, published)
    original_tree = git(repo, "rev-parse", "HEAD^{tree}")
    prefix_object = git(repo, "cat-file", "commit", prefix)

    result = verify.finalize_commit(repo, baseline, "finalized story")

    final = git(repo, "rev-parse", "HEAD")
    assert result == final
    assert git(repo, "rev-parse", "HEAD^{tree}") == original_tree
    assert git(repo, "status", "--porcelain") == ""
    assert git(repo, "symbolic-ref", "--short", "HEAD") == "epic"
    if published == "all":
        assert final == head
        assert git(repo, "rev-list", f"{prefix}..HEAD") == ""
    else:
        expected_parent = prefix if published == "partial" else baseline
        assert git(repo, "rev-parse", "HEAD^") == expected_parent
        assert git(repo, "log", "--format=%s", f"{expected_parent}..HEAD") == "finalized story"
    if published != "none":
        assert prefix in git(repo, "rev-list", "HEAD").splitlines()
        assert git(repo, "cat-file", "commit", prefix) == prefix_object


def test_finalize_published_head_retains_uncommitted_delta(project):
    repo, baseline, _prefix, head = _history(project, "all")
    (repo / "bookkeeping.txt").write_text("done\n", encoding="utf-8")

    result = verify.finalize_commit(repo, baseline, "finalized bookkeeping")

    assert result == git(repo, "rev-parse", "HEAD")
    assert git(repo, "rev-parse", "HEAD^") == head
    assert (
        git(repo, "diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD") == "bookkeeping.txt"
    )
    assert git(repo, "show", "HEAD:bookkeeping.txt") == "done"


def test_finalize_missing_target_with_remote_refuses_before_mutation(project):
    """A configured remote without its target ref is uncertainty, not offline use.

    Ablation: bypass the missing-ref refusal; the expected GitError disappears
    and the old finalizer rewrites the committed chain.
    """
    repo, baseline, _prefix, head = _history(project, "partial")
    git(repo, "update-ref", "-d", "refs/remotes/origin/main")
    git(repo, "remote", "add", "origin", str(repo))
    (repo / "src.txt").write_text("unstaged local edit\n", encoding="utf-8")
    index = git(repo, "write-tree")
    status = git(repo, "status", "--porcelain")

    with pytest.raises(verify.GitError, match="(?i)(target|ref)"):
        verify.finalize_commit(repo, baseline, "must not finalize")

    assert git(repo, "rev-parse", "HEAD") == head
    assert git(repo, "write-tree") == index
    assert git(repo, "status", "--porcelain") == status
    assert (repo / "src.txt").read_text(encoding="utf-8") == "unstaged local edit\n"


def test_finalize_without_remote_or_target_preserves_offline_behavior(project):
    repo, baseline, _prefix, _head = _history(project, "none")
    git(repo, "update-ref", "-d", "refs/remotes/origin/main")

    result = verify.finalize_commit(repo, baseline, "offline story")

    assert result == git(repo, "rev-parse", "HEAD")
    assert git(repo, "rev-parse", "HEAD^") == baseline
    assert git(repo, "log", "--format=%s", f"{baseline}..HEAD") == "offline story"


def test_finalize_explicit_target_reports_adopted_baseline(project):
    repo, baseline, prefix, _head = _history(project, "partial")
    git(repo, "update-ref", "refs/remotes/origin/main", baseline)
    git(repo, "update-ref", "refs/remotes/upstream/release", prefix)
    adopted = []

    result = verify.finalize_commit(
        repo,
        baseline,
        "release story",
        target_ref="refs/remotes/upstream/release",
        on_baseline_adopted=lambda old, new: adopted.append((old, new)),
    )

    assert result == git(repo, "rev-parse", "HEAD")
    assert git(repo, "rev-parse", "HEAD^") == prefix
    assert adopted == [(baseline, prefix)]


@pytest.mark.parametrize("private_baseline", [False, True])
def test_finalize_preserves_synced_target_sidearm(project, private_baseline):
    """Syncing main into the story must not squash the published merge parent.

    A private story baseline can itself be incomparable with the target tip;
    its bytes remain in the squash while the target's ancestry stays intact.
    """
    repo = project.project
    fork = git(repo, "rev-parse", "HEAD")
    git(repo, "checkout", "-q", "-b", "epic")
    baseline = _commit(repo, "seed.txt", "private baseline\n") if private_baseline else fork
    _commit(repo, "dev.txt", "private development\n")
    git(repo, "checkout", "-q", "main")
    first_published = _commit(repo, "shared.txt", "published main first\n")
    target = _commit(repo, "shared.txt", "published main second\n")
    git(repo, "update-ref", "refs/remotes/origin/main", target)
    git(repo, "checkout", "-q", "epic")
    git(repo, "merge", "--no-ff", "-m", "sync main", "main")
    _commit(repo, "review.txt", "private review\n")
    tree = git(repo, "rev-parse", "HEAD^{tree}")
    published_objects = [git(repo, "cat-file", "commit", sha) for sha in (first_published, target)]

    result = verify.finalize_commit(repo, baseline, "unpublished story delta")

    assert result == git(repo, "rev-parse", "HEAD")
    assert git(repo, "rev-parse", "HEAD^") == target
    assert git(repo, "rev-parse", "HEAD^{tree}") == tree
    assert git(repo, "rev-list", "--count", f"{target}..HEAD") == "1"
    assert [
        git(repo, "cat-file", "commit", sha) for sha in (first_published, target)
    ] == published_objects
    expected = {"dev.txt", "review.txt"} | ({"seed.txt"} if private_baseline else set())
    assert set(git(repo, "diff", "--name-only", target, "HEAD").splitlines()) == expected


def _snapshot(repo):
    return (
        git(repo, "rev-parse", "HEAD"),
        git(repo, "write-tree"),
        git(repo, "status", "--porcelain"),
        (repo / "src.txt").read_bytes(),
        (repo / "loose.txt").read_bytes(),
    )


def _stage_and_dirty(repo):
    (repo / "src.txt").write_text("staged local edit\n", encoding="utf-8")
    git(repo, "add", "src.txt")
    (repo / "src.txt").write_text("unstaged local edit\n", encoding="utf-8")
    (repo / "loose.txt").write_text("untracked local edit\n", encoding="utf-8")
    return _snapshot(repo)


@pytest.mark.parametrize("probe", ["--is-ancestor", "--all"])
def test_finalize_graph_probe_error_refuses_before_mutation(project, monkeypatch, probe):
    """An actual fatal Git response cannot be interpreted as no shared history.

    Ablation: skip the graph guard; finalization rewrites HEAD/index instead of
    surfacing the injected Git failure before any write.
    """
    repo, baseline, _prefix, _head = _history(project, "partial")
    before = _stage_and_dirty(repo)
    run_git = verify._run_git
    failures = []

    def fail_graph(cmd, root, **kwargs):
        if "merge-base" in cmd and probe in cmd:
            result = run_git([*cmd[:-1], "missing-history-object"], root, **kwargs)
            failures.append(result.returncode)
            return result
        return run_git(cmd, root, **kwargs)

    monkeypatch.setattr(verify, "_run_git", fail_graph)
    with pytest.raises(verify.GitError, match="(?i)ancestry"):
        verify.finalize_commit(repo, baseline, "must not finalize")

    assert failures and all(rc > 1 for rc in failures)
    assert _snapshot(repo) == before


def test_finalize_multiple_common_bases_refuses_before_mutation(project):
    """Crisscross histories require reconciliation, not an arbitrary squash base.

    Ablation: bypass the finalization graph guard; the expected refusal goes
    missing and the branch/index are rewritten.
    """
    repo = project.project
    baseline = git(repo, "rev-parse", "HEAD")
    left = _commit(repo, "left.txt", "left history\n")
    git(repo, "checkout", "-q", "-b", "right", baseline)
    right = _commit(repo, "right.txt", "right history\n")
    tree = git(repo, "rev-parse", "HEAD^{tree}")
    first = git(repo, "commit-tree", tree, "-p", left, "-p", right, "-m", "left merge")
    second = git(repo, "commit-tree", tree, "-p", right, "-p", left, "-m", "right merge")
    git(repo, "reset", "--hard", first)
    git(repo, "update-ref", "refs/remotes/origin/main", second)
    assert set(git(repo, "merge-base", "--all", first, second).splitlines()) == {left, right}
    before = _stage_and_dirty(repo)

    with pytest.raises(verify.GitError, match="(?i)ambiguous"):
        verify.finalize_commit(repo, baseline, "must not finalize")

    assert _snapshot(repo) == before


def test_finalize_target_uses_loaded_plugin_defaults(project):
    write_plugin(
        project.project,
        "push-merge",
        """[plugin]
name = "push-merge"
api_version = 1
[[settings]]
key = "remote"
type = "str"
default = "upstream"
[[settings]]
key = "target_branch"
type = "str"
default = "release"
""",
    )
    engine, _ = make_engine(project, [])
    loaded = engine._registry.get("push-merge")
    assert loaded is not None and loaded.active
    assert loaded.settings == {"remote": "upstream", "target_branch": "release"}
    baseline = committing_crash_state(project, engine)
    git(project.project, "add", "-A")
    git(project.project, "commit", "-q", "-m", "published with custom plugin defaults")
    published = git(project.project, "rev-parse", "HEAD")
    git(project.project, "update-ref", "refs/remotes/origin/main", baseline)
    git(project.project, "update-ref", "refs/remotes/upstream/release", published)

    engine._finalize_commit_phase(engine.state.tasks["1-1-a"])

    final = load_state(engine.run_dir).tasks["1-1-a"]
    assert final.phase == Phase.DONE
    assert final.commit_sha == published
    assert final.baseline_commit == baseline
    assert git(project.project, "rev-parse", "HEAD") == published


@pytest.mark.parametrize("resume", ["reverify", "committing"])
@pytest.mark.parametrize("target", ["main", "release"])
def test_native_resume_preserves_published_history_and_journals_adoption(project, resume, target):
    """Both real resume arms reach the same guarded finalization boundary."""
    engine, _ = make_engine(project, [])
    engine.policy = dataclasses.replace(engine.policy, review=ReviewPolicy(enabled=False))
    if target != "main":
        engine.policy = dataclasses.replace(
            engine.policy,
            plugins=PluginsPolicy(settings={"push-merge": {"target_branch": target}}),
        )
    baseline = committing_crash_state(project, engine)
    task = engine.state.tasks["1-1-a"]
    # Publish the complete verified product, including spec/board bookkeeping.
    git(project.project, "add", "-A")
    git(project.project, "commit", "-q", "-m", "already published product")
    published = git(project.project, "rev-parse", "HEAD")
    target_ref = f"refs/remotes/origin/{target}"
    git(project.project, "update-ref", target_ref, published)
    if target != "main":
        # A hardcoded origin/main would rewrite this published product.
        git(project.project, "update-ref", "refs/remotes/origin/main", baseline)
    if resume == "reverify":
        task.phase = Phase.DEV_VERIFY
        task.reverify_from = "deferred"
    engine._save()

    resumed, adapter = resume_engine(project, engine, [])
    resumed._finish_inflight()

    final = load_state(resumed.run_dir).tasks["1-1-a"]
    assert final.phase == Phase.DONE
    assert final.commit_sha == published
    assert final.baseline_commit == baseline
    assert git(project.project, "rev-parse", "HEAD") == published
    assert adapter.sessions == []
    events = [
        row for row in resumed.journal.entries() if row["kind"] == "baseline-adopted-from-target"
    ]
    assert len(events) == 1
    assert events[0]["old_baseline"] == baseline
    assert events[0]["baseline"] == published
    assert events[0]["target_branch"] == target_ref
    if resume == "reverify":
        decisions = [row for row in resumed.journal.entries() if row["kind"] == "reverify-decision"]
        assert [row["action"] for row in decisions] == ["proceed"]
