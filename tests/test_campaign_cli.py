"""Campaign command wiring and the single-document/observer contracts."""

import json
import sys
import types

import pytest

from bmad_loop import cli
from bmad_loop.campaign_cli import status_document
from bmad_loop.campaign_model import (
    CampaignSnapshot,
    CampaignUnit,
    Hold,
    UnitSpec,
    UnitState,
    spec_from_dict,
)
from bmad_loop.campaign_store import CampaignStore


@pytest.fixture
def campaign_spec(tmp_path):
    path = tmp_path / "campaign.toml"
    path.write_text('schema_version = 1\nname = "offline"\n[[sources]]\nkind = "stories"\n')
    return path


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    import bmad_loop
    from bmad_loop import campaign_preflight

    store = CampaignStore(tmp_path / "campaign-state")
    spec = spec_from_dict(
        {"schema_version": 1, "name": "test", "sources": [{"kind": "stories", "path": "specs"}]}
    )
    unit = CampaignUnit(
        UnitSpec("s:e:u", "s", "e", "One unit"),
        UnitState.READY,
        bindings={"head": "head", "branch": "branch", "pr_url": "https://example/pr/1"},
    )
    snapshot = store.create(
        CampaignSnapshot(
            "cli-campaign",
            "org/repo",
            spec,
            {unit.id: unit},
            config_snapshot={"credentials": "private-policy-secret"},
        )
    )
    calls = []

    class Runtime:
        def __init__(self, project, saved_store):
            calls.append(("runtime", project, saved_store.root))

        def authorize_backup_resume(self, saved_snapshot):
            calls.append(("backup-resume", saved_snapshot.id))

        def attach(self, campaign_id, unit_id, role="execution"):
            calls.append(("attach", campaign_id, unit_id, role))
            return 0

    class Supervisor:
        def __init__(self, saved_store, driver):
            self.store = saved_store

        def run(self, campaign_id):
            calls.append(("run", campaign_id))
            return self.store.load(campaign_id)

        def reconcile(self, campaign_id):
            calls.append(("reconcile", campaign_id))
            return self.store.load(campaign_id)

    def create(project, spec):
        calls.append(("create", project, spec))
        return store, snapshot

    runtime_module = types.ModuleType("bmad_loop.campaign_runtime")
    runtime_module.CampaignRuntime = Runtime
    runtime_module.create = create
    runtime_module.store_for = lambda project: store
    supervisor_module = types.ModuleType("bmad_loop.campaign_supervisor")
    supervisor_module.CampaignSupervisor = Supervisor
    monkeypatch.setitem(sys.modules, "bmad_loop.campaign_runtime", runtime_module)
    monkeypatch.setitem(sys.modules, "bmad_loop.campaign_supervisor", supervisor_module)
    monkeypatch.setattr(bmad_loop, "campaign_runtime", runtime_module, raising=False)
    monkeypatch.setattr(bmad_loop, "campaign_supervisor", supervisor_module, raising=False)
    monkeypatch.setattr(
        campaign_preflight,
        "inspect",
        lambda project, spec: {"schema_version": 1, "ok": True, "capabilities": {}},
    )
    return store, snapshot, calls


def test_validate_is_offline_and_skips_project_mux_and_policy(
    campaign_spec, tmp_path, monkeypatch, capsys
):
    def forbidden(*args, **kwargs):
        raise AssertionError("offline validate reached a project/configuration seam")

    monkeypatch.setattr(cli, "_project", forbidden)
    monkeypatch.setattr(cli, "_configure_mux", forbidden)
    monkeypatch.setattr(cli.policy_mod, "load", forbidden)
    before = set(tmp_path.rglob("*"))
    assert (
        cli.main(
            ["campaign", "validate", str(campaign_spec), "--project", "/does/not/exist", "--json"]
        )
        == 0
    )
    output = capsys.readouterr()
    doc = json.loads(output.out)
    assert doc["schema_version"] == 1
    assert doc["command"] == "campaign.validate"
    assert doc["ok"] is True
    assert output.err == ""
    assert set(tmp_path.rglob("*")) == before


def test_invalid_spec_is_a_complete_failing_verdict(campaign_spec, capsys):
    campaign_spec.write_text('schema_version = 999\nname = "future"\n')
    assert cli.main(["campaign", "validate", str(campaign_spec), "--json"]) == 1
    output = capsys.readouterr()
    doc = json.loads(output.out)
    assert doc["ok"] is False
    assert "schema_version" in doc["problems"][0]
    assert output.err == ""


def test_preflight_has_separate_capabilities_and_does_not_create_campaign(
    campaign_spec, runtime, capsys, monkeypatch
):
    from bmad_loop import campaign_preflight

    store, _, calls = runtime
    before = set(store.root.rglob("*"))
    observed = {
        "schema_version": 1,
        "ok": False,
        "capabilities": {
            "execution": {"ready": True, "problems": []},
            "publication": {"ready": False, "problems": ["strict checks required"]},
        },
    }
    monkeypatch.setattr(campaign_preflight, "inspect", lambda project, spec: observed)
    assert cli.main(["campaign", "preflight", str(campaign_spec), "--json"]) == 1
    doc = json.loads(capsys.readouterr().out)
    assert doc["capabilities"]["execution"]["ready"] is True
    assert doc["capabilities"]["publication"]["ready"] is False
    assert set(store.root.rglob("*")) == before
    assert calls == []


