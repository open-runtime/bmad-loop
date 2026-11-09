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
    unit.bindings["issue"] = {"number": 1}
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
        assert branch["changed_paths"] == []
        assert branch["changed_paths_scope"] == ["src/unrelated"]


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


def test_many_preserved_aliases_keep_complete_scoped_branch_evidence(semantic_inventory):
    import subprocess

    from conftest import git

    repo, remote, base, head, gateway = semantic_inventory
    # Real branch refs, exceeding the old 128-alias limit before the relevant
    # branch sorts into the inventory. None may be deleted to make admission fit.
    aliases = {f"refs/heads/checkpoint/{i:04d}": base for i in range(900)}
    aliases.update({f"refs/heads/preserved/{i:04d}": head for i in range(900)})
    subprocess.run(
        ["git", "-C", str(remote), "update-ref", "--stdin"],
        input="".join(f"update {ref} {sha}\n" for ref, sha in aliases.items()),
        text=True,
        capture_output=True,
        check=True,
    )
    discovery = RepositoryDiscovery(gateway, repo)
    calls = []
    inspect = discovery.inspect_branch

    def counted(*args, **kwargs):
        calls.append(args)
        return inspect(*args, **kwargs)

    discovery.inspect_branch = counted
    before = git(remote, "for-each-ref", "--format=%(refname) %(objectname)")
    receipt = discovery.collect(remote="inventory", relevant_paths=("src/shared.py",))
    assert receipt.complete, (receipt.unavailable, receipt.truncated)
    assert all(
        any(row["ref"] == ref and row["sha"] == sha for row in receipt.remote_refs)
        for ref, sha in aliases.items()
    )
    assert sum(args == (base, head) for args in calls) == 1
    head_rows = [row for row in receipt.remote_refs if row["sha"] == head]
    bodies = [row for row in head_rows if "branch_evidence" in row]
    assert len(bodies) == 1
    assert "external implementation" in bodies[0]["branch_evidence"]["diff"]
    identity = f"ref:{bodies[0]['ref']}@{head}"
    assert all(row.get("branch_evidence_ref") == identity for row in head_rows if row not in bodies)
    assert git(remote, "for-each-ref", "--format=%(refname) %(objectname)") == before


def test_many_distinct_relevant_heads_are_limited_by_bytes_not_branch_count(semantic_inventory):
    """Ablation: removing the aggregate marker guard repeats the same truncation reason."""
    from conftest import git

    repo, remote, base, head, gateway = semantic_inventory
    tree = git(remote, "rev-parse", head + "^{tree}")
    for i in range(40):
        tip = git(
            remote,
            "-c",
            "user.name=Owner",
            "-c",
            "user.email=owner@example.test",
            "commit-tree",
            tree,
            "-p",
            base,
            "-m",
            f"Independent unfinished change {i}",
        )
        git(remote, "update-ref", f"refs/heads/independent/{i:02d}", tip)
    receipt = RepositoryDiscovery(gateway, repo).collect(
        remote="inventory", relevant_paths=("src/shared.py",)
    )
    assert receipt.complete, (receipt.unavailable, receipt.truncated)
    independent = [
        row for row in receipt.remote_refs if row["ref"].startswith("refs/heads/independent/")
    ]
    assert len(independent) == 40
    assert all("external implementation" in row["branch_evidence"]["diff"] for row in independent)
    bounded = RepositoryDiscovery(gateway, repo, max_detail_bytes=2000).collect(
        remote="inventory", relevant_paths=("src/shared.py",)
    )
    assert not bounded.complete
    assert bounded.truncated.count("git:branch_detail_byte_limit") == 1
    assert {(row["ref"], row["sha"]) for row in bounded.remote_refs} == {
        (row["ref"], row["sha"]) for row in receipt.remote_refs
    }
    limited = [
        row for row in bounded.remote_refs if row["ref"].startswith("refs/heads/independent/")
    ]
    assert len(limited) == 40
    assert sum(row.get("inspection") == "detail_byte_limit" for row in limited) > 1
    assert all(
        "branch_evidence" in row or row.get("inspection") == "detail_byte_limit" for row in limited
    )


def test_scoped_branch_evidence_excludes_unrelated_patch_and_commit_history(semantic_inventory):
    from conftest import git

    repo, _, base, _, gateway = semantic_inventory
    git(repo, "switch", "external-work")
    selected = git(repo, "rev-parse", "HEAD")
    (repo / "unrelated.txt").write_text("unrelated large change\n" * 1000)
    git(repo, "add", "unrelated.txt")
    git(repo, "commit", "-qm", "Unrelated large change")
    for i in range(35):
        git(repo, "commit", "--allow-empty", "-qm", f"Unrelated checkpoint {i}")
    head = git(repo, "rev-parse", "HEAD")
    discovery = RepositoryDiscovery(gateway, repo, max_detail_bytes=2000)
    detail = discovery.inspect_branch(base, head, relevant_paths=("src/shared.py",))
    assert detail["truncated"] is False and detail["commits_truncated"] is False
    assert detail["changed_paths"] == ["src/shared.py"]
    assert "external implementation" in detail["diff"]
    assert {commit["sha"] for commit in detail["commits"]} == {head, selected}
    assert "unrelated large change" not in detail["diff"]
    broad = discovery.inspect_branch(base, head)
    assert broad["truncated"] is True
    assert broad["commits_truncated"] is True


