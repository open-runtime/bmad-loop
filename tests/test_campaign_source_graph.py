"""Project ticket metadata preserves declared gates without serializing pinned work."""

from dataclasses import replace

import pytest
from conftest import EntrySpec, EpicSpec, write_ticket_tree

from bmad_loop.campaign_model import (
    CampaignError,
    CampaignSpec,
    SourceSelector,
    spec_from_dict,
    spec_to_dict,
)
from bmad_loop.campaign_sources import import_sources, qualified
from bmad_loop.ticketlayout import BREAKDOWN, breakdown_dir, epic_folder


def setup(layout, *, pinned=True, ready_on="tickets", hard_gate=False):
    tree = (
        EpicSpec(
            1,
            "epic-cart",
            "First",
            (EntrySpec(1, "Cart model", refined=True), EntrySpec(2, "Cart rules", refined=True)),
        ),
        EpicSpec(
            2,
            "epic-checkout",
            "Next",
            (
                EntrySpec(1, "Checkout form", after=("1.1",) if pinned else (), refined=True),
                EntrySpec(2, "Checkout crash", type="bug", refined=True),
            ),
            gated_by=("epic-cart",) if hard_gate else (),
        ),
    )
    write_ticket_tree(layout, tree)
    path = breakdown_dir(layout.initiative_folder) / BREAKDOWN
    content = path.read_text()
    if not hard_gate:
        content = content.replace(
            'slug = "epic-checkout"',
            'slug = "epic-checkout"\nafter = [{epic = 1, needs = "Pinned API"}]',
        )
    content = content.replace(
        'slug = "epic-checkout"', f'slug = "epic-checkout"\nready_on = "{ready_on}"'
    )
    path.write_text(content)
    return SourceSelector("tickets", initiative=layout.initiative, id="tickets")


def graph(layout, source):
    result = import_sources(layout.project, CampaignSpec("Graph", sources=(source,)))
    return result, {unit.id: unit for unit in result.units}


def declare_paths(layout, epic, *, broad=("src",), per_ticket=True):
    path = breakdown_dir(layout.initiative_folder) / BREAKDOWN
    import json

    content = path.read_text().replace(
        f'slug = "{epic}"',
        f'slug = "{epic}"\nlease_scope = "declared"\ntouches = {json.dumps(broad)}',
    )
    path.write_text(content)
    if per_ticket:
        entries = breakdown_dir(epic_folder(layout.initiative_folder, epic)) / BREAKDOWN
        content = (
            entries.read_text()
            .replace("id = 1\n", 'id = 1\ntouches = ["src/one/./"]\n')
            .replace("id = 2\n", 'id = 2\ntouches = ["src/two"]\n')
        )
        entries.write_text(content)


def test_ready_on_tickets_uses_pins_and_keeps_unrelated_prerequisite_children_parallel(project_v7):
    source = setup(project_v7)
    _, units = graph(project_v7, source)
    first = qualified(source, "1.1")
    unrelated = qualified(source, "1.2")
    closure = qualified(source, "closure/epic-cart")
    for ref in ("2.1", "2.2"):
        assert first in units[qualified(source, ref)].after
        assert closure not in units[qualified(source, ref)].after
        assert unrelated not in units[qualified(source, ref)].after


@pytest.mark.parametrize(
    "hard_gate,pinned,ready_on",
    [(True, True, "tickets"), (False, False, "tickets"), (False, True, "epics")],
)
def test_explicit_epic_file_gate_or_unpinned_declaration_waits_for_closure(
    project_v7, hard_gate, pinned, ready_on
):
    source = setup(project_v7, hard_gate=hard_gate, pinned=pinned, ready_on=ready_on)
    _, units = graph(project_v7, source)
    closure = qualified(source, "closure/epic-cart")
    assert all(closure in units[qualified(source, ref)].after for ref in ("2.1", "2.2"))


