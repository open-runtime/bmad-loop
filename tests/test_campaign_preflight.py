"""Preflight separates real resource evidence from installation assumptions."""

import json
import shutil
from dataclasses import replace
from types import SimpleNamespace

import pytest
from conftest import git

from bmad_loop import campaign_preflight, cli, platform_util
from bmad_loop.campaign_migration import ServiceInventory
from bmad_loop.campaign_model import (
    CampaignSpec,
    CommunicationSpec,
    GitHubSpec,
    ParadevSpec,
    ReasoningSpec,
    ResourceSpec,
    RoleSpec,
    UnitSpec,
)
from bmad_loop.campaign_scheduler import CapacityObservation


def inspect(project, tmp_path, monkeypatch, spec, *, which=None, **kwargs):
    git(project.project, "remote", "add", "origin", "https://github.com/example/repo.git")
    monkeypatch.setattr(shutil, "which", which or (lambda binary: "/qualified/fake-cli"))
    return campaign_preflight.inspect(
        project.project,
        spec,
        capacity_observer=lambda candidate, project: CapacityObservation(
            True, slots=4, model_access=True
        ),
        **kwargs,
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


def test_preflight_checks_inherited_developer_without_execution_override(
    project, tmp_path, monkeypatch
):
    from bmad_loop import policy

    path = project.project / policy.POLICY_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        '[adapter.dev]\nname="codex"\nmodel="astra"\n[adapter.triage]\nname="claude"\nmodel="fable"\n[adapter.review]\nname="claude"\nmodel="fable"\n'
    )
    result = inspect(
        project,
        tmp_path,
        monkeypatch,
        spec(tmp_path),
        which=lambda binary: None if binary == "codex" else "/qualified/claude",
    )
    execution = result["capabilities"]["execution"]
    assert not execution["ready"]
    route = execution["observations"]["execution"][0]
    assert route["profile"] == "codex" and route["model"] == "astra"
    assert not route["installed"] and not route["eligible"]
    assert result["capabilities"]["planning"]["ready"]


@pytest.mark.parametrize("reasoning", [False, True])
def test_headless_writer_is_required_without_handoff(project, tmp_path, monkeypatch, reasoning):
    selected = replace(
        spec(tmp_path),
        roles=(("execution", RoleSpec(profile="grok", model="grok-test")),),
        reasoning=ReasoningSpec(enabled=reasoning, purposes=()),
    )
    result = inspect(project, tmp_path, monkeypatch, selected)
    execution = result["capabilities"]["execution"]
    assert not execution["ready"]
    route = execution["observations"]["execution"][0]
    assert not route["eligible"]
    assert "workspace-tools" in route["launcher"]["reason"]


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


def test_reasoning_requires_qualification_even_when_binary_and_capacity_are_available(
    project, tmp_path, monkeypatch
):
    selected = replace(spec(tmp_path), reasoning=ReasoningSpec(enabled=True))
    result = inspect(project, tmp_path, monkeypatch, selected)
    planning = result["capabilities"]["planning"]
    assert not planning["ready"]
    assert "reasoning requires a qualified capabilities manifest" in planning["problems"]


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


def test_optional_missing_hcom_reports_unmuted_hooks_without_blocking_other_work(
    project, tmp_path, monkeypatch
):
    from bmad_loop.campaign_communication import HcomObserver

    monkeypatch.setattr(HcomObserver, "_read", lambda *args, **kwargs: pytest.fail("hcom read"))
    selected = replace(spec(tmp_path), communication=CommunicationSpec("observe", str(tmp_path)))
    result = inspect(
        project,
        tmp_path,
        monkeypatch,
        selected,
        which=lambda binary: None if binary == "hcom" else "/qualified/fake-cli",
    )
    observed = result["capabilities"]["reflection"]["observations"]["communication"]
    assert not observed["available"]
    assert not observed["incoming_hooks_muted"]
    assert not observed["automatic_peer_delivery"]
    assert not observed["sends"] and not observed["subscriptions"]
    assert "Preexisting enrolled actors may receive mail" in observed["detail"]
    assert result["capabilities"]["reflection"]["ready"]