def test_run_persists_resolved_reflection_overrides_once(campaign_spec, runtime, capsys):
    _, _, calls = runtime
    assert (
        cli.main(
            [
                "campaign",
                "run",
                str(campaign_spec),
                "--paradev-reflect",
                "--paradev-project",
                "project-42",
                "--json",
            ]
        )
        == 0
    )
    doc = json.loads(capsys.readouterr().out)
    assert doc["campaign"]["id"] == "cli-campaign"
    creation = next(call for call in calls if call[0] == "create")
    assert creation[2].paradev.reflect is True
    assert creation[2].paradev.project_id == "project-42"
    assert ("run", "cli-campaign") in calls


def test_failed_preflight_prevents_run_creation(campaign_spec, runtime, monkeypatch, capsys):
    from bmad_loop import campaign_preflight

    _, _, calls = runtime
    monkeypatch.setattr(
        campaign_preflight,
        "inspect",
        lambda project, spec: {"schema_version": 1, "ok": False, "capabilities": {}},
    )
    assert cli.main(["campaign", "run", str(campaign_spec), "--json"]) == 1
    assert json.loads(capsys.readouterr().out)["ok"] is False
    assert not any(call[0] in {"create", "run"} for call in calls)


def test_status_and_list_are_observer_projections_without_private_config(runtime, capsys):
    store, snapshot, calls = runtime
    snapshot.bindings["decision_receipts"] = {"private": {"chat": "private-chat-source"}}
    snapshot.units["s:e:u"].evidence["diagnosis"] = "private-technical-context"
    snapshot = store.save(snapshot, snapshot.revision)
    assert cli.main(["campaign", "status", snapshot.id, "--json"]) == 0
    raw = capsys.readouterr().out
    doc = json.loads(raw)
    assert doc["campaign"]["units"][0]["links"]["head"] == "head"
    for secret in ("private-policy-secret", "private-chat-source", "private-technical-context"):
        assert secret not in raw
    assert cli.main(["campaign", "list", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["campaigns"][0]["id"] == snapshot.id
    assert store.load(snapshot.id).revision == snapshot.revision
    assert calls == []


@pytest.mark.parametrize(
    "command,flags",
    [
        ("stop", []),
        ("stop", ["--terminate-workers"]),
        ("hold", ["--unit", "s:e:u", "--reason", "operator takeover"]),
        ("release", ["--unit", "s:e:u"]),
    ],
)
def test_controls_queue_requests_without_editing_state(runtime, capsys, command, flags):
    store, snapshot, calls = runtime
    assert cli.main(["campaign", command, snapshot.id, *flags, "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    request = doc["request"]
    assert request["kind"] == command
    assert request["payload"]["terminate_workers"] == ("--terminate-workers" in flags)
    assert store.commands(snapshot.id) == [request]
    assert store.load(snapshot.id).revision == snapshot.revision
    assert calls == []


def test_unknown_unit_error_leaves_stdout_and_inbox_empty(runtime, capsys):
    store, snapshot, _ = runtime
    assert (
        cli.main(
            ["campaign", "hold", snapshot.id, "--unit", "missing", "--reason", "hold", "--json"]
        )
        == 1
    )
    output = capsys.readouterr()
    assert output.out == ""
    assert "unknown campaign unit" in output.err
    assert store.commands(snapshot.id) == []


def test_resume_and_reconcile_use_distinct_no_launch_entrypoints(runtime, capsys):
    _, snapshot, calls = runtime
    assert cli.main(["campaign", "reconcile", snapshot.id, "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["command"] == "campaign.reconcile"
    assert ("reconcile", snapshot.id) in calls
    assert ("run", snapshot.id) not in calls
    assert ("backup-resume", snapshot.id) not in calls
    assert cli.main(["campaign", "resume", snapshot.id, "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["command"] == "campaign.resume"
    assert ("run", snapshot.id) in calls
    assert calls.index(("backup-resume", snapshot.id)) < calls.index(("run", snapshot.id))


def test_attach_uses_existing_runtime_identity_and_role(runtime, capsys):
    _, snapshot, calls = runtime
    assert (
        cli.main(
            ["campaign", "attach", snapshot.id, "--unit", "s:e:u", "--role", "publication_review"]
        )
        == 0
    )
    assert ("attach", snapshot.id, "s:e:u", "publication_review") in calls
    assert capsys.readouterr().out == ""
    with pytest.raises(SystemExit) as error:
        cli.main(["campaign", "attach", snapshot.id, "--unit", "s:e:u", "--json"])
    assert error.value.code == 2


def test_project_option_works_before_or_after_subcommand(runtime, capsys, tmp_path):
    _, snapshot, calls = runtime
    for arguments in (
        ["--project", str(tmp_path), "resume", snapshot.id],
        ["resume", snapshot.id, "--project", str(tmp_path)],
    ):
        assert cli.main(["campaign", *arguments, "--json"]) == 0
        capsys.readouterr()
    project_calls = [call for call in calls if call[0] == "runtime"]
    assert all(call[1] == tmp_path for call in project_calls)


def test_operational_failure_never_emits_partial_json(runtime, capsys):
    assert cli.main(["campaign", "status", "missing", "--json"]) == 1
    output = capsys.readouterr()
    assert output.out == ""
    assert "cannot read campaign" in output.err


def test_projection_preserves_actual_hold_and_delivery_distinctions(runtime):
    _, snapshot, _ = runtime
    unit = snapshot.units["s:e:u"]
    unit.state = UnitState.MERGED_PENDING_SYNC
    unit.hold = Hold("tracking", "GitHub unavailable", id="hold")
    view = status_document(snapshot)
    assert view["counts"] == {"merged_pending_sync": 1}
    assert view["units"][0]["hold"]["kind"] == "tracking"
    assert view["units"][0]["state"] != "delivered"
