"""An explicit selected-ticket campaign delivers children without closing parents."""

from dataclasses import replace

import pytest
from conftest import DEFAULT_TREE, write_plan, write_ticket_tree
from test_campaign_parent_closure import configured, prepare

from bmad_loop import campaign_closure, ticketlayout
from bmad_loop.campaign_cli import status_document
from bmad_loop.campaign_model import (
    CampaignError,
    CampaignSpec,
    CampaignUnit,
    SourceSelector,
    UnitSpec,
    snapshot_from_dict,
    snapshot_to_dict,
    spec_from_dict,
    spec_to_dict,
)
from bmad_loop.campaign_sources import import_sources, qualified


def selector(**kwargs):
    return SourceSelector(
        "tickets", id="tickets", units=("1.1",), completion_scope="selected-units", **kwargs
    )


@pytest.mark.parametrize("scope", ["wrong", [], None, 1])
def test_invalid_completion_scope_is_rejected(scope):
    with pytest.raises(CampaignError, match="completion_scope"):
        spec_from_dict(
            {
                "schema_version": 1,
                "name": "selected",
                "sources": [{"kind": "tickets", "completion_scope": scope}],
            }
        )


@pytest.mark.parametrize(
    "kind,units", [("tickets", []), ("stories", ["first"]), ("sprint-status", ["1-1-first"])]
)
def test_selected_scope_requires_explicit_ticket_selection(kind, units):
    with pytest.raises(CampaignError, match="explicit ticket"):
        spec_from_dict(
            {
                "schema_version": 1,
                "name": "selected",
                "sources": [{"kind": kind, "units": units, "completion_scope": "selected-units"}],
            }
        )


def test_scope_roundtrip_and_old_snapshot_default(tmp_path):
    _, state, _ = configured(tmp_path, count=1)
    source = selector(initiative="initiative")
    state.spec = replace(state.spec, sources=(source,))
    frozen = snapshot_to_dict(state)
    resumed = snapshot_from_dict(frozen)
    assert resumed.spec.sources == (source,)
    assert spec_from_dict(spec_to_dict(state.spec)) == state.spec
    frozen["spec"]["sources"][0].pop("completion_scope")
    assert snapshot_from_dict(frozen).spec.sources[0].completion_scope == "epic"


def test_selected_scope_import_keeps_dependencies_and_no_parent_writer(project_v7):
    write_ticket_tree(project_v7, DEFAULT_TREE)
    source = replace(selector(initiative=project_v7.initiative), units=("1.1", "1.2"))
    imported = import_sources(project_v7.project, CampaignSpec("selected", sources=(source,)))
    assert [unit.selector for unit in imported.units] == ["1.1", "1.2"]
    assert imported.units[1].after == (qualified(source, "1.1"),)
    assert all(unit.kind != "closure" for unit in imported.units)
    with pytest.raises(CampaignError, match="dependencies outside selected graph"):
        import_sources(
            project_v7.project,
            CampaignSpec("selected", sources=(replace(source, units=("1.2",)),)),
        )
    write_plan(project_v7, "epic-cart", "story-cart-model-plan", 1, "done")
    reconciled = import_sources(
        project_v7.project,
        CampaignSpec("selected", sources=(replace(source, units=("1.2",)),)),
    )
    assert len(reconciled.units) == 2
    assert reconciled.observations[qualified(source, "1.1")]["dependency_reconciliation"]


def test_selected_scope_preserves_already_closed_native_epic_prerequisite(project_v7):
    write_ticket_tree(project_v7, DEFAULT_TREE)
    for number, stem in ((1, "story-cart-model-plan"), (2, "story-cart-rules-plan")):
        write_plan(project_v7, "epic-cart", stem, number, "done")
    epic = ticketlayout.epic_file(project_v7.initiative_folder, "epic-cart")
    epic.write_text(epic.read_text().replace("status: in-progress", "status: done"))
    source = replace(selector(initiative=project_v7.initiative), units=("2.1",))
    spec = CampaignSpec("selected", sources=(source,))
    with pytest.raises(CampaignError, match="retrospective"):
        import_sources(project_v7.project, spec)
    retrospective = ticketlayout.retrospective_doc(project_v7.initiative_folder, "epic-cart")
    retrospective.write_text(
        "# Existing closure\nAll native children were delivered; preserve this accepted history.\n"
    )
    before = epic.read_bytes(), retrospective.read_bytes()
    imported = import_sources(project_v7.project, spec)
    closure = next(unit for unit in imported.units if unit.kind == "closure")
    assert closure.selector == "epic-cart"
    assert imported.observations[closure.id]["dependency_reconciliation"]
    assert (epic.read_bytes(), retrospective.read_bytes()) == before


