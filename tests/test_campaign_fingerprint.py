"""Read-only stages include ignored private source, index and untracked bytes."""

import pytest
from conftest import git

from bmad_loop import platform_util
from bmad_loop.campaign_fingerprint import FingerprintUnavailable, capture
from bmad_loop.campaign_resolution import diagnosis_unchanged


@pytest.fixture(autouse=True)
def supported_candidate_reads(request):
    if (
        not platform_util.HANDLE_ANCHORED_WRITES
        and request.node.name != "test_unsupported_candidate_reads_fail_explicitly"
    ):
        pytest.skip("handle-anchored candidate reads are unavailable on this platform")


def test_unsupported_candidate_reads_fail_explicitly(project, monkeypatch):
    monkeypatch.setattr(platform_util, "HANDLE_ANCHORED_WRITES", False)
    with pytest.raises(FingerprintUnavailable, match="handle-anchored file reads"):
        capture(project.project)


def test_unchanged_candidate_fingerprint_is_stable(project):
    first = capture(project.project)
    second = capture(project.project)
    assert first == second
    assert first.file_count > 0
    assert first.byte_count > 0
    diagnosis_unchanged(first.to_dict(), second.to_dict())


def test_tracked_bytes_change_detected_without_any_head_change(project):
    before = capture(project.project)
    (project.project / "src.txt").write_text("new candidate bytes")
    after = capture(project.project)
    assert before.head == after.head
    assert before.worktree != after.worktree
    with pytest.raises(ValueError, match="changed candidate"):
        diagnosis_unchanged(before.to_dict(), after.to_dict())


def test_index_only_change_is_not_hidden_by_unchanged_worktree(project):
    path = project.project / "src.txt"
    path.write_text("new candidate bytes")
    before = capture(project.project)
    git(project.project, "add", "src.txt")
    after = capture(project.project)
    assert before.head == after.head
    assert before.worktree == after.worktree
    assert before.index != after.index


def test_untracked_file_bytes_names_and_deletion_are_observed(project):
    path = project.project / "untracked.txt"
    path.write_text("first")
    before = capture(project.project)
    path.write_text("second")
    changed = capture(project.project)
    assert changed.head == before.head
    assert changed.untracked != before.untracked
    path.unlink()
    assert capture(project.project).untracked != changed.untracked


def test_explicit_ignored_private_ticket_source_is_observed(project):
    private = project.project / ".bmad-loop" / "campaign-source"
    private.mkdir(parents=True)
    (project.project / ".git" / "info" / "exclude").write_text("/.bmad-loop/campaign-source/\n")
    path = private / "plan.md"
    path.write_text("accepted source")
    before = capture(project.project, (private,))
    without_private_before = capture(project.project)
    path.write_text("changed ignored source")
    after = capture(project.project, (private,))
    without_private_after = capture(project.project)
    assert before.head == after.head
    assert before.index == after.index
    assert before.worktree == after.worktree
    assert before.untracked == after.untracked
    assert before.source != after.source
    assert without_private_before == without_private_after


def test_arbitrary_ignored_dependencies_are_not_scanned(project):
    dependencies = project.project / "node_modules"
    dependencies.mkdir()
    (project.project / ".git" / "info" / "exclude").write_text("/node_modules/\n")
    # A symlink in an ignored dependency must not be traversed or inspected.
    target = dependencies / "link"
    try:
        target.symlink_to(project.project)
    except OSError:
        pytest.skip("test host cannot create symlinks")
    assert capture(project.project).file_count > 0


def test_selected_source_symlink_is_refused_without_reading_target(project, tmp_path):
    private = project.project / "private-source"
    private.mkdir()
    external = tmp_path / "outside"
    external.write_text("must not read outside private source")
    link = private / "plan.md"
    try:
        link.symlink_to(external)
    except OSError:
        pytest.skip("test host cannot create symlinks")
    with pytest.raises(FingerprintUnavailable, match="link-like|redirected"):
        capture(project.project, (private,))
    assert external.read_text() == "must not read outside private source"


def test_selected_source_parent_redirect_and_external_roots_refuse(project, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "plan.md").write_text("private")
    try:
        (project.project / "linked").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("test host cannot create directory symlinks")
    with pytest.raises(FingerprintUnavailable, match="link-like|redirected"):
        capture(project.project, (project.project / "linked" / "plan.md",))
    (project.project / "linked").unlink()
    with pytest.raises(FingerprintUnavailable, match="inside the worker"):
        capture(project.project, (outside,))


def test_exclusion_cannot_hide_product_source(project):
    with pytest.raises(FingerprintUnavailable, match="only known run-output"):
        capture(project.project, exclude_paths=(project.project / "src.txt",))
    with pytest.raises(FingerprintUnavailable, match="only known run-output"):
        capture(project.project, exclude_paths=(project.project,))


def test_owned_run_output_is_excluded_but_private_source_stays_visible(project):
    runs = project.project / ".bmad-loop" / "runs" / "worker"
    runs.mkdir(parents=True)
    (runs / "log.txt").write_text("first")
    before = capture(
        project.project, source_roots=(project.project / ".bmad-loop",), exclude_paths=(runs,)
    )
    (runs / "log.txt").write_text("latest output")
    after = capture(
        project.project, source_roots=(project.project / ".bmad-loop",), exclude_paths=(runs,)
    )
    assert before == after


def test_bounds_and_missing_explicit_source_are_visible_failures(project):
    with pytest.raises(FingerprintUnavailable, match="bound"):
        capture(project.project, max_bytes=1)
    with pytest.raises(FingerprintUnavailable, match="unavailable"):
        capture(project.project, (project.project / "missing-private-source",))


def test_assume_unchanged_flags_cannot_hide_tracked_byte_mutation(project):
    git(project.project, "update-index", "--assume-unchanged", "src.txt")
    before = capture(project.project)
    (project.project / "src.txt").write_text("changed despite hidden Git status")
    after = capture(project.project)
    assert before.head == after.head
    assert before.worktree != after.worktree


def test_deleted_tracked_parent_is_a_content_change_not_unknown_observation(project):
    import shutil

    directory = project.project / "tracked-package"
    directory.mkdir()
    (directory / "source.txt").write_text("tracked candidate")
    git(project.project, "add", "tracked-package")
    git(project.project, "commit", "-qm", "candidate package")
    before = capture(project.project)
    shutil.rmtree(directory)
    after = capture(project.project)
    assert before.head == after.head
    assert before.worktree != after.worktree
