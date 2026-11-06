"""Same-store selectors share proven private bytes, never canonical writers."""

import base64
import dataclasses
import hashlib
import json
from pathlib import Path

import pytest
from conftest import DEFAULT_TREE, git, write_plan, write_ticket_tree

from bmad_loop import bmadconfig, verify
from bmad_loop.campaign_model import CampaignError, SourceSelector
from bmad_loop.campaign_sources import prepare_source_view, source_paths


@pytest.fixture
def views(project_v7_external_store, tmp_path):
    first = project_v7_external_store
    second = dataclasses.replace(
        first, initiative="second", initiative_folder=first.initiative_folder.parent / "second"
    )
    for layout in (first, second):
        write_ticket_tree(layout, DEFAULT_TREE)
        write_plan(layout, "epic-cart", "story-cart-model-plan", 1, "ready-for-dev")
    workspace = tmp_path / "worker"
    git(first.project, "worktree", "add", "-b", "source-views", str(workspace), "HEAD")
    selectors = tuple(
        SourceSelector("tickets", id=name, initiative=layout.initiative)
        for name, layout in zip(("first", "second"), (first, second), strict=True)
    )
    return first, second, workspace, tmp_path / "authority", selectors


def prepare(case, index=0):
    first, _, workspace, authority, selectors = case
    return prepare_source_view(first.project, workspace, selectors[index], authority_dir=authority)


def test_same_store_two_initiatives_reuse_one_private_copy_and_keep_primary_default(views):
    first, second, workspace, authority, selectors = views
    canonical = (first.project / "_bmad/config.toml").read_bytes()
    initial = prepare(views)
    private_edit = initial.paths.output_folder / "worker-product.md"
    private_edit.write_text("preserve uncommitted private work", encoding="utf-8")
    another = prepare(views, 1)
    assert initial.paths.output_folder == another.paths.output_folder
    assert another.paths.initiative_folder.name == second.initiative
    assert source_paths(workspace, initial.selector).initiative_folder.name == first.initiative
    assert bmadconfig.load_paths(workspace, want_tickets=True).active_initiative == first.initiative
    assert private_edit.read_text() == "preserve uncommitted private work"
    assert not (first.output_folder / private_edit.name).exists()
    assert (first.project / "_bmad/config.toml").read_bytes() == canonical
    assert json.loads((authority / "ticket-store.json").read_text())["state"] == "complete"
    assert not authority.is_relative_to(workspace)


@pytest.mark.parametrize("prepare_explicit_first", [False, True])
def test_implicit_ticket_selector_is_frozen_to_its_actual_initiative(views, prepare_explicit_first):
    first, second, workspace, authority, selectors = views
    if prepare_explicit_first:
        prepare(views, 1)
    implicit = prepare_source_view(
        first.project,
        workspace,
        dataclasses.replace(selectors[0], initiative=""),
        authority_dir=authority,
    )
    prepare(views, 1)
    assert implicit.selector.initiative == first.initiative
    assert implicit.paths.initiative_folder.name == first.initiative
    assert source_paths(workspace, implicit.selector).initiative_folder.name == first.initiative
    assert bmadconfig.load_paths(workspace, want_tickets=True).active_initiative == (
        second.initiative if prepare_explicit_first else first.initiative
    )