def test_long_selected_history_bounds_capture_and_keeps_every_commit_that_fits(
    semantic_inventory, monkeypatch
):
    from conftest import git

    from bmad_loop import campaign_discovery

    repo, _, base, _, gateway = semantic_inventory
    trees = [git(repo, "rev-parse", ref + "^{tree}") for ref in (base, "external-work")]
    tip = base
    expected = set()
    for index in range(140):
        tip = git(
            repo,
            "commit-tree",
            trees[index % 2],
            "-p",
            tip,
            "-m",
            f"Selected change {index}",
        )
        if index:
            expected.add(tip)
    calls = []
    run_git = campaign_discovery.git_bytes

    def measured(repo, *args, **kwargs):
        result = run_git(repo, *args, **kwargs)
        calls.append((args, result.stdout))
        return result

    monkeypatch.setattr(campaign_discovery, "git_bytes", measured)
    bounded = RepositoryDiscovery(gateway, repo, max_detail_bytes=2000).inspect_branch(
        base, tip, relevant_paths=("src/shared.py",)
    )
    assert bounded["truncated"] is False
    assert bounded["commits_truncated"] is True
    assert bounded["commits"][0]["sha"] == tip
    history = [data for args, data in calls if "log" in args]
    # Fixed-width IDs add LF framing and one overflow sentinel to the 2 KiB cap.
    assert history and max(map(len, history)) <= 2100
    assert sum("cat-file" in args for args, _ in calls) == 1
    assert sum("--stdin" in args for args, _ in calls) == 1

    complete = RepositoryDiscovery(gateway, repo, max_detail_bytes=100_000).inspect_branch(
        base, tip, relevant_paths=("src/shared.py",)
    )
    assert complete["commits_truncated"] is False
    # The initial commit has the base tree; all 139 later commits change scope.
    assert len(complete["commits"]) == 139
    assert {row["sha"] for row in complete["commits"]} == expected


