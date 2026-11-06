"""Early draft specs carry selected native intent, never generic dispatch text."""

import hashlib
from dataclasses import replace

import pytest
from conftest import DEFAULT_TREE, install_bmad_config, write_plan, write_ticket_tree
from test_stories_engine import SPEC_FOLDER, setup_stories

from bmad_loop import platform_util
from bmad_loop.campaign_model import CampaignError, SourceSelector, UnitSpec
from bmad_loop.campaign_planning import execution_spec
from bmad_loop.campaign_product_input import MAX_BYTES, collect


def render(unit, product=()):
    return execution_spec(
        unit, campaign_id="campaign", graph_revision=1, source={}, native_input=product
    )


def test_empty_explicit_execution_cannot_manufacture_dispatch_acceptance():
    unit = UnitSpec("s:i:u", "s", "e", "Metadata only")
    with pytest.raises(CampaignError, match="concrete acceptance criteria"):
        render(unit)
    text = render(replace(unit, acceptance=("Persist the owner's selected cart quantity.",)))
    assert "Persist the owner's selected cart quantity." in text
    assert "Satisfy the selected BMAD" not in text
    for kind in ("planning", "tracking"):
        assert "Metadata only" in render(replace(unit, kind=kind))


@pytest.fixture(autouse=True)
def anchored_reads(request):
    if (
        not platform_util.HANDLE_ANCHORED_WRITES
        and request.node.name
        != "test_empty_explicit_execution_cannot_manufacture_dispatch_acceptance"
    ):
        pytest.skip("selected native product capture requires anchored reads")


def ticket_unit(initiative, selector="1.1", **kwargs):
    return UnitSpec(
        "t:" + initiative + ":" + selector,
        "t",
        "epic-cart",
        "Selected",
        selector,
        initiative=initiative,
        **kwargs,
    )


@pytest.mark.parametrize("status", ["ready-for-dev", "built"])
def test_planned_ticket_embeds_exact_private_plan_and_hash_without_neighbor_products(
    project_v7, status
):
    own = write_plan(project_v7, "epic-cart", "story-cart-model-plan", 1, status)
    own.write_bytes(own.read_bytes() + b"\nPreserve cart quantities across process restarts.\n")
    other = write_plan(project_v7, "epic-cart", "story-cart-rules-plan", 2, "ready-for-dev")
    other.write_bytes(other.read_bytes() + b"\nNEIGHBOR_PRIVATE_CONTENT\n")
    source = SourceSelector("tickets", id="t", initiative=project_v7.initiative)
    unit = ticket_unit(project_v7.initiative)
    products = collect(project_v7.project, source, unit)
    text = render(unit, products)
    assert "Preserve cart quantities across process restarts." in text
    assert "NEIGHBOR_PRIVATE_CONTENT" not in text
    assert hashlib.sha256(own.read_bytes()).hexdigest() in text


def test_unplanned_ticket_embeds_only_exact_real_native_entry(project_v7):
    write_ticket_tree(project_v7, DEFAULT_TREE)
    source = SourceSelector("tickets", id="t", initiative=project_v7.initiative)
    unit = ticket_unit(project_v7.initiative)
    products = collect(project_v7.project, source, unit)
    assert products[0]["path"].endswith("tickets.toml#entry:1")
    text = render(unit, products)
    assert "Cart model" in text and "Cart rules" not in text and "Checkout" not in text


def test_selected_non_active_initiative_product_does_not_read_active_tree(project_v7):
    from conftest import v7_layout

    other = v7_layout(project_v7.project, initiative="initiative-other")
    write_ticket_tree(other, DEFAULT_TREE)
    path = write_plan(other, "epic-cart", "story-cart-model-plan", 1, "ready-for-dev")
    path.write_bytes(path.read_bytes() + b"\nSelected nonactive initiative product.\n")
    source = SourceSelector("tickets", id="t", initiative=other.initiative)
    assert "Selected nonactive initiative product." in render(
        ticket_unit(other.initiative),
        collect(project_v7.project, source, ticket_unit(other.initiative)),
    )


