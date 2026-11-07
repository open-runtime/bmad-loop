"""Real preflight and durable controls exercise their production consumers."""

import json
import time
from types import SimpleNamespace

import pytest
from conftest import git

from bmad_loop import campaign_capacity, campaign_preflight, campaign_runtime, cli
from bmad_loop.campaign_model import (
    CampaignSnapshot,
    CampaignSpec,
    CampaignUnit,
    GitHubSpec,
    OutboxAction,
    RoleSpec,
    UnitSpec,
    UnitState,
    WorkerClaim,
)
from bmad_loop.campaign_scheduler import CapacityObservation, select_role
from bmad_loop.campaign_store import CampaignStore
from bmad_loop.campaign_supervisor import CampaignSupervisor


def test_default_teamrouter_observer_admits_unknown_concurrency(project, tmp_path, monkeypatch):
    git(project.project, "remote", "add", "origin", "https://github.com/example/repo.git")
    selected = CampaignSpec(
        "router",
        units=(UnitSpec("unit", "source", "epic", "Unit"),),
        roles=tuple(
            (role, RoleSpec(profile="codex", model="gpt-6-astra"))
            for role in campaign_preflight.ROLES
        ),
        github=GitHubSpec(enabled=False),
        worktree_root=str(tmp_path / "worktrees"),
    )
    calls = []

    def status(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(
            returncode=0,
            timed_out=False,
            interrupted=False,
            stdout=json.dumps(
                {
                    "availability": {
                        "gpt-6-astra": {
                            "observedAt": time.time() * 1000,
                            "accounts": [{"eligible": True, "quotaKnown": True, "blockers": []}],
                        }
                    }
                }
            ),
        )

    monkeypatch.setattr(campaign_capacity, "run_argv", status)
    monkeypatch.setattr(campaign_preflight.shutil, "which", lambda _: "/qualified/fake-cli")
    observed = campaign_preflight.inspect(project.project, selected)
    assert calls and all(command == ("teamrouter", "status", "--json") for command in calls)
    assert observed["capabilities"]["planning"]["ready"]
    choice = observed["capabilities"]["planning"]["observations"]["triage"][0]
    assert choice["eligible"] and not choice["capacity"]["concurrency_known"]
    assert select_role(
        selected, "triage", {("codex", "gpt-6-astra"): CapacityObservation(**choice["capacity"])}
    )[0]


@pytest.mark.parametrize("terminate", [False, True])
def test_cli_stop_flag_reaches_real_supervisor_termination_proposals(
    project, tmp_path, monkeypatch, capsys, terminate
):
    unit = CampaignUnit(UnitSpec("unit", "source", "epic", "Live unit"), UnitState.RUNNING)
    unit.claim = WorkerClaim(unit.id, 1, 0, "owned-launch", writes=True)
    snapshot = CampaignSnapshot(
        "controls", "org/repo", CampaignSpec("controls", units=(unit.spec,)), {unit.id: unit}
    )
    snapshot.outbox.append(
        OutboxAction("owned-launch", "launch", unit.id, {"kind": "execution"}, 0, state="confirmed")
    )
    store = CampaignStore(tmp_path / "state")
    snapshot = store.create(snapshot)
    monkeypatch.setattr(campaign_runtime, "store_for", lambda _: store)
    flags = ["--terminate-workers"] if terminate else []
    assert cli.main(["campaign", "stop", snapshot.id, *flags, "--json"]) == 0
    assert (
        json.loads(capsys.readouterr().out)["request"]["payload"]["terminate_workers"] is terminate
    )
    runtime = campaign_runtime.CampaignRuntime(project.project, store)
    supervisor = CampaignSupervisor(store, runtime)
    changed = supervisor._commands(store.load(snapshot.id))
    assert changed.phase == "draining"
    assert not changed.units[unit.id].claim.quiescent
    proposals = runtime.observe_workers(changed)
    targets = [p.payload["launch_id"] for p in proposals if p.kind == "terminate-worker"]
    assert targets == (["owned-launch"] if terminate else [])
    # Delivery is idempotent and survives a newly constructed coordinator.
    again = CampaignSupervisor(store, runtime)._commands(store.load(snapshot.id))
    assert again.revision == changed.revision
    assert again.bindings.get("terminate_requested", False) is terminate


@pytest.mark.parametrize(
    "known,slots,active,available,access,budget,eligible",
    [
        (False, 0, 0, True, True, None, True),
        (True, 0, 0, True, True, None, False),
        (True, 3, 3, True, True, None, False),
        (True, 3, 2, True, True, None, True),
        (False, 0, 0, False, True, None, False),
        (False, 0, 0, True, False, None, False),
        (False, 2, 0, True, True, 0, False),
    ],
)
def test_preflight_and_scheduler_share_route_eligibility(
    project, tmp_path, monkeypatch, known, slots, active, available, access, budget, eligible
):
    git(project.project, "remote", "add", "origin", "https://github.com/example/repo.git")
    selected = CampaignSpec(
        "routes",
        units=(UnitSpec("unit", "s", "e", "Unit"),),
        roles=tuple(
            (role, RoleSpec(profile="codex", model="gpt-6-astra"))
            for role in campaign_preflight.ROLES
        ),
        github=GitHubSpec(enabled=False),
        worktree_root=str(tmp_path / "worktrees"),
    )
    route = CapacityObservation(
        available,
        slots=slots,
        active=active,
        model_access=access,
        budget_remaining=budget,
        concurrency_known=known,
    )
    monkeypatch.setattr(campaign_preflight.shutil, "which", lambda _: "/qualified/fake-cli")
    result = campaign_preflight.inspect(
        project.project, selected, capacity_observer=lambda *_: route
    )
    assert result["capabilities"]["planning"]["ready"] is eligible
    assert bool(select_role(selected, "triage", {("codex", "gpt-6-astra"): route})[0]) is eligible
