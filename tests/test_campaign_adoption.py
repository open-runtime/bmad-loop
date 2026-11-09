"""Explicit adoption preserves native PR identity and requires fresh real fences."""

from __future__ import annotations

import dataclasses
import json
import subprocess
import time
import types

import pytest
from conftest import git

from bmad_loop import campaign_adoption as adoption
from bmad_loop.campaign_github import ApiResponse, GithubGateway
from bmad_loop.campaign_migration import MigrationItem, ServiceInventory
from bmad_loop.campaign_model import (
    CampaignSnapshot,
    CampaignSpec,
    CampaignUnit,
    GitHubSpec,
    SourceSelector,
    UnitSpec,
)


@pytest.fixture
def case(project, tmp_path):
    repo = project.project
    config = repo / "_bmad" / "bmm" / "config.yaml"
    config.parent.mkdir(parents=True)
    config.write_text(
        "output_folder: _bmad-output\nimplementation_artifacts: _bmad-output/implementation-artifacts\nplanning_artifacts: _bmad-output/planning-artifacts\n",
        encoding="utf-8",
    )
    story = repo / "specs" / "stories" / "u-story.md"
    story.parent.mkdir(parents=True)
    story.write_text(
        "---\nstatus: ready-for-dev\n---\n# U\nAccepted source intent.\n", encoding="utf-8"
    )
    (repo / "specs" / "stories.yaml").write_text(
        "- id: u\n  title: U\n  description: Accepted source intent.\n", encoding="utf-8"
    )
    git(repo, "add", "_bmad", "specs")
    git(repo, "commit", "-qm", "native adoption source")
    base = git(repo, "rev-parse", "HEAD")
    remote = tmp_path / "remote.git"
    subprocess.run(
        ["git", "clone", "--bare", str(repo), str(remote)], check=True, capture_output=True
    )
    git(repo, "remote", "add", "origin", str(remote))
    root = tmp_path / "workers"
    root.mkdir()
    workspace = root / "legacy"
    git(repo, "worktree", "add", "-b", "legacy", str(workspace), base)
    (workspace / "src.txt").write_text("partial preserved work\n", encoding="utf-8")
    git(workspace, "add", "src.txt")
    git(workspace, "commit", "-qm", "preserved external implementation")
    head = git(workspace, "rev-parse", "HEAD")
    git(workspace, "push", "origin", "legacy")
    unit = UnitSpec(
        "source:project:u",
        "source",
        "epic",
        "U",
        selector="u",
        acceptance=("Accepted source intent.",),
        scopes=("src.txt",),
    )
    spec = CampaignSpec(
        "adoption",
        sources=(SourceSelector("stories", id="source", path="specs"),),
        units=(unit,),
        worktree_root=str(root),
        github=GitHubSpec(repository="a/b"),
    )
    snapshot = CampaignSnapshot(
        "campaign",
        "a/b",
        spec,
        {unit.id: CampaignUnit(unit)},
        common_dir=str(adoption.common(repo)),
        root=str(repo),
        remote_url=str(remote),
    )
    _, _, source_identity = adoption.source_binding(repo, snapshot, unit.id)
    manifest = adoption.AdoptionManifest(
        1,
        snapshot.id,
        unit.id,
        "a/b",
        snapshot.common_dir,
        7,
        "PR-7",
        "legacy",
        head,
        base,
        "main",
        source_identity,
        "actor-7",
        "actor-v1",
        "binding-v1",
        "fence-v1",
        "explicit-owner-command",
        workspace=str(workspace),
    )
    details = {
        "repository": "a/b",
        "pr_number": 7,
        "pr_node_id": "PR-7",
        "head": head,
        "base": base,
        "branch": "legacy",
        "source_unit": unit.id,
        "source_identity": source_identity,
        "binding_revision": "binding-v1",
        "fence_revision": "fence-v1",
        "workspace": str(workspace),
        "preserved_clean": True,
        "resumable_binding_verified": True,
        "admissions_open": False,
        "active_workers": 0,
    }
    actor = MigrationItem(
        "actor-7",
        "workspace",
        "actor-v1",
        "trusted-service",
        "stopped",
        "dead",
        "unowned",
        (unit.id,),
        details,
    )
    service = ServiceInventory("trusted-service", snapshot.common_dir, 100, "service-v1", (actor,))
    provider = adoption.ProviderIdentity("handoff", "trusted:observer", "trusted", "1", "a" * 64)
    pr = {
        "id": 707,
        "node_id": "PR-7",
        "number": 7,
        "draft": False,
        "state": "open",
        "merged": False,
        "updated_at": "v1",
        "head": {"ref": "legacy", "sha": head, "repo": {"full_name": "a/b"}},
        "base": {"ref": "main", "sha": base, "repo": {"full_name": "a/b"}},
        "html_url": "https://github.com/a/b/pull/7",
    }
    value = types.SimpleNamespace(
        repo=repo,
        remote=remote,
        workspace=workspace,
        root=root,
        snapshot=snapshot,
        unit=unit,
        manifest=manifest,
        service=service,
        provider=provider,
        pr=pr,
    )
    value.resolver = lambda name, **kwargs: (value.provider, lambda *args: value.service)
    value.gateway = GithubGateway(
        types.SimpleNamespace(
            request=lambda method, path, payload=None: ApiResponse(200, value.pr, {})
        ),
        "a/b",
    )
    return value


