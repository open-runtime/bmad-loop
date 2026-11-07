"""Checkpoint evidence observes real progress without staging a live index."""

import hashlib
import json
import os
from types import SimpleNamespace

import pytest
from conftest import git, install_bmad_config

from bmad_loop import campaign_checkpoint as checkpoint
from bmad_loop import verify
from bmad_loop.campaign_model import CampaignError

NATIVE = checkpoint._native


@pytest.fixture
def case(project, tmp_path, monkeypatch):
    install_bmad_config(project)
    repo = project.project
    git(repo, "add", "_bmad")
    git(repo, "commit", "-qm", "configure native source")
    native = repo / "private-plan.md"
    native.write_text("Native source baseline\n")
    monkeypatch.setattr(
        checkpoint,
        "_native",
        lambda workspace, request: {
            "private-plan.md": hashlib.sha256(native.read_bytes()).hexdigest()
        },
    )
    request = {
        "campaign_id": "campaign",
        "unit_id": "unit",
        "attempt": 2,
        "generation": 3,
        "launch_id": "launch",
        "branch": git(repo, "symbolic-ref", "--short", "HEAD"),
        "workspace": str(repo),
        "checkpoint_interval_s": 20,
        "checkpoint_paths": ["src.txt"],
        "checkpoint_base": verify.rev_parse_head(repo),
    }
    directory = tmp_path / "worker"
    directory.mkdir()
    monitor = checkpoint.ProgressMonitor(request, directory, clock=lambda: 100.0)
    return repo, native, request, directory, monitor


def read(case, *, now=100.0):
    _, _, expected, directory, _ = case
    return checkpoint.read(directory, expected, now=now, max_age_s=30.0)


def test_worker_reports_commit_and_dirty_progress_without_staging(case):
    repo, native, request, directory, monitor = case
    first = monitor.observe()
    before_index = git(repo, "diff", "--cached", "--binary")
    (repo / "src.txt").write_text("work in progress\n")
    dirty = monitor.observe()
    assert dirty["dirty"] and dirty["dirty_since"] == 100.0
    assert dirty["head"] == first["head"]
    assert git(repo, "diff", "--cached", "--binary") == before_index
    git(repo, "add", "src.txt")
    git(repo, "commit", "-qm", "checkpoint: incomplete progress; no verification claim")
    committed = monitor.observe()
    assert committed["head"] != first["head"]
    assert committed["committed_paths"] == ["src.txt"]
    assert "incomplete progress" in committed["commit_metadata"]
    assert read(case)["head"] == committed["head"]


def test_monitor_status_never_refreshes_or_locks_live_index(case):
    repo, _, _, _, monitor = case
    before = (repo / ".git" / "index").read_bytes()
    product = repo / "src.txt"
    timestamp = product.stat().st_mtime_ns + 2_000_000_000
    os.utime(product, ns=(timestamp, timestamp))
    assert monitor.observe()["state"] == "observed"
    assert (repo / ".git" / "index").read_bytes() == before


def test_dirty_observation_disables_external_diff_and_text_conversion(case, monkeypatch):
    _, _, _, _, monitor = case
    original = verify.git_bytes
    seen = []

    def checked(repo, *args, **kwargs):
        if "diff" in args and "--binary" in args:
            assert "--no-ext-diff" in args
            assert "--no-textconv" in args
            seen.append(args)
        return original(repo, *args, **kwargs)

    monkeypatch.setattr(verify, "git_bytes", checked)
    assert monitor.observe()["state"] == "observed"
    assert len(seen) == 2


def test_private_ignored_source_change_is_visible_even_with_clean_tracked_tree(case):
    repo, native, request, directory, monitor = case
    (repo / ".git" / "info" / "exclude").write_text("private-plan.md\n")
    first = monitor.observe()
    assert not first["dirty"]
    native.write_text("Ignored source changed without a tracked mirror\n")
    changed = monitor.observe()
    assert not changed["dirty"]
    assert changed["private_source_changed"]
    assert changed["dirty_since"] == 100.0


def test_receipt_rejects_foreign_generation_stale_observation_and_unowned_commit(case):
    repo, _, request, directory, monitor = case
    monitor.observe()
    with pytest.raises(CampaignError, match="identity, freshness"):
        checkpoint.read(directory, {**request, "generation": 4}, now=100.0, max_age_s=30.0)
    with pytest.raises(CampaignError, match="identity, freshness"):
        read(case, now=131.0)
    outside = repo / "outside.txt"
    outside.write_text("Unowned change\n")
    git(repo, "add", "outside.txt")
    git(repo, "commit", "-qm", "outside ownership")
    monitor.observe()
    with pytest.raises(CampaignError, match="outside the owned scope"):
        read(case)


