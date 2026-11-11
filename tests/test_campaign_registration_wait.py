"""Real bridge contention stays pre-dispatch; only its exact worker may retry."""

import dataclasses
import errno
import json
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
import test_campaign_runtime as runtime_fixture
from test_campaign_checkpoint_runtime import checkpoint_case as checkpoint_case
from test_campaign_launch_preparation import pending as pending
from test_campaign_message_bridge import FakeHcom, seed_room
from test_campaign_runtime import production_case as production_case

from bmad_loop import campaign_communication_runtime as communication
from bmad_loop import campaign_message_bridge as bridge
from bmad_loop import campaign_runtime
from bmad_loop.campaign_cli import status_document
from bmad_loop.campaign_managed_communication import ManagedCommunication
from bmad_loop.campaign_model import CampaignError, CommunicationSpec, Hold
from bmad_loop.campaign_supervisor import CampaignSupervisor
from bmad_loop.platform_util import LockUnavailableError, file_lock


@pytest.fixture(autouse=True)
def managed_spec(monkeypatch, tmp_path):
    room = tmp_path / "managed-room"
    seed_room(room)
    original = runtime_fixture.CampaignSpec

    def spec(*args, **kwargs):
        return dataclasses.replace(
            original(*args, **kwargs),
            communication=CommunicationSpec(mode="managed", room=str(room)),
        )

    monkeypatch.setattr(runtime_fixture, "CampaignSpec", spec)


@pytest.fixture
def registration(pending, monkeypatch, tmp_path):
    case, action = pending
    monkeypatch.setattr(
        bridge,
        "get_process_host",
        lambda: SimpleNamespace(identity=lambda pid: 42.5, liveness_of=lambda *args: "dead"),
    )
    fake = FakeHcom()
    manager = ManagedCommunication(
        case.store.directory(case.snapshot.id),
        executable="fake-hcom",
        runner=fake,
        clock=lambda: 100,
    )
    monkeypatch.setattr(
        case.runtime, "communication_manager", lambda directory: manager, raising=False
    )
    case.snapshot.phase = "active"
    case.snapshot.generation = 1
    case.unit.claim.generation = action.generation = 1
    action.payload.update(role="admission_review", stage="admission_review")
    inventory_action = dataclasses.replace(
        action, id="inventory", kind="inventory", unit="", payload={}
    )
    inventory = case.runtime._perform_inventory(case.snapshot, inventory_action)
    assert inventory.state == "confirmed"
    case.runtime.apply(case.snapshot, inventory_action, inventory)
    case.snapshot = case.store.save(case.snapshot, case.snapshot.revision)
    case.unit = case.snapshot.units[action.unit]
    action = next(row for row in case.snapshot.outbox if row.id == action.id)
    endpoint = communication.prepare(case.runtime, case.snapshot, unit=case.unit)
    assert endpoint is not None
    actor = manager._actor(endpoint["room"], endpoint["target"])
    directory = case.store.directory(case.snapshot.id) / "workers" / action.id
    return case, action, manager, fake, actor, directory


def wait(registration):
    case, action, _, fake, actor, directory = registration
    with file_lock(actor.directory / ".registration.lock", blocking=False):
        observed = case.runtime._perform_launch(case.snapshot, action)
    assert observed.state == "waiting"
    assert observed.retry_at is not None
    assert fake.count("start") == 1 and not case.processes
    assert not (directory / "request.json").exists()
    assert (directory / "registration-wait.json").is_file()
    return observed


def test_real_registration_contention_reconciles_one_actor_and_one_worker(registration):
    case, action, manager, fake, _, directory = registration
    original = (case.unit.attempt, case.unit.claim.attempt, case.unit.claim.launch_id)
    first = wait(registration)
    # Simulate losing the waiting effect reply: only the sealed file survives.
    case.snapshot = case.store.load(case.snapshot.id)
    action = next(row for row in case.snapshot.outbox if row.id == action.id)
    assert not action.receipt
    result = case.runtime.reconcile_action(case.snapshot, action)
    assert result.state == "confirmed" and result.data["launched"]
    assert len(case.processes) == 1 and fake.count("start") == 1
    process = next(iter(case.processes.values()))
    process.wait(timeout=20)
    assert process.returncode == 0, (directory / "child.log").read_text()
    receipt = json.loads((directory / "receipt.json").read_text())
    assert receipt["launch_id"] == action.id
    assert (directory / "registration-wait-consumed.json").is_file()
    assert (
        json.loads((directory / "registration-wait.json").read_text())
        == first.data["registration_wait"]
    )
    again = case.runtime.reconcile_action(case.snapshot, action)
    assert again.state == "confirmed" and again.data["launched"]
    assert len(case.processes) == 1 and fake.count("start") == 1
    assert (case.unit.attempt, case.unit.claim.attempt, case.unit.claim.launch_id) == original
    case.runtime.apply(case.snapshot, action, result)
    case.snapshot.units[action.unit].claim.quiescent = True
    assert manager.observe(case.snapshot)["available"] and not manager.has_open()


