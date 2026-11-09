"""Inventory coverage and adoption are explicit, read-only evidence."""

import os
from dataclasses import replace

import pytest

from bmad_loop.campaign_discovery import OverlapDisposition, RepositoryDiscovery, validate_overlap


class Gateway:
    repository = "a/b"

    def issues(self):
        return [{"id": 1, "number": 1, "title": "Existing"}]

    def pulls(self, state="open"):
        return [{"id": 2, "number": 2, "title": "Active", "state": state, "head": {"sha": "head"}}]


def test_inventory_observes_local_refs_worktree_dirty_content_without_checkouts(project):
    gateway = Gateway()
    discovery = RepositoryDiscovery(gateway, project.project, clock=lambda: 100)
    # No remote is configured in the sandbox; that is a visible coverage failure.
    receipt = discovery.collect()
    assert not receipt.complete
    assert any("remote_git" in reason for reason in receipt.unavailable)
    assert receipt.common_dir.endswith(".git")
    assert receipt.issues[0]["number"] == 1
    assert receipt.local_refs
    assert receipt.worktrees[0]["ownership"] == "unknown"
    detail = discovery.inspect_branch("HEAD", "HEAD")
    assert detail["base"] == detail["head"]
    assert detail["diff"] == ""


def test_inventory_status_preserves_live_worker_index_bytes_after_timestamp_change(
    project, monkeypatch
):
    from conftest import git

    repo = project.project
    product = repo / "tracked-product.txt"
    product.write_text("Existing worker content\n")
    git(repo, "add", product.name)
    git(repo, "commit", "-qm", "tracked worker product")
    git(repo, "update-index", "--refresh")
    index = repo / ".git" / "index"
    before = index.read_bytes()
    stat = product.stat()
    os.utime(product, ns=(stat.st_atime_ns, stat.st_mtime_ns + 2_000_000_000))
    monkeypatch.setenv("GIT_OPTIONAL_LOCKS", "1")  # the observation must override its caller

    receipt = RepositoryDiscovery(Gateway(), repo).collect()
    observed = next(row for row in receipt.worktrees if row["worktree"] == str(repo))
    assert observed["dirty_porcelain"] == ""
    assert index.read_bytes() == before


def test_partial_inventory_and_active_overlap_do_not_authorize_adoption(project):
    receipt = RepositoryDiscovery(Gateway(), project.project).collect()
    receipt = replace(receipt, complete=True, unavailable=())
    disposition = OverlapDisposition(
        "unit",
        "new_work",
        receipt.digest,
        (),
        ("New acceptance",),
        (),
        (),
        "Acceptance is absent from existing work",
    )
    validate_overlap(disposition, receipt)
    partial = replace(receipt, complete=False)
    with pytest.raises(ValueError, match="Incomplete"):
        validate_overlap(replace(disposition, inventory_digest=partial.digest), partial)
    adoption = replace(
        disposition, disposition="continue_existing_work", evidence_ids=("pr:2@head",), adopted_pr=2
    )
    with pytest.raises(ValueError, match="authorization"):
        validate_overlap(adoption, receipt)
    validate_overlap(adoption, receipt, owned_pr=2)
    with pytest.raises(ValueError, match="absent from.*inventory"):
        validate_overlap(replace(disposition, evidence_ids=("pr:999@invented",)), receipt)
    with pytest.raises(ValueError, match="merged/source"):
        validate_overlap(replace(adoption, disposition="already_delivered"), receipt)