def test_declared_ticket_ownership_normalizes_without_inventing_sibling_independence(project_v7):
    source = setup(project_v7)
    declare_paths(project_v7, "epic-cart")
    _, units = graph(project_v7, source)
    assert units[qualified(source, "1.1")].scopes == ("src/one",)
    assert units[qualified(source, "1.2")].scopes == ("src/two",)
    assert units[qualified(source, "2.2")].scopes == ()
    path = breakdown_dir(epic_folder(project_v7.initiative_folder, "epic-cart")) / BREAKDOWN
    path.write_text(path.read_text().replace('touches = ["src/two"]\n', ""))
    _, units = graph(project_v7, source)
    assert units[qualified(source, "1.2")].scopes == ()


def test_legacy_epic_ownership_remains_broad_without_explicit_ticket_opt_in(project_v7):
    source = setup(project_v7)
    path = breakdown_dir(project_v7.initiative_folder) / BREAKDOWN
    path.write_text(
        path.read_text().replace(
            'slug = "epic-cart"', 'slug = "epic-cart"\ntouches = ["src/shared"]'
        )
    )
    _, units = graph(project_v7, source)
    assert (
        units[qualified(source, "1.1")].scopes
        == units[qualified(source, "1.2")].scopes
        == ("src/shared",)
    )


def test_invalid_declared_scope_and_unconfigured_scheduler_are_visible_failures(project_v7):
    source = setup(project_v7)
    declare_paths(project_v7, "epic-cart", broad=("../outside",))
    with pytest.raises(CampaignError, match="ownership scope"):
        graph(project_v7, source)
    source = setup(project_v7)
    config = project_v7.project / "tools/bmad-factory/scheduler.toml"
    config.parent.mkdir(parents=True)
    config.write_text("[scheduler]\nholds = []\n")
    with pytest.raises(CampaignError, match="source.scheduler_config"):
        graph(project_v7, source)
    _, units = graph(project_v7, replace(source, scheduler_config=str(config)))
    assert units


def test_cross_initiative_hold_becomes_selected_ticket_dependency(project_v7):
    source = setup(project_v7)
    declare_paths(project_v7, "epic-cart", broad=("src/product",))
    declare_paths(project_v7, "epic-checkout", broad=("docs",))
    other = replace(
        project_v7,
        initiative="initiative-other",
        initiative_folder=project_v7.output_folder / "initiative-other",
    )
    setup(other)
    config = project_v7.project / "scheduler.toml"
    config.write_text(
        f'[[scheduler.holds]]\ninitiative = "{project_v7.initiative}"\ntouching = ["src/product"]\nuntil = "initiative-other/1.1"\nwhy = "shared schema"\n'
    )
    source = replace(source, scheduler_config=str(config))
    second = SourceSelector("tickets", initiative=other.initiative, id="other")
    imported = import_sources(project_v7.project, CampaignSpec("Both", sources=(source, second)))
    units = {unit.id: unit for unit in imported.units}
    dependency = qualified(second, "1.1")
    for ref in ("1.1", "1.2"):
        assert dependency in units[qualified(source, ref)].after
        assert (
            imported.observations[qualified(source, ref)]["scheduler_holds"][0]["dependency"]
            == dependency
        )
    assert dependency not in units[qualified(source, "2.1")].after


def test_missing_external_hold_ticket_holds_epic_instead_of_silently_releasing(project_v7):
    source = setup(project_v7)
    config = project_v7.project / "scheduler.toml"
    config.write_text(
        f'[[scheduler.holds]]\ninitiative = "{project_v7.initiative}"\nepic = "epic-cart"\nuntil = "initiative-missing/99.1"\nwhy = "must remain held"\n'
    )
    imported, _ = graph(project_v7, replace(source, scheduler_config=str(config)))
    fact = imported.observations[qualified(source, "1.1")]
    assert "scheduler hold until initiative-missing/99.1" in fact["blocked_reason"]
    assert fact["scheduler_holds"][0]["active"] is True
    assert fact["source_metadata_revisions"]


def test_source_scheduler_configuration_roundtrips_and_cannot_apply_to_stories():
    source = SourceSelector("tickets", id="t", scheduler_config="tools/scheduler.toml")
    spec = CampaignSpec("Graph", sources=(source,))
    assert spec_from_dict(spec_to_dict(spec)).sources == (source,)
    raw = spec_to_dict(spec)
    raw["sources"][0]["kind"] = "stories"
    with pytest.raises(CampaignError, match="only to tickets"):
        spec_from_dict(raw)
