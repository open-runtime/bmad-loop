"""Fresh branch-only evidence observes external work and ignores owned checkpoints."""

from dataclasses import replace

import pytest
from conftest import git

from bmad_loop import campaign_branch_frontier as frontier
from bmad_loop import verify
from bmad_loop.campaign_model import (
    CampaignError,
    CampaignSnapshot,
    CampaignSpec,
    CampaignUnit,
    OutboxAction,
    UnitSpec,
    WorkerClaim,
)


@pytest.fixture
def repository(project, tmp_path):
    repo = project.project
    remote = tmp_path / "remote.git"
    git(repo, "clone", "--bare", str(repo), str(remote))
    git(repo, "remote", "add", "origin", str(remote))
    common = (repo / git(repo, "rev-parse", "--git-common-dir")).resolve()
    snapshot = CampaignSnapshot(
        "campaign", "a/b", CampaignSpec("frontier"), common_dir=str(common), root=str(repo)
    )
    return repo, remote, snapshot


def branch(repo, name):
    git(repo, "branch", name)
    return verify.rev_parse_head(repo)


def commit(repo, text):
    (repo / "src.txt").write_text(text, encoding="utf-8")
    git(repo, "add", "src.txt")
    git(repo, "commit", "-qm", "partial branch work")
    return verify.rev_parse_head(repo)


def test_fresh_remote_and_local_branch_only_heads_are_visible_without_fetch_or_checkout(repository):
    repo, remote, snapshot = repository
    head = branch(repo, "partial/no-pr")
    git(repo, "push", "origin", "partial/no-pr")
    before_head = verify.rev_parse_head(repo)
    before_refs = git(repo, "for-each-ref", "--format=%(refname) %(objectname)")
    observed = frontier.collect(repo, snapshot)
    rows = [row for row in observed["branches"] if row["ref"] == "refs/heads/partial/no-pr"]
    assert {(row["origin"], row["sha"]) for row in rows} == {("local", head), ("remote", head)}
    assert all(row["evidence_id"].endswith("@" + head) for row in rows)
    assert verify.rev_parse_head(repo) == before_head
    assert git(repo, "for-each-ref", "--format=%(refname) %(objectname)") == before_refs
    assert git(repo, "status", "--porcelain") == ""


def test_remote_only_update_is_fresh_without_periodic_inventory_or_local_object_fetch(repository):
    repo, remote, snapshot = repository
    head = branch(repo, "partial/no-pr")
    git(repo, "push", "origin", "partial/no-pr")
    first = frontier.collect(repo, snapshot)
    new_head = git(
        remote,
        "-c",
        "user.name=Owner",
        "-c",
        "user.email=owner@example.test",
        "commit-tree",
        head + "^{tree}",
        "-p",
        head,
        "-m",
        "remote partial checkpoint",
    )
    git(remote, "update-ref", "refs/heads/partial/no-pr", new_head, head)
    observed = frontier.collect(repo, snapshot)
    assert observed["branches"] != first["branches"]
    assert next(row["sha"] for row in observed["branches"] if row["origin"] == "remote") == new_head
    assert verify.git_bytes(repo, "cat-file", "-e", new_head + "^{commit}").returncode != 0


def test_exact_recorded_campaign_branches_do_not_invalidate_planning_but_prefix_is_not_authority(
    repository,
):
    repo, remote, snapshot = repository
    git(repo, "checkout", "-b", "bmad/campaign/owned")
    head = verify.rev_parse_head(repo)
    git(repo, "push", "origin", "bmad/campaign/owned")
    unit = CampaignUnit(
        UnitSpec("s:i:u", "s", "e", "Unit"),
        bindings={"workspace": str(repo), "branch": "bmad/campaign/owned", "head": head},
    )
    snapshot.units[unit.id] = unit
    branch(repo, "bmad/campaign/unowned")
    git(repo, "update-ref", "refs/bmad-campaign/checkpoints/internal", head)
    observed = frontier.collect(repo, snapshot)
    assert not any(row["ref"] == "refs/heads/bmad/campaign/owned" for row in observed["branches"])
    assert any(row["ref"] == "refs/heads/bmad/campaign/unowned" for row in observed["branches"])
    assert not any(row["ref"].startswith("refs/bmad-campaign/") for row in observed["branches"])


def test_live_owned_descendant_and_initial_draft_remote_remain_excluded_after_model_completion(
    repository,
):
    repo, remote, snapshot = repository
    git(repo, "checkout", "-b", "bmad/owned")
    first = verify.rev_parse_head(repo)
    git(repo, "push", "origin", "bmad/owned")
    unit = CampaignUnit(
        UnitSpec("s:i:u", "s", "e", "Unit"),
        bindings={"workspace": str(repo), "branch": "bmad/owned", "head": first},
    )
    snapshot.units[unit.id] = unit
    snapshot.outbox.append(
        OutboxAction(
            "allocation", "prepare-unit", unit.id, {}, 0, state="confirmed", receipt={"head": first}
        )
    )
    unit.claim = WorkerClaim(unit.id, 1, 0, "worker", workspace=str(repo))
    before = frontier.collect(repo, snapshot)["branches"]
    next_head = commit(repo, "actual own checkpoint\n")
    assert frontier.collect(repo, snapshot)["branches"] == before
    unit.claim.quiescent = True
    unit.bindings["head"] = next_head
    assert frontier.collect(repo, snapshot)["branches"] == before
    # Operator work on the retained branch after actor quiescence is new input.
    commit(repo, "owner continued retained branch\n")
    assert frontier.collect(repo, snapshot)["branches"] != before


def test_frontier_faults_and_bounds_remain_visible_instead_of_healthy_empty_input(repository):
    repo, remote, snapshot = repository
    with pytest.raises(CampaignError, match="combined ref bound"):
        frontier.collect(repo, snapshot, max_refs=1)
    with pytest.raises(CampaignError, match="byte bound"):
        frontier.collect(repo, snapshot, max_bytes=1)
    missing = replace(snapshot.spec, remote="missing-origin")
    snapshot.spec = missing
    with pytest.raises(CampaignError, match="observation failed"):
        frontier.collect(repo, snapshot)


def test_branch_only_work_created_during_planning_invalidates_the_actual_stage_boundary(
    project, tmp_path
):
    from test_campaign_boundary_sync import completed, setup

    from bmad_loop import campaign_boundary_sync as boundary

    runtime, snapshot, unit = setup(project, tmp_path)
    action = completed(runtime, snapshot, unit)
    git(project.project, "branch", "owner/partial-without-pr")
    result = boundary.after_effect(runtime, snapshot, action)
    assert result.data["changed"] is True
    assert any(
        row["ref"] == "refs/heads/owner/partial-without-pr"
        for row in result.data["inputs"]["external_branches"]["branches"]
    )
    boundary.after_apply(runtime, snapshot, action, result.data)
    assert unit.evidence["stage_input_invalidations"][-1]["receipt"]["launch_id"] == "model"
