"""Tracking debt becomes a real branch/commit and waits for actual main delivery."""

import json
import os
import subprocess
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import git
from test_campaign_runtime import diagnose
from test_campaign_runtime import production_case as production_case
from test_campaign_runtime import run_case
from test_campaign_source_publication import built, configure, reviewed

from bmad_loop import campaign_closure, verify
from bmad_loop.campaign_model import (
    CampaignError,
    CampaignSnapshot,
    CampaignSpec,
    CampaignUnit,
    UnitState,
)
from bmad_loop.campaign_runtime import CampaignRuntime
from bmad_loop.campaign_runtime_tracking import (
    admit,
    confirm_tracking,
    pending_tracking,
    prepare,
    run_tracking,
)
from bmad_loop.campaign_source_publication import ArtifactChange, SourcePublicationResult
from bmad_loop.campaign_store import CampaignStore, add_action
from bmad_loop.journal import Journal


@pytest.fixture
def tracking(project_v7, tmp_path):
    publisher, source, spec, plan = configure(project_v7, tmp_path)
    binding = publisher.capture(source, spec)
    artifact = built(binding)
    merge, acceptance = reviewed(publisher, artifact)
    original = CampaignUnit(spec, state=UnitState.MERGED_PENDING_SYNC)
    snapshot = CampaignSnapshot(
        "campaign",
        "owner/repo",
        CampaignSpec("tracking", remote="source-code", sources=(source,), units=(spec,)),
        units={original.id: original},
        generation=1,
    )
    debt = publisher.publish(
        "original",
        source,
        spec,
        binding,
        merged_commit=merge["merged_commit"],
        journal=Journal(tmp_path / "journal"),
    )
    descriptor = prepare(
        snapshot,
        original,
        debt,
        state_dir=tmp_path / "authority",
        publisher_project=publisher.project,
        source=source,
        binding=binding,
        artifact=artifact,
        merge=merge,
        acceptance=acceptance,
    )
    unit = admit(snapshot, descriptor)
    workspace = tmp_path / "tracking-checkout"
    git(publisher.project, "worktree", "add", "-b", "tracking-source", str(workspace), "HEAD")
    product = workspace / "docs" / "bmad-campaign" / "tracking.md"
    product.write_text("Tracking draft PR specification\n", encoding="utf-8")
    git(workspace, "add", str(product))
    git(workspace, "commit", "-m", "docs: tracking draft specification")
    envelope = SimpleNamespace(
        campaign_id=snapshot.id, unit_id=unit.id, config_revision=1, directory=tmp_path / "worker"
    )
    request = {"tracking": descriptor, "workspace": str(workspace)}
    return snapshot, original, unit, publisher, binding, source, plan, envelope, request


def run(case, tmp_path):
    return run_tracking(case[-1], case[-2], journal=Journal(tmp_path / "child-journal"))


def test_tracking_control_deduplicates_without_finishing_original(tracking):
    snapshot, original, unit, _, _, _, _, _, request = tracking
    assert admit(snapshot, json.loads(json.dumps(request["tracking"]))) is unit
    assert pending_tracking(snapshot, original) == (unit.id,)
    assert original.state == UnitState.MERGED_PENDING_SYNC
    assert unit.state == UnitState.PENDING and unit.spec.kind == "tracking"
    assert unit.spec.parent == original.id and original.id not in unit.spec.after