def test_closure_draft_contains_its_native_epic_intent(project_v7):
    source = SourceSelector("tickets", id="t", initiative=project_v7.initiative)
    unit = ticket_unit(project_v7.initiative, "epic-cart", kind="closure")
    products = collect(project_v7.project, source, unit)
    assert products[0]["path"].endswith("epic-cart/epic-cart.md")
    assert "# Cart" in render(unit, products)


def test_sprint_draft_embeds_exact_selected_spec_or_scoped_native_story_section(project):
    install_bmad_config(project)
    source = SourceSelector("sprint-status", id="s")
    unit = UnitSpec("s:p:1-1-cart", "s", "1", "Cart", "1-1-cart")
    own = project.implementation_artifacts / "spec-1-1-cart.md"
    own.parent.mkdir(parents=True, exist_ok=True)
    own.write_text(
        "# Cart story\n\nPreserve the selected owner's cart quantities.\n", encoding="utf-8"
    )
    (own.parent / "spec-1-2-neighbor.md").write_text("NEIGHBOR_PRIVATE_CONTENT", encoding="utf-8")
    assert "Preserve the selected owner's cart quantities." in render(
        unit, collect(project.project, source, unit)
    )
    own.unlink()
    epic = project.planning_artifacts / "epics.md"
    epic.parent.mkdir(parents=True, exist_ok=True)
    epic.write_text(
        "# Cart\n\n## Story 1.1: Cart persistence\n\nKeep every quantity.\n\n### Acceptance\nRestore quantities after restart.\n\n## Story 1.2: Neighbor\n\nNEIGHBOR_PRIVATE_CONTENT\n",
        encoding="utf-8",
    )
    text = render(unit, collect(project.project, source, unit))
    assert "Restore quantities after restart." in text and "NEIGHBOR_PRIVATE_CONTENT" not in text


def test_sprint_status_or_slug_without_native_product_is_not_a_specification(project):
    install_bmad_config(project)
    unit = UnitSpec("s:p:1-1-cart", "s", "1", "Cart", "1-1-cart")
    with pytest.raises(CampaignError, match="genuine selected native product input"):
        render(unit, collect(project.project, SourceSelector("sprint-status", id="s"), unit))


def test_stories_manifest_embeds_only_selected_product_when_no_spec_exists(project):
    install_bmad_config(project)
    setup_stories(
        project,
        [
            {
                "id": "selected",
                "title": "Selected",
                "description": "Persist selected cart quantities.",
            },
            {"id": "neighbor", "title": "NEIGHBOR_PRIVATE_CONTENT", "description": "Other work"},
        ],
    )
    source = SourceSelector("stories", id="s", path=SPEC_FOLDER)
    unit = UnitSpec("s:p:selected", "s", "e", "Selected", "selected")
    text = render(unit, collect(project.project, source, unit))
    assert "Persist selected cart quantities." in text and "NEIGHBOR_PRIVATE_CONTENT" not in text


@pytest.mark.parametrize("mutation", ["oversize", "redirect"])
def test_product_documents_remain_bounded_and_confined(project_v7, tmp_path, mutation):
    path = write_plan(project_v7, "epic-cart", "story-cart-model-plan", 1, "ready-for-dev")
    if mutation == "oversize":
        path.write_bytes(b"x" * (MAX_BYTES + 1))
    else:
        private = tmp_path / "shared-secret"
        private.write_text("SHARED_SECRET", encoding="utf-8")
        path.unlink()
        try:
            path.symlink_to(private)
        except OSError:
            pytest.skip("test host cannot create symlinks")
    with pytest.raises(CampaignError):
        collect(
            project_v7.project,
            SourceSelector("tickets", id="t", initiative=project_v7.initiative),
            ticket_unit(project_v7.initiative),
        )