@pytest.mark.parametrize("owner_change", ["none", "modify", "delete", "new"])
def test_runtime_planning_preparation_replays_two_same_store_selectors(
    views, tmp_path, monkeypatch, owner_change
):
    from bmad_loop import campaign_runtime
    from bmad_loop.campaign_model import (
        CampaignSnapshot,
        CampaignSpec,
        CampaignUnit,
        OutboxAction,
        UnitSpec,
    )
    from bmad_loop.campaign_store import CampaignStore

    first, second, _, _, selectors = views
    remote = tmp_path / "runtime-code.git"
    git(first.project, "clone", "--bare", str(first.project), str(remote))
    git(first.project, "remote", "add", "origin", str(remote))
    store = CampaignStore(tmp_path / "runtime-state")
    unit = UnitSpec("planning", "planning", "", "Plan both initiatives", kind="planning")
    spec = CampaignSpec(
        "same-store",
        sources=selectors,
        units=(unit,),
        worktree_root=str(tmp_path / "runtime-workers"),
    )
    snapshot = CampaignSnapshot("same-store", "a/b", spec, {unit.id: CampaignUnit(unit)})
    runtime = campaign_runtime.CampaignRuntime(first.project, store)
    action = OutboxAction("allocate", "prepare-unit", unit.id, {}, 1)
    original = campaign_runtime.prepare_source_view
    interrupted = False

    def prepare_then_lose_ack(project, workspace, source, **kwargs):
        nonlocal interrupted
        result = original(project, workspace, source, **kwargs)
        if source.id == selectors[1].id and not interrupted:
            interrupted = True
            (result.paths.output_folder / "private-work.md").write_text("keep private edits")
            if owner_change != "none":
                from bmad_loop.tickettree import TicketTree

                plan = Path(
                    TicketTree(first.project, folder=first.initiative_folder).find("1.1").plan
                )
                if owner_change == "modify":
                    plan.write_text(plan.read_text() + "\nNewer owner requirement\n")
                elif owner_change == "delete":
                    plan.unlink()
                else:
                    (first.output_folder / "new-owner-direction.md").write_text("New direction")
            raise RuntimeError("lost second selector preparation acknowledgement")
        return result

    monkeypatch.setattr(campaign_runtime, "prepare_source_view", prepare_then_lose_ack)
    with pytest.raises(RuntimeError, match="lost second selector"):
        runtime._perform_prepare_unit(snapshot, action)
    if owner_change != "none":
        with pytest.raises(CampaignError, match="changed since private copy capture"):
            runtime._perform_prepare_unit(snapshot, action)
        state_dir = store.directory(snapshot.id) / "allocations" / action.id
        assert not (state_dir / "planning-preimages.json").exists()
        return
    result = runtime._perform_prepare_unit(snapshot, action)
    assert result.state == "confirmed"
    workspace = Path(result.data["workspace"])
    private = workspace / ".bmad-loop/campaign-source"
    state_dir = store.directory(snapshot.id) / "allocations" / action.id
    binding = json.loads((state_dir / "source.json").read_text())
    assert binding["effective_source"]["initiative"] == first.initiative
    assert binding["output_folder"] == str(private)
    assert {row["initiative"] for row in result.data["effective_sources"]} == {
        first.initiative,
        second.initiative,
    }
    assert set(result.data["private_paths"]) == set(binding["private_paths"])
    assert len(result.data["private_paths"]) == len(set(result.data["private_paths"]))
    assert (private / "private-work.md").read_text() == "keep private edits"
    manifest = json.loads(Path(result.data["planning_manifest"]).read_text())
    assert {row["source"] for row in manifest["sources"]} == {row.id for row in selectors}
    assert not (first.output_folder / "private-work.md").exists()
    assert runtime._perform_prepare_unit(snapshot, action).data == result.data


@pytest.mark.parametrize("owner_edit_during_capture", [False, True])
def test_planning_preimages_preserve_exact_copy_bytes_and_never_rebaseline_on_replay(
    views, tmp_path, monkeypatch, owner_edit_during_capture
):
    from bmad_loop.campaign_planning_publication import capture
    from bmad_loop.tickettree import TicketTree

    first, _, workspace, authority, selectors = views
    prepared = prepare(views)
    plan = Path(TicketTree(first.project, folder=first.initiative_folder).find("1.1").plan)
    original = plan.read_bytes()
    changed = original + b"\nNewer owner requirement\n"
    read = Path.read_bytes

    def edit_after_read(path):
        data = read(path)
        if path == plan and data == original:
            path.write_bytes(changed)
        return data

    if owner_edit_during_capture:
        monkeypatch.setattr(Path, "read_bytes", edit_after_read)
    manifest_path = tmp_path / "planning-preimages.json"
    arguments = {
        "project": first.project,
        "workspace": workspace,
        "sources": ((selectors[0], prepared.selector),),
        "source_authority": authority,
    }
    captured = capture(manifest_path, **arguments)
    relative = plan.relative_to(first.output_folder).as_posix()
    assert base64.b64decode(captured["sources"][0]["preimages"][relative]) == original
    if not owner_edit_during_capture:
        plan.write_bytes(changed)
    assert plan.read_bytes() == changed
    assert capture(manifest_path, **arguments) == captured


@pytest.mark.parametrize("fault", ["before-copy", "partial-copy", "after-rename"])
def test_private_source_copy_replays_its_durable_manifest_without_recopying_completed_work(
    views, monkeypatch, fault
):
    from bmad_loop import campaign_closure, campaign_source_views

    first, _, workspace, authority, _ = views
    for name in ("00-one.md", "00-two.md"):
        (first.output_folder / name).write_text(name)
    save, read = campaign_source_views._save, campaign_closure._read
    private = workspace / ".bmad-loop/campaign-source"
    staging = private.with_name("campaign-source-staging")
    triggered = False

    def crashing_save(path, row):
        nonlocal triggered
        if not triggered and fault == "after-rename" and row["state"] == "complete":
            triggered = True
            raise RuntimeError("lost copy completion acknowledgement")
        save(path, row)
        if not triggered and fault == "before-copy" and "staging_identity" not in row:
            triggered = True
            raise RuntimeError("stopped after durable source origin")

    def crashing_read(root, path):
        nonlocal triggered
        if (
            not triggered
            and fault == "partial-copy"
            and root == first.output_folder
            and path.name == "00-two.md"
            and (staging / "00-one.md").exists()
        ):
            triggered = True
            raise RuntimeError("stopped during copied files")
        return read(root, path)

    monkeypatch.setattr(campaign_source_views, "_save", crashing_save)
    monkeypatch.setattr(campaign_closure, "_read", crashing_read)
    with pytest.raises(RuntimeError):
        prepare(views)
    assert triggered and (authority / "ticket-store.json").is_file()
    completed = prepare(views, 1)
    assert (completed.paths.output_folder / "00-two.md").read_text() == "00-two.md"
    retained = completed.paths.output_folder / "00-one.md"
    retained.write_text("actor's edit after complete")
    prepare(views)
    assert retained.read_text() == "actor's edit after complete"


