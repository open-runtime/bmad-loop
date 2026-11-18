"""Terminal rearm gates cannot hide failures or return after owner release."""

import copy
import dataclasses
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import git
from test_campaign_git_activity import Host
from test_campaign_git_activity import subject as subject

from bmad_loop import campaign_git_activity as activity
from bmad_loop import campaign_hold, campaign_takeover, runs
from bmad_loop.campaign_model import (
    CampaignError,
    CampaignSnapshot,
    CampaignSpec,
    CampaignUnit,
    Hold,
    OutboxAction,
    SourceSelector,
    UnitSpec,
    UnitState,
    WorkerClaim,
)
from bmad_loop.campaign_resolution_flow import proposals
from bmad_loop.campaign_runtime import CampaignRuntime
from bmad_loop.campaign_store import CampaignStore


@pytest.fixture
def terminal_case(subject, tmp_path):
    store = CampaignStore(tmp_path / "terminal-controller")
    directory = store.directory("campaign") / "workers" / "launch"
    envelope = dataclasses.replace(subject.envelope, directory=directory)
    path, digest = envelope.save()
    spec = UnitSpec(
        "unit", "s", "epic", "Owned interrupted work", selector="task", acceptance=("unfinished",)
    )
    request = {
        **subject.request,
        "kind": "execution",
        "execution": str(path),
        "digest": digest,
        "unit": dataclasses.asdict(spec),
    }
    (directory / "request.json").write_text(json.dumps(request))
    claim = WorkerClaim(
        "unit",
        1,
        2,
        "launch",
        run_id="run",
        run_dir=str(directory),
        workspace=str(subject.repo),
        branch="main",
        role="execution",
        writes=True,
    )
    internal = Hold(
        "technical_rearm",
        "Exact retained development continuation is in progress",
        id="owned-rearm",
    )
    unit = CampaignUnit(
        spec,
        UnitState.RUNNING,
        attempt=1,
        claim=claim,
        hold=internal,
        bindings={
            "workspace": str(subject.repo),
            "branch": "main",
            "head": git(subject.repo, "rev-parse", "HEAD"),
        },
        evidence={
            "technical_rearm": {
                "id": internal.id,
                "phase": "continuing",
                "successor": dataclasses.asdict(claim),
            },
            # Opaque prior authority is retained; this transition does not mint or
            # reconstruct a permit, a task, a native result or an approval.
            "continuation_permits": {
                "spent": {
                    "consumption": {"state": "consumed", "successor_launch_id": claim.launch_id}
                }
            },
            "dev_attempts_spent": 3,
            "review_cycles_spent": 2,
            "developer_families": ["anthropic", "openai"],
            "unresolved_approval_requirements": ["Unfinished owner signoff"],
        },
    )
    snapshot = CampaignSnapshot(
        "campaign",
        "example/repo",
        CampaignSpec(
            "Terminal", units=(spec,), sources=(SourceSelector("stories", id="s", path="spec"),)
        ),
        {unit.id: unit},
        generation=2,
        config_revision=3,
        root=str(subject.repo),
        common_dir=str(subject.repo / ".git"),
    )
    snapshot.bindings["execution_template"] = {"path": str(path), "digest": digest}
    (store.directory(snapshot.id) / "holds").mkdir(parents=True)
    runtime = CampaignRuntime(subject.repo, store, clock=lambda: 100)
    runtime._git_activity_observer = activity.GitActivityObserver(host=Host(), wait_s=1)
    # Independent Git actor observation is real production validation over the
    # scripted, completed finalization record; no release guard is patched out.
    subject.record.update(state="completed", committed_head=unit.bindings["head"], context=None)
    subject.save()
    action = OutboxAction(
        "terminal-observation",
        "observe-worker",
        unit.id,
        {"launch_id": claim.launch_id},
        snapshot.generation,
    )
    startup = {
        key: request[key]
        for key in ("campaign_id", "unit_id", "launch_id", "attempt", "generation")
    }
    data = {
        "state": "interrupted",
        "quiescence_confirmed": True,
        "startup": startup,
        "head": unit.bindings["head"],
        "reason": "Scripted worker interrupted after all observed actors stopped",
    }
    assert runtime._observe_git_activity(snapshot, request, data) is None
    yield runtime, snapshot, unit, action, data, internal, request
    runtime.close_observers()


@pytest.mark.parametrize("boundary", ["interrupted", "promotion_interrupted", "promotion_finished"])
def test_interrupted_and_promotion_failures_retire_only_the_exact_rearm(terminal_case, boundary):
    runtime, snapshot, unit, action, data, _, request = terminal_case
    permits = copy.deepcopy(unit.evidence["continuation_permits"])
    if boundary.startswith("promotion"):
        data["object_promotion_error"] = "Owned Git object promotion could not be verified"
    if boundary == "promotion_finished":
        data["state"] = "finished"
        data["receipt"] = {
            **{
                key: request[key]
                for key in (
                    "campaign_id",
                    "unit_id",
                    "launch_id",
                    "attempt",
                    "generation",
                    "run_id",
                )
            },
            "outcome": "failed",
            "error": "Retained child failed",
            "head": data["head"],
        }
    runtime._apply_worker(snapshot, unit, action, data)
    assert unit.evidence["technical_rearm"]["phase"] == "finished"
    assert unit.evidence["continuation_permits"] == permits
    assert unit.evidence["dev_attempts_spent"] == 3
    assert unit.evidence["review_cycles_spent"] == 2
    assert unit.evidence["developer_families"] == ["anthropic", "openai"]
    if boundary == "interrupted":
        assert unit.hold.kind == "technical_failure"
        assert any(
            row.kind == "resolution-context"
            for row in proposals(snapshot, runtime.project, {}, now=100)
        )
    else:
        assert unit.hold.kind == "ownership_uncertain"
        assert unit.hold.reason == data["object_promotion_error"]
        assert unit.evidence["failure"]["detail"] == data["object_promotion_error"]