@pytest.fixture
def semantic_inventory(project, tmp_path):
    from conftest import git

    repo = project.project
    initial = git(repo, "branch", "--show-current")
    base = git(repo, "rev-parse", "HEAD")
    git(repo, "switch", "-c", "external-work")
    product = repo / "src" / "shared.py"
    product.parent.mkdir(exist_ok=True)
    product.write_text("external implementation\n", encoding="utf-8")
    git(repo, "add", "src/shared.py")
    git(repo, "commit", "-m", "Preserved external acceptance work")
    partial = git(repo, "rev-parse", "HEAD")
    git(repo, "switch", initial)
    remote = tmp_path / "remote.git"
    git(repo, "clone", "--bare", str(repo), str(remote))
    git(repo, "remote", "add", "inventory", str(remote))
    tree = git(remote, "rev-parse", partial + "^{tree}")
    head = git(
        remote,
        "-c",
        "user.name=External owner",
        "-c",
        "user.email=owner@example.test",
        "commit-tree",
        tree,
        "-p",
        partial,
        "-m",
        "Remote-only external checkpoint",
    )
    git(remote, "update-ref", "refs/heads/external-work", head)

    class DetailedGateway:
        repository = "org/repo"
        root = "repos/org/repo"

        def __init__(self):
            self.calls = []
            self.conclusion = "success"
            self.fail_checks = False
            self.items = {
                1: {
                    "id": 101,
                    "number": 1,
                    "state": "open",
                    "title": "Incoming",
                    "body": "Continue https://github.com/org/repo/pull/9",
                    "updated_at": "2026-11-05T10:00:00Z",
                },
                3: {
                    "id": 103,
                    "number": 3,
                    "state": "closed",
                    "title": "Prior research",
                    "body": "Preserve acceptance research",
                    "updated_at": "2020-01-01T00:00:00Z",
                },
            }
            self.prs = {
                number: {
                    "id": 200 + number,
                    "number": number,
                    "title": "Active" if number == 2 else "Old partial work",
                    "state": "open" if number == 2 else "closed",
                    "merged": False,
                    "body": "References #3",
                    "head": {"sha": head},
                    "base": {"sha": base},
                    "updated_at": "2020-01-01T00:00:00Z",
                }
                for number in (2, 9)
            }

        def issues(self):
            return [dict(self.items[1])]

        def pulls(self):
            return [dict(self.prs[2])]

        def recent_pulls(self, since):
            return []

        def issue(self, number):
            self.calls.append(("issue", number))
            return (
                dict(self.items[number])
                if number in self.items
                else {**self.prs[number], "pull_request": {}}
            )

        def pull(self, number):
            self.calls.append(("pull", number))
            return dict(self.prs[number])

        def files(self, number):
            self.calls.append(("files", number))
            return [
                {
                    "filename": "src/shared.py",
                    "status": "added",
                    "patch": "+external implementation",
                }
            ]

        def reviews(self, number):
            return [{"id": 10, "state": "APPROVED", "commit_id": head}]

        def review_threads(self, number):
            return []

        def pages(self, path, *, collection=None):
            from bmad_loop.campaign_github import GithubError

            self.calls.append(("page", path))
            if path.endswith("/check-runs"):
                if self.fail_checks:
                    raise GithubError("check coverage unavailable")
                return [
                    {
                        "id": 4,
                        "head_sha": head,
                        "name": "CI",
                        "app": {"id": 7},
                        "conclusion": self.conclusion,
                    }
                ]
            assert path.endswith("/statuses")
            return []

    return repo, remote, base, head, DetailedGateway()


def test_production_inventory_expands_linked_old_work_and_real_branch_semantics(
    semantic_inventory, tmp_path
):
    import dataclasses

    from test_campaign_runtime_admission import configured

    from bmad_loop.campaign_model import CampaignUnit, UnitSpec

    repo, remote, base, head, gateway = semantic_inventory
    project = type("Project", (), {"project": repo})()
    runtime, snapshot, _, _ = configured(project, tmp_path)
    runtime._gateway = gateway
    snapshot.spec = dataclasses.replace(snapshot.spec, remote="inventory")
    unit = CampaignUnit(
        UnitSpec("source:project:new", "source", "e", "New", scopes=("src/shared.py",))
    )
    snapshot.units[unit.id] = unit
    first = runtime._perform_inventory(snapshot, None).data
    assert first["complete"], first["unavailable"]
    prior = next(row for row in first["pulls"] if row["number"] == 9)
    assert prior["state"] == "closed" and prior["merged"] is False
    assert prior["linked_issues"][0]["id"] == 103
    assert prior["check_runs"][0]["head_sha"] == head
    branch = next(row for row in first["remote_refs"] if row["ref"] == "refs/heads/external-work")
    assert "external implementation" in branch["branch_evidence"]["diff"]
    assert branch["branch_evidence"]["commits"][0]["sha"] == head
    from bmad_loop.campaign_model import OutboxAction
    from bmad_loop.campaign_planning import StageCoordinates, stage_packet
    from bmad_loop.campaign_supervisor import Observation

    runtime._apply_effect(
        snapshot,
        OutboxAction("inventory", "inventory", "", {}, snapshot.generation),
        Observation("confirmed", first),
    )
    packet = stage_packet(
        StageCoordinates(
            snapshot.id,
            unit.id,
            "architecture",
            1,
            1,
            base,
            base,
            snapshot.bindings["inventory"]["digest"],
        ),
        context={"inventory": snapshot.bindings["inventory"]},
    )
    assert "external implementation" in packet and "Remote-only external checkpoint" in packet
    from conftest import git

    assert git(repo, "rev-parse", "HEAD") == base
    assert git(repo, "status", "--porcelain") == ""
    assert git(repo, "rev-parse", "refs/bmad-campaign/discovery/" + head) == head
    files = sum(call[0] == "files" for call in gateway.calls)
    gateway.conclusion = "failure"
    second = runtime._perform_inventory(snapshot, None).data
    assert second["complete"]
    assert sum(call[0] == "files" for call in gateway.calls) == files
    assert all(row["check_runs"][0]["conclusion"] == "failure" for row in second["pulls"])


