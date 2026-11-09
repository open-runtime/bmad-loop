"""Import real source fixtures and isolate stores before starting campaign work."""

from dataclasses import asdict, replace

import pytest
from conftest import (
    DEFAULT_TREE,
    git,
    install_bmad_config,
    write_plan,
    write_sprint,
    write_ticket_tree,
)
from test_stories_engine import SPEC_FOLDER, setup_stories

from bmad_loop import bmadconfig, sprintstatus, verify
from bmad_loop.campaign_model import CampaignError, CampaignSpec, SourceSelector, UnitSpec
from bmad_loop.campaign_sources import (
    import_sources,
    prepare_private_source,
    publish_status,
    qualified,
)
from bmad_loop.journal import Journal


@pytest.fixture(autouse=True)
def configure_v6_if_requested(request):
    if "project" in request.fixturenames:
        install_bmad_config(request.getfixturevalue("project"))


def spec(source):
    return CampaignSpec("sources", sources=(source,))


def test_worker_story_grants_only_its_resolved_lifecycle_file(project):
    from bmad_loop.campaign_sources import worker_story_write_paths

    setup_stories(project, [{"id": "first", "title": "First", "description": "First"}])
    folder = project.project / SPEC_FOLDER / "stories"
    folder.mkdir(exist_ok=True)
    leaf = folder / "first-story.md"
    leaf.write_text("---\nstatus: ready-for-dev\n---\n")
    source = SourceSelector("stories", id="spec", path=SPEC_FOLDER)
    unit = UnitSpec("first", source.id, "epic", "First", "first", scopes=("src",))
    assert worker_story_write_paths(project.project, source, unit) == (leaf.resolve(),)
    assert (
        worker_story_write_paths(project.project, source, replace(unit, selector="missing")) == ()
    )
    (folder / "first-other.md").write_text(leaf.read_text())
    with pytest.raises(CampaignError, match="ambiguous"):
        worker_story_write_paths(project.project, source, unit)


def test_worker_story_grant_refuses_redirected_source(project, tmp_path):
    from bmad_loop.campaign_sources import worker_story_write_paths

    setup_stories(project, [{"id": "first", "title": "First", "description": "First"}])
    folder = project.project / SPEC_FOLDER / "stories"
    folder.mkdir(exist_ok=True)
    outside = tmp_path / "outside.md"
    outside.write_text("---\nstatus: ready-for-dev\n---\n")
    (folder / "first-story.md").symlink_to(outside)
    source = SourceSelector("stories", id="spec", path=SPEC_FOLDER)
    unit = UnitSpec("first", source.id, "epic", "First", "first")
    with pytest.raises(CampaignError, match="redirects"):
        worker_story_write_paths(project.project, source, unit)


def test_sprint_import_keeps_selected_order_and_does_not_depend_on_unselected_rows(project):
    write_sprint(
        project,
        {"1-1-first": "ready-for-dev", "1-2-skip": "ready-for-dev", "1-3-last": "ready-for-dev"},
    )
    source = SourceSelector("sprint-status", id="sprint", units=("1-1-first", "1-3-last"))
    imported = import_sources(project.project, spec(source))
    assert [unit.selector for unit in imported.units] == ["1-1-first", "1-3-last"]
    assert imported.units[0].after == ()
    assert imported.units[1].after == (imported.units[0].id,)
    assert imported.units[0].epic == "1"
    assert imported.observations[imported.units[0].id]["status"] == "ready-for-dev"


def test_sprint_selecting_one_later_story_has_no_missing_implicit_predecessor(project):
    write_sprint(project, {"1-1-first": "done", "2-1-selected": "ready-for-dev"})
    source = SourceSelector("sprint-status", id="sprint", epics=("2",))
    imported = import_sources(project.project, spec(source))
    assert len(imported.units) == 1
    assert imported.units[0].after == ()