def qualify(case):
    return adoption.qualify_pr(
        case.repo,
        case.snapshot,
        case.manifest,
        case.provider,
        case.gateway,
        resolver=case.resolver,
        now=100,
    )


def test_exact_fenced_clean_pr_is_qualified_and_native_draft_is_first_mutation(case):
    before = git(case.workspace, "rev-parse", "HEAD")
    qualified = qualify(case)
    assert qualified.head == before
    intent = adoption.draft_intent(
        case.snapshot, case.manifest, qualified, case.gateway, action_id="adopt-draft"
    )
    assert intent.kind == "draft" and intent.coordinates["head"] == before
    assert git(case.workspace, "rev-parse", "HEAD") == before
    case.pr["draft"] = True
    assert (
        adoption.draft_intent(
            case.snapshot, case.manifest, qualify(case), case.gateway, action_id="adopt-draft"
        )
        is None
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("admissions_open", True),
        ("active_workers", 1),
        ("fence_revision", ""),
        ("source_identity", "0" * 64),
        ("resumable_binding_verified", False),
        ("head", "0" * 40),
    ],
)
def test_manifest_or_archive_words_cannot_replace_authenticated_handoff(case, field, value):
    actor = case.service.items[0]
    case.service = dataclasses.replace(
        case.service, items=(dataclasses.replace(actor, details={**actor.details, field: value}),)
    )
    with pytest.raises(adoption.AdoptionError, match="external handoff"):
        qualify(case)


def test_different_installed_provider_version_invalidates_pending_adoption(case):
    case.provider = dataclasses.replace(case.provider, version="2")
    original = dataclasses.replace(case.provider, version="1")
    with pytest.raises(adoption.AdoptionError, match="installation changed"):
        adoption.qualify_pr(
            case.repo,
            case.snapshot,
            case.manifest,
            original,
            case.gateway,
            resolver=case.resolver,
            now=100,
        )


def test_unknown_human_or_scheduler_overlapping_actor_prevents_second_worker(case):
    competitor = MigrationItem(
        "human",
        "process",
        "human-v1",
        "trusted-service",
        "running",
        "unknown",
        "owned",
        (case.unit.id,),
        {},
    )
    case.service = dataclasses.replace(case.service, items=(*case.service.items, competitor))
    with pytest.raises(adoption.AdoptionError, match="another active, human or uncertain"):
        qualify(case)


def test_source_or_remote_branch_change_invalidates_owner_selection(case):
    path = case.repo / "specs" / "stories" / "u-story.md"
    path.write_text(path.read_text(encoding="utf-8") + "New owner acceptance", encoding="utf-8")
    with pytest.raises(adoption.AdoptionError, match="source/acceptance contract changed"):
        qualify(case)