@pytest.mark.parametrize("control", ["stop", "hold"])
@pytest.mark.parametrize("during_prepare", [False, True])
def test_registration_retry_honors_current_control(
    registration, monkeypatch, control, during_prepare
):
    case, action, _, _, _, _ = registration
    wait(registration)

    def change():
        current = case.store.load(case.snapshot.id)
        if control == "stop":
            current.phase = "draining"
        else:
            current.units[action.unit].hold = Hold("operator", "owner pause")
        case.store.save(current, current.revision)

    if during_prepare:
        original = campaign_runtime.prepare_launch

        def prepare(*args, **kwargs):
            path = original(*args, **kwargs)
            change()
            return path

        monkeypatch.setattr(campaign_runtime, "prepare_launch", prepare)
    else:
        change()
    result = case.runtime.reconcile_action(case.snapshot, action)
    assert result.state == "confirmed" and result.data["not_launched"]
    assert not case.processes


@pytest.mark.parametrize(
    "artifact", ["request.json", "launch-intent.json", "registration-wait-consumed.json"]
)
@pytest.mark.parametrize("linked", [False, True])
def test_wait_proof_never_overrides_possible_dispatch(registration, artifact, linked):
    case, action, _, fake, _, directory = registration
    wait(registration)
    if linked:
        (directory / artifact).symlink_to(directory / "absent")
    else:
        (directory / artifact).write_text("{}")
    result = case.runtime.reconcile_action(case.snapshot, action)
    assert result.state == "unknown"
    assert not case.processes and fake.count("start") == 1


@pytest.mark.parametrize(
    "field,value",
    [
        ("generation", 999),
        ("attempt", 999),
        ("payload_digest", "different"),
        ("config_revision", 999),
    ],
)
def test_wait_proof_requires_frozen_binding(registration, field, value):
    case, action, _, _, _, directory = registration
    wait(registration)
    path = directory / "registration-wait.json"
    record = json.loads(path.read_text())
    record[field] = value
    path.write_text(json.dumps(record))
    assert case.runtime.reconcile_action(case.snapshot, action).state == "unknown"
    assert not case.processes


def test_failed_wait_proof_write_does_not_authorize_retry(registration, monkeypatch):
    case, action, _, _, actor, directory = registration
    original = campaign_runtime.atomic_write_text

    def fail(path, *args, **kwargs):
        if path.name == "registration-wait.json":
            raise OSError("marker disk failure")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(campaign_runtime, "atomic_write_text", fail)
    with file_lock(actor.directory / ".registration.lock", blocking=False):
        with pytest.raises(OSError, match="marker disk failure"):
            case.runtime._perform_launch(case.snapshot, action)
    assert not (directory / "registration-wait.json").exists()
    assert case.runtime.reconcile_action(case.snapshot, action).state == "unknown"
    assert not case.processes


@pytest.mark.parametrize("phase", ["request", "spawn"])
def test_consumed_wait_preserves_dispatch_failure_uncertainty(registration, monkeypatch, phase):
    case, action, _, _, _, directory = registration
    wait(registration)

    def fail(*args, **kwargs):
        raise CampaignError("dispatch acknowledgement lost")

    monkeypatch.setattr(campaign_runtime, "prepare_launch" if phase == "request" else "spawn", fail)
    with pytest.raises(CampaignError, match="dispatch acknowledgement lost"):
        case.runtime.reconcile_action(case.snapshot, action)
    assert (directory / "registration-wait-consumed.json").is_file()
    assert case.runtime.reconcile_action(case.snapshot, action).state == "unknown"
    assert not case.processes


