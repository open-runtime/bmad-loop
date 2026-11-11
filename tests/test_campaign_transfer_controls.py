"""A committed retirement fences old controls and effects through normal entry points."""

import copy
import json

import pytest
from test_campaign_transfer import adoption_case as adoption_case
from test_campaign_transfer import case as case
from test_campaign_transfer import invoke

from bmad_loop import campaign_runtime, campaign_transfer, cli
from bmad_loop.campaign_cli import status_document
from bmad_loop.campaign_model import CampaignError, UnitState
from bmad_loop.campaign_reasoning_flow import admission_reason
from bmad_loop.campaign_scheduler import admit
from bmad_loop.campaign_store import add_action
from bmad_loop.campaign_supervisor import CampaignSupervisor


def test_retired_campaign_exposes_successor_and_cannot_reenter_admission(case):
    invoke(case)
    snapshot = case.store.load(case.before.id)
    view = status_document(snapshot)
    assert view["phase"] == "stopped"
    assert view["ownership_retirement"] == {
        "state": "retired",
        "successor": case.successor.id,
        "released_units": [case.unit.id],
    }
    snapshot.phase = "active"  # Even a caller bypassing persisted phase cannot admit work.
    snapshot.units[case.unit.id].state = UnitState.READY
    snapshot.units[case.unit.id].hold = None
    assert not admit(snapshot).units
    assert admission_reason(snapshot, case.unit.id) == "campaign ownership was transferred"
    runtime = campaign_runtime.CampaignRuntime(case.repo, case.store, gateway=case.gateway)
    assert runtime.proposals(snapshot) == runtime.observe_workers(snapshot) == ()
    with pytest.raises(CampaignError, match="retired"):
        runtime.control(snapshot, {"kind": "release", "unit": case.unit.id})


@pytest.mark.parametrize("kind", ["launch", "push", "github", "reasoning-launch", "semantic-plan"])
def test_stale_effect_snapshot_cannot_write_after_retirement(case, kind, monkeypatch):
    stale = copy.deepcopy(case.before)
    action = add_action(stale, kind, case.unit.id, {})
    invoke(case)
    runtime = campaign_runtime.CampaignRuntime(case.repo, case.store, gateway=case.gateway)
    called = []
    monkeypatch.setattr(runtime, "gateway", lambda *_: called.append("gateway"))
    before = case.store.load(stale.id).revision
    for dispatch in (runtime.perform, runtime.reconcile_action):
        result = dispatch(stale, action)
        assert result.state == "rejected" and "transferred" in result.reason
    assert called == []
    assert case.store.load(stale.id).revision == before


@pytest.mark.parametrize("operation", ["run", "reconcile"])
def test_supervisor_refuses_retired_authority_before_resume_hooks(case, operation, monkeypatch):
    invoke(case)
    runtime = campaign_runtime.CampaignRuntime(case.repo, case.store, gateway=case.gateway)
    entered = []
    monkeypatch.setattr(runtime, "prepare_resume", lambda *_: entered.append("resume"))
    supervisor = CampaignSupervisor(case.store, runtime)
    with pytest.raises(CampaignError, match="retired"):
        getattr(supervisor, operation)(case.before.id)
    assert entered == []


def test_transfer_cli_preview_then_commit_keeps_successor_stopped(case, monkeypatch, capsys):
    monkeypatch.setattr(campaign_runtime, "store_for", lambda _: case.store)
    preview, release = campaign_transfer.preview, campaign_transfer.release
    for name, operation in (("preview", preview), ("release", release)):

        def invoke_from_cli(store, request, *, project, _operation=operation, **options):
            assert request == case.request
            options.setdefault("gateway", case.gateway)
            options.setdefault("observer", case.observer)
            options.setdefault("now", 100)
            return _operation(store, request, project=project, **options)

        monkeypatch.setattr(campaign_transfer, name, invoke_from_cli)
    arguments = [
        "campaign",
        "transfer",
        case.before.id,
        "--project",
        str(case.repo),
        "--unit",
        case.unit.id,
        "--successor",
        case.successor.id,
        "--successor-unit",
        case.unit.id,
        "--pr",
        "7",
        "--expected-revision",
        str(case.before.revision),
        "--expected-successor-revision",
        str(case.successor.revision),
        "--reason",
        case.request.reason,
        "--json",
    ]
    assert cli.main([*arguments, "--preview"]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["preview"] and shown["operation"] == "startup-only-whole-predecessor-retirement"
    assert not campaign_transfer.retired(case.store.load(case.before.id))
    assert cli.main(arguments) == 0
    committed = json.loads(capsys.readouterr().out)
    assert not committed["preview"] and not committed["idempotent"]
    assert case.store.load(case.successor.id).phase == "stopped"
    assert (
        cli.main(["campaign", "resume", case.before.id, "--project", str(case.repo), "--json"]) == 1
    )
    refusal = capsys.readouterr()
    assert not refusal.err
    assert "retired" in json.loads(refusal.out)["problems"][0]


def test_retired_worker_refuses_even_non_model_verification(case, tmp_path, monkeypatch):
    from dataclasses import replace

    from test_campaign_workers import envelope, worker_request

    from bmad_loop import campaign_hold, campaign_workers
    from bmad_loop.adapters import multiplexer

    invoke(case)
    context = replace(
        envelope(case.repo, tmp_path / "stale-worker"),
        campaign_id=case.before.id,
        unit_id=case.unit.id,
    )
    path = campaign_workers.prepare_launch(
        context,
        worker_request(
            case.repo,
            context,
            hold_channel=str(
                campaign_hold.channel(case.store.directory(case.before.id), case.unit.id)
            ),
        ),
    )
    called = []
    monkeypatch.setattr(multiplexer, "configure_multiplexer", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(campaign_workers, "_verify", lambda *_: called.append("verify"))
    assert campaign_workers.child_main(path) == 1
    receipt = json.loads((context.directory / "receipt.json").read_text())
    assert receipt["outcome"] == "failed" and "retired" in receipt["error"]
    assert called == []
