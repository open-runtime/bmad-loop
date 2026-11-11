"""Native reasoning retains its exact job reservation while its bridge is busy."""

import dataclasses
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_campaign_headless import prepared as prepared
from test_campaign_message_bridge import FakeHcom, seed_room
from test_campaign_reasoning_jobs import case

from bmad_loop import campaign_communication_runtime as communication
from bmad_loop import campaign_message_bridge as bridge
from bmad_loop.campaign_cli import status_document
from bmad_loop.campaign_managed_communication import ManagedCommunication
from bmad_loop.campaign_model import CommunicationSpec, Hold
from bmad_loop.campaign_reasoning_flow import apply, perform
from bmad_loop.platform_util import file_lock


@pytest.fixture
def reasoning_registration(tmp_path, prepared, monkeypatch):
    room = tmp_path / "managed-room"
    seed_room(room)
    monkeypatch.setattr(
        bridge,
        "get_process_host",
        lambda: SimpleNamespace(identity=lambda pid: 42.5, liveness_of=lambda *args: "dead"),
    )

    def configure(snapshot):
        snapshot.spec = dataclasses.replace(
            snapshot.spec, communication=CommunicationSpec(mode="managed", room=str(room))
        )

    snapshot, driver, supervisor, _ = case(tmp_path, prepared, count=1, configure=configure)
    snapshot = supervisor._queue(snapshot, driver.observe_workers(snapshot))
    action = next(item for item in snapshot.outbox if item.kind == "reasoning-launch")
    fake = FakeHcom()
    manager = ManagedCommunication(
        driver.store.directory(snapshot.id), executable="fake-hcom", runner=fake, clock=lambda: 100
    )
    monkeypatch.setattr(driver, "communication_manager", lambda directory: manager, raising=False)
    job = snapshot.jobs["job-0"]
    endpoint = communication.prepare(driver, snapshot, job=job)
    assert endpoint is not None
    actor = manager._actor(endpoint["room"], endpoint["target"])
    return snapshot, driver, action, job, fake, actor, prepared[0]


def wait(case):
    snapshot, driver, action, job, fake, actor, runner = case
    with file_lock(actor.directory / ".registration.lock", blocking=False):
        observed = perform(driver, snapshot, action)
    assert observed.state == "waiting"
    assert "registration busy" in observed.reason
    assert fake.count("start") == 1 and not runner.mux.processes
    assert not (Path(job["claim"]["run_dir"]) / "request.json").exists()
    return observed


def test_real_reasoning_registration_lock_retries_same_job_actor_attempt(reasoning_registration):
    snapshot, driver, action, job, fake, _, runner = reasoning_registration
    original = (job["attempt"], dict(job["claim"]), job.get("failure_attempts", 0))
    wait(reasoning_registration)
    # A lost waiting reply is retried through the existing native intent protocol.
    result = perform(driver, driver.store.load(snapshot.id), action, reconcile=True)
    assert result.state == "confirmed" and result.data["state"] == "running"
    assert len(runner.mux.processes) == 1 and fake.count("start") == 1
    process = next(iter(runner.mux.processes.values()))
    process.wait(timeout=20)
    assert process.returncode == 0
    result = perform(driver, snapshot, action, reconcile=True)
    assert result.state == "confirmed" and result.data["state"] == "finished"
    assert result.data["receipt"]["outcome"] == "completed"
    assert (job["attempt"], job["claim"], job.get("failure_attempts", 0)) == original
    apply(driver, snapshot, action, result)
    assert job["claim"]["quiescent"] and len(runner.mux.processes) == 1


@pytest.mark.parametrize("control", ["stop", "hold"])
def test_reasoning_registration_retry_rechecks_current_control(reasoning_registration, control):
    snapshot, driver, action, job, _, _, runner = reasoning_registration
    wait(reasoning_registration)
    current = driver.store.load(snapshot.id)
    if control == "stop":
        current.phase = "draining"
    else:
        current.units[job["request"]["unit"]].hold = Hold("operator", "owner pause")
    driver.store.save(current, current.revision)
    result = perform(driver, snapshot, action, reconcile=True)
    assert result.state == "confirmed" and result.data["state"] == "deferred"
    apply(driver, snapshot, action, result)
    assert job["state"] == "proposed" and job["claim"]["status"] == "not_launched"
    assert not runner.mux.processes and not job.get("failure_attempts", 0)


@pytest.mark.parametrize("linked", [False, True])
@pytest.mark.parametrize(
    "artifact", ["request.json", "launch-intent.json", "startup.json", "receipt.json"]
)
def test_reasoning_busy_does_not_override_possible_native_dispatch(
    reasoning_registration, monkeypatch, linked, artifact
):
    snapshot, driver, action, job, _, actor, runner = reasoning_registration
    manager = driver.communication_manager(None)
    original = manager.prepare
    directory = Path(job["claim"]["run_dir"])

    def prepare(*args, **kwargs):
        directory.mkdir(parents=True, exist_ok=True)
        intent = directory / artifact
        if linked:
            intent.symlink_to(directory / "missing")
        else:
            intent.write_text(json.dumps({"state": "sent_unknown"}))
        return original(*args, **kwargs)

    monkeypatch.setattr(manager, "prepare", prepare)
    with file_lock(actor.directory / ".registration.lock", blocking=False):
        result = perform(driver, snapshot, action)
    assert result.state == "unknown"
    assert not runner.mux.processes and not job.get("failure_attempts", 0)


def test_reasoning_supervisor_wait_is_visible_without_attempt_failure(reasoning_registration):
    from concurrent.futures import Future

    from bmad_loop.campaign_supervisor import CampaignSupervisor

    snapshot, driver, action, job, fake, _, runner = reasoning_registration
    result = wait(reasoning_registration)
    action.state = "sent_unknown"
    original = (job["attempt"], job.get("failure_attempts", 0), job["claim"]["launch_id"])
    supervisor = CampaignSupervisor(driver.store, driver)
    future = Future()
    future.set_result(result)
    supervisor._active[action.id] = future
    snapshot = supervisor._harvest(snapshot)
    job = snapshot.jobs[job["id"]]
    assert (job["attempt"], job.get("failure_attempts", 0), job["claim"]["launch_id"]) == original
    public = next(row for row in status_document(snapshot)["jobs"] if row["id"] == job["id"])
    assert public["launch_wait"]["phase"] == "before_native_request"
    assert public["launch_wait"]["reason"] == "managed-registration-busy"
    assert job["state"] == "prepared" and job["claim"]["status"] == "prepared"
    assert not runner.mux.processes and fake.count("start") == 1