def test_stories_import_preserves_serial_selection_and_checkpoint_flags(project):
    setup_stories(
        project,
        [
            {
                "id": "first",
                "title": "First",
                "description": "Implement first",
                "spec_checkpoint": True,
            },
            {"id": "skip", "title": "Skip", "description": "Do not select"},
            {
                "id": "last",
                "title": "Last",
                "description": "Implement last",
                "done_checkpoint": True,
            },
        ],
    )
    source = SourceSelector("stories", id="spec", path=SPEC_FOLDER, units=("first", "last"))
    imported = import_sources(project.project, spec(source))
    assert [unit.selector for unit in imported.units] == ["first", "last"]
    assert imported.units[1].after == (imported.units[0].id,)
    assert imported.units[0].acceptance == ("Implement first",)
    assert imported.observations[imported.units[0].id]["plan_checkpoint"] is True
    assert imported.observations[imported.units[1].id]["done_checkpoint"] is True
    independent = import_sources(project.project, spec(replace(source, independent=True)))
    assert all(not unit.after for unit in independent.units)


def test_ticket_import_uses_actual_native_ids_dependencies_hitl_and_closure(project_v7):
    write_ticket_tree(project_v7, DEFAULT_TREE)
    source = SourceSelector("tickets", id="tickets", initiative=project_v7.initiative)
    imported = import_sources(project_v7.project, spec(source))
    indexed = {unit.id: unit for unit in imported.units}
    first, second = qualified(source, "1.1"), qualified(source, "1.2")
    closure = qualified(source, "closure/epic-cart")
    assert indexed[first].selector == "1.1"
    assert indexed[first].epic == "epic-cart"
    assert first in indexed[second].after
    assert indexed[closure].kind == "closure"
    assert set(indexed[closure].after) == {first, second}
    assert closure in indexed[qualified(source, "2.1")].after
    assert imported.observations[second]["hitl"] is True
    assert imported.observations[first]["plan_checkpoint"] is True
    assert imported.observations[closure]["all_children_selected"] is True


@pytest.mark.parametrize("explicit", [False, True])
def test_ticket_import_freezes_effective_initiative_without_renaming_units(project_v7, explicit):
    write_ticket_tree(project_v7, DEFAULT_TREE)
    source = SourceSelector(
        "tickets", id="tickets", initiative=project_v7.initiative if explicit else ""
    )
    imported = import_sources(project_v7.project, spec(source))
    unit_id = qualified(source, "1.1")
    unit = next(unit for unit in imported.units if unit.id == unit_id)
    assert unit.id == f"tickets:{project_v7.initiative if explicit else 'project'}:1.1"
    assert unit.initiative == source.initiative
    assert imported.observations[unit_id]["effective_initiative"] == project_v7.initiative