@pytest.mark.parametrize("scoped", [False, True])
def test_oversized_commit_subject_is_refused_before_payload_capture(
    semantic_inventory, monkeypatch, scoped
):
    import subprocess

    from conftest import git

    from bmad_loop import campaign_discovery

    repo, _, base, _, gateway = semantic_inventory
    tree = git(repo, "rev-parse", "external-work^{tree}")
    tip = subprocess.run(
        ["git", "-C", str(repo), "commit-tree", tree, "-p", base, "-F", "-"],
        input="oversized subject " * 20_000 + "\n",
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()
    calls = []
    run_git = campaign_discovery.git_bytes

    def measured(repo, *args, **kwargs):
        result = run_git(repo, *args, **kwargs)
        calls.append((args, result.stdout))
        return result

    monkeypatch.setattr(campaign_discovery, "git_bytes", measured)
    detail = RepositoryDiscovery(gateway, repo, max_detail_bytes=8000).inspect_branch(
        base, tip, relevant_paths=("src/shared.py",) if scoped else ()
    )
    assert detail["truncated"] is False
    assert detail["commits_truncated"] is True
    assert detail["commits"] == []
    assert any("cat-file" in args for args, _ in calls)
    assert not any("--stdin" in args for args, _ in calls)
    assert max(len(data) for _, data in calls) < 8000


def test_main_only_changes_do_not_become_unfinished_branch_work(semantic_inventory):
    from conftest import git

    repo, _, _, head, gateway = semantic_inventory
    main_only = repo / "main-only.py"
    main_only.write_text("new main implementation\n")
    git(repo, "add", main_only.name)
    git(repo, "commit", "-qm", "Main moved independently")
    base = git(repo, "rev-parse", "HEAD")
    git(repo, "fetch", "inventory", "external-work")
    detail = RepositoryDiscovery(gateway, repo).inspect_branch(
        base, head, relevant_paths=("main-only.py",)
    )
    assert detail["changed_paths"] == [] and detail["diff"] == ""
    assert detail["merge_base"] != base
    # The branch really does have unfinished changes in its own scope.
    actual = RepositoryDiscovery(gateway, repo).inspect_branch(
        base, head, relevant_paths=("src/shared.py",)
    )
    assert actual["changed_paths"] == ["src/shared.py"]
    assert "external implementation" in actual["diff"]


def test_scoped_oversize_relevant_evidence_remains_incomplete(semantic_inventory):
    from conftest import git

    repo, _, _, _, gateway = semantic_inventory
    canonical = git(repo, "branch", "--show-current")
    git(repo, "switch", "external-work")
    git(repo, "fetch", "inventory", "external-work")
    git(repo, "merge", "--ff-only", "FETCH_HEAD")
    (repo / "src/shared.py").write_text("required unfinished work\n" * 1000)
    git(repo, "add", "src/shared.py")
    git(repo, "commit", "-qm", "Large selected implementation")
    git(repo, "push", "inventory", "HEAD:refs/heads/external-work")
    git(repo, "switch", canonical)
    receipt = RepositoryDiscovery(gateway, repo, max_detail_bytes=2000).collect(
        remote="inventory", relevant_paths=("src/shared.py",)
    )
    assert not receipt.complete
    assert "ref:refs/heads/external-work:detail" in receipt.truncated
    branch = next(row for row in receipt.remote_refs if row["ref"] == "refs/heads/external-work")
    assert branch["branch_evidence"]["truncated"] is True


def test_criss_cross_history_retains_every_best_base_selected_delta(semantic_inventory):
    from conftest import git

    repo, _, base, _, gateway = semantic_inventory
    first = git(repo, "rev-parse", "external-work")
    base_tree = git(repo, "rev-parse", base + "^{tree}")
    selected_tree = git(repo, "rev-parse", first + "^{tree}")
    second = git(repo, "commit-tree", base_tree, "-p", base, "-m", "Independent base")
    target = git(
        repo, "commit-tree", selected_tree, "-p", first, "-p", second, "-m", "Canonical merge"
    )
    head = git(
        repo,
        "commit-tree",
        selected_tree,
        "-p",
        second,
        "-p",
        first,
        "-m",
        "Preserved independent merge",
    )
    detail = RepositoryDiscovery(gateway, repo).inspect_branch(
        target, head, relevant_paths=("src/shared.py",)
    )
    assert detail["merge_bases"] == sorted([first, second])
    assert detail["truncated"] is False
    assert detail["changed_paths"] == ["src/shared.py"]
    assert len(detail["alternate_diffs"]) == 1
    deltas = [detail["diff"], detail["alternate_diffs"][0]["diff"]]
    assert "" in deltas and any("external implementation" in value for value in deltas)
    unrelated = RepositoryDiscovery(gateway, repo).inspect_branch(
        target, head, relevant_paths=("never-changed.py",)
    )
    assert unrelated["changed_paths"] == []
    assert unrelated["equivalent_merge_bases"] == sorted([first, second])
    assert unrelated["alternate_diffs"] == []
    classified = RepositoryDiscovery(gateway, repo)._pull_paths(
        {"number": 44, "base": {"sha": target}, "head": {"sha": head}}
    )
    assert classified["merge_bases"] == sorted([first, second])
    assert classified["changed_paths"] == ["src/shared.py"]
    assert classified["source"] == "exact_local_git_objects"
    assert gateway.calls == []


def test_optional_missing_body_references_are_visible_and_deduplicated(semantic_inventory):
    from bmad_loop.campaign_github import GithubError

    repo, _, _, _, gateway = semantic_inventory
    gateway.items[1]["body"] = "Audit #900, also https://github.com/org/repo/issues/901"
    gateway.prs[2]["body"] = "Same audit #900"
    attempts = []

    def absent(number):
        attempts.append(number)
        raise GithubError("Not found or inaccessible", status=404)

    gateway.issue = absent
    receipt = RepositoryDiscovery(gateway, repo).collect(remote="inventory")
    assert receipt.complete, receipt.unavailable
    assert attempts == [900, 901]
    observations = [
        item
        for row in (*receipt.issues, *receipt.pulls)
        for item in row.get("discovery_reference_observations", [])
    ]
    assert {(r["source"], r["number"], r["status"]) for r in observations} == {
        (1, 900, "not_found_or_inaccessible"),
        (1, 901, "not_found_or_inaccessible"),
        (2, 900, "not_found_or_inaccessible"),
    }
    assert receipt.github_coverage["links"]["attempted"] == 2
    assert receipt.github_coverage["links"]["not_found_or_inaccessible"] == 2


@pytest.mark.parametrize(
    "kind,status",
    [("selected", 404), ("mapped", 404), ("parent", 404), ("body", 403), ("body", 500)],
)
def test_authoritative_missing_or_other_errors_keep_discovery_incomplete(
    semantic_inventory, kind, status
):
    from bmad_loop.campaign_github import GithubError

    repo, _, _, _, gateway = semantic_inventory
    gateway.items[1]["body"] = "#900" if kind == "body" else ""
    gateway.prs[2]["body"] = ""
    if kind == "parent":
        gateway.items[1]["parent_issue_url"] = "https://api.github.com/repos/org/repo/issues/900"

    def refused(number):
        raise GithubError("required request refused", status=status)

    gateway.issue = refused
    if kind == "mapped":
        gateway.pull = refused
    receipt = RepositoryDiscovery(gateway, repo).collect(
        remote="inventory",
        selected_issues=(900,) if kind == "selected" else (),
        mapped_prs=(900,) if kind == "mapped" else (),
    )
    assert not receipt.complete
    assert any("github:" in reason for reason in receipt.unavailable)


def test_failed_optional_lookups_consume_finite_link_attempt_budget(semantic_inventory):
    from bmad_loop.campaign_github import GithubError

    repo, _, _, _, gateway = semantic_inventory
    gateway.items[1]["body"] = " ".join(f"#{number}" for number in range(900, 965))
    gateway.prs[2]["body"] = ""
    attempts = []

    def absent(number):
        attempts.append(number)
        if len(attempts) > 65:
            pytest.fail("diagnostic hang guard")
        raise GithubError("not found", status=404)

    gateway.issue = absent
    receipt = RepositoryDiscovery(gateway, repo).collect(remote="inventory")
    assert attempts == list(range(900, 964))
    assert not receipt.complete and "github:linked_object_limit" in receipt.truncated
    assert receipt.github_coverage["links"]["attempted"] == 64


def test_scoped_closure_keeps_selected_links_and_reverse_prs_but_not_unrelated_bodies(
    semantic_inventory,
):
    repo, _, _, _, gateway = semantic_inventory
    gateway.items[4] = {
        "id": 104,
        "number": 4,
        "state": "open",
        "title": "Unrelated",
        "body": "Audit #9999",
    }
    gateway.issues = lambda: [dict(gateway.items[n]) for n in (1, 4)]
    gateway.prs[2]["body"] = "References selected #1"
    gateway.files = lambda number: [{"filename": "unrelated.py", "status": "modified"}]
    receipt = RepositoryDiscovery(gateway, repo).collect(
        remote="inventory",
        detailed_prs=None,
        relevant_paths=("owned.py",),
        selected_issues=(1,),
    )
    assert receipt.complete, receipt.unavailable
    assert {row["number"] for row in receipt.pulls if "reviews" in row} == {2, 9}
    assert ("issue", 9999) not in gateway.calls
    unrelated = next(row for row in receipt.issues if row["number"] == 4)
    assert unrelated["body"] == "Audit #9999"
    assert unrelated["discovery_link_coverage"] == "not_expanded_outside_selected_frontier"
    assert "unrelated_body_closures_not_expanded" in receipt.scopes
    assert receipt.github_coverage["pr_paths"]["classified"] == 1
    assert receipt.github_coverage["links"]["unexpanded"] == 1


def test_scoped_recent_pr_classification_uses_both_rename_paths(semantic_inventory):
    repo, _, _, _, gateway = semantic_inventory
    gateway.items[1]["body"] = ""
    gateway.prs[2]["body"] = ""
    gateway.prs[9]["body"] = ""
    gateway.recent_pulls = lambda since: [dict(gateway.prs[9])]
    gateway.files = lambda number: (
        [{"filename": "outside.py", "previous_filename": "owned/module.py", "status": "renamed"}]
        if number == 9
        else [{"filename": "unrelated.py", "status": "modified"}]
    )
    receipt = RepositoryDiscovery(gateway, repo).collect(
        remote="inventory",
        detailed_prs=None,
        relevant_paths=("owned",),
        recent_since="2020",
    )
    assert receipt.complete, receipt.unavailable
    records = {row["number"]: row for row in receipt.pulls}
    assert records[9]["discovery_path_coverage"]["changed_paths"] == [
        "outside.py",
        "owned/module.py",
    ]
    assert records[9]["discovery_path_coverage"]["overlaps"] is True
    assert "reviews" in records[9] and "reviews" not in records[2]
    assert receipt.github_coverage["pr_paths"]["classified"] == 2


@pytest.mark.parametrize("problem", ["rename-source", "native-limit", "moved"])
def test_incomplete_unrelated_pr_classification_is_not_silently_excluded(
    semantic_inventory, problem
):

    repo, _, _, _, gateway = semantic_inventory
    gateway.items[1]["body"] = ""
    gateway.prs[2]["body"] = ""
    if problem == "rename-source":
        gateway.files = lambda number: [{"filename": "outside.py", "status": "renamed"}]
    elif problem == "native-limit":
        gateway.files = lambda number: [
            {"filename": f"outside/{n}", "status": "modified"} for n in range(3000)
        ]
    else:
        original = gateway.pull

        def moved(number):
            result = original(number)
            result["head"] = {"sha": "f" * 40}
            return result

        gateway.pull = moved
    receipt = RepositoryDiscovery(gateway, repo).collect(
        remote="inventory",
        detailed_prs=None,
        relevant_paths=("owned.py",),
    )
    assert not receipt.complete
    assert any("classification" in reason for reason in receipt.unavailable)


def test_combined_metadata_budget_preserves_public_fields_and_refuses_overflow(semantic_inventory):
    import json

    repo, _, _, _, gateway = semantic_inventory
    gateway.items[1]["body"] = "Owner intent " * 300
    gateway.prs[2]["body"] = "Other intent " * 300
    gateway.items[1]["labels"] = [
        {"id": 1, "name": "scope", "description": "Keep exact public object"}
    ]
    gateway.items[1]["assignees"] = [
        {"id": 10, "login": "owner", "url": "https://api.github.com/users/owner"}
    ]
    required = sum(len(json.dumps(row).encode()) for row in [gateway.items[1], gateway.prs[2]])
    discovery = RepositoryDiscovery(gateway, repo, max_metadata_bytes=required + 1000)
    complete = discovery.collect(remote="inventory")
    assert complete.complete
    assert complete.issues[0]["body"] == gateway.items[1]["body"]
    assert complete.issues[0]["labels"] == gateway.items[1]["labels"]
    assert complete.issues[0]["assignees"] == gateway.items[1]["assignees"]
    limited = RepositoryDiscovery(gateway, repo, max_metadata_bytes=required - 1).collect(
        remote="inventory"
    )
    assert not limited.complete
    assert "pulls:metadata_byte_limit" in limited.truncated
    assert limited.github_coverage["metadata"]["observed_rows"] == {"issues": 1, "pulls": 1}


def test_scoped_pr_selection_is_literal_and_does_not_use_titles(semantic_inventory):
    repo, _, _, _, gateway = semantic_inventory
    gateway.items[1]["body"] = ""
    gateway.prs[2]["body"] = ""
    gateway.prs[2]["title"] = "Implements owned[1].py and owned-directory"
    gateway.files = lambda number: [
        {"filename": "owned1.py", "status": "modified"},
        {"filename": "owned-directory-other/file.py", "status": "modified"},
    ]
    receipt = RepositoryDiscovery(gateway, repo).collect(
        remote="inventory",
        detailed_prs=None,
        relevant_paths=("owned[1].py", "owned-directory"),
        selected_issues=(1,),
    )
    assert receipt.complete
    assert receipt.pulls[0]["discovery_path_coverage"]["overlaps"] is False
    assert "reviews" not in receipt.pulls[0]
    assert receipt.github_coverage["details"]["detailed"] == 0


def test_local_pr_classification_keeps_rename_source_without_fetching(semantic_inventory):
    from conftest import git

    repo, _, _, _, gateway = semantic_inventory
    git(repo, "switch", "external-work")
    base = git(repo, "rev-parse", "HEAD")
    (repo / "docs").mkdir(exist_ok=True)
    git(repo, "mv", "src/shared.py", "docs/shared.py")
    git(repo, "commit", "-qm", "Move source outside the owned directory")
    head = git(repo, "rev-parse", "HEAD")
    before = git(repo, "for-each-ref", "--format=%(refname) %(objectname)")
    result = RepositoryDiscovery(gateway, repo)._pull_paths(
        {"number": 22, "head": {"sha": head}, "base": {"sha": base}}
    )
    assert result["changed_paths"] == ["docs/shared.py", "src/shared.py"]
    assert result["source"] == "exact_local_git_objects"
    assert gateway.calls == []
    assert git(repo, "for-each-ref", "--format=%(refname) %(objectname)") == before


def test_unavailable_repository_does_not_enter_optional_reference_lookup(semantic_inventory):
    from bmad_loop.campaign_github import GithubError

    repo, _, _, _, gateway = semantic_inventory

    def unavailable():
        raise GithubError("repository access unavailable", status=404)

    gateway.issues = unavailable
    receipt = RepositoryDiscovery(gateway, repo).collect(remote="inventory")
    assert not receipt.complete
    assert any("repository access unavailable" in reason for reason in receipt.unavailable)
    assert gateway.calls == []


@pytest.mark.parametrize("explicit", ["none", "selected-link", "mapped", "reverse-link"])
def test_merged_overlap_keeps_history_without_expanding_unrelated_current_reviews(
    semantic_inventory, explicit
):
    repo, _, _, _, gateway = semantic_inventory
    gateway.items[1]["body"] = "#9" if explicit == "selected-link" else ""
    gateway.prs[2]["body"] = ""
    gateway.prs[9].update(merged=True, merged_at="2026-01-01T00:00:00Z")
    gateway.prs[9]["body"] = "#1" if explicit == "reverse-link" else "#3"
    gateway.recent_pulls = lambda since: [dict(gateway.prs[9])]
    gateway.files = lambda number: [{"filename": "owned/file.py", "status": "modified"}]
    receipt = RepositoryDiscovery(gateway, repo).collect(
        remote="inventory",
        relevant_paths=("owned",),
        detailed_prs=None,
        selected_issues=(1,),
        mapped_prs=(9,) if explicit == "mapped" else (),
        recent_since="2020",
    )
    assert receipt.complete, receipt.unavailable
    historical = next(row for row in receipt.pulls if row["number"] == 9)
    assert historical["discovery_path_coverage"]["overlaps"] is True
    assert historical["body"] == gateway.prs[9]["body"]
    assert receipt.github_coverage["pr_paths"]["historical_merged_overlapping"] == 1
    if explicit == "none":
        assert "reviews" not in historical
        assert (
            historical["discovery_detail_coverage"]
            == "metadata_paths_ancestry_only_historical_merged"
        )
        assert historical["discovery_link_coverage"] == "not_expanded_outside_selected_frontier"
        assert ("issue", 3) not in gateway.calls
    else:
        assert historical["reviews"][0]["state"] == "APPROVED"


def test_reverse_links_do_not_expand_every_sibling_of_a_reached_parent(semantic_inventory):
    repo, _, _, _, gateway = semantic_inventory
    gateway.items[1]["body"] = ""
    gateway.items[1]["parent_issue_url"] = "https://api.github.com/repos/org/repo/issues/3"
    gateway.items[4] = {"id": 104, "number": 4, "body": "Sibling of #3; audit #9999"}
    gateway.issues = lambda: [dict(gateway.items[n]) for n in (1, 4)]
    gateway.prs[2]["body"] = ""
    receipt = RepositoryDiscovery(gateway, repo).collect(
        remote="inventory",
        relevant_paths=("unrelated.py",),
        detailed_prs=None,
        selected_issues=(1,),
    )
    assert receipt.complete, receipt.unavailable
    assert ("issue", 3) in gateway.calls
    assert ("issue", 9999) not in gateway.calls
    sibling = next(row for row in receipt.issues if row["number"] == 4)
    assert sibling["body"] == gateway.items[4]["body"]
    assert sibling["discovery_link_coverage"] == "not_expanded_outside_selected_frontier"


def test_pr_detail_budget_counts_expansion_separately_from_global_metadata(semantic_inventory):
    repo, _, _, _, gateway = semantic_inventory
    gateway.items[1]["body"] = ""
    gateway.prs[2]["body"] = "Full owner intent " * 1000
    complete = RepositoryDiscovery(gateway, repo, max_detail_bytes=8000).collect(
        remote="inventory", detailed_prs=(2,), relevant_paths=("unrelated.py",)
    )
    assert complete.complete, (complete.unavailable, complete.truncated)
    assert complete.pulls[0]["body"] == gateway.prs[2]["body"]
    assert complete.pulls[0]["reviews"][0]["state"] == "APPROVED"
    assert complete.github_coverage["details"]["observed_bytes"] < 8000
    gateway.review_threads = lambda number: [{"body": "required review finding " * 1000}]
    limited = RepositoryDiscovery(
        gateway, repo, max_detail_bytes=8000, max_pr_detail_bytes=64_000
    ).collect(remote="inventory", detailed_prs=(2,), relevant_paths=("unrelated.py",))
    assert not limited.complete and "pr:2:detail" in limited.truncated
    assert limited.pulls[0]["body"] == gateway.prs[2]["body"]


@pytest.mark.parametrize("value", [0, -1, True, False, 1.5, "4000000"])
def test_aggregate_pr_detail_budget_requires_positive_integer(project, value):
    with pytest.raises(ValueError, match="PR detail byte limit.*positive integer"):
        RepositoryDiscovery(Gateway(), project.project, max_pr_detail_bytes=value)


def test_aggregate_pr_detail_budget_retains_every_required_review(semantic_inventory):
    import copy

    repo, _, _, _, gateway = semantic_inventory
    gateway.items[1]["body"] = ""
    template = gateway.prs[2]
    numbers = tuple(range(2, 10))
    gateway.prs = {
        number: {
            **copy.deepcopy(template),
            "number": number,
            "id": 200 + number,
            "body": "Full owner intent " * 1000,
        }
        for number in numbers
    }
    gateway.pulls = lambda: list(gateway.prs.values())
    gateway.reviews = lambda number: [{"id": number, "state": "APPROVED", "body": "r" * 4300}]
    gateway.review_threads = lambda number: [{"id": number, "body": "t" * 1000}]
    arguments = {"remote": "inventory", "detailed_prs": numbers, "relevant_paths": ("src",)}
    legacy = RepositoryDiscovery(gateway, repo, max_detail_bytes=8000).collect(**arguments)
    assert not legacy.complete and legacy.truncated == ("github:pr_detail_byte_limit",)
    coverage = legacy.github_coverage["details"]
    assert coverage["limit_bytes"] == 32000
    assert coverage["observed_bytes"] > coverage["limit_bytes"]
    assert (
        RepositoryDiscovery(
            gateway, repo, max_detail_bytes=8000, max_pr_detail_bytes=None
        ).max_pr_detail_bytes
        == 32000
    )

    budget = coverage["observed_bytes"]
    complete = RepositoryDiscovery(
        gateway, repo, max_detail_bytes=8000, max_pr_detail_bytes=budget
    ).collect(**arguments)
    assert complete.complete, (complete.unavailable, complete.truncated)
    assert complete.github_coverage["details"]["limit_bytes"] == budget
    assert len(complete.pulls) == len(numbers)
    for row in complete.pulls:
        assert row["body"] == gateway.prs[row["number"]]["body"]
        assert row["reviews"] == gateway.reviews(row["number"])
        assert row["review_threads"] == gateway.review_threads(row["number"])
        assert row["check_runs"][0]["conclusion"] == "success"
        assert row["changed_files_detail"] == gateway.files(row["number"])

    limited = RepositoryDiscovery(
        gateway, repo, max_detail_bytes=8000, max_pr_detail_bytes=budget - 1
    ).collect(**arguments)
    assert not limited.complete and limited.truncated == ("github:pr_detail_byte_limit",)
    assert limited.github_coverage["details"]["limit_bytes"] == budget - 1
    assert limited.pulls[-1]["details_truncated"] is True
    assert "reviews" not in limited.pulls[-1]
    assert [row["body"] for row in limited.pulls] == [row["body"] for row in complete.pulls]


@pytest.mark.parametrize(
    "opening,interior,closing",
    [
        ("```text", "", "```"),
        ("~~~json", "", "~~~"),
        ("  ````text", "```\n#71\n~~~~\n#72\n````not-a-close\n#73\n", "  `````  "),
        ("~~~~", "~~~\n#71\n```\n#72\n~~~~not-a-close\n#73\n", "~~~~~"),
    ],
)
def test_body_reference_fences_ignore_bare_numbers_but_preserve_explicit_links(
    project, opening, interior, closing
):
    body = (
        f"Keep `PR #161` and #249.\n{opening}\n"
        "full-text #1 Clas Ohlson order\nerror (P3-T10c, #70)\n"
        "Explicit https://github.com/a/b/pull/254 remains required.\n"
        f"{interior}{closing}\nAfter #300.\n"
    )
    row = {"body": body}
    assert RepositoryDiscovery(Gateway(), project.project)._references(row) == (161, 249, 254, 300)
    assert row["body"] == body


def test_fenced_output_does_not_expand_unrelated_existing_pr(semantic_inventory):
    repo, _, _, _, gateway = semantic_inventory
    body = "```text\nfull-text #9 rank\nhttps://github.com/org/repo/pull/2\n```\n"
    gateway.items[1]["body"] = body
    gateway.prs[2]["body"] = ""
    receipt = RepositoryDiscovery(gateway, repo).collect(
        remote="inventory",
        relevant_paths=("unrelated.py",),
        selected_issues=(1,),
        detailed_prs=None,
    )
    assert receipt.complete, (receipt.unavailable, receipt.truncated)
    assert {row["number"] for row in receipt.pulls} == {2}
    assert ("issue", 9) not in gateway.calls
    assert receipt.pulls[0]["reviews"][0]["state"] == "APPROVED"
    assert receipt.issues[0]["body"] == body


def test_legacy_receipt_digest_survives_optional_coverage_extension(project):
    import hashlib
    import json

    from bmad_loop.campaign_discovery import CoverageReceipt

    receipt = replace(RepositoryDiscovery(Gateway(), project.project).collect(), github_coverage={})
    old = receipt.to_dict()
    assert "github_coverage" not in old
    restored = CoverageReceipt(**old)
    assert restored.digest == hashlib.sha256(json.dumps(old, sort_keys=True).encode()).hexdigest()


@pytest.mark.parametrize(
    "field",
    ["head", "base", "id", "number", "body", "state", "draft", "merged_at", "parent_issue_url"],
)
def test_pr_moving_after_path_classification_cannot_mix_old_and_new_evidence(
    semantic_inventory, field
):
    repo, _, base, _, gateway = semantic_inventory
    gateway.items[1]["body"] = ""
    gateway.prs[2].update(body="", draft=False, merged_at=None)
    original = gateway.pull
    calls = []

    def moved(number):
        result = original(number)
        calls.append(number)
        # Classification reads before+after; detailed observation sees a new
        # stable revision. Its own later readback would not detect the movement.
        if len(calls) >= 3:
            result[field] = {
                "head": {"sha": base},
                "base": {"sha": "f" * 40},
                "id": 999,
                "number": 999,
                "body": "New owner intent #900",
                "state": "closed",
                "draft": True,
                "merged_at": "2026-11-10T00:00:00Z",
                "parent_issue_url": "https://api.github.com/repos/org/repo/issues/900",
            }[field]
        return result

    gateway.pull = moved
    receipt = RepositoryDiscovery(gateway, repo).collect(
        remote="inventory", relevant_paths=("src/shared.py",), detailed_prs=None
    )
    assert not receipt.complete
    assert "github:PR identity moved before detail observation" in receipt.unavailable
    assert calls == [2, 2, 2]
    assert "reviews" not in receipt.pulls[0]
    assert receipt.github_coverage["identity_observations"] == [
        {
            "number": 2,
            "phase": "before_detail",
            "fields": [field + ".sha" if field in {"head", "base"} else field],
            "refreshed": False,
        }
    ]


@pytest.mark.parametrize("metadata_limit", [64 * 1024 * 1024, 8000])
def test_pre_detail_clock_refresh_reads_fresh_metadata_and_review_facts(
    semantic_inventory, metadata_limit
):
    repo, _, _, head, gateway = semantic_inventory
    gateway.items[1]["body"] = ""
    gateway.prs[2]["body"] = ""
    original = gateway.pull
    calls = []
    labels = [{"id": 17, "name": "hold", "description": "Current owner context " * 1500}]

    def refreshed(number):
        result = original(number)
        calls.append(number)
        if len(calls) >= 3:
            result.update(updated_at="2026-11-10T00:00:00Z", labels=labels, title="Current title")
        return result

    gateway.pull = refreshed
    gateway.reviews = lambda number: [{"id": 11, "state": "CHANGES_REQUESTED", "commit_id": head}]
    gateway.review_threads = lambda number: [
        {"id": "T", "isResolved": False, "comments": [{"body": "Fix this"}]}
    ]
    gateway.conclusion = "failure"
    receipt = RepositoryDiscovery(gateway, repo, max_metadata_bytes=metadata_limit).collect(
        remote="inventory", relevant_paths=("src/shared.py",), detailed_prs=None
    )
    assert receipt.github_coverage["identity_observations"] == [
        {"number": 2, "phase": "before_detail", "fields": ["updated_at"], "refreshed": True}
    ]
    metadata = receipt.github_coverage["metadata"]
    assert metadata["observed_bytes"]["pr_enrichment"] > 30_000
    if metadata_limit == 8000:
        assert not receipt.complete and "pr:2:metadata_byte_limit" in receipt.truncated
        assert metadata["retained_bytes"] <= metadata_limit < metadata["observed_total_bytes"]
        assert "reviews" not in receipt.pulls[0]
        assert calls == [2, 2, 2]
    else:
        assert receipt.complete, (receipt.unavailable, receipt.truncated)
        full = receipt.pulls[0]
        assert full["updated_at"] == "2026-11-10T00:00:00Z"
        assert full["labels"] == labels and full["title"] == "Current title"
        assert full["reviews"] == gateway.reviews(2)
        assert full["review_threads"] == gateway.review_threads(2)
        assert full["check_runs"][0]["conclusion"] == "failure"
        assert full["head"]["sha"] == head
        assert calls == [2, 2, 2, 2]  # One classification and one fresh detail boundary, no retry.


def test_pr_clock_change_during_details_still_refuses_mixed_facts(semantic_inventory):
    repo, _, _, _, gateway = semantic_inventory
    gateway.items[1]["body"] = ""
    gateway.prs[2]["body"] = ""
    original = gateway.pull
    calls = []

    def moved(number):
        result = original(number)
        calls.append(number)
        if len(calls) >= 4:
            result["updated_at"] = "2026-11-10T00:00:00Z"
        return result

    gateway.pull = moved
    receipt = RepositoryDiscovery(gateway, repo).collect(
        remote="inventory", relevant_paths=("src/shared.py",), detailed_prs=None
    )
    assert calls == [2, 2, 2, 2]
    assert not receipt.complete
    assert "github:PR identity moved during discovery detail observation" in receipt.unavailable
    assert "reviews" not in receipt.pulls[0]
    assert receipt.github_coverage["identity_observations"] == [
        {"number": 2, "phase": "during_detail", "fields": ["updated_at"], "refreshed": False}
    ]


@pytest.mark.parametrize(
    "field",
    [
        "node_id",
        "url",
        "html_url",
        "head.ref",
        "base.ref",
        "head.repo.id",
        "base.repo.node_id",
        "head.repo.full_name",
    ],
)
def test_native_pull_identity_preserves_available_repository_and_ref_coordinates(
    semantic_inventory, field
):
    import copy

    repo, _, _, _, gateway = semantic_inventory
    before = copy.deepcopy(gateway.prs[2])
    before.update(
        node_id="PR_202",
        url="https://api.github.com/repos/org/repo/pulls/2",
        html_url="https://github.com/org/repo/pull/2",
    )
    for role in ("head", "base"):
        before[role].update(
            ref="work" if role == "head" else "main",
            repo={"id": 1, "node_id": "R_1", "full_name": "org/repo"},
        )
    after = copy.deepcopy(before)
    target = after
    *parents, leaf = field.split(".")
    for parent in parents:
        target = target[parent]
    target[leaf] = 999 if leaf == "id" else "other identity"
    discovery = RepositoryDiscovery(gateway, repo)
    assert discovery._pull_identity_changes(before, after) == (field,)
    assert not discovery._same_pull_identity(before, after)


@pytest.mark.parametrize("value", ["head", "A" * 40, "g" * 40, "a" * 39, "a" * 41])
def test_pr_classification_rejects_non_object_identity_before_api(semantic_inventory, value):
    from bmad_loop.campaign_github import GithubError

    repo, _, base, _, gateway = semantic_inventory
    with pytest.raises(GithubError, match="exact head/base identity"):
        RepositoryDiscovery(gateway, repo)._pull_paths(
            {"number": 2, "head": {"sha": value}, "base": {"sha": base}}
        )
    assert gateway.calls == []


def test_optional_native_decoration_does_not_invalidate_the_same_pr_revision(semantic_inventory):
    repo, _, _, _, gateway = semantic_inventory
    before = dict(gateway.prs[2])
    before.pop("merged")
    after = {**before, "merged": False, "head": {**before["head"], "repo": {"description": "new"}}}
    assert RepositoryDiscovery(gateway, repo)._same_pull_identity(before, after)


def test_scoped_patch_projection_retains_every_file_identity_and_rename_endpoint():
    files = [
        {"filename": "owned/a.py", "status": "modified", "sha": "a", "patch": "+owned"},
        {
            "filename": "outside.py",
            "previous_filename": "owned/old.py",
            "status": "renamed",
            "sha": "b",
            "patch": "rename",
            "blob_url": "https://example.test/b",
        },
        {"filename": "owned-other/a.py", "status": "modified", "sha": "c", "patch": "+outside"},
        {"filename": "owned1.py", "status": "modified", "sha": "d", "patch": "+not-literal-match"},
        {"filename": "owned/image.png", "status": "added", "sha": "e"},
    ]
    result, coverage = RepositoryDiscovery._scoped_file_patches(files, ("owned", "owned[1].py"))
    assert len(result) == len(files)
    for before, after in zip(files, result):
        assert {key: value for key, value in before.items() if key != "patch"} == {
            key: value for key, value in after.items() if key != "patch"
        }
    assert result[:2] == files[:2]
    assert "patch" not in result[2] and "patch" not in result[3]
    assert files[2]["patch"] == "+outside"  # immutable cache input remains intact
    assert coverage["retained_patch_files"] == ["owned/a.py", "outside.py"]
    assert coverage["omitted_patch_files"] == ["owned-other/a.py", "owned1.py"]
    assert coverage["native_patch_unavailable_files"] == ["owned/image.png"]
    assert coverage["omitted_patch_bytes"] == len("+outside+not-literal-match".encode())
    assert coverage["full_code_review"] is False


@pytest.mark.parametrize("problem", ["filename", "rename-source"])
def test_scoped_patch_projection_refuses_incomplete_file_identities(problem):
    from bmad_loop.campaign_github import GithubError

    record = {"filename": "outside.py", "status": "renamed", "patch": "rename"}
    if problem == "filename":
        record.update(filename="", previous_filename="owned/old.py")
    with pytest.raises(GithubError, match="PR file details lack"):
        RepositoryDiscovery._scoped_file_patches([record], ("owned",))


def test_scoped_discovery_omits_only_unowned_patches_and_retains_full_review_facts(
    semantic_inventory,
):
    repo, _, _, _, gateway = semantic_inventory
    gateway.items[1]["body"] = ""
    gateway.prs[2]["body"] = ""
    files = [
        {"filename": "owned.py", "status": "modified", "sha": "a", "patch": "+owned fix"},
        {
            "filename": "unrelated.py",
            "status": "modified",
            "sha": "b",
            "patch": "+large unrelated patch\n" * 1000,
        },
    ]
    gateway.files = lambda number: files
    gateway.review_threads = lambda number: [
        {"id": "T", "comments": [{"body": "Full review finding"}]}
    ]
    scoped = RepositoryDiscovery(gateway, repo, max_detail_bytes=8000).collect(
        remote="inventory", relevant_paths=("owned.py",), detailed_prs=None
    )
    assert scoped.complete, (scoped.unavailable, scoped.truncated)
    detail = scoped.pulls[0]
    assert detail["changed_files_detail"][0] == files[0]
    assert detail["changed_files_detail"][1] == {
        key: value for key, value in files[1].items() if key != "patch"
    }
    assert detail["patch_coverage"]["omitted_patch_files"] == ["unrelated.py"]
    assert detail["reviews"][0]["state"] == "APPROVED"
    assert detail["review_threads"][0]["comments"][0]["body"] == "Full review finding"
    assert detail["check_runs"][0]["conclusion"] == "success"
    broad = RepositoryDiscovery(gateway, repo, max_detail_bytes=8000).collect(
        remote="inventory", detailed_prs=(2,)
    )
    assert not broad.complete and "pr:2:detail" in broad.truncated


def test_scoped_file_metadata_uses_shared_bound_after_native_enrichment(semantic_inventory):
    import json

    repo, _, _, _, gateway = semantic_inventory
    gateway.items[1]["body"] = ""
    gateway.prs[2]["body"] = ""
    files = [
        {
            "filename": "owned.py" if number == 0 else f"other/{number}.py",
            "status": "modified",
            "sha": "a" * 40,
            "blob_url": "https://github.com/org/repo/blob/" + "b" * 350,
            "raw_url": "https://github.com/org/repo/raw/" + "b" * 350,
            "contents_url": "https://api.github.com/repos/org/repo/contents/" + "b" * 350,
            **({"patch": "+selected change"} if number == 0 else {}),
        }
        for number in range(1704)
    ]
    gateway.files = lambda number: files
    original = gateway.pull

    def enriched(number):
        return {**original(number), "mergeability_observation": "native metadata " * 1000}

    gateway.pull = enriched
    metadata_bytes = len(
        json.dumps(
            [{key: value for key, value in item.items() if key != "patch"} for item in files]
        ).encode()
    )
    assert metadata_bytes > 1_000_000
    complete = RepositoryDiscovery(gateway, repo).collect(
        remote="inventory", relevant_paths=("owned.py",), detailed_prs=None, mapped_prs=(2,)
    )
    assert complete.complete, (complete.unavailable, complete.truncated)
    assert complete.pulls[0]["changed_files_detail"] == files
    assert complete.pulls[0]["reviews"][0]["state"] == "APPROVED"
    coverage = complete.github_coverage["metadata"]
    assert coverage["file_metadata_rows"] == 1704
    assert coverage["observed_bytes"]["file_metadata"] == metadata_bytes
    assert coverage["observed_bytes"]["pr_enrichment"] > 10_000
    assert (
        coverage["retained_bytes"]
        == coverage["observed_total_bytes"]
        == sum(coverage["observed_bytes"].values())
    )
    assert complete.github_coverage["details"]["observed_bytes"] < 1_000_000
    limited = RepositoryDiscovery(gateway, repo, max_metadata_bytes=metadata_bytes // 2).collect(
        remote="inventory", relevant_paths=("owned.py",), detailed_prs=None, mapped_prs=(2,)
    )
    assert not limited.complete and "pr:2:metadata_byte_limit" in limited.truncated
    assert limited.github_coverage["metadata"]["retained_bytes"] <= metadata_bytes // 2
    assert limited.github_coverage["metadata"]["observed_total_bytes"] > metadata_bytes // 2
    assert "changed_files_detail" not in limited.pulls[0]


def test_required_scoped_review_findings_still_exceed_original_content_bound(semantic_inventory):
    repo, _, _, _, gateway = semantic_inventory
    gateway.items[1]["body"] = ""
    gateway.prs[2]["body"] = ""
    files = [{"filename": "owned.py", "status": "modified", "sha": "a", "patch": "+fix"}]
    gateway.files = lambda number: files
    gateway.review_threads = lambda number: [
        {"id": "T", "comments": [{"body": "Required finding " * 100000}]}
    ]
    receipt = RepositoryDiscovery(gateway, repo).collect(
        remote="inventory", relevant_paths=("owned.py",), detailed_prs=None
    )
    assert not receipt.complete and "pr:2:detail" in receipt.truncated
    assert receipt.pulls[0]["details_truncated"] is True
    assert receipt.pulls[0]["changed_files_detail"] == [
        {"filename": "owned.py", "status": "modified", "sha": "a"}
    ]