@pytest.mark.parametrize("paths", [("src/unrelated",), ("*",)])
def test_discovery_cache_is_bounded_and_scopes_select_actual_changed_paths(
    semantic_inventory, paths
):
    repo, remote, base, head, gateway = semantic_inventory
    cache = {}
    discovery = RepositoryDiscovery(gateway, repo, cache=cache, max_cache_entries=1)
    receipt = discovery.collect(remote="inventory", detailed_prs=None, relevant_paths=paths)
    assert receipt.complete
    assert len(cache) == 1
    branch = next(row for row in receipt.remote_refs if row["ref"] == "refs/heads/external-work")
    if paths == ("*",):
        assert "external implementation" in branch["branch_evidence"]["diff"]
    else:
        assert branch["inspection"] == "outside_declared_paths"
        assert branch["changed_paths"] == ["src/shared.py"]


def test_cached_immutable_details_never_hide_missing_fresh_check_coverage(semantic_inventory):
    repo, remote, base, head, gateway = semantic_inventory
    discovery = RepositoryDiscovery(gateway, repo)
    assert discovery.collect(remote="inventory", detailed_prs=None).complete
    gateway.fail_checks = True
    partial = discovery.collect(remote="inventory", detailed_prs=None)
    assert not partial.complete
    assert any("check coverage unavailable" in value for value in partial.unavailable)


def test_detail_readback_detects_native_pr_movement_during_inventory(semantic_inventory):
    repo, remote, base, head, gateway = semantic_inventory
    original = gateway.pull
    reads = []

    def moving(number):
        if number == 2:
            reads.append(number)
            if len(reads) == 2:
                gateway.prs[number]["head"] = {"sha": base}
        return original(number)

    gateway.pull = moving
    partial = RepositoryDiscovery(gateway, repo).collect(remote="inventory", detailed_prs=None)
    assert not partial.complete
    assert any("PR identity moved" in value for value in partial.unavailable)


def test_branch_rename_out_of_selected_scope_keeps_full_rename_evidence(semantic_inventory):
    from conftest import git

    repo, _, _, _, gateway = semantic_inventory
    canonical = git(repo, "branch", "--show-current")
    # The source path exists on canonical main before the external rename.
    git(repo, "merge", "--ff-only", "external-work")
    base = git(repo, "rev-parse", "HEAD")
    git(repo, "push", "inventory", "HEAD:refs/heads/" + canonical)
    git(
        repo, "fetch", "--no-tags", "inventory", "refs/heads/external-work:refs/heads/remote-rename"
    )
    git(repo, "switch", "remote-rename")
    (repo / "docs").mkdir(exist_ok=True)
    git(repo, "mv", "src/shared.py", "docs/shared.py")
    git(repo, "commit", "-m", "Move selected cache implementation outside its prior scope")
    head = git(repo, "rev-parse", "HEAD")
    git(repo, "push", "inventory", "HEAD:refs/heads/external-work")
    git(repo, "switch", canonical)
    git(repo, "config", "diff.renames", "true")

    receipt = RepositoryDiscovery(gateway, repo).collect(
        remote="inventory", target=canonical, relevant_paths=("src/shared.py",)
    )
    assert receipt.complete, receipt.unavailable
    branch = next(row for row in receipt.remote_refs if row["ref"] == "refs/heads/external-work")
    evidence = branch["branch_evidence"]
    assert evidence["base"] == base and evidence["head"] == head
    assert "rename from src/shared.py" in evidence["diff"]
    assert "rename to docs/shared.py" in evidence["diff"]
    assert git(repo, "rev-parse", "HEAD") == base
    assert git(repo, "status", "--porcelain") == ""