@pytest.mark.parametrize("boundary", ["interrupted", "promotion_finished"])
def test_owner_hold_terminal_then_release_promotes_actual_failure_not_finished_internal_gate(
    terminal_case, monkeypatch, boundary
):
    from bmad_loop.adapters import multiplexer

    runtime, snapshot, unit, action, data, internal, request = terminal_case
    runtime.control(
        snapshot,
        {
            "id": "owner-hold",
            "kind": "hold",
            "unit": unit.id,
            "reason": "Owner still controls release",
        },
    )
    owner = copy.deepcopy(unit.hold)
    assert unit.evidence["operator_prior_hold"] == dataclasses.asdict(internal)
    if boundary == "promotion_finished":
        data.update(
            state="finished",
            object_promotion_error="Exact promotion failure remains unresolved",
            receipt={
                **{
                    key: request[key]
                    for key in (
                        "campaign_id",
                        "unit_id",
                        "launch_id",
                        "attempt",
                        "generation",
                        "run_id",
                    )
                },
                "outcome": "failed",
                "error": "Retained child failed",
                "head": data["head"],
            },
        )
    runtime._apply_worker(snapshot, unit, action, data)
    assert unit.hold == owner
    assert "operator_prior_hold" not in unit.evidence
    assert unit.evidence["technical_rearm"]["retired_operator_prior_hold"] == dataclasses.asdict(
        internal
    )
    expected = "ownership_uncertain" if boundary == "promotion_finished" else "technical_failure"
    assert unit.evidence["pending_failure_hold"]["kind"] == expected

    # Script the external process/mux observations, retaining the production
    # release authority, Git readback, original hold and pending-error path.
    def pid_identity(path):
        assert path == Path(unit.claim.run_dir) / "engine.pid"
        return 900001, 1.0

    def liveness(pid, identity):
        assert (pid, identity) == (900001, 1.0)
        return "dead"

    def windows(name):
        assert name == runs.session_name(unit.claim.run_id)
        return []

    monkeypatch.setattr(runs, "read_named_pid_identity", pid_identity)
    monkeypatch.setattr(runs, "probe_liveness", liveness)
    monkeypatch.setattr(multiplexer, "configure_multiplexer", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        multiplexer, "get_multiplexer", lambda: SimpleNamespace(list_window_ids=windows)
    )
    runtime.control(snapshot, {"id": "owner-release", "kind": "release", "unit": unit.id})
    release = OutboxAction(
        "release-observation",
        "release-observation",
        unit.id,
        {"hold_id": owner.id, "command_id": "owner-release"},
        snapshot.generation,
    )
    proof = campaign_takeover.observe(runtime, snapshot, release)
    campaign_takeover.apply(runtime, snapshot, release, proof)
    assert unit.hold.kind == expected
    assert unit.hold.id == unit.claim.launch_id
    assert unit.hold.kind != "technical_rearm"
    assert not campaign_hold.active(
        campaign_hold.channel(runtime.store.directory(snapshot.id), unit.id),
        campaign=snapshot.id,
        unit=unit.id,
    )
    assert unit.evidence["unresolved_approval_requirements"] == ["Unfinished owner signoff"]


@pytest.mark.parametrize(
    "backup",
    [
        Hold("owner", "Other approval", id="other"),
        Hold("technical_rearm", "Other internal gate", id="other"),
    ],
)
def test_terminal_finish_preserves_other_hold_backups(terminal_case, backup):
    runtime, snapshot, unit, action, data, _, _ = terminal_case
    owner = Hold("operator", "New owner hold", id="current-owner")
    unit.hold = owner
    unit.evidence["operator_prior_hold"] = dataclasses.asdict(backup)
    runtime._apply_worker(snapshot, unit, action, data)
    assert unit.hold == owner
    assert unit.evidence["operator_prior_hold"] == dataclasses.asdict(backup)


def test_interruption_cannot_retire_a_different_startup_generation(terminal_case):
    runtime, snapshot, unit, action, data, internal, _ = terminal_case
    baseline = snapshot.clone()
    runtime._apply_worker(baseline, baseline.units[unit.id], action, copy.deepcopy(data))
    assert baseline.units[unit.id].evidence["technical_rearm"]["phase"] == "finished"
    data["startup"]["generation"] += 1
    with pytest.raises(CampaignError, match="stopped successor"):
        runtime._apply_worker(snapshot, unit, action, data)
    assert unit.evidence["technical_rearm"]["phase"] == "continuing"
    assert unit.hold == internal
