"""Foreground CLI service recovery against a persistent sandbox, without model calls."""

import json
import subprocess
import sys
import time
from contextlib import contextmanager

import pytest

from bmad_loop import cli
from bmad_loop.campaign_model import CampaignSnapshot, CampaignSpec, CampaignUnit, UnitSpec
from bmad_loop.campaign_runtime import store_for
from bmad_loop.campaign_store import add_action
from bmad_loop.campaign_usage import remaining


def test_service_recovers_killed_coordinator_and_honors_offline_stop(
    project, tmp_path, monkeypatch
):
    repo = project.project
    monkeypatch.setenv("BMAD_LOOP_STATE_DIR", str(tmp_path / "service-state"))
    store = store_for(repo)
    unit = CampaignUnit(
        UnitSpec("s:e:u", "s", "e", "Held work"),
        attempt=2,
        bindings={"workspace": str(repo), "branch": "service-test"},
        evidence={"dev_attempts_spent": 2, "review_cycles_spent": 1},
    )
    snapshot = CampaignSnapshot(
        "service-test",
        "a/b",
        CampaignSpec("service-test", units=(unit.spec,), poll_interval_s=0.05),
        {unit.id: unit},
        generation=1,
        common_dir=str(repo / ".git"),
        root=str(repo),
    )
    # Delayed inventory keeps the test offline. The checkpoint has exhausted
    # its write allowance.
    inventory = add_action(snapshot, "inventory", "", {})
    inventory.state, inventory.next_retry_at = "sent_unknown", 10**12
    backup = add_action(snapshot, "checkpoint", unit.id, {"launch_id": "prior-launch"})
    backup.state = "sent_unknown"
    cap = snapshot.spec.checkpoints.max_backup_failures
    backup.receipt.update(backup_failures=cap, backup_retry_exhausted=True)
    store.create(snapshot)
    service = [
        sys.executable,
        "-m",
        "bmad_loop",
        "campaign",
        "resume",
        snapshot.id,
        "--project",
        str(repo),
        "--active-only",
        "--json",
    ]

    @contextmanager
    def running(generation):
        process = subprocess.Popen(
            service, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )
        try:
            deadline = time.monotonic() + 30
            while store.load(snapshot.id).generation != generation:
                assert process.poll() is None, process.communicate()
                assert time.monotonic() < deadline, "service never acquired coordinator ownership"
                time.sleep(0.05)
            yield store.load(snapshot.id)
        finally:
            process.kill()
            process.communicate(timeout=15)

    with running(2) as first:
        reconciled = next(a for a in first.outbox if a.id == backup.id)
        assert reconciled.receipt["backup_failures"] == cap
        assert reconciled.receipt["backup_retry_exhausted"]
        assert "exhausted backup readback unavailable" in reconciled.error
        events = [e["kind"] for e in first.events]
        assert events.index("effect-wait") < events.index("coordinator-start")

    control = ["--project", str(repo), "--json"]
    assert (
        cli.main(
            ["campaign", "hold", snapshot.id, "--unit", unit.id, "--reason", "owner", *control]
        )
        == 0
    )
    with running(3) as recovered:
        recovered_unit = recovered.units[unit.id]
        assert recovered_unit.hold.kind == "operator"
        assert recovered_unit.attempt == unit.attempt
        assert remaining(recovered, recovered_unit) == (1, 2)
        assert next(a for a in recovered.outbox if a.id == backup.id).receipt == reconciled.receipt

    assert cli.main(["campaign", "stop", snapshot.id, "--terminate-workers", *control]) == 0
    for _ in range(2):
        result = subprocess.run(service, capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout)["campaign"]["phase"] == "stopped"
        stopped = store.load(snapshot.id)
        assert stopped.generation == 4
        assert stopped.bindings["terminate_requested"]
        assert stopped.units[unit.id].hold.kind == "operator"
        assert {a.id for a in stopped.outbox} == {inventory.id, backup.id}
    assert [s.id for s in store.list()] == [snapshot.id]


def test_service_finishes_an_interrupted_drain(project, tmp_path, monkeypatch):
    from bmad_loop.campaign_runtime import CampaignRuntime
    from bmad_loop.campaign_supervisor import CampaignSupervisor

    repo = project.project
    monkeypatch.setenv("BMAD_LOOP_STATE_DIR", str(tmp_path / "service-state"))
    store = store_for(repo)
    unit = CampaignUnit(UnitSpec("s:e:u", "s", "e", "Draining work"))
    saved = store.create(
        CampaignSnapshot(
            "drain-test",
            "a/b",
            CampaignSpec("drain-test", units=(unit.spec,)),
            {unit.id: unit},
            generation=1,
            phase="draining",
            bindings={"terminate_requested": True},
            common_dir=str(repo / ".git"),
            root=str(repo),
        )
    )
    service = CampaignSupervisor(store, CampaignRuntime(repo, store))
    drained = service.run(saved.id, active_only=True, max_ticks=1)
    assert drained.phase == "stopped"
    assert drained.generation == 2
    assert drained.bindings["terminate_requested"]
    assert [e["kind"] for e in drained.events][-1] == "campaign-drained"


@pytest.mark.parametrize(
    ("phase", "generation"),
    [("stopped", 1), ("delivered", 1), ("held", 1), ("active", 0)],
)
def test_service_uses_phase_observed_under_coordinator_lock(
    project, tmp_path, monkeypatch, phase, generation
):
    from bmad_loop.campaign_runtime import CampaignRuntime
    from bmad_loop.campaign_supervisor import CampaignSupervisor

    repo = project.project
    monkeypatch.setenv("BMAD_LOOP_STATE_DIR", str(tmp_path / "service-state"))
    store = store_for(repo)
    unit = CampaignUnit(UnitSpec("s:e:u", "s", "e", "Service work"))
    saved = store.create(
        CampaignSnapshot(
            "service-test",
            "a/b",
            CampaignSpec("service-test", units=(unit.spec,)),
            {unit.id: unit},
            generation=generation,
            common_dir=str(repo / ".git"),
            root=str(repo),
        )
    )
    coordinator = store.coordinator
    revisions = []

    @contextmanager
    def after_prior_owner(*args, **kwargs):
        with coordinator(*args, **kwargs):
            current = store.load(saved.id)
            current.phase = phase
            revisions.append(store.save(current, current.revision).revision)
            yield

    monkeypatch.setattr(store, "coordinator", after_prior_owner)
    runtime = CampaignRuntime(repo, store)
    monkeypatch.setattr(
        runtime, "prepare_resume", lambda *args, **kwargs: pytest.fail("inactive campaign resumed")
    )
    service = CampaignSupervisor(store, runtime)
    result = service.run(saved.id, active_only=True, max_ticks=1)
    assert result.phase == phase
    assert [result.revision] == revisions
    assert result.generation == generation