def test_retrospective_tracking_debt_creates_a_real_control_without_finishing_epic(
    tracking, tmp_path, monkeypatch
):
    snapshot, original, prior, publisher, binding, source, plan, _, _ = tracking
    snapshot.units.pop(prior.id)
    original.bindings["tracking_units"] = []
    original.spec = replace(original.spec, kind="closure")
    original.bindings.update(
        canonical_source=binding.to_dict(), workspace=str(publisher.project), pr={"number": 42}
    )
    original.evidence.update(
        closure_manifest={"captured": True},
        merge={"merge_commit": "merged"},
        integrated={"head": "reviewed"},
        review={"launch_id": "review-1"},
    )
    path = plan.parent / "retrospective.md"
    before = path.read_bytes() if path.exists() else None
    debt = SourcePublicationResult(
        "tracking_pr_required",
        "current-main",
        proposal={
            "kind": "planning-tracking",
            "artifacts": [{"path": path.relative_to(publisher.project).as_posix()}],
        },
    )
    change = ArtifactChange(str(path), b"# Reviewed retrospective\n", None, "docs/retrospective.md")
    monkeypatch.setattr(campaign_closure, "publish_retrospective", lambda *args, **kwargs: debt)
    monkeypatch.setattr(campaign_closure, "retrospective_change", lambda unit: change)
    runtime = CampaignRuntime(publisher.project, CampaignStore(tmp_path / "runtime-authority"))
    monkeypatch.setattr(runtime, "source", lambda *args: source)
    monkeypatch.setattr(runtime, "_source_publisher", lambda *args, **kwargs: publisher)
    action = add_action(snapshot, "sync-tracking", original.id, {})
    result = runtime._perform_sync_tracking(snapshot, action)
    assert result.state == "confirmed"
    descriptor = result.data["tracking_control"]
    control = admit(snapshot, descriptor)
    assert control.spec.kind == "tracking" and control.spec.parent == original.id
    assert original.state == UnitState.MERGED_PENDING_SYNC
    request = json.loads(Path(descriptor["request"]).read_text())
    assert request["changes"][0]["path"] == str(path)
    assert (path.read_bytes() if path.exists() else None) == before


def test_deterministic_tracking_commit_is_reviewable_and_does_not_edit_canonical(
    tracking, tmp_path
):
    _, original, _, publisher, _, _, plan, _, request = tracking
    before, parent = plan.read_bytes(), verify.rev_parse_head(Path(request["workspace"]))
    receipt = run(tracking, tmp_path)
    assert receipt["outcome"] == "tracking_complete"
    assert verify.is_ancestor(Path(request["workspace"]), parent, receipt["head"])
    assert plan.read_bytes() == before and original.state == UnitState.MERGED_PENDING_SYNC
    changed = verify.git_bytes(
        Path(request["workspace"]),
        "diff-tree",
        "--no-commit-id",
        "--name-only",
        "-r",
        receipt["head"],
    )
    assert changed.stdout.decode().strip() == plan.relative_to(publisher.project).as_posix()
    assert (
        b"status: done"
        in (Path(request["workspace"]) / plan.relative_to(publisher.project)).read_bytes()
    )


def test_tracking_confirmation_needs_actual_remote_main_merge(tracking, tmp_path):
    snapshot, original, unit, publisher, binding, source, _, _, request = tracking
    receipt = run(tracking, tmp_path)
    unit.state = UnitState.MERGED_PENDING_SYNC
    unit.evidence = {
        "integrated": {"head": receipt["head"]},
        "merge": {"merge_commit": receipt["head"]},
    }
    with pytest.raises(Exception, match="reachable"):
        confirm_tracking(snapshot, unit, state_dir=tmp_path / "confirmation")
    git(Path(request["workspace"]), "push", "source-code", "HEAD:refs/heads/main")
    confirmed = confirm_tracking(snapshot, unit, state_dir=tmp_path / "confirmation")
    assert confirmed["state"] == "confirmed" and original.state == UnitState.MERGED_PENDING_SYNC
    assert (
        publisher.publish(
            "original-retry",
            source,
            original.spec,
            binding,
            merged_commit=read_original_merge(request),
            journal=Journal(tmp_path / "journal"),
        ).state
        == "confirmed"
    )
    assert pending_tracking(snapshot, original) == (unit.id,)
    unit.state = UnitState.DELIVERED
    assert pending_tracking(snapshot, original) == ()


def read_original_merge(request):
    return json.loads(Path(request["tracking"]["request"]).read_text())["merge"]["merged_commit"]


def test_crash_after_staging_keeps_work_and_resumes_same_source_transaction(
    tracking, tmp_path, monkeypatch
):
    original_commit = verify.commit_paths
    monkeypatch.setattr(
        verify,
        "commit_paths",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("crash before commit")),
    )
    with pytest.raises(OSError, match="crash"):
        run(tracking, tmp_path)
    monkeypatch.setattr(verify, "commit_paths", original_commit)
    receipt = run(tracking, tmp_path)
    assert receipt["outcome"] == "tracking_complete"
    assert verify.worktree_clean(Path(tracking[-1]["workspace"]))