def native_probes(monkeypatch, *, controller, transport=True):
    from bmad_loop.adapters.paradev_backend import ParadevMultiplexer
    from bmad_loop.campaign_lifecycle import CampaignLifecycle, ControllerProtection
    from bmad_loop.campaign_paradev import ParadevWorkspaceProvider

    def forbidden(*args, **kwargs):
        pytest.fail("read-only preflight attempted workspace, terminal or lifecycle mutation")

    for method in ("allocate", "bind_terminal", "history_view"):
        monkeypatch.setattr(ParadevWorkspaceProvider, method, forbidden)
    for method in ("new_session", "new_window", "kill_window"):
        monkeypatch.setattr(ParadevMultiplexer, method, forbidden)
    monkeypatch.setattr(CampaignLifecycle, "retire", forbidden)
    monkeypatch.setattr(ControllerProtection, "observe", forbidden)
    monkeypatch.setattr(ControllerProtection, "available", lambda self: controller)
    monkeypatch.setattr(ParadevMultiplexer, "available", lambda self: transport)
    return {
        "reflection_provider": SimpleNamespace(preflight=lambda: {"ready": True}),
        "native_observer": lambda project, common: ServiceInventory(
            "native", str(common), 1, "read-only", (), True
        ),
    }


@pytest.mark.parametrize("controller,transport", [(True, True), (False, True), (True, False)])
def test_reflection_preflight_requires_supported_lifecycle_and_transport_without_launching(
    project, tmp_path, monkeypatch, controller, transport
):
    probes = native_probes(monkeypatch, controller=controller, transport=transport)
    selected = replace(
        spec(tmp_path),
        paradev=ParadevSpec(
            reflect=True, project_id="registered-project", worktree_root=str(tmp_path / "worktrees")
        ),
    )
    before = set(project.project.rglob("*"))
    result = inspect(project, tmp_path, monkeypatch, selected, **probes)
    reflected = result["capabilities"]["reflection"]
    assert reflected["ready"] is (controller and transport)
    assert reflected["observations"]["lifecycle"]["controller_required"]
    assert reflected["observations"]["lifecycle"]["qualified"] is controller
    assert reflected["observations"]["transport_available"] is transport
    assert set(project.project.rglob("*")) == before
    if not controller:
        assert "Required supported controller route unavailable" in reflected["problems"]
    if not transport:
        assert any("optional dependency" in value for value in reflected["problems"])


def test_cli_preflight_surfaces_lifecycle_and_communication_without_creating_resources(
    project, tmp_path, monkeypatch, capsys
):
    probes = native_probes(monkeypatch, controller=False)
    real_inspect = campaign_preflight.inspect
    git(project.project, "remote", "add", "origin", "https://github.com/example/repo.git")
    monkeypatch.setattr(shutil, "which", lambda binary: "/qualified/fake-cli")
    monkeypatch.setattr(
        campaign_preflight,
        "inspect",
        lambda root, selected: real_inspect(
            root,
            selected,
            **probes,
            capacity_observer=lambda *args: CapacityObservation(True, slots=4, model_access=True),
        ),
    )
    path = tmp_path / "campaign.toml"
    path.write_text(
        'schema_version = 1\nname = "preflight"\n[[units]]\nid = "s:e:u"\nsource = "s"\nepic = "e"\ntitle = "Unit"\n'
        '[github]\nenabled = false\n[paradev]\nreflect = true\nproject_id = "project"\n'
        f'worktree_root = {json.dumps(str(tmp_path / "worktrees"))}\n'
        f'[communication]\nmode = "observe"\nroom = {json.dumps(str(tmp_path))}\n'
    )
    before = set(tmp_path.rglob("*"))
    assert (
        cli.main(["campaign", "preflight", str(path), "--project", str(project.project), "--json"])
        == 1
    )
    output = capsys.readouterr()
    document = json.loads(output.out)
    assert "capabilities" in document, document
    observed = document["capabilities"]["reflection"]
    assert not observed["ready"]
    assert not observed["observations"]["lifecycle"]["qualified"]
    assert not observed["observations"]["communication"]["incoming_hooks_muted"]
    assert output.err == ""
    assert set(tmp_path.rglob("*")) == before