@pytest.mark.parametrize(
    "error",
    [
        OSError(errno.EAGAIN, "body failure"),
        LockUnavailableError(errno.EIO, "bad lock", "/wrong/.registration.lock"),
        LockUnavailableError(errno.EAGAIN, "peer lock", "/wrong/.registration.lock"),
    ],
)
def test_unrelated_io_failure_is_not_registration_wait(registration, monkeypatch, error):
    case, action, manager, _, _, directory = registration

    def fail(*args, **kwargs):
        raise error

    monkeypatch.setattr(manager, "prepare", fail)
    with pytest.raises(OSError):
        case.runtime._perform_launch(case.snapshot, action)
    assert not (directory / "registration-wait.json").exists()
    assert case.runtime.reconcile_action(case.snapshot, action).state == "unknown"


def test_supervisor_harvest_exposes_wait_then_reconciles_one_launch(registration):
    case, action, manager, fake, actor, directory = registration
    supervisor = CampaignSupervisor(case.store, case.runtime)
    with ThreadPoolExecutor(max_workers=1) as executor:
        with file_lock(actor.directory / ".registration.lock", blocking=False):
            snapshot = supervisor._submit(case.snapshot, action, executor, reconcile=False)
            assert supervisor._active[action.id].result(timeout=10).state == "waiting"
            snapshot = supervisor._harvest(snapshot)
        row = next(row for row in snapshot.outbox if row.id == action.id)
        assert row.state == "sent_unknown" and row.receipt["registration_wait"]
        public = next(
            unit for unit in status_document(snapshot)["units"] if unit["id"] == action.unit
        )
        assert public["launch_wait"]["phase"] == "before_worker_request"
        assert public["launch_wait"]["reason"] == "managed-registration-busy"
        assert public["claim"]["status"] == "prepared" and public["claim"]["pid"] is None
        assert any(
            event["kind"] == "effect-wait" and event["reason"] == row.error
            for event in snapshot.events
        )
        snapshot = supervisor._submit(snapshot, row, executor, reconcile=True)
        assert supervisor._active[action.id].result(timeout=10).data["launched"]
        snapshot = supervisor._harvest(snapshot)
    assert len(case.processes) == 1 and fake.count("start") == 1
    process = next(iter(case.processes.values()))
    process.wait(timeout=20)
    assert process.returncode == 0
    assert (
        next(unit for unit in status_document(snapshot)["units"] if unit["id"] == action.unit)[
            "launch_wait"
        ]
        is None
    )
    claim = snapshot.units[action.unit].claim
    assert claim is not None
    claim.quiescent = True
    assert manager.observe(snapshot)["available"] and not manager.has_open()


@pytest.mark.parametrize("change", ["claim", "configuration"])
def test_retry_checks_fresh_store_ownership(registration, change):
    case, action, _, _, _, _ = registration
    wait(registration)
    current = case.store.load(case.snapshot.id)
    if change == "claim":
        current.units[action.unit].claim.launch_id = "replacement-launch"
    else:
        current.config_revision += 1
    case.store.save(current, current.revision)
    result = case.runtime.reconcile_action(case.snapshot, action)
    assert result.state == "unknown" and "ownership changed" in result.reason
    assert not case.processes


def test_failed_consumption_write_stays_pre_request_and_reuses_actor(registration, monkeypatch):
    case, action, _, fake, _, directory = registration
    wait(registration)
    original = campaign_runtime.atomic_write_text

    def fail(path, *args, **kwargs):
        if path.name == "registration-wait-consumed.json":
            raise OSError("consumption disk failure")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(campaign_runtime, "atomic_write_text", fail)
    with pytest.raises(OSError, match="consumption disk failure"):
        case.runtime.reconcile_action(case.snapshot, action)
    assert not (directory / "request.json").exists() and not case.processes
    monkeypatch.setattr(campaign_runtime, "atomic_write_text", original)
    result = case.runtime.reconcile_action(case.snapshot, action)
    assert result.state == "confirmed" and result.data["launched"]
    assert len(case.processes) == 1 and fake.count("start") == 1
    process = next(iter(case.processes.values()))
    process.wait(timeout=20)
    assert process.returncode == 0
