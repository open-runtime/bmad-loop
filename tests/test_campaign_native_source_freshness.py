"""Canonical intent changes cannot hide behind unchanged lifecycle bookkeeping."""

import dataclasses
from pathlib import Path

import pytest
from conftest import (
    DEFAULT_TREE,
    OMIT,
    git,
    install_bmad_config,
    spec_path,
    write_spec,
    write_sprint,
    write_ticket_tree,
)
from test_campaign_ownership import unit
from test_campaign_runtime import production_case as production_case

from bmad_loop import platform_util
from bmad_loop.campaign_model import CampaignError, OutboxAction, SourceSelector
from bmad_loop.campaign_ownership import canonical_unchanged
from bmad_loop.campaign_source_publication import SourceBinding, SourcePublication


@pytest.fixture(autouse=True)
def anchored_reads():
    if not platform_util.HANDLE_ANCHORED_WRITES:
        pytest.skip("native product input requires anchored reads")


def bind(project, tmp_path, source, selected):
    authority = tmp_path / "authority"
    captured = SourcePublication(project.project, authority).capture(source, selected.spec)
    selected.bindings.update(
        canonical_source=captured.to_dict(),
        canonical_selector=dataclasses.asdict(source),
        source_authority_root=str(authority),
    )
    canonical_unchanged(selected)
    return captured


@pytest.mark.parametrize("board_tracked", [False, True])
@pytest.mark.parametrize("change", ["edit", "delete", "create", "legacy"])
def test_sprint_selected_spec_is_bound_even_when_board_is_tracked(
    project, tmp_path, board_tracked, change
):
    install_bmad_config(project)
    write_sprint(project, {"1-1-owned": "ready-for-dev", "1-2-other": "ready-for-dev"})
    product = spec_path(project, "1-1-owned")
    if change != "create":
        write_spec(product, "ready-for-dev", OMIT)
    with (project.project / ".gitignore").open("a", encoding="utf-8") as stream:
        stream.write("\n/_bmad-output/\n")
    if board_tracked:
        git(project.project, "add", "-f", str(project.sprint_status))
    git(project.project, "add", "-A")
    git(project.project, "commit", "-m", "source configuration")
    selected = unit(project, ("src/owned",))
    selected.spec = dataclasses.replace(selected.spec, selector="1-1-owned")
    source = SourceSelector("sprint-status", id="source")
    captured = bind(project, tmp_path, source, selected)
    assert (captured.versioning == "code-repo") is board_tracked
    assert SourceBinding.from_dict(captured.to_dict()) == captured

    # A sibling completion never changes this unit's product requirements.
    write_sprint(project, {"1-1-owned": "ready-for-dev", "1-2-other": "done"})
    canonical_unchanged(selected)
    if change == "delete":
        product.unlink()
    elif change == "legacy":
        selected.bindings["canonical_source"].pop("product_inputs")
        legacy = SourceBinding.from_dict(selected.bindings["canonical_source"])
        assert legacy.product_inputs is None
        assert "product_inputs" not in legacy.to_dict()
    else:
        product.write_text("# Revised selected acceptance\n", encoding="utf-8")
    with pytest.raises(CampaignError, match="canonical (source changed|product-input dispatch)"):
        canonical_unchanged(selected)


def test_unplanned_sprint_section_ignores_neighbor_requirements(project, tmp_path):
    install_bmad_config(project)
    write_sprint(project, {"1-1-owned": "ready-for-dev"})
    document = project.planning_artifacts / "epics.md"
    document.parent.mkdir(parents=True, exist_ok=True)
    document.write_text(
        "# Scope\n## Story 1.1: Selected\nPreserve quantities.\n## Story 1.2: Other\nOld neighbor.\n",
        encoding="utf-8",
    )
    selected = unit(project, ("src/owned",))
    selected.spec = dataclasses.replace(selected.spec, selector="1-1-owned")
    bind(project, tmp_path, SourceSelector("sprint-status", id="source"), selected)
    document.write_text(
        document.read_text(encoding="utf-8").replace("Old neighbor", "New neighbor"),
        encoding="utf-8",
    )
    canonical_unchanged(selected)
    document.write_text(
        document.read_text(encoding="utf-8").replace("Preserve quantities", "Drop quantities"),
        encoding="utf-8",
    )
    with pytest.raises(CampaignError, match="selected product input"):
        canonical_unchanged(selected)