@pytest.mark.parametrize(
    "change",
    [
        {"merged": True},
        {"state": "closed"},
        {"node_id": "other"},
        {"head": {"ref": "legacy", "sha": "0" * 40, "repo": {"full_name": "other/repo"}}},
    ],
)
def test_native_pr_identity_cannot_change_or_borrow_cross_repo_branch(case, change):
    case.pr.update(change)
    with pytest.raises(adoption.AdoptionError, match="native PR"):
        qualify(case)


def test_existing_dirty_worktree_is_preserved_and_refused(case):
    path = case.workspace / "src.txt"
    path.write_text("human uncommitted edits", encoding="utf-8")
    with pytest.raises(adoption.AdoptionError, match="foreign, dirty"):
        qualify(case)
    assert path.read_text(encoding="utf-8") == "human uncommitted edits"


def test_manifest_cannot_select_code_or_skip_exact_source_binding(case, tmp_path):
    path = tmp_path / "adopt.json"
    path.write_text(
        json.dumps({**dataclasses.asdict(case.manifest), "observer": "arbitrary.module"}),
        encoding="utf-8",
    )
    with pytest.raises(adoption.AdoptionError, match="unsupported fields"):
        adoption.read_manifest(path)


@pytest.mark.parametrize("legacy", [False, True])
def test_explicit_prepare_converts_draft_preserves_history_and_pushes_same_branch(
    case, tmp_path, legacy
):
    from bmad_loop.campaign_runtime import CampaignRuntime
    from bmad_loop.campaign_store import CampaignStore, add_action

    store = CampaignStore(tmp_path / "state")
    snapshot = store.create(case.snapshot)
    runtime = CampaignRuntime(
        case.repo, store, gateway=case.gateway, adoption_resolver=case.resolver
    )
    case.service = dataclasses.replace(case.service, observed_at=time.time())
    path = tmp_path / "adopt.json"
    path.write_text(json.dumps(dataclasses.asdict(case.manifest)), encoding="utf-8")
    payload = adoption.prepare_request(
        case.repo,
        snapshot,
        case.unit.id,
        path,
        "handoff",
        gateway=case.gateway,
        resolver=case.resolver,
    )
    adoption.begin(snapshot, case.unit.id, payload, command_id="owner-adopt")
    case.pr["draft"] = True  # Native gateway ACK precedes this durable stage.
    snapshot.units[case.unit.id].bindings["adoption"]["state"] = "draft_confirmed"
    action = add_action(snapshot, "prepare-unit", case.unit.id, {"adoption": True})
    result = adoption.prepare(runtime, snapshot, action)
    assert result["pr"]["number"] == 7 and result["workspace"] == str(case.workspace)
    assert result["last_pushed_head"] == result["remote_head_anchor"] == result["head"]
    assert git(case.remote, "rev-parse", "refs/heads/legacy") == result["head"]
    assert (
        git(case.workspace, "merge-base", case.manifest.head, result["head"]) == case.manifest.head
    )
    assert (case.workspace / "src.txt").read_text(encoding="utf-8") == "partial preserved work\n"
    case.pr["head"]["sha"] = result["head"]
    assert adoption.prepare(runtime, snapshot, action) == result
    if legacy:
        # The real confirmed push already exists. Serialize the historical
        # pre-anchor result, archive that action, then exercise normal discovery.
        from bmad_loop import campaign_remote_head
        from bmad_loop.campaign_supervisor import Observation

        old = {
            k: v for k, v in result.items() if k not in {"last_pushed_head", "remote_head_anchor"}
        }
        action.state, action.receipt = "confirmed", old
        runtime.apply(snapshot, action, Observation("confirmed", old))
        snapshot = store.save(snapshot, snapshot.revision)
        later = add_action(snapshot, "prepare-unit", case.unit.id, {})
        later.state = "rejected"
        snapshot = store.save(snapshot, snapshot.revision)
        assert action.id not in {row.id for row in snapshot.outbox}
        unit = snapshot.units[case.unit.id]
        assert "remote_head_anchor" not in unit.bindings
        observed = campaign_remote_head.observe(runtime, snapshot, unit)
        assert observed["recovered_remote_head_anchor"] == result["head"]
        campaign_remote_head.apply(runtime, snapshot, unit, observed)
        assert unit.bindings["remote_head_anchor"] == result["head"]


