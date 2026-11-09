"""Done source reconciliation verifies existing code without starting an Engine."""

import sys
from dataclasses import asdict
from types import SimpleNamespace

import pytest
from conftest import (
    DEFAULT_TREE,
    git,
    install_bmad_config,
    write_plan,
    write_spec,
    write_sprint,
    write_ticket_tree,
)
from test_stories_engine import SPEC_FOLDER, setup_stories, story_spec

from bmad_loop import policy as policy_mod
from bmad_loop import verify
from bmad_loop.campaign_existing import terminal_path, verify_existing
from bmad_loop.campaign_model import CampaignError, SourceSelector, UnitSpec


def request(project, source, unit, tmp_path):
    from bmad_loop import platform_util

    if not platform_util.HANDLE_ANCHORED_WRITES:
        pytest.skip(
            "existing-source verification requires handle-anchored reads; unsupported preflight is tested separately"
        )
    return {
        "workspace": str(project.project),
        "source": asdict(source),
        "unit": asdict(unit),
        "commands": [
            [
                sys.executable,
                "-c",
                "from pathlib import Path; assert Path('existing.py').read_text() == 'accepted existing behavior\\n'",
            ]
        ],
    }, SimpleNamespace(directory=tmp_path / "worker-state", policy=policy_mod.loads(""))


def seed_code(project):
    (project.project / "existing.py").write_text("accepted existing behavior\n")
    git(project.project, "add", "-A")
    git(project.project, "commit", "-qm", "existing accepted implementation")


def test_existing_done_sprint_runs_real_commands_and_keeps_source_and_code_unchanged(
    project, tmp_path
):
    install_bmad_config(project)
    write_sprint(project, {"1-1-existing": "done"})
    seed_code(project)
    source = SourceSelector("sprint-status", id="s")
    unit = UnitSpec("s:project:1-1-existing", "s", "1", "Existing", selector="1-1-existing")
    req, envelope = request(project, source, unit, tmp_path)
    before = verify.rev_parse_head(project.project)
    board = project.sprint_status.read_bytes()
    result = verify_existing(req, envelope)
    assert result["outcome"] == "existing_verified"
    assert result["source_terminal_receipt"]["delivery_pending"] is True
    assert result["source_terminal_receipt"]["no_development_session"] is True
    assert not result["sessions"]
    assert verify.rev_parse_head(project.project) == before
    assert project.sprint_status.read_bytes() == board
    assert (
        result["source_artifacts"][0]["path"]
        == project.sprint_status.relative_to(project.project).as_posix()
    )


def test_existing_done_story_exposes_exact_reviewable_source(project, tmp_path):
    install_bmad_config(project)
    setup_stories(
        project, [{"id": "existing", "title": "Existing", "description": "Already accepted"}]
    )
    path = story_spec(project, "existing")
    write_spec(path, "done", verify.rev_parse_head(project.project))
    seed_code(project)
    source = SourceSelector("stories", id="s", path=SPEC_FOLDER)
    unit = UnitSpec("s:project:existing", "s", "e", "Existing", selector="existing")
    req, envelope = request(project, source, unit, tmp_path)
    result = verify_existing(req, envelope)
    assert result["outcome"] == "existing_verified"
    assert result["source_artifacts"][0]["path"] == path.relative_to(project.project).as_posix()
    assert path.read_bytes()


def test_existing_done_ticket_requires_real_selected_done_plan(project_v7, tmp_path):
    write_ticket_tree(project_v7, DEFAULT_TREE)
    path = write_plan(project_v7, "epic-cart", "story-cart-model-plan", 1, "done")
    seed_code(project_v7)
    source = SourceSelector("tickets", id="t", initiative=project_v7.initiative)
    unit = UnitSpec("t:initiative-demo:1.1", "t", "epic-cart", "Existing", selector="1.1")
    req, envelope = request(project_v7, source, unit, tmp_path)
    result = verify_existing(req, envelope)
    assert result["outcome"] == "existing_verified"
    assert result["source_artifacts"][0]["path"] == path.relative_to(project_v7.project).as_posix()
    assert result["source_terminal_receipt"]["selector"] == "1.1"


def test_terminal_ticket_uses_explicit_non_active_initiative(project_v7):
    from conftest import v7_layout

    other = v7_layout(project_v7.project, initiative="initiative-other")
    write_ticket_tree(other, DEFAULT_TREE)
    expected = write_plan(other, "epic-cart", "story-cart-model-plan", 1, "done")
    source = SourceSelector("tickets", id="t", initiative=other.initiative)
    unit = UnitSpec(
        "t:initiative-other:1.1",
        "t",
        "epic-cart",
        "Other initiative",
        selector="1.1",
        initiative=other.initiative,
    )
    path, status = terminal_path(project_v7.project, source, unit)
    assert path == expected and status == "done"


def test_explicit_adoption_can_verify_built_ticket_then_accept_only_private_plan(
    project_v7, tmp_path
):
    write_ticket_tree(project_v7, DEFAULT_TREE)
    path = write_plan(project_v7, "epic-cart", "story-cart-model-plan", 1, "built")
    seed_code(project_v7)
    source = SourceSelector("tickets", id="t", initiative=project_v7.initiative)
    unit = UnitSpec("t:initiative-demo:1.1", "t", "epic-cart", "Existing", selector="1.1")
    req, envelope = request(project_v7, source, unit, tmp_path)
    with pytest.raises(CampaignError, match="not done"):
        verify_existing(req, envelope)
    req["adoption_mode"] = "verify-existing"
    result = verify_existing(req, envelope)
    assert result["outcome"] == "existing_verified"
    assert result["source_terminal_receipt"]["status"] == "done"
    assert b"status: done" in path.read_bytes()
    assert not result["sessions"]


