"""Git worktree metadata is serial; uncertain allocation preserves exact intent."""

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from conftest import git
from test_campaign_queue import group as group
from test_campaign_runtime import production_case as production_case

from bmad_loop import campaign_workers as workers
from bmad_loop import verify
from bmad_loop.campaign_model import CampaignError


def allocate(project, tmp_path, *, name="one", base=None):
    return workers.allocate_worktree(
        project.project,
        tmp_path / "workers",
        branch="campaign/" + name,
        unit_id=name,
        base=base or verify.rev_parse_head(project.project),
        allocation_dir=tmp_path / "claims" / name,
    )


def fail_after_branch(monkeypatch):
    original = workers.git
    failed = []

    def git_once(repo, *args):
        if args[:3] == ("worktree", "add", "-b") and not failed:
            failed.append(args[3])
            original(repo, "branch", args[3], args[-1])
            raise CampaignError("injected Git registration failure after branch creation")
        return original(repo, *args)

    monkeypatch.setattr(workers, "git", git_once)
    return failed


def test_partial_branch_creation_recovers_same_baseline_without_deleting_or_resetting(
    project, tmp_path, monkeypatch
):
    base = verify.rev_parse_head(project.project)
    failed = fail_after_branch(monkeypatch)
    with pytest.raises(CampaignError, match="after branch creation"):
        allocate(project, tmp_path)
    assert failed == ["campaign/one"]
    assert git(project.project, "rev-parse", "campaign/one") == base
    path = allocate(project, tmp_path)
    assert verify.rev_parse_head(path) == base
    assert git(path, "branch", "--show-current") == "campaign/one"
    assert (
        len(
            [
                row
                for row in git(project.project, "worktree", "list", "--porcelain").splitlines()
                if row == "branch refs/heads/campaign/one"
            ]
        )
        == 1
    )


def test_completed_git_add_lost_ack_reuses_exact_registered_workspace(
    project, tmp_path, monkeypatch
):
    original = workers.git
    additions = []

    def lose_once(repo, *args):
        result = original(repo, *args)
        if args[:2] == ("worktree", "add"):
            additions.append(args)
            if len(additions) == 1:
                raise CampaignError("injected lost worktree acknowledgement")
        return result

    monkeypatch.setattr(workers, "git", lose_once)
    with pytest.raises(CampaignError, match="lost worktree acknowledgement"):
        allocate(project, tmp_path)
    path = allocate(project, tmp_path)
    assert path.is_dir() and len(additions) == 1
    assert verify.rev_parse_head(path) == verify.rev_parse_head(project.project)


@pytest.mark.parametrize("mutation", ["unrecorded", "moved", "bound-elsewhere"])
def test_unknown_or_modified_branch_is_preserved_and_never_adopted(
    project, tmp_path, monkeypatch, mutation
):
    base = verify.rev_parse_head(project.project)
    if mutation == "unrecorded":
        git(project.project, "branch", "campaign/one", base)
    else:
        fail_after_branch(monkeypatch)
        with pytest.raises(CampaignError):
            allocate(project, tmp_path, base=base)
        if mutation == "moved":
            (project.project / "src.txt").write_text("operator checkpoint\n", encoding="utf-8")
            git(project.project, "add", "src.txt")
            git(project.project, "commit", "-qm", "operator branch work")
            git(
                project.project,
                "update-ref",
                "refs/heads/campaign/one",
                verify.rev_parse_head(project.project),
                base,
            )
        else:
            git(project.project, "worktree", "add", str(tmp_path / "operator"), "campaign/one")
    before = git(project.project, "rev-parse", "campaign/one")
    with pytest.raises(
        CampaignError, match="unrecorded existing|orphan branch changed|another worktree"
    ):
        allocate(project, tmp_path, base=base)
    assert git(project.project, "rev-parse", "campaign/one") == before
    assert not (tmp_path / "workers" / "one").exists()


def test_parallel_allocations_serialize_only_git_admin_metadata(project, tmp_path, monkeypatch):
    original = workers.git
    state = {"active": 0, "maximum": 0}
    mutex = threading.Lock()

    def observed(repo, *args):
        if args[:2] != ("worktree", "add"):
            return original(repo, *args)
        with mutex:
            state["active"] += 1
            state["maximum"] = max(state["active"], state["maximum"])
        try:
            threading.Event().wait(0.03)
            return original(repo, *args)
        finally:
            with mutex:
                state["active"] -= 1

    monkeypatch.setattr(workers, "git", observed)
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(allocate, project, tmp_path, name=name) for name in ("one", "two")
        ]
        paths = [future.result() for future in futures]
    assert state["maximum"] == 1
    assert len(set(paths)) == 2
    assert all(
        verify.rev_parse_head(path) == verify.rev_parse_head(project.project) for path in paths
    )


def test_prepare_unit_retry_keeps_original_base_after_main_advances(production_case):
    from bmad_loop.campaign_model import OutboxAction

    case = production_case
    unit = case.snapshot.units["source:project:first"]
    action = OutboxAction("same-allocation", "prepare-unit", unit.id, {}, case.snapshot.generation)
    first = case.runtime._perform_prepare_unit(case.snapshot, action)
    assert first.state == "confirmed"
    (case.repo / "unrelated-main-update.txt").write_text("new canonical work\n", encoding="utf-8")
    git(case.repo, "add", "unrelated-main-update.txt")
    git(case.repo, "commit", "-qm", "main advanced during lost allocation acknowledgement")
    git(case.repo, "push", "origin", "main")
    second = case.runtime._perform_prepare_unit(case.snapshot, action)
    assert second.state == "confirmed"
    assert second.data["workspace"] == first.data["workspace"]
    assert second.data["base"] == first.data["base"]
    assert second.data["head"] == first.data["head"]
    assert verify.rev_parse_head(case.repo) != second.data["base"]


def test_queue_checkout_and_normal_allocation_use_same_short_lock_and_fetch_outside_it(
    project, tmp_path, group, monkeypatch
):
    from contextlib import contextmanager

    from bmad_loop import campaign_queue

    _, queued = group
    original_lock = workers.worktree_metadata_lock
    original_git = verify.git_bytes
    local = threading.local()
    calls = []
    state = {"active": 0, "maximum": 0}
    mutex = threading.Lock()

    @contextmanager
    def observed_lock(repo):
        with original_lock(repo) as common:
            local.held = True
            try:
                yield common
            finally:
                local.held = False

    def observed_git(repo, *args, **kwargs):
        if args and args[0] == "fetch":
            calls.append(("fetch", getattr(local, "held", False)))
        if args[:2] != ("worktree", "add"):
            return original_git(repo, *args, **kwargs)
        calls.append(("add", getattr(local, "held", False)))
        with mutex:
            state["active"] += 1
            state["maximum"] = max(state["maximum"], state["active"])
        try:
            threading.Event().wait(0.03)
            return original_git(repo, *args, **kwargs)
        finally:
            with mutex:
                state["active"] -= 1

    monkeypatch.setattr(workers, "worktree_metadata_lock", observed_lock)
    monkeypatch.setattr(verify, "git_bytes", observed_git)
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(allocate, project, tmp_path, name="ordinary")
        second = executor.submit(
            campaign_queue.allocate_checkout,
            project.project,
            tmp_path / "queue-views",
            tmp_path / "queue-claims",
            queued,
            remote="queue-test",
        )
        paths = first.result(), second.result()
    assert state["maximum"] == 1
    assert all(held for kind, held in calls if kind == "add")
    assert all(not held for kind, held in calls if kind == "fetch")
    assert len(set(paths)) == 2
