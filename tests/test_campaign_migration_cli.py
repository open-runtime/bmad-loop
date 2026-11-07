"""Owner manifests bind cached review receipts and fresh authenticated facts."""

import dataclasses
import json
from pathlib import Path

import pytest
from test_campaign_native_inventory import Host

from bmad_loop.campaign_migration import MigrationError
from bmad_loop.campaign_migration_cli import _inventory, inventory_document, qualify_document
from bmad_loop.campaign_model import CampaignSpec, ParadevSpec


class Gateway:
    repository = "org/repo"

    def pulls(self):
        return []


def context(project):
    spec = CampaignSpec("migration", paradev=ParadevSpec(project_id="project"))
    host = Host(project.project)
    return spec, host, Gateway()


def manifest(tmp_path, document):
    value = document["manifest_template"]
    value["authorized_by"] = "owner:explicit-reviewed-request"
    path = tmp_path / "migration.json"
    path.write_text(json.dumps(value))
    return path


def ready(project, spec, **kwargs):
    return {
        "schema_version": 1,
        "common_dir": str(project / ".git"),
        "config_digest": "actual-preflight",
        "capabilities": {
            area: {"ready": True} for area in ("planning", "execution", "publication")
        },
    }


def test_inventory_returns_complete_cached_native_and_git_observations(project, tmp_path):
    spec, host, gateway = context(project)
    result = inventory_document(project.project, spec, host=host, gateway=gateway)
    assert result["ok"] and result["inventory_digest"]
    assert not Path(result["receipt"]).is_relative_to(project.project)
    receipt = json.loads(Path(result["receipt"]).read_text())
    assert _inventory(receipt["inventory"]).digest == result["inventory_digest"]
    assert {item["kind"] for item in result["inventory"]["items"]} >= {
        "worktree",
        "workspace",
        "process",
        "automation",
    }
    assert not result["native_capabilities"]["admission_fence_readback"]
    assert all(
        name
        in {
            "project.list",
            "workspace.list",
            "terminal.daemon.listSessions",
            "terminal.list",
            "terminalAgents.get",
            "cli",
        }
        for name, _ in host.calls
    )


def test_same_authenticated_inventory_receipt_can_be_represented_idempotently(project, monkeypatch):
    from bmad_loop import campaign_migration_cli

    spec, host, gateway = context(project)
    observed = campaign_migration_cli._context(project.project, spec, host=host, gateway=gateway)
    monkeypatch.setattr(campaign_migration_cli, "_context", lambda *args, **kwargs: observed)
    first = inventory_document(project.project, spec)
    assert (
        inventory_document(project.project, spec)["inventory_digest"] == first["inventory_digest"]
    )


def test_fresh_identical_objects_keep_original_review_digest_despite_timestamps(project, tmp_path):
    spec, host, gateway = context(project)
    result = inventory_document(project.project, spec, host=host, gateway=gateway)
    path = manifest(tmp_path, result)
    qualified = qualify_document(
        project.project, spec, path, host=host, gateway=gateway, preflight=ready
    )
    assert qualified["ok"]
    assert qualified["inventory_digest"] == result["inventory_digest"]
    assert qualified["fresh_inventory_digest"] != result["inventory_digest"]
    assert qualified["qualification"]["inventory_digest"] == result["inventory_digest"]
    assert not qualified["qualification"]["replaceable_automations"]


def test_disabled_native_schedule_cannot_become_a_fabricated_fence(project, tmp_path):
    spec, host, gateway = context(project)
    result = inventory_document(project.project, spec, host=host, gateway=gateway)
    path = manifest(tmp_path, result)
    value = json.loads(path.read_text())
    automation = next(
        item for item in value["selections"] if item["id"] == "paradev:automation:automation"
    )
    automation["disposition"] = "replace"
    value["disable_automations"] = [automation["id"]]
    path.write_text(json.dumps(value))
    qualified = qualify_document(
        project.project, spec, path, host=host, gateway=gateway, preflight=ready
    )
    assert not qualified["ok"]
    assert any(
        "external admissions fence not proven" in problem for problem in qualified["problems"]
    )
    assert not qualified["qualification"]["replaceable_automations"]
    assert all(name != "project.setWorktreeBaseDir" for name, _ in host.calls)