@pytest.mark.parametrize(
    "observation_version",
    ["current", "old-prepared", "wrong-kind", "wrong-id", "conflicting-initiative"],
)
def test_default_ticket_import_reuses_exact_native_mirror_before_hierarchy_create(
    project_v7, tmp_path, observation_version
):
    from test_campaign_tracking import HierarchyTransport

    from bmad_loop.campaign_github import GithubError, GithubGateway
    from bmad_loop.campaign_model import CampaignSnapshot, CampaignUnit
    from bmad_loop.campaign_sources import prepare_source_view
    from bmad_loop.campaign_tracking import HierarchyCoordinator

    write_ticket_tree(project_v7, DEFAULT_TREE)
    source = SourceSelector(
        "tickets", id="tickets", units=("1.1",), completion_scope="selected-units"
    )
    campaign = spec(source)
    imported = import_sources(project_v7.project, campaign)
    native = imported.units[0]
    assert native.id == "tickets:project:1.1" and native.initiative == ""
    snapshot = CampaignSnapshot(
        "campaign", "a/b", campaign, {native.id: CampaignUnit(native)}, generation=1
    )
    transport = HierarchyTransport()
    milestone = {"id": 9007, "number": 7, "title": "Native initiative", "state": "open"}
    transport.milestones[7] = milestone
    transport.issues[10] = {
        "id": 1010,
        "number": 10,
        "html_url": "https://github.com/a/b/issues/10",
        "state": "open",
        "milestone": milestone,
    }
    body = f"Owner content\n<!-- github-mirror:issue:{project_v7.initiative}/1.1 -->"
    transport.issues[529] = {
        "id": 1529,
        "node_id": "native-529",
        "number": 529,
        "html_url": "https://github.com/a/b/issues/529",
        "repository_url": "https://api.github.com/repos/a/b",
        "state": "open",
        "body": body,
        "milestone": milestone,
    }
    transport.parents[529] = 10
    coordinator = HierarchyCoordinator(GithubGateway(transport, "a/b"))
    unit = snapshot.units[native.id]
    if observation_version != "current":
        if observation_version == "old-prepared":
            imported.observations[native.id].pop("effective_initiative")
        workspace = tmp_path / "prepared-worker"
        git(project_v7.project, "worktree", "add", "-b", "prepared-worker", str(workspace), "HEAD")
        canonical_config = {
            path: path.read_bytes() for path in (project_v7.project / "_bmad").rglob("*.toml")
        }
        prepared = prepare_source_view(project_v7.project, workspace, source)
        assert prepared.selector.initiative == project_v7.initiative
        assert all(path.read_bytes() == raw for path, raw in canonical_config.items())
        unit.bindings["effective_source"] = asdict(prepared.selector)
    if observation_version in {"wrong-kind", "wrong-id", "conflicting-initiative"}:
        key, value = {
            "wrong-kind": ("kind", "stories"),
            "wrong-id": ("id", "other-source"),
            "conflicting-initiative": ("initiative", "other-initiative"),
        }[observation_version]
        unit.bindings["effective_source"][key] = value
        with pytest.raises(GithubError, match="source identity|prepared source"):
            coordinator.step(snapshot, native.id, source_metadata=imported.observations)
        assert transport.effects == [] and transport.issues[529]["body"] == body
        return
    receipts = {}
    for _ in range(10):
        step = coordinator.step(
            snapshot, native.id, source_metadata=imported.observations, receipts=receipts
        )
        snapshot.bindings["github_hierarchy"] = step.bindings
        unit.bindings = step.unit_bindings
        if step.intent:
            assert (
                step.intent.state == "confirmed"
            ), "Existing native mirror must precede any create"
            receipts[step.action_key] = step.intent
        if step.complete:
            break
    assert step.complete
    assert unit.bindings["issue"]["number"] == 529
    assert unit.bindings["epic_issue"]["number"] == 10
    assert unit.bindings["milestone"]["number"] == 7
    assert "initiative:tickets:sources" in step.bindings  # Existing namespace stays stable.
    assert unit.spec == native
    assert transport.effects == [] and transport.issues[529]["body"] == body


def test_partial_ticket_epic_does_not_claim_whole_epic_closure(project_v7):
    write_ticket_tree(project_v7, DEFAULT_TREE)
    source = SourceSelector(
        "tickets", id="tickets", initiative=project_v7.initiative, units=("1.1",)
    )
    imported = import_sources(project_v7.project, spec(source))
    closure = qualified(source, "closure/epic-cart")
    assert imported.observations[closure]["all_children_selected"] is False
    assert len(imported.units) == 2


def test_explicit_missing_ticket_dependencies_fail_before_mutation(project_v7):
    write_ticket_tree(project_v7, DEFAULT_TREE)
    source = SourceSelector(
        "tickets", id="tickets", initiative=project_v7.initiative, units=("1.2",)
    )
    before = verify.rev_parse_head(project_v7.project)
    with pytest.raises(CampaignError, match="dependencies outside selected graph"):
        import_sources(project_v7.project, spec(source))
    assert verify.rev_parse_head(project_v7.project) == before