def test_status_alone_never_ignores_verification_failure(project, tmp_path):
    install_bmad_config(project)
    write_sprint(project, {"1-1-existing": "done"})
    seed_code(project)
    source = SourceSelector("sprint-status", id="s")
    unit = UnitSpec("s:project:1-1-existing", "s", "1", "Existing", selector="1-1-existing")
    req, envelope = request(project, source, unit, tmp_path)
    req["commands"] = [[sys.executable, "-c", "raise SystemExit(1)"]]
    before = verify.rev_parse_head(project.project)
    result = verify_existing(req, envelope)
    assert result["outcome"] == "verification_failed"
    assert "source_terminal_receipt" not in result
    assert verify.rev_parse_head(project.project) == before


def test_existing_path_refuses_dropped_or_nonterminal_source(project, tmp_path):
    install_bmad_config(project)
    write_sprint(project, {"1-1-existing": "ready-for-dev"})
    source = SourceSelector("sprint-status", id="s")
    unit = UnitSpec("s:project:1-1-existing", "s", "1", "Existing", selector="1-1-existing")
    with pytest.raises(CampaignError, match="not done"):
        terminal_path(project.project, source, unit)


def test_omitted_done_prerequisites_are_explicit_units_owing_delivery():
    from bmad_loop.campaign_existing import dependency_reconciliation

    done = UnitSpec("t:i:1.1", "t", "e", "Done")
    next_unit = UnitSpec("t:i:1.2", "t", "e", "Next", after=(done.id,))
    observations = {}
    result = dependency_reconciliation(
        [next_unit], observations, [done, next_unit], {done.id: {"state": "done"}}
    )
    assert {unit.id for unit in result} == {done.id, next_unit.id}
    assert observations[done.id]["dependency_reconciliation"] is True
    assert next(unit for unit in result if unit.id == next_unit.id).after == (done.id,)
    with pytest.raises(CampaignError, match="no verified terminal"):
        dependency_reconciliation(
            [next_unit], {}, [done, next_unit], {done.id: {"state": "dropped"}}
        )


def test_prior_delivery_requires_an_actual_delivered_campaign_and_fresh_pr_tracking(
    project, tmp_path, monkeypatch
):
    from bmad_loop.campaign_existing import prior_delivery
    from bmad_loop.campaign_model import CampaignSnapshot, CampaignSpec, CampaignUnit, UnitState
    from bmad_loop.campaign_publication import MergeReceipt, PublicationController
    from bmad_loop.campaign_store import CampaignStore

    install_bmad_config(project)
    write_sprint(project, {"1-1-existing": "done"})
    seed_code(project)
    source = SourceSelector("sprint-status", id="s")
    unit = UnitSpec("s:project:1-1-existing", "s", "1", "Existing", selector="1-1-existing")
    selected = SimpleNamespace(spec=unit, id=unit.id)
    store = CampaignStore(tmp_path / "campaigns")
    head = verify.rev_parse_head(project.project)
    artifact = {
        "sha256": __import__("hashlib").sha256(project.sprint_status.read_bytes()).hexdigest()
    }
    prior = CampaignUnit(
        unit,
        UnitState.DELIVERED,
        bindings={"issue": {"number": 2, "id": 22}, "pr": {"number": 3}},
        evidence={
            "source_artifacts": [artifact],
            "merge": {"merge_commit": head},
            "integrated": {"head": head, "base": head},
            "verification": {"outcome": "verified", "launch_id": "verify"},
            "review": {
                "validated": {"verdict": "approved"},
                "launch_id": "review",
                "observed_model": "gpt-6-astra",
            },
        },
    )
    spec = CampaignSpec("Source", sources=(source,))
    old = CampaignSnapshot("previous", "a/b", spec, {unit.id: prior}, common_dir="/same-common")
    store.create(old)
    current = CampaignSnapshot(
        "current", "a/b", spec, {unit.id: CampaignUnit(unit)}, common_dir="/same-common"
    )
    tracker = {"state": "closed", "id": 22}
    gateway = SimpleNamespace(root="repos/a/b", get=lambda _: tracker)
    runtime = SimpleNamespace(
        project=project.project, store=store, source=lambda *a: source, gateway=lambda *a: gateway
    )
    monkeypatch.setattr(
        PublicationController,
        "merge_status",
        lambda *a, **k: MergeReceipt("merged_pending_sync", merge_commit=head),
    )
    monkeypatch.setattr(
        PublicationController,
        "confirm_canonical",
        lambda *a, **k: MergeReceipt("merged_pending_sync", merge_commit=head, canonical_head=head),
    )
    proof = prior_delivery(runtime, current, selected)
    assert proof is not None and proof.prior_pr == 3 and proof.source_sha256 == artifact["sha256"]
    from bmad_loop.campaign_store import reconcile_prior_delivery

    current = store.create(current)
    reconcile_prior_delivery(current, unit.id, proof)
    committed = store.save(current, current.revision)
    assert committed.units[unit.id].state == UnitState.DELIVERED
    assert committed.events[-1]["kind"] == "prior-delivery-reconciled"
    assert not [event for event in committed.events if event["kind"] == "transition"]
    from bmad_loop.campaign_existing import ExistingDeliveryHeld

    tracker["labels"] = [{"name": "bmad:hold"}]
    with pytest.raises(ExistingDeliveryHeld, match="explicit hold"):
        prior_delivery(runtime, current, selected)
    tracker["labels"] = []
    tracker["state"] = "open"
    assert prior_delivery(runtime, current, selected) is None