def test_crash_after_commit_adopts_exact_action_commit_without_recommit(
    tracking, tmp_path, monkeypatch
):
    original_commit = verify.commit_paths

    def crash(*args, **kwargs):
        original_commit(*args, **kwargs)
        raise OSError("lost commit reply")

    monkeypatch.setattr(verify, "commit_paths", crash)
    with pytest.raises(OSError, match="lost commit"):
        run(tracking, tmp_path)
    head = verify.rev_parse_head(Path(tracking[-1]["workspace"]))
    monkeypatch.setattr(verify, "commit_paths", original_commit)
    assert run(tracking, tmp_path)["head"] == head


def test_tracking_recovery_preserves_foreign_dirty_work(tracking, tmp_path, monkeypatch):
    original_commit = verify.commit_paths
    monkeypatch.setattr(
        verify, "commit_paths", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("crash"))
    )
    with pytest.raises(OSError):
        run(tracking, tmp_path)
    monkeypatch.setattr(verify, "commit_paths", original_commit)
    foreign = Path(tracking[-1]["workspace"]) / "owner.txt"
    foreign.write_text("Owner draft", encoding="utf-8")
    head = verify.rev_parse_head(foreign.parent)
    with pytest.raises(CampaignError):
        run(tracking, tmp_path)
    assert foreign.read_text() == "Owner draft" and verify.rev_parse_head(foreign.parent) == head


def test_tracking_request_digest_is_not_replaceable_by_worker(tracking, tmp_path):
    descriptor = tracking[-1]["tracking"]
    path = Path(descriptor["request"])
    data = json.loads(path.read_text())
    data["acceptance"]["review_id"] = "fake review"
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(CampaignError, match="digest changed"):
        run(tracking, tmp_path)


def test_no_recursive_tracking_control_debt(tracking, tmp_path):
    snapshot, _, unit, publisher, binding, source, _, _, request = tracking
    payload = json.loads(Path(request["tracking"]["request"]).read_text())
    unit.state = UnitState.MERGED_PENDING_SYNC
    with pytest.raises(CampaignError, match="recursively"):
        prepare(
            snapshot,
            unit,
            {"state": "tracking_pr_required"},
            state_dir=tmp_path / "other",
            publisher_project=publisher.project,
            source=source,
            binding=binding,
            merge=payload["merge"],
            acceptance=payload["acceptance"],
        )


def test_tracking_preserves_foreign_staged_version_of_its_source(tracking, tmp_path, monkeypatch):
    original_commit = verify.commit_paths
    monkeypatch.setattr(
        verify, "commit_paths", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("crash"))
    )
    with pytest.raises(OSError):
        run(tracking, tmp_path)
    monkeypatch.setattr(verify, "commit_paths", original_commit)
    _, _, _, publisher, _, _, plan, _, request = tracking
    workspace = Path(request["workspace"])
    relative = plan.relative_to(publisher.project)
    path = workspace / relative
    accepted = path.read_bytes()
    path.write_bytes(accepted + b"\nOwner staged change\n")
    git(workspace, "add", relative.as_posix())
    path.write_bytes(accepted)
    staged = verify.git_bytes(workspace, "show", ":" + relative.as_posix()).stdout
    with pytest.raises(CampaignError, match="staged source"):
        run(tracking, tmp_path)
    assert verify.git_bytes(workspace, "show", ":" + relative.as_posix()).stdout == staged


def test_planning_tracking_uses_reviewed_manifest_and_native_plan_writer(tracking, tmp_path):
    from bmad_loop.campaign_model import UnitSpec
    from bmad_loop.campaign_source_publication import ArtifactChange

    _, _, _, publisher, _, source, plan, _, _ = tracking
    before = plan.read_bytes()
    artifact = before.replace(b"ready-for-dev", b"draft") + b"\nAccepted new planning detail\n"
    merge, acceptance = reviewed(publisher, artifact)
    original_spec = UnitSpec("planning-original", "explicit", "planning", "Plan", kind="planning")
    original = CampaignUnit(original_spec, state=UnitState.MERGED_PENDING_SYNC)
    snapshot = CampaignSnapshot(
        "campaign",
        "owner/repo",
        CampaignSpec("tracking", remote="source-code", sources=(source,), units=(original_spec,)),
        units={original.id: original},
        generation=1,
    )
    change = ArtifactChange(str(plan), artifact, before, acceptance["review_path"], selector="1.1")
    debt = publisher.publish_artifacts(
        "planning-original",
        source,
        original_spec,
        (change,),
        merge=merge,
        acceptance=acceptance,
        journal=Journal(tmp_path / "planning-journal"),
    )
    assert debt.state == "tracking_pr_required"
    descriptor = prepare(
        snapshot,
        original,
        debt,
        state_dir=tmp_path / "planning-authority",
        publisher_project=publisher.project,
        source=source,
        changes=(change,),
        merge=merge,
        acceptance=acceptance,
    )
    unit = admit(snapshot, descriptor)
    workspace = tmp_path / "tracking-planning"
    git(publisher.project, "worktree", "add", "-b", "tracking-planning", str(workspace), "HEAD")
    envelope = SimpleNamespace(
        campaign_id=snapshot.id,
        unit_id=unit.id,
        config_revision=1,
        directory=tmp_path / "planning-worker",
    )
    receipt = run_tracking(
        {"tracking": descriptor, "workspace": str(workspace)},
        envelope,
        journal=Journal(tmp_path / "planning-child-journal"),
    )
    assert receipt["outcome"] == "tracking_complete"
    assert (workspace / plan.relative_to(publisher.project)).read_bytes() == artifact
    assert plan.read_bytes() == before and original.state == UnitState.MERGED_PENDING_SYNC