def test_unplanned_ticket_entry_changes_require_fresh_review(project_v7, tmp_path):
    write_ticket_tree(project_v7, DEFAULT_TREE)
    selected = unit(project_v7, ("src/owned",))
    selected.spec = dataclasses.replace(selected.spec, selector="1.1")
    source = SourceSelector("tickets", id="source", initiative=project_v7.initiative)
    captured = bind(project_v7, tmp_path, source, selected)
    assert captured.revision is None  # The absent plan is not the product input.
    assert captured.product_inputs and captured.product_inputs[0][0].endswith("#entry:1")
    manifest = Path(captured.product_inputs[0][0].split("#entry:")[0])
    original = manifest.read_text(encoding="utf-8")
    manifest.write_text(original.replace("Cart rules", "Neighbor rules"), encoding="utf-8")
    canonical_unchanged(selected)
    manifest.write_text(
        original.replace("Cart model", "Changed selected requirements"), encoding="utf-8"
    )
    with pytest.raises(CampaignError, match="selected product input"):
        canonical_unchanged(selected)


@pytest.mark.parametrize("change_during_copy", [False, True])
def test_normal_preparation_binds_canonical_product_separately_from_worker_status(
    production_case, project, monkeypatch, change_during_copy
):
    from bmad_loop import campaign_runtime
    from bmad_loop.campaign_sources import source_paths

    case = production_case
    write_sprint(project, {"1-1-owned": "ready-for-dev"})
    product = spec_path(project, "1-1-owned")
    write_spec(product, "ready-for-dev", OMIT)
    with (case.repo / ".gitignore").open("a", encoding="utf-8") as stream:
        stream.write("\n/_bmad-output/\n")
    git(case.repo, "add", ".gitignore")
    git(case.repo, "add", "-f", str(project.sprint_status))
    git(case.repo, "commit", "-m", "tracked board and ignored product specification")
    git(case.repo, "push", "origin", "main")
    source = SourceSelector("sprint-status", id="source")
    case.snapshot.spec = dataclasses.replace(case.snapshot.spec, sources=(source,))
    selected = next(iter(case.snapshot.units.values()))
    selected.spec = dataclasses.replace(selected.spec, selector="1-1-owned")
    action = OutboxAction(
        "source-prepare", "prepare-unit", selected.id, {}, case.snapshot.generation
    )

    def owner_edit():
        product.write_text(
            product.read_text(encoding="utf-8") + "\nNew owner acceptance.\n", encoding="utf-8"
        )

    if change_during_copy:
        original = campaign_runtime.prepare_source_view

        def copy_then_edit(*args, **kwargs):
            result = original(*args, **kwargs)
            owner_edit()
            return result

        monkeypatch.setattr(campaign_runtime, "prepare_source_view", copy_then_edit)
        with pytest.raises(CampaignError, match="changed while preparing"):
            case.runtime._perform_prepare_unit(case.snapshot, action)
        return

    result = case.runtime._perform_prepare_unit(case.snapshot, action)
    assert result.state == "confirmed"
    selected.bindings.update(result.data)
    binding = SourceBinding.from_dict(selected.bindings["canonical_source"])
    assert binding.versioning == "code-repo"
    assert binding.project == selected.bindings["workspace"]
    assert binding.product_project == str(case.repo)
    canonical_unchanged(selected)
    workspace = Path(selected.bindings["workspace"])
    effective = SourceSelector(**selected.bindings["effective_source"])
    private = source_paths(workspace, effective).implementation_artifacts / product.name
    private.write_text(
        private.read_text(encoding="utf-8").replace("ready-for-dev", "done"), encoding="utf-8"
    )
    canonical_unchanged(selected)  # Authorized worker lifecycle is private.
    owner_edit()
    with pytest.raises(CampaignError, match="selected product input"):
        canonical_unchanged(selected)


@pytest.mark.parametrize("overlay", ["none", "worktree", "index", "deleted"])
def test_fresh_remote_tracked_spec_preserves_clean_base_and_refuses_local_overlays(
    production_case, project, overlay
):
    case = production_case
    write_sprint(project, {"1-1-owned": "ready-for-dev"})
    product = spec_path(project, "1-1-owned")
    write_spec(product, "ready-for-dev", OMIT)
    git(case.repo, "add", str(project.sprint_status), str(product))
    git(case.repo, "commit", "-m", "initial tracked product")
    baseline = git(case.repo, "rev-parse", "HEAD")
    original = product.read_text(encoding="utf-8")
    product.write_text(original + "\nAccepted requirement on newer main.\n", encoding="utf-8")
    git(case.repo, "add", str(product))
    git(case.repo, "commit", "-m", "new main requirement")
    git(case.repo, "push", "origin", "main")
    git(case.repo, "reset", "--hard", baseline)
    assert not git(case.repo, "status", "--porcelain")
    if overlay == "deleted":
        product.unlink()
    elif overlay != "none":
        product.write_text(original + "\nPending owner requirement.\n", encoding="utf-8")
        if overlay == "index":
            git(case.repo, "add", str(product))
            product.write_text(original, encoding="utf-8")
    source = SourceSelector("sprint-status", id="source")
    case.snapshot.spec = dataclasses.replace(case.snapshot.spec, sources=(source,))
    selected = next(iter(case.snapshot.units.values()))
    selected.spec = dataclasses.replace(selected.spec, selector="1-1-owned")
    action = OutboxAction(
        "fresh-source-prepare", "prepare-unit", selected.id, {}, case.snapshot.generation
    )
    if overlay != "none":
        with pytest.raises(CampaignError, match="canonical selected product"):
            case.runtime._perform_prepare_unit(case.snapshot, action)
        return
    result = case.runtime._perform_prepare_unit(case.snapshot, action)
    assert result.state == "confirmed"
    selected.bindings.update(result.data)
    canonical_unchanged(selected)
    assert git(case.repo, "rev-parse", "HEAD") == baseline
    assert not git(case.repo, "status", "--porcelain")
    product.write_text(original + "\nOwner edits after dispatch.\n", encoding="utf-8")
    with pytest.raises(CampaignError, match="selected product input"):
        canonical_unchanged(selected)