def test_git_failure_produces_visible_unavailable_receipt(case, monkeypatch):
    _, _, _, directory, monitor = case
    monkeypatch.setattr(
        checkpoint, "_git", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("read failed"))
    )
    observed = monitor.observe()
    assert observed["state"] == "unavailable"
    assert "read failed" in observed["error"]
    assert "head" not in observed
    with pytest.raises(CampaignError, match="observation is unavailable"):
        read(case)
    assert json.loads((directory / checkpoint.NAME).read_text())["state"] == "unavailable"


def test_head_move_during_observation_is_not_coherent_progress(case, monkeypatch):
    repo, _, _, _, monitor = case
    old = verify.rev_parse_head(repo)
    heads = iter([old, "f" * 40])
    monkeypatch.setattr(verify, "rev_parse_head", lambda _: next(heads))
    row = monitor.observe()
    assert row["state"] == "unavailable"
    assert "changed during observation" in row["error"]


def test_stop_joins_observer_before_final_progress_receipt(case):
    _, _, _, _, monitor = case
    monitor.start()
    monitor.stop()
    assert not monitor.thread.is_alive()
    assert monitor.previous["state"] == "observed"


def test_committed_native_source_and_exact_tracked_mirror_clear_unbacked_debt(case):
    repo, native, request, directory, monitor = case
    git(repo, "add", native.name)
    git(repo, "commit", "-qm", "track selected native source")
    native.write_text("Committed native source progress\n")
    git(repo, "add", native.name)
    git(repo, "commit", "-qm", "checkpoint native source")
    tracked = monitor.observe()
    assert tracked["private_source_changed"]
    assert not tracked["private_source_unbacked"]
    assert tracked["dirty_since"] is None


def test_ignored_native_source_clears_debt_only_after_exact_worker_mirror_commit(case):
    repo, native, request, directory, monitor = case
    (repo / ".git" / "info" / "exclude").write_text("private-plan.md\n")
    native.write_text("Private source needs an exact tracked mirror\n")
    assert monitor.observe()["private_source_unbacked"]
    mirror = repo / "reviewed-source.md"
    mirror.write_bytes(native.read_bytes())
    git(repo, "add", mirror.name)
    git(repo, "commit", "-qm", "worker records exact source mirror")
    assert monitor.observe()["private_source_unbacked"]
    monitor.stop({"artifact_mirrors": {native.name: mirror.name}})
    assert not monitor.previous["private_source_unbacked"]
    assert monitor.previous["dirty_since"] is None


def test_repeated_dirty_edit_of_same_path_updates_content_progress_age(case):
    repo, _, _, _, monitor = case
    (repo / "src.txt").write_text("first edit\n")
    first = monitor.observe()
    monitor.clock = lambda: 120.0
    (repo / "src.txt").write_text("second edit of the same path\n")
    second = monitor.observe()
    assert first["head"] == second["head"]
    assert first["dirty_digest"] != second["dirty_digest"]
    assert second["progress_at"] == 120.0
    assert second["dirty_since"] == 100.0


@pytest.mark.parametrize("history", ["rename", "reverted"])
def test_unowned_rename_source_and_reverted_intermediate_commit_are_refused(case, history):
    repo, _, request, directory, _ = case
    unowned = repo / "unowned.txt"
    unowned.write_text("Preserve this source intent\n")
    git(repo, "add", unowned.name)
    git(repo, "commit", "-qm", "existing unowned file")
    request["checkpoint_paths"] = ["owned.txt"]
    request["checkpoint_base"] = verify.rev_parse_head(repo)
    monitor = checkpoint.ProgressMonitor(request, directory, clock=lambda: 100.0)
    if history == "rename":
        git(repo, "mv", "unowned.txt", "owned.txt")
        git(repo, "commit", "-qm", "move unowned source into own scope")
    else:
        unowned.write_text("Unauthorized intermediate change\n")
        git(repo, "add", unowned.name)
        git(repo, "commit", "-qm", "unowned intermediate change")
        git(repo, "revert", "--no-edit", "HEAD")
    row = monitor.observe()
    assert "unowned.txt" in row["committed_paths"]
    with pytest.raises(CampaignError, match="outside the owned scope"):
        read(case)


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO boundary is POSIX-only")
def test_fifo_progress_is_refused_without_blocking(case):
    _, _, _, directory, _ = case
    os.mkfifo(directory / checkpoint.NAME)
    with pytest.raises((CampaignError, OSError)):
        read(case)


def test_redirected_progress_record_cannot_supply_valid_foreign_bytes(case, tmp_path):
    _, _, _, directory, monitor = case
    monitor.observe()
    path = directory / checkpoint.NAME
    outside = tmp_path / "redirected-progress.json"
    path.replace(outside)
    path.symlink_to(outside)
    with pytest.raises((CampaignError, OSError)):
        read(case)