def test_tracking_repair_inherits_original_single_inflight_slot(tracking):
    import dataclasses

    from bmad_loop.campaign_scheduler import admit as schedule

    snapshot, original, unit, _, _, _, _, _, _ = tracking
    snapshot.spec = dataclasses.replace(snapshot.spec, max_in_flight_units=1, max_active_epics=1)
    unit.state = UnitState.READY
    assert schedule(snapshot).units == (unit.id,)
    assert original.state == UnitState.MERGED_PENDING_SYNC


def test_production_runtime_automatically_merges_source_tracking_draft(
    production_case, tmp_path, monkeypatch
):
    """Real missing canonical status creates a fourth PR through normal workers."""
    from bmad_loop.campaign_source_publication import SourcePublication

    case = production_case
    original_publish = SourcePublication.publish
    changed = []
    source_relative = "specs/epic/stories/first-story.md"
    before = (case.repo / source_relative).read_bytes()

    def source_debt(self, action_id, source, unit, binding, **kwargs):
        if unit.id == "source:project:first" and not changed:
            # Simulate a canonical source row restored to its accepted preimage
            # after code merge. The new main commit preserves the real merge.
            current = git(case.remote, "rev-parse", "refs/heads/main")
            assert "status: done" in git(case.remote, "show", current + ":" + source_relative)
            preimage = tmp_path / "accepted-source-preimage"
            preimage.write_bytes(before)
            task_env = {
                key: value for key, value in os.environ.items() if not key.startswith("GIT_")
            }
            task_env.update(
                GIT_INDEX_FILE=str(tmp_path / "source-repair-index"),
                GIT_AUTHOR_NAME="Source fixture",
                GIT_AUTHOR_EMAIL="source@example.test",
                GIT_COMMITTER_NAME="Source fixture",
                GIT_COMMITTER_EMAIL="source@example.test",
            )

            def native_git(*args):
                return subprocess.run(
                    ["git", "-C", str(case.remote), *args],
                    env=task_env,
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout.strip()

            blob = native_git("hash-object", "-w", str(preimage))
            native_git("read-tree", current)
            native_git("update-index", "--cacheinfo", "100644," + blob + "," + source_relative)
            tree = native_git("write-tree")
            revision = native_git(
                "commit-tree", tree, "-p", current, "-m", "test: source status requires tracking"
            )
            native_git("update-ref", "refs/heads/main", revision, current)
            changed.append(revision)
        return original_publish(self, action_id, source, unit, binding, **kwargs)

    monkeypatch.setattr(SourcePublication, "publish", source_debt)
    result = run_case(case)
    assert result.phase == "delivered", diagnose(result)
    controls = [unit for unit in result.units.values() if unit.spec.kind == "tracking"]
    assert len(controls) == 1 and controls[0].state == UnitState.DELIVERED
    original = result.units["source:project:first"]
    assert original.bindings["tracking_units"] == [controls[0].id]
    assert len(case.service.pulls) == 4 and len(changed) == 1
    assert "status: done" in git(case.remote, "show", "refs/heads/main:" + source_relative)
    history = controls[0].evidence["worker_history"]
    assert any(receipt["outcome"] == "tracking_complete" for receipt in history)
    assert any(receipt.get("stage") == "publication_review" for receipt in history)
    closed = case.service.closed_units
    assert closed.index(controls[0].id) < closed.index(original.id)