def test_authenticated_remote_adoption_creates_worktree_under_shared_metadata_lock(
    case, tmp_path, monkeypatch
):
    from contextlib import contextmanager

    from bmad_loop import campaign_workers
    from bmad_loop.campaign_runtime import CampaignRuntime
    from bmad_loop.campaign_store import CampaignStore, add_action

    # This sandbox actor has relinquished its registered tree; its original
    # branch and every preserved commit remain intact for the authenticated PR.
    git(case.repo, "worktree", "remove", str(case.workspace))
    case.manifest = dataclasses.replace(case.manifest, workspace="")
    actor = case.service.items[0]
    actor = dataclasses.replace(actor, details={**actor.details, "workspace": ""})
    case.service = dataclasses.replace(case.service, items=(actor,), observed_at=time.time())
    store = CampaignStore(tmp_path / "state")
    snapshot = store.create(case.snapshot)
    runtime = CampaignRuntime(
        case.repo, store, gateway=case.gateway, adoption_resolver=case.resolver
    )
    path = tmp_path / "remote-adopt.json"
    path.write_text(json.dumps(dataclasses.asdict(case.manifest)), encoding="utf-8")
    payload = adoption.prepare_request(
        case.repo,
        snapshot,
        case.unit.id,
        path,
        "handoff",
        gateway=case.gateway,
        resolver=case.resolver,
    )
    adoption.begin(snapshot, case.unit.id, payload, command_id="owner-adopt")
    case.pr["draft"] = True
    snapshot.units[case.unit.id].bindings["adoption"]["state"] = "draft_confirmed"
    action = add_action(snapshot, "prepare-unit", case.unit.id, {"adoption": True})
    original_lock = campaign_workers.worktree_metadata_lock
    original_git = adoption._git
    state, calls = {"held": False}, []

    @contextmanager
    def observed_lock(repo):
        with original_lock(repo) as common:
            state["held"] = True
            try:
                yield common
            finally:
                state["held"] = False

    def observed_git(repo, *args):
        if args[:2] == ("worktree", "add") or (args and args[0] == "fetch"):
            calls.append((args[0], state["held"]))
        return original_git(repo, *args)

    monkeypatch.setattr(campaign_workers, "worktree_metadata_lock", observed_lock)
    monkeypatch.setattr(adoption, "_git", observed_git)
    result = adoption.prepare(runtime, snapshot, action)
    assert ("worktree", True) in calls and ("fetch", False) in calls
    assert not any(held for kind, held in calls if kind == "fetch")
    assert git(case.repo, "merge-base", case.manifest.head, result["head"]) == case.manifest.head


def test_selected_installed_observer_identity_pins_distribution_version_and_module_hash(
    case, monkeypatch
):
    def provider(*args):
        return case.service

    dist = types.SimpleNamespace(metadata={"Name": "owner-handoff"}, version="1.2.3")
    entry = types.SimpleNamespace(
        name="owner-handoff", value="installed_owner:observe", dist=dist, load=lambda: provider
    )
    monkeypatch.setattr(adoption.importlib.metadata, "entry_points", lambda **kwargs: (entry,))
    identity, resolved = adoption.load_provider("owner-handoff")
    assert resolved is provider and identity.distribution == "owner-handoff"
    assert identity.version == "1.2.3" and len(identity.module_sha256) == 64
    dist.version = "2.0.0"
    assert adoption.load_provider("owner-handoff")[0] != identity
    with pytest.raises(adoption.AdoptionError, match="missing or ambiguous"):
        adoption.load_provider("not-installed")


def test_existing_unit_with_actual_implementation_cannot_be_silently_retired(case):
    unit = case.snapshot.units[case.unit.id]
    unit.state = adoption.UnitState.READY
    unit.bindings.update(
        workspace=str(case.workspace),
        branch="legacy",
        head=case.manifest.head,
        base=case.manifest.base,
        pr={"number": 99},
        spec_path="docs/bmad-campaign/provisional.md",
    )
    with pytest.raises(adoption.AdoptionError, match="actual work beyond"):
        adoption.provisional(case.repo, case.snapshot, case.unit.id)
    assert (case.workspace / "src.txt").read_text(encoding="utf-8") == "partial preserved work\n"