def test_launch_json_cannot_self_authorize_foreign_attempt(case):
    repo, _, request, directory, _ = case
    runtime = SimpleNamespace(store=SimpleNamespace(directory=lambda _: directory.parent))
    workers = directory.parent / "workers" / "launch"
    workers.mkdir(parents=True)
    (workers / "request.json").write_text(json.dumps({**request, "attempt": 99}))
    unit = SimpleNamespace(
        id="unit",
        claim=SimpleNamespace(launch_id="launch", attempt=2, generation=3),
        evidence={},
        bindings={"workspace": str(repo), "branch": request["branch"]},
    )
    with pytest.raises(CampaignError, match="launch binding differs"):
        checkpoint.request(runtime, SimpleNamespace(id="campaign"), unit, "launch")


def test_planning_monitor_observes_all_effective_ignored_native_sources(case, monkeypatch):
    repo, _, request, directory, _ = case
    monkeypatch.setattr(checkpoint, "_native", NATIVE)
    for name in ("plans-one", "plans-two"):
        folder = repo / name
        folder.mkdir()
        (folder / "requirements.md").write_text("Initial private native requirements\n")
    (repo / ".git" / "info" / "exclude").write_text("plans-one/\nplans-two/\nprivate-plan.md\n")
    request.update(
        source={"kind": "stories", "path": "plans-one"},
        sources=[{"kind": "stories", "path": name} for name in ("plans-one", "plans-two")],
    )
    monitor = checkpoint.ProgressMonitor(request, directory, clock=lambda: 100.0)
    first = monitor.observe()
    assert not first["private_source_changed"]
    (repo / "plans-two" / "requirements.md").write_text("New ignored architecture decision\n")
    second = monitor.observe()
    assert second["private_source_unbacked"]
    assert not second["dirty"]


@pytest.mark.parametrize(
    "mutation", ["none", "actor-added", "main-blob", "h-only-delete", "main-mode"]
)
def test_real_merge_preserves_only_authorized_main_side_changes(case, mutation):
    repo, _, request, directory, _ = case
    git(repo, "checkout", "-b", "canonical-target")
    (repo / "main-only.txt").write_text("Accepted canonical main work\n")
    git(repo, "add", "main-only.txt")
    git(repo, "commit", "-qm", "main independently delivers another scope")
    main = verify.rev_parse_head(repo)
    git(repo, "checkout", request["branch"])
    (repo / "h-only.txt").write_text("Pre-existing candidate intent must survive\n")
    git(repo, "add", "h-only.txt")
    git(repo, "commit", "-qm", "candidate already contains outside intent")
    request["checkpoint_base"] = verify.rev_parse_head(repo)
    request["checkpoint_preserved_bases"] = [main]
    monitor = checkpoint.ProgressMonitor(request, directory, clock=lambda: 100.0)
    (repo / "src.txt").write_text("Owned actor implementation\n")
    git(repo, "add", "src.txt")
    git(repo, "commit", "-qm", "owned implementation")
    git(repo, "merge", "--no-ff", "--no-commit", main)
    if mutation == "main-blob":
        (repo / "main-only.txt").write_text("Actor changed unowned imported work\n")
        git(repo, "add", "main-only.txt")
    elif mutation == "h-only-delete":
        git(repo, "rm", "h-only.txt")
    elif mutation == "main-mode":
        git(repo, "update-index", "--chmod=+x", "main-only.txt")
    git(repo, "commit", "-qm", "retain both independent parents")
    if mutation == "actor-added":
        (repo / "outside-actor.txt").write_text("Unauthorized outside addition\n")
        git(repo, "add", "outside-actor.txt")
        git(repo, "commit", "-qm", "actor added outside scope")
    row = monitor.observe()
    assert row["state"] == "observed"
    assert "main-only.txt" in row["committed_paths"]
    if mutation == "none":
        assert read(case)["head"] == verify.rev_parse_head(repo)
        assert (repo / "h-only.txt").read_text().startswith("Pre-existing")
    else:
        with pytest.raises(CampaignError, match="outside the owned scope or preserved main"):
            read(case)


def test_immutable_path_cache_survives_observation_fault(case, monkeypatch):
    _, _, _, _, monitor = case
    original = checkpoint._committed_paths
    calls = []

    def count(*args):
        calls.append(args[-1])
        return original(*args)

    monkeypatch.setattr(checkpoint, "_committed_paths", count)
    monitor.observe()
    original_dirty = checkpoint._dirty_content
    monkeypatch.setattr(
        checkpoint,
        "_dirty_content",
        lambda *args: (_ for _ in ()).throw(OSError("temporary read failure")),
    )
    assert monitor.observe()["state"] == "unavailable"
    monkeypatch.setattr(checkpoint, "_dirty_content", original_dirty)
    assert monitor.observe()["state"] == "observed"
    assert len(calls) == 1