def test_canonical_done_prerequisite_outside_selection_gets_own_reconciliation_unit(project_v7):
    write_ticket_tree(project_v7, DEFAULT_TREE)
    write_plan(project_v7, "epic-cart", "story-cart-model-plan", 1, "done")
    source = SourceSelector(
        "tickets", id="tickets", initiative=project_v7.initiative, units=("1.2",)
    )
    imported = import_sources(project_v7.project, spec(source))
    first = qualified(source, "1.1")
    second = qualified(source, "1.2")
    units = {unit.id: unit for unit in imported.units}
    assert first in units and first in units[second].after
    assert imported.observations[first]["state"] == "done"
    assert imported.observations[first]["dependency_reconciliation"] is True


def test_explicit_unit_graph_needs_no_dummy_source(project):
    unit = UnitSpec("custom:initiative:a", "custom", "epic", "A", selector="a")
    imported = import_sources(project.project, CampaignSpec("explicit", units=(unit,)))
    assert imported.units == (unit,)
    assert imported.observations == {}


def test_shared_external_ticket_store_is_copied_and_worker_only_redirected(
    project_v7_external_store, tmp_path
):
    layout = project_v7_external_store
    write_ticket_tree(layout, DEFAULT_TREE)
    workspace = tmp_path / "worker"
    git(layout.project, "worktree", "add", "-b", "worker-private", str(workspace), "HEAD")
    source = SourceSelector("tickets", initiative=layout.initiative, id="tickets")
    canonical_before = bmadconfig.load_paths(layout.project, want_tickets=True)
    paths = prepare_private_source(layout.project, workspace, source)
    assert paths.output_folder.is_relative_to(workspace)
    assert paths.output_folder != canonical_before.output_folder
    assert paths.initiative_folder.is_dir()
    assert not (paths.output_folder / ".git").exists()
    assert (
        bmadconfig.load_paths(layout.project, want_tickets=True).output_folder
        == canonical_before.output_folder
    )
    plan = paths.initiative_folder / "worker-only.txt"
    plan.write_text("private change")
    assert not (canonical_before.initiative_folder / plan.name).exists()


def test_ignored_nested_store_is_also_copied_not_missing_in_worker(project_v7_store_repo, tmp_path):
    layout = project_v7_store_repo
    write_ticket_tree(layout, DEFAULT_TREE)
    workspace = tmp_path / "worker"
    git(layout.project, "worktree", "add", "-b", "worker-nested-store", str(workspace), "HEAD")
    source = SourceSelector("tickets", initiative=layout.initiative, id="tickets")
    paths = prepare_private_source(layout.project, workspace, source)
    assert paths.output_folder.is_relative_to(workspace)
    assert paths.initiative_folder.is_dir()
    assert not (paths.output_folder / ".git").exists()
    assert (
        bmadconfig.load_paths(layout.project, want_tickets=True).output_folder
        == layout.output_folder
    )


def test_tracked_ticket_store_stays_on_worker_branch(project_v7, tmp_path):
    write_ticket_tree(project_v7, DEFAULT_TREE)
    git(project_v7.project, "add", "-A")
    git(project_v7.project, "commit", "--allow-empty", "-qm", "fixture tree")
    workspace = tmp_path / "worker"
    git(project_v7.project, "worktree", "add", "-b", "worker-tracked", str(workspace), "HEAD")
    source = SourceSelector("tickets", initiative=project_v7.initiative, id="tickets")
    paths = prepare_private_source(project_v7.project, workspace, source)
    assert paths.output_folder == workspace / "_bmad-output"
    assert paths.initiative_folder.is_dir()
    assert (workspace / ".bmad-loop/campaign-source").exists() is False