def test_selected_scope_rejects_missing_parent_writer_even_after_source_drift(tmp_path):
    with pytest.raises(CampaignError, match="cannot author native parent closure"):
        campaign_closure.prepare(
            tmp_path,
            selector(),
            UnitSpec("closure", "tickets", "epic", "Close", kind="closure"),
            canonical={},
            canonical_head="head",
        )


def test_selected_scope_parent_retention_survives_empty_remote_projection(tmp_path):
    runtime, state, transport = configured(tmp_path, count=1)
    state.spec = replace(state.spec, sources=(selector(initiative="initiative"),))
    state.config_revision += 1
    milestone = {"id": 2001, "number": 1, "state": "open", "updated_at": "t0"}
    transport.milestones[1] = milestone
    from bmad_loop.campaign_tracking import HierarchyCoordinator

    state.bindings["github_hierarchy"]["initiative:tickets:initiative"] = (
        HierarchyCoordinator._binding(milestone, "milestone")
    )
    state = runtime.store.save(state, state.revision)
    for _ in range(3):
        state = prepare(runtime, state)
    assert state.bindings["hierarchy_finalized_graph"] == state.graph_revision
    assert "parent scope remains open" in state.bindings["hierarchy_remaining_scope"]
    assert transport.effects == []
    assert transport.issues[1]["state"] == transport.milestones[1]["state"] == "open"
    state.phase = "delivered"
    status = status_document(state)
    assert status["tracking"]["source_completion_scopes"] == {"tickets": "selected-units"}
    assert (
        status["tracking"]["selected_work"] == "Selected work delivered; parent scope remains open"
    )


@pytest.mark.parametrize("aggregate", [False, True])
def test_selected_scope_blocks_prepared_parent_intent_and_unknown_retry(tmp_path, aggregate):
    runtime, state, transport = configured(tmp_path, count=1)
    state = prepare(runtime, state)
    action = next(row for row in state.outbox if row.kind == "github")
    if aggregate:
        action.payload.pop("parent_key")
    state.spec = replace(state.spec, sources=(selector(initiative="initiative"),))
    state.config_revision += 1
    state = runtime.store.save(state, state.revision)
    for operation in (runtime._perform_github, runtime.reconcile_action):
        result = operation(state, action)
        assert result.state == "confirmed" and result.data["canceled_before_write"]
        assert "parent scope remains open" in result.data["reason"]
    assert transport.effects == []
    assert transport.issues[1]["state"] == "open"


def test_landing_repair_reads_accepted_native_product_without_lifecycle_selector(project, tmp_path):
    import hashlib

    from conftest import git

    from bmad_loop.campaign_landing_acceptance import repair_product_input

    _, state, _ = configured(tmp_path, count=1)
    root = next(iter(state.units.values()))
    original = project.project / "docs/accepted-native-plan.md"
    original.parent.mkdir(exist_ok=True)
    content = (
        "# Accepted native requirement\nPreserve the original product intent and obligations.\n"
    )
    original.write_text(content)
    git(project.project, "add", "docs/accepted-native-plan.md")
    git(project.project, "commit", "-qm", "accepted native plan")
    head = git(project.project, "rev-parse", "HEAD")
    root.bindings["spec_path"] = "docs/accepted-native-plan.md"
    root.evidence["integrated"] = {"head": head}
    repair = CampaignUnit(
        replace(root.spec, id="repair", kind="landing_repair", selector="", acceptance=()),
        bindings={"landing_root": root.id},
    )
    # A later checkout edit must not rewrite the accepted input or make the
    # repair acquire a native lifecycle selector to find the original intent.
    original.write_text("Unaccepted changed intent\n")
    inputs = repair_product_input(state, repair, project.project)
    assert inputs == (
        {
            "path": root.bindings["spec_path"] + "@" + head,
            "sha256": hashlib.sha256(content.encode()).hexdigest(),
            "text": content,
        },
    )
    assert repair.spec.selector == "" and repair.spec.acceptance == ()