def test_revision_change_requires_new_owner_review(project, tmp_path):
    spec, host, gateway = context(project)
    result = inventory_document(project.project, spec, host=host, gateway=gateway)
    path = manifest(tmp_path, result)
    host.automations[0]["updatedAt"] = "new-owner-edit"
    with pytest.raises(MigrationError, match="ownership/revisions changed"):
        qualify_document(project.project, spec, path, host=host, gateway=gateway, preflight=ready)


def test_manifest_cannot_supply_service_data_or_replacement_command(project, tmp_path):
    spec, host, gateway = context(project)
    result = inventory_document(project.project, spec, host=host, gateway=gateway)
    path = manifest(tmp_path, result)
    value = json.loads(path.read_text())
    value["observers"] = [{"command": ["pretend-fenced"], "active_workers": 0}]
    path.write_text(json.dumps(value))
    with pytest.raises(MigrationError, match="unsupported migration manifest"):
        qualify_document(project.project, spec, path, host=host, gateway=gateway, preflight=ready)


def test_cache_tamper_and_changed_campaign_spec_are_refused(project, tmp_path):
    spec, host, gateway = context(project)
    result = inventory_document(project.project, spec, host=host, gateway=gateway)
    path = manifest(tmp_path, result)
    with pytest.raises(MigrationError, match="specification changed"):
        qualify_document(
            project.project,
            dataclasses.replace(spec, max_workers=9),
            path,
            host=host,
            gateway=gateway,
            preflight=ready,
        )
    cache = Path(result["receipt"])
    data = json.loads(cache.read_text())
    data["inventory"]["items"][0]["ownership"] = "forged"
    cache.write_text(json.dumps(data))
    with pytest.raises(MigrationError, match="cached migration inventory digest differs"):
        qualify_document(project.project, spec, path, host=host, gateway=gateway, preflight=ready)


def test_missing_daemon_never_qualifies_a_healthy_empty_inventory(project, tmp_path):
    spec, host, gateway = context(project)
    result = inventory_document(project.project, spec, host=host, gateway=gateway)
    path = manifest(tmp_path, result)
    host.fail_daemon = True
    with pytest.raises(MigrationError, match="coverage is incomplete"):
        qualify_document(project.project, spec, path, host=host, gateway=gateway, preflight=ready)


def test_actual_cli_inventory_review_and_fresh_qualification_one_document(
    project, tmp_path, monkeypatch, capsys
):
    from bmad_loop import campaign_migration_cli, campaign_preflight, cli

    spec_path = tmp_path / "campaign.toml"
    spec_path.write_text(
        'schema_version=1\nname="migration"\n[[sources]]\nkind="stories"\n[paradev]\nproject_id="project"\n'
    )
    actual_context = campaign_migration_cli._context
    host, gateway = Host(project.project), Gateway()
    monkeypatch.setattr(
        campaign_migration_cli,
        "_context",
        lambda project, spec, **kwargs: actual_context(project, spec, host=host, gateway=gateway),
    )
    monkeypatch.setattr(campaign_preflight, "inspect", ready)
    assert (
        cli.main(
            [
                "campaign",
                "migration-inventory",
                str(spec_path),
                "--project",
                str(project.project),
                "--json",
            ]
        )
        == 0
    )
    output = capsys.readouterr()
    document = json.loads(output.out)
    assert output.err == "" and document["command"] == "campaign.migration-inventory"
    path = manifest(tmp_path, document)
    assert (
        cli.main(
            [
                "campaign",
                "migration-qualify",
                str(spec_path),
                str(path),
                "--project",
                str(project.project),
                "--json",
            ]
        )
        == 0
    )
    output = capsys.readouterr()
    document = json.loads(output.out)
    assert output.err == "" and document["command"] == "campaign.migration-qualify"
    assert document["qualification"]["qualified"] and document["effects"].startswith(
        "observation only"
    )