def test_publication_requires_remote_merge_and_preserves_other_rows(project, tmp_path):
    write_sprint(project, {"1-1-one": "ready-for-dev", "1-2-other": "in-progress"})
    source = SourceSelector("sprint-status", id="sprint")
    unit = UnitSpec("sprint:project:1-1-one", "sprint", "1", "One", selector="1-1-one")
    journal = Journal(tmp_path / "journal")
    before = (
        project.sprint_status.read_bytes()
        if hasattr(project, "sprint_status")
        else (project.implementation_artifacts / "sprint-status.yaml").read_bytes()
    )
    with pytest.raises(CampaignError, match="confirmed remote merge"):
        publish_status(
            project.project,
            source,
            unit,
            journal=journal,
            expected_status="ready-for-dev",
            merged=False,
        )
    board = project.implementation_artifacts / "sprint-status.yaml"
    assert board.read_bytes() == before
    publish_status(
        project.project, source, unit, journal=journal, expected_status="ready-for-dev", merged=True
    )
    assert sprintstatus.story_status(board, "1-1-one") == "done"
    assert sprintstatus.story_status(board, "1-2-other") == "in-progress"
    after = board.read_bytes()
    publish_status(
        project.project, source, unit, journal=journal, expected_status="ready-for-dev", merged=True
    )
    assert board.read_bytes() == after


def test_conflicting_same_unit_canonical_status_is_not_overwritten(project, tmp_path):
    write_sprint(project, {"1-1-one": "awaiting-operator"})
    source = SourceSelector("sprint-status", id="sprint")
    unit = UnitSpec("sprint:project:1-1-one", "sprint", "1", "One", selector="1-1-one")
    journal = Journal(tmp_path / "journal")
    with pytest.raises(CampaignError, match="canonical story changed"):
        publish_status(
            project.project,
            source,
            unit,
            journal=journal,
            expected_status="ready-for-dev",
            merged=True,
        )
    assert (
        sprintstatus.story_status(
            project.implementation_artifacts / "sprint-status.yaml", unit.selector
        )
        == "awaiting-operator"
    )


def test_ignored_sprint_board_and_specs_are_copied_into_worker_checkout(project, tmp_path):

    from bmad_loop.campaign_sources import prepare_source_view

    write_sprint(project, {"1-1-one": "ready-for-dev"})
    git(project.project, "add", "_bmad/bmm/config.yaml")
    git(project.project, "commit", "-qm", "tracked relative config")
    exclude = project.project / ".git" / "info" / "exclude"
    exclude.write_text("/_bmad-output/\n")
    workspace = tmp_path / "worker"
    git(project.project, "worktree", "add", "-b", "private-sprint", str(workspace), "HEAD")
    view = prepare_source_view(project.project, workspace, SourceSelector("sprint-status", id="s"))
    assert view.paths.sprint_status.is_file()
    assert view.paths.sprint_status.is_relative_to(workspace)
    sprintstatus.advance(view.paths.sprint_status, "1-1-one", "done")
    assert sprintstatus.story_status(project.sprint_status, "1-1-one") == "ready-for-dev"
    assert sprintstatus.story_status(view.paths.sprint_status, "1-1-one") == "done"


def test_external_stories_folder_has_effective_private_selector(project, tmp_path):
    import shutil

    from bmad_loop import stories
    from bmad_loop.campaign_sources import prepare_source_view

    folder = setup_stories(
        project, [{"id": "first", "title": "First", "description": "First feature"}]
    )
    external = tmp_path / "external-stories"
    shutil.move(str(folder), str(external))
    git(project.project, "add", "-A")
    git(project.project, "commit", "-qm", "stories live externally")
    workspace = tmp_path / "worker"
    git(project.project, "worktree", "add", "-b", "private-stories", str(workspace), "HEAD")
    selector = SourceSelector("stories", id="stories", path=str(external))
    view = prepare_source_view(project.project, workspace, selector)
    selected = stories.resolve_spec_folder(workspace, view.selector.path)
    assert selected.is_relative_to(workspace)
    assert selected != external
    assert (selected / "stories.yaml").is_file()
    assert view.selector.id == selector.id
    (selected / "worker-only.txt").write_text("worker")
    assert not (external / "worker-only.txt").exists()