@pytest.mark.parametrize(
    "fault", ["unbound", "origin", "configuration", "redirected", "internal-link"]
)
def test_reuse_refuses_unproven_or_changed_copy_identity(views, monkeypatch, tmp_path, fault):
    from bmad_loop import campaign_sources

    first, _, workspace, authority, selectors = views
    private = workspace / ".bmad-loop/campaign-source"
    if fault == "unbound":
        private.mkdir(parents=True)
        (private / "owner-note.md").write_text("not yours")
    else:
        prepare(views)
    selector = selectors[1]
    if fault == "origin":
        foreign = tmp_path / "foreign-store"
        foreign.mkdir()
        original = campaign_sources.source_paths

        def changed(project, source):
            value = original(project, source)
            return (
                dataclasses.replace(value, output_folder=foreign)
                if project == first.project
                else value
            )

        monkeypatch.setattr(campaign_sources, "source_paths", changed)
    elif fault == "configuration":
        selector = dataclasses.replace(selector, store_branch="other-target")
    elif fault == "redirected":
        preserved = private.with_name("preserved")
        private.rename(preserved)
        private.symlink_to(preserved, target_is_directory=True)
    elif fault == "internal-link":
        (private / "escape").symlink_to(first.output_folder, target_is_directory=True)
    with pytest.raises(CampaignError):
        prepare_source_view(first.project, workspace, selector, authority_dir=authority)
    if fault == "unbound":
        assert (private / "owner-note.md").read_text() == "not yours"


def test_private_source_authority_cannot_be_owned_by_the_planning_actor(views):
    first, _, workspace, _, selectors = views
    with pytest.raises(CampaignError, match="outside the editable workspace"):
        prepare_source_view(
            first.project, workspace, selectors[0], authority_dir=workspace / "authority"
        )
    assert not (workspace / ".bmad-loop/campaign-source").exists()


def test_unacknowledged_staging_allocation_is_preserved_for_manual_reconciliation(
    views, monkeypatch
):
    from bmad_loop import campaign_source_views

    _, _, workspace, _, _ = views
    original = campaign_source_views._save

    def lose_staging_ack(path, row):
        if "staging_identity" in row:
            raise RuntimeError("lost staging identity")
        original(path, row)

    monkeypatch.setattr(campaign_source_views, "_save", lose_staging_ack)
    with pytest.raises(RuntimeError, match="lost staging identity"):
        prepare(views)
    staging = workspace / ".bmad-loop/campaign-source-staging"
    (staging / "owner-note.md").write_text("preserve this uncertainty")
    monkeypatch.setattr(campaign_source_views, "_save", original)
    with pytest.raises(CampaignError, match="preserve it for manual reconciliation"):
        prepare(views)
    assert (staging / "owner-note.md").read_text() == "preserve this uncertainty"


def test_reviewed_source_binding_cannot_publish_another_initiatives_plan(views, tmp_path):
    from bmad_loop.campaign_planning_publication import capture, changes
    from bmad_loop.tickettree import TicketTree

    first, _, workspace, _, selectors = views
    prepared = [prepare(views, index) for index in (0, 1)]
    manifest = capture(
        tmp_path / "manifest.json",
        project=first.project,
        workspace=workspace,
        sources=tuple(zip(selectors, (row.selector for row in prepared), strict=True)),
    )
    other = Path(TicketTree(workspace, folder=prepared[1].paths.initiative_folder).find("1.1").plan)
    relative = other.relative_to(workspace).as_posix()
    with pytest.raises(CampaignError, match="belongs to another initiative"):
        changes(
            manifest,
            workspace,
            selectors[0].id,
            [{"path": relative, "sha256": hashlib.sha256(other.read_bytes()).hexdigest()}],
            {"unit": {"path": relative}},
            {"unit": {"source": selectors[0].id, "selector": "1.1"}},
        )


