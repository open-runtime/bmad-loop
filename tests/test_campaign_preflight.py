"""Preflight separates real resource evidence from installation assumptions."""

from dataclasses import replace

from conftest import git

from bmad_loop import campaign_preflight, platform_util
from bmad_loop.campaign_model import CampaignSpec, GitHubSpec, ResourceSpec, RoleSpec, UnitSpec
from bmad_loop.campaign_scheduler import CapacityObservation


def inspect(project, tmp_path, monkeypatch, spec):
    git(project.project, "remote", "add", "origin", "https://github.com/example/repo.git")
    monkeypatch.setattr(campaign_preflight.shutil, "which", lambda binary: "/qualified/fake-cli")
    return campaign_preflight.inspect(
        project.project,
        spec,
        capacity_observer=lambda candidate, project: CapacityObservation(
            True, slots=4, model_access=True
        ),
    )


def spec(tmp_path):
    return CampaignSpec(
        "preflight",
        units=(UnitSpec("s:project:u", "s", "e", "Unit"),),
        github=GitHubSpec(enabled=False),
        worktree_root=str(tmp_path / "worktrees"),
    )


def test_default_resource_capabilities_do_not_imply_missing_currency_budget(
    project, tmp_path, monkeypatch
):
    result = inspect(project, tmp_path, monkeypatch, spec(tmp_path))
    assert result["capabilities"]["planning"]["ready"] is True
    assert result["capabilities"]["execution"]["ready"] is platform_util.HANDLE_ANCHORED_WRITES
    assert result["capabilities"]["publication"]["ready"] is False


def test_usd_budget_refuses_without_actual_cost_receipt_accounting(project, tmp_path, monkeypatch):
    selected = replace(spec(tmp_path), resources=ResourceSpec(budget_usd=20))
    result = inspect(project, tmp_path, monkeypatch, selected)
    assert result["capabilities"]["planning"]["ready"] is False
    assert result["capabilities"]["execution"]["ready"] is False
    assert any(
        "actual-cost receipts" in item for item in result["capabilities"]["execution"]["problems"]
    )


def test_role_currency_budget_has_explicit_readiness_failure(project, tmp_path, monkeypatch):
    selected = replace(spec(tmp_path), roles=(("escalation", RoleSpec(budget_usd=3)),))
    result = inspect(project, tmp_path, monkeypatch, selected)
    assert result["capabilities"]["execution"]["ready"] is False
    assert any(
        "escalation: USD" in item for item in result["capabilities"]["execution"]["problems"]
    )


def test_unsupported_candidate_read_confinement_is_visible_before_execution(
    project, tmp_path, monkeypatch
):
    monkeypatch.setattr(platform_util, "HANDLE_ANCHORED_WRITES", False)
    result = inspect(project, tmp_path, monkeypatch, spec(tmp_path))
    for area in ("planning", "execution", "publication"):
        assert result["capabilities"][area]["ready"] is False
        assert any(
            "handle-anchored candidate/source reads" in item
            for item in result["capabilities"][area]["problems"]
        )