def test_external_v6_artifacts_use_native_private_user_overlay(project, tmp_path):
    import shutil

    from bmad_loop.campaign_sources import prepare_source_view

    write_sprint(project, {"1-1-one": "ready-for-dev"})
    external = tmp_path / "external-artifacts"
    shutil.copytree(project.implementation_artifacts, external)
    base = project.project / "_bmad" / "config.toml"
    base.write_text(
        '[core]\noutput_folder="{project-root}/_bmad-output"\n[modules.bmm]\nimplementation_artifacts='
        + repr(str(external))
        + '\nplanning_artifacts="{project-root}/_bmad-output/planning-artifacts"\n'
    )
    git(project.project, "add", "_bmad")
    git(project.project, "commit", "-qm", "central external artifacts")
    workspace = tmp_path / "worker"
    git(project.project, "worktree", "add", "-b", "private-v6-external", str(workspace), "HEAD")
    canonical_config = base.read_bytes()
    original_board = (external / "sprint-status.yaml").read_bytes()
    view = prepare_source_view(project.project, workspace, SourceSelector("sprint-status", id="s"))
    assert view.paths.implementation_artifacts.is_relative_to(workspace)
    assert view.paths.sprint_status.is_file()
    sprintstatus.advance(view.paths.sprint_status, "1-1-one", "done")
    assert (external / "sprint-status.yaml").read_bytes() == original_board
    assert base.read_bytes() == canonical_config
    assert verify.path_ignored(workspace, workspace / "_bmad/custom/config.user.toml")
    assert not verify.path_tracked(workspace, "_bmad/custom/config.user.toml")


def test_seeded_untracked_legacy_config_redirects_without_publishing_absolute_paths(
    project, tmp_path
):
    import shutil

    import yaml

    from bmad_loop.campaign_sources import prepare_source_view

    write_sprint(project, {"1-1-one": "ready-for-dev"})
    external = tmp_path / "external-legacy"
    shutil.copytree(project.implementation_artifacts, external)
    original = project.project / "_bmad/bmm/config.yaml"
    original.write_text(
        yaml.safe_dump(
            {
                "implementation_artifacts": str(external),
                "planning_artifacts": str(project.planning_artifacts),
            }
        )
    )
    workspace = tmp_path / "worker"
    git(
        project.project, "worktree", "add", "-b", "private-untracked-legacy", str(workspace), "HEAD"
    )
    shutil.copytree(project.project / "_bmad", workspace / "_bmad")
    source_bytes = original.read_bytes()
    view = prepare_source_view(project.project, workspace, SourceSelector("sprint-status", id="s"))
    worker_legacy = yaml.safe_load((workspace / "_bmad/bmm/config.yaml").read_text())
    assert worker_legacy["implementation_artifacts"] == str(view.paths.implementation_artifacts)
    assert original.read_bytes() == source_bytes
    assert view.paths.sprint_status.is_relative_to(workspace)
    assert verify.path_ignored(workspace, workspace / "_bmad/bmm/config.yaml")


def test_tracked_legacy_external_config_refuses_ephemeral_path_publication(project, tmp_path):
    import yaml

    from bmad_loop.campaign_sources import prepare_source_view

    original = project.project / "_bmad/bmm/config.yaml"
    original.write_text(
        yaml.safe_dump(
            {
                "implementation_artifacts": str(tmp_path / "external"),
                "planning_artifacts": str(project.planning_artifacts),
            }
        )
    )
    git(project.project, "add", "_bmad")
    git(project.project, "commit", "-qm", "tracked legacy absolute path")
    workspace = tmp_path / "worker"
    git(project.project, "worktree", "add", "-b", "private-tracked-legacy", str(workspace), "HEAD")
    before = (workspace / "_bmad/bmm/config.yaml").read_bytes()
    with pytest.raises(CampaignError, match="qualified native central-config"):
        prepare_source_view(project.project, workspace, SourceSelector("sprint-status", id="s"))
    assert (workspace / "_bmad/bmm/config.yaml").read_bytes() == before