@pytest.mark.parametrize("owner_edit", [False, True])
def test_two_initiative_planning_artifacts_route_native_publication_and_shared_metadata_once(
    views, tmp_path, owner_edit
):
    from test_campaign_source_publication import remote

    from bmad_loop.campaign_model import UnitSpec
    from bmad_loop.campaign_planning_publication import (
        bind_mirrors,
        capture,
        changes,
        mirror_artifacts,
    )
    from bmad_loop.campaign_source_publication import SourcePublication, SourcePublicationHold
    from bmad_loop.journal import Journal
    from bmad_loop.tickettree import TicketTree

    first, second, workspace, _, selectors = views
    prepared = [prepare(views, index) for index in (0, 1)]
    manifest = capture(
        tmp_path / "preimages.json",
        project=first.project,
        workspace=workspace,
        sources=tuple(zip(selectors, (row.selector for row in prepared), strict=True)),
    )
    bindings, units, artifacts = {}, {}, []
    for source, view in zip(selectors, prepared, strict=True):
        plan = Path(TicketTree(workspace, folder=view.paths.initiative_folder).find("1.1").plan)
        plan.write_text(plan.read_text() + "\nReviewed acceptance for " + source.id + "\n")
        relative = plan.relative_to(workspace).as_posix()
        units[source.id] = {"source": source.id, "selector": "1.1"}
        bindings[source.id] = {"path": relative}
        artifacts.append(
            {"path": relative, "sha256": hashlib.sha256(plan.read_bytes()).hexdigest()}
        )
    shared = prepared[0].paths.output_folder / "project-context.md"
    shared.write_text("Reviewed shared context")
    artifacts.append(
        {
            "path": shared.relative_to(workspace).as_posix(),
            "sha256": hashlib.sha256(shared.read_bytes()).hexdigest(),
        }
    )
    paths, mirrors = mirror_artifacts(workspace, "planning", [row["path"] for row in artifacts])
    verify.commit_paths(workspace, "reviewed private plans", paths)
    receipt = {
        "head": verify.rev_parse_head(workspace),
        "artifact_mirrors": mirrors,
        "validated": {"artifacts": artifacts},
    }
    bind_mirrors(workspace, receipt)
    remote(first.project, tmp_path / "code.git", "source-code")
    git(workspace, "push", "source-code", "HEAD:refs/heads/main")
    # The external store has its own actual remote delivery, too.
    git(first.output_folder, "add", "-A")
    git(first.output_folder, "commit", "--allow-empty", "-m", "initial ticket sources")
    remote(first.output_folder, tmp_path / "store.git", "origin")
    publisher = SourcePublication(first.project, tmp_path / "publication", remote="source-code")
    merge = {"head": receipt["head"], "merged_commit": receipt["head"], "pr_number": 18}
    acceptance = {
        "head": receipt["head"],
        "verdict": "approved",
        "review_id": "independent-review",
        "graph_revision": 1,
        "config_revision": 1,
    }
    if owner_edit:
        plan = Path(TicketTree(first.project, folder=first.initiative_folder).find("1.1").plan)
        plan.write_text(plan.read_text() + "\nNewer owner direction\n")
        authored = changes(manifest, workspace, selectors[0].id, artifacts, bindings, units)
        with pytest.raises(SourcePublicationHold, match="edited after capture"):
            publisher.publish_artifacts(
                "planning:first",
                selectors[0],
                UnitSpec("planning", selectors[0].id, "", "Plan", kind="planning"),
                authored,
                merge=merge,
                acceptance=acceptance,
                journal=Journal(tmp_path / "journal"),
                store_remote="origin",
                store_branch="main",
            )
        assert "Newer owner direction" in plan.read_text()
        assert not (first.output_folder / "project-context.md").exists()
        return
    all_paths = []
    for index, source in enumerate(selectors):
        authored = changes(manifest, workspace, source.id, artifacts, bindings, units)
        assert len(authored) == (2 if index == 0 else 1)
        assert [row.selector for row in authored if row.selector] == ["1.1"]
        all_paths.extend(row.path for row in authored)
        result = publisher.publish_artifacts(
            "planning:" + source.id,
            source,
            UnitSpec("planning", source.id, "", "Plan", kind="planning"),
            authored,
            merge=merge,
            acceptance=acceptance,
            journal=Journal(tmp_path / "journal"),
            store_remote="origin",
            store_branch="main",
        )
        assert result.state == "confirmed"
        assert (
            publisher.publish_artifacts(
                "planning:" + source.id,
                source,
                UnitSpec("planning", source.id, "", "Plan", kind="planning"),
                authored,
                merge=merge,
                acceptance=acceptance,
                journal=Journal(tmp_path / "journal"),
                store_remote="origin",
                store_branch="main",
            ).state
            == "confirmed"
        )
    assert len(all_paths) == len(set(all_paths)) == 3
    for layout in (first, second):
        plan = Path(TicketTree(first.project, folder=layout.initiative_folder).find("1.1").plan)
        assert "Reviewed acceptance" in plan.read_text()