def capture_tracked_fragment(project, tmp_path, source, selected):
    from bmad_loop.campaign_product_input import collect
    from bmad_loop.campaign_source_intent import parts, projection

    authority = tmp_path / "authority"
    binding = SourcePublication(project.project, authority).capture(source, selected.spec)
    assert binding.product_inputs[0][2] == "tracked-clean"
    selected.bindings.update(
        canonical_source=binding.to_dict(),
        canonical_selector=dataclasses.asdict(source),
        source_authority_root=str(authority),
    )
    canonical_unchanged(selected)
    for row in collect(project.project, source, selected.spec, canonical=True):
        path, marker = parts(row["path"])
        assert projection(path.read_bytes(), marker) == row["text"]
    return binding


def test_selected_tracked_epics_fragment_ignores_neighbor_index_and_worktree(project, tmp_path):
    install_bmad_config(project)
    write_sprint(project, {"1-1-owned": "ready-for-dev"})
    document = project.planning_artifacts / "epics.md"
    document.parent.mkdir(parents=True, exist_ok=True)
    document.write_text(
        "# Scope\n## Story 1.1: Selected\nPreserve quantities.\n## Story 1.2: Other\nOld neighbor.\n",
        encoding="utf-8",
    )
    git(project.project, "add", "-A")
    git(project.project, "commit", "-m", "tracked selected source")
    selected = unit(project, ("src/owned",))
    selected.spec = dataclasses.replace(selected.spec, selector="1-1-owned")
    capture_tracked_fragment(
        project, tmp_path, SourceSelector("sprint-status", id="source"), selected
    )
    document.write_text(
        document.read_text(encoding="utf-8").replace("Old neighbor", "New neighbor"),
        encoding="utf-8",
    )
    git(project.project, "add", str(document))
    document.write_text(
        document.read_text(encoding="utf-8").replace("New neighbor", "Another neighbor"),
        encoding="utf-8",
    )
    canonical_unchanged(selected)
    document.write_text(
        document.read_text(encoding="utf-8").replace("Preserve quantities", "Drop quantities"),
        encoding="utf-8",
    )
    with pytest.raises(CampaignError, match="selected product input"):
        canonical_unchanged(selected)


def test_selected_tracked_ticket_entry_ignores_neighbor_index_and_worktree(project_v7, tmp_path):
    write_ticket_tree(project_v7, DEFAULT_TREE)
    git(project_v7.project, "add", "-A")
    git(project_v7.project, "commit", "--allow-empty", "-m", "tracked selected tickets")
    selected = unit(project_v7, ("src/owned",))
    selected.spec = dataclasses.replace(selected.spec, selector="1.1")
    source = SourceSelector("tickets", id="source", initiative=project_v7.initiative)
    binding = capture_tracked_fragment(project_v7, tmp_path, source, selected)
    from bmad_loop.campaign_source_intent import parts

    path, _ = parts(binding.product_inputs[0][0])
    path.write_text(
        path.read_text(encoding="utf-8").replace("Cart rules", "Neighbor rules"), encoding="utf-8"
    )
    git(project_v7.project, "add", str(path))
    path.write_text(
        path.read_text(encoding="utf-8").replace("Neighbor rules", "Changed neighbor rules"),
        encoding="utf-8",
    )
    canonical_unchanged(selected)
    path.write_text(
        path.read_text(encoding="utf-8").replace("Cart model", "Changed selected requirements"),
        encoding="utf-8",
    )
    with pytest.raises(CampaignError, match="selected product input"):
        canonical_unchanged(selected)
