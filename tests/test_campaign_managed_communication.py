"""Indexed shared-room communication with no real hcom or native launches."""

import dataclasses
import json
import subprocess
from types import SimpleNamespace

import pytest
from test_campaign_message_bridge import FakeHcom, seed_room

from bmad_loop import campaign_message_bridge as bridge
from bmad_loop.campaign_managed_communication import ManagedCommunication
from bmad_loop.campaign_model import (
    CampaignError,
    CampaignSnapshot,
    CampaignSpec,
    CampaignUnit,
    CommunicationSpec,
    OutboxAction,
    UnitSpec,
    UnitState,
    WorkerClaim,
)


@pytest.fixture
def managed(tmp_path, monkeypatch):
    room = tmp_path / "room"
    seed_room(room)
    host = SimpleNamespace(identity=lambda pid: 42.5, liveness_of=lambda *args: "dead")
    monkeypatch.setattr(bridge, "get_process_host", lambda: host)
    snapshot = CampaignSnapshot(
        "campaign",
        "owner/repo",
        CampaignSpec("mail", communication=CommunicationSpec(mode="managed", room=str(room))),
    )
    for index in (1, 2):
        unit = CampaignUnit(UnitSpec(f"u{index}", "source", "epic", f"Story {index}"))
        unit.claim = WorkerClaim(unit.spec.id, 1, 1, f"launch-{index}")
        snapshot.units[unit.spec.id] = unit
    fake = FakeHcom()
    manager = ManagedCommunication(
        tmp_path / "campaign", executable="fake-hcom", runner=fake, clock=lambda: 100
    )
    return manager, snapshot, fake, host


def target(snapshot, unit="u1"):
    claim = snapshot.units[unit].claim
    return {
        "campaign_id": snapshot.id,
        "unit_id": unit,
        "launch_id": claim.launch_id,
        "generation": claim.generation,
    }


def send(
    fake, endpoint, text="Preserve the agreed acceptance", *, generation=None, intent="request"
):
    selected = dict(endpoint["target"])
    if generation is not None:
        selected["generation"] = generation
    return fake.message(
        json.dumps({"bmad": 1, "kind": "steer", "target": selected, "text": text}),
        name=endpoint["name"],
        intent=intent,
    )


def test_shared_room_observation_reads_once_and_queues_exact_targets(managed):
    manager, snapshot, fake, _ = managed
    first = manager.prepare(snapshot, target(snapshot), {"epic": "epic", "issue": 1, "pr": 2})
    second = manager.prepare(snapshot, target(snapshot, "u2"))
    send(fake, first)
    send(fake, second, "Finish story 2")
    before = len(fake.calls)
    result = manager.observe(snapshot)
    assert result["available"], result
    assert result["counts"] == {"accepting": 2}
    assert result["queued_messages"] == 2
    observed = [argv[1] for argv, _ in fake.calls[before:]]
    assert observed.count("list") == 1 and observed.count("events") == 2
    assert len(manager.ledger.pending("u1")) == len(manager.ledger.pending("u2")) == 1
    assert manager.ledger.pending("u1")[0]["source"]["target"] == first["target"]
    assert manager.ledger.pending("u1")[0]["authority"] == "advisory"
    assert not snapshot.bindings and not snapshot.events
    again = manager.observe(snapshot)
    assert again["available"] and again["queued_messages"] == 0
    assert fake.count("send") == 2


def test_open_gate_and_status_use_current_claim_facts_without_losing_labels(managed):
    manager, snapshot, fake, _ = managed
    assert not manager.has_open()
    manager.prepare(
        snapshot, target(snapshot), {"label": "Keep this operator label", "model": "old"}
    )
    assert manager.has_open()
    unit = snapshot.units["u1"]
    unit.state = UnitState.RUNNING
    unit.bindings["pr"] = {"number": 123, "url": "https://github.com/owner/repo/pull/123"}
    unit.bindings["issue"] = {"number": 45}
    unit.claim.session_ids = ["native-current"]
    snapshot.outbox.append(
        OutboxAction(
            "launch-1",
            "launch",
            "u1",
            {"stage": "repair", "candidate": {"model": "astra"}},
            generation=1,
        )
    )
    endpoint = manager.observe(snapshot)["endpoints"][0]
    assert endpoint["facts"]["label"] == "Keep this operator label"
    assert endpoint["facts"]["model"] == "astra" and endpoint["facts"]["stage"] == "repair"
    assert endpoint["facts"]["pr"]["number"] == 123 and endpoint["facts"]["issue"]["number"] == 45
    assert endpoint["facts"]["endpoint_kind"] == "bridge" and not endpoint["facts"]["model_reply"]
    assert endpoint["native_session_id"] == "native-current"
    unit.claim.quiescent = True
    assert manager.observe(snapshot)["available"]
    assert not manager.has_open()


def test_old_generation_receives_status_without_queued_steering(managed):
    manager, snapshot, fake, _ = managed
    endpoint = manager.prepare(snapshot, target(snapshot))
    event = send(fake, endpoint, generation=2)
    result = manager.observe(snapshot)
    assert result["available"] and result["queued_messages"] == 0
    assert manager.ledger.list() == []
    record = bridge.read(
        manager._actor(endpoint["room"], endpoint["target"]).directory
        / "inbox"
        / f"{event['id']}.json"
    )
    assert record["disposition"] == "stale"


def test_ledger_failure_recovers_after_bridge_ack_without_losing_or_duplicating_mail(
    managed, monkeypatch
):
    manager, snapshot, fake, _ = managed
    endpoint = manager.prepare(snapshot, target(snapshot))
    send(fake, endpoint)
    accept = manager.ledger.accept
    monkeypatch.setattr(
        manager.ledger,
        "accept",
        lambda **kwargs: (_ for _ in ()).throw(CampaignError("temporary ledger failure")),
    )
    first = manager.observe(snapshot)
    assert not first["available"] and fake.count("send") == 1
    monkeypatch.setattr(manager.ledger, "accept", accept)
    second = manager.observe(snapshot)
    assert second["available"], second
    assert len(manager.ledger.pending("u1")) == 1
    assert fake.count("send") == 1


def test_lost_registration_is_indexed_and_reconciled_by_observation(managed):
    manager, snapshot, fake, _ = managed
    fake.lose = "start"
    with pytest.raises(subprocess.TimeoutExpired):
        manager.prepare(snapshot, target(snapshot))
    assert manager.endpoints()[0]["state"] == "registering"
    result = manager.observe(snapshot)
    assert result["available"], result
    assert result["counts"] == {"accepting": 1}
    assert fake.count("start") == 1


def test_terminal_ledger_closure_between_bridge_ack_and_enqueue_does_not_strand_room(
    managed, monkeypatch
):
    manager, snapshot, fake, _ = managed
    endpoint = manager.prepare(snapshot, target(snapshot))
    event = send(fake, endpoint)
    accept = manager.ledger.accept

    def close_first(**kwargs):
        manager.ledger.close_target(
            kwargs["unit"], job=kwargs["job"], reason="Unit delivered before enqueue"
        )
        return accept(**kwargs)

    monkeypatch.setattr(manager.ledger, "accept", close_first)
    observed = manager.observe(snapshot)
    assert observed["available"] and observed["queued_messages"] == 0, observed
    actor = manager._actor(endpoint["room"], endpoint["target"])
    receipt = bridge.read(actor.directory / "inbox" / f"{event['id']}.json")
    assert receipt["disposition"] == "closed" and receipt["closure_reason"]
    assert receipt["response_state"] == "confirmed"
    assert manager.ledger.list() == []
    assert manager.observe(snapshot)["available"] and fake.count("send") == 1


def test_lost_registration_can_drain_after_claim_becomes_quiescent(managed):
    manager, snapshot, fake, _ = managed
    fake.lose = "start"
    with pytest.raises(subprocess.TimeoutExpired):
        manager.prepare(snapshot, target(snapshot))
    snapshot.units["u1"].claim.quiescent = True
    result = manager.observe(snapshot)
    assert result["available"], result
    assert not manager.has_open()
    assert fake.count("start") == 1 and fake.count("stop") == 1


def test_missing_indexed_binding_does_not_create_another_identity(managed):
    manager, snapshot, fake, _ = managed
    endpoint = manager.prepare(snapshot, target(snapshot))
    actor = manager._actor(endpoint["room"], endpoint["target"])
    actor.path.unlink()
    with pytest.raises(bridge.BridgeError, match="binding is missing"):
        manager.prepare(snapshot, target(snapshot))
    result = manager.observe(snapshot)
    assert not result["available"] and manager.has_open()
    assert fake.count("start") == 1


def test_quiescent_claim_closes_bridge_and_late_generation_mail_stays_closed(managed):
    manager, snapshot, fake, _ = managed
    endpoint = manager.prepare(snapshot, target(snapshot))
    snapshot.units["u1"].claim.quiescent = True
    result = manager.observe(snapshot)
    assert result["available"], result
    assert result["counts"] == {"closed": 1} and fake.count("stop") == 1
    send(fake, endpoint)
    result = manager.observe(snapshot)
    assert result["available"] and result["queued_messages"] == 0
    assert manager.ledger.list() == []
    assert fake.count("send") == 0


def test_closed_tombstone_adopts_dead_coordinator_for_late_mail(managed):
    manager, snapshot, fake, host = managed
    endpoint = manager.prepare(snapshot, target(snapshot))
    snapshot.units["u1"].claim.quiescent = True
    assert manager.observe(snapshot)["available"]
    actor = manager._actor(endpoint["room"], endpoint["target"])
    record = bridge.read(actor.path)
    record.update(pid=987654, identity=3.0)
    bridge.write(actor.path, record)
    send(fake, endpoint)
    result = manager.observe(snapshot)
    assert result["available"], result
    assert fake.count("start") == fake.count("stop") == 1
    assert manager.ledger.list() == [] and bridge.read(actor.path)["previous_owners"]


@pytest.mark.parametrize("liveness", ["dead", "alive", "unknown"])
def test_coordinator_restart_preserves_endpoint_generation_and_requires_dead_predecessor(
    managed, liveness
):
    manager, snapshot, fake, host = managed
    endpoint = manager.prepare(snapshot, target(snapshot))
    actor = manager._actor(endpoint["room"], endpoint["target"])
    record = bridge.read(actor.path)
    record.update(pid=987654, identity=3.0)
    bridge.write(actor.path, record)
    host.liveness_of = lambda *args: liveness
    result = manager.observe(snapshot)
    assert result["available"] is (liveness == "dead")
    assert fake.count("start") == 1 and fake.count("stop") == 0
    assert manager.endpoints()[0]["target"] == endpoint["target"]
    if liveness == "dead":
        assert bridge.read(actor.path)["previous_owners"]


def test_global_reasoning_job_uses_campaign_ledger_sentinel(managed):
    manager, snapshot, fake, _ = managed
    snapshot.jobs["job"] = {
        "request": {"unit": ""},
        "claim": {"launch_id": "job-launch", "generation": 1, "quiescent": False},
    }
    endpoint = manager.prepare(
        snapshot,
        {"campaign_id": snapshot.id, "job_id": "job", "launch_id": "job-launch", "generation": 1},
    )
    send(fake, endpoint)
    assert manager.observe(snapshot)["available"]
    assert len(manager.ledger.pending("@campaign", job="job")) == 1
    assert manager.ledger.pending("@campaign", job="other") == []


def test_closed_job_bridge_mail_is_closed_after_later_archive_without_new_events(managed):
    manager, snapshot, fake, _ = managed
    snapshot.jobs["job"] = {
        "state": "running",
        "request": {"unit": ""},
        "claim": {"launch_id": "job-launch", "generation": 1, "quiescent": False},
    }
    endpoint = manager.prepare(
        snapshot,
        {"campaign_id": snapshot.id, "job_id": "job", "launch_id": "job-launch", "generation": 1},
    )
    send(fake, endpoint, text="x" * 16384)
    assert manager.observe(snapshot)["available"]
    snapshot.jobs["job"]["state"] = "validated"
    snapshot.jobs["job"]["claim"]["quiescent"] = True
    assert manager.observe(snapshot)["available"]
    assert not manager.has_open()
    assert manager.ledger.list(job="job")[0]["state"] == "queued"
    snapshot.jobs.pop("job")
    assert manager.observe(snapshot)["available"]
    assert manager.ledger.list(job="job")[0]["state"] == "closed"


def test_prepare_requires_exact_live_claim_before_external_side_effect(managed):
    manager, snapshot, fake, _ = managed
    with pytest.raises(CampaignError, match="owned launch claim"):
        manager.prepare(snapshot, {**target(snapshot), "generation": 2})
    assert fake.calls == [] and not manager.path.exists()
    snapshot.units["u1"].claim.quiescent = True
    with pytest.raises(CampaignError, match="owned launch claim"):
        manager.prepare(snapshot, target(snapshot))
    assert fake.calls == []


def test_room_database_reset_is_visible_without_ack_or_cursor_advance(managed):
    manager, snapshot, fake, _ = managed
    fake.message("historical", event_id=10)
    endpoint = manager.prepare(snapshot, target(snapshot))
    fake.events.clear()
    send(fake, endpoint)
    result = manager.observe(snapshot)
    assert not result["available"] and any("reset" in error for error in result["errors"])
    assert manager.ledger.list() == [] and fake.count("send") == 0
    assert manager.endpoints()[0]["cursor"] == 10


def test_bounded_room_pages_retain_all_inform_messages(managed):
    manager, snapshot, fake, _ = managed
    endpoint = manager.prepare(snapshot, target(snapshot))
    for index in range(220):
        send(fake, endpoint, text=f"Advisory {index}", intent="inform")
    assert manager.observe(snapshot)["queued_messages"] == 100
    assert manager.observe(snapshot)["queued_messages"] == 100
    assert manager.observe(snapshot)["queued_messages"] == 20
    assert len(manager.ledger.list(limit=500)) == 220
    assert fake.count("send") == 0


def test_observe_mode_has_no_managed_side_effects_and_endpoint_pages_are_bounded(managed):
    manager, snapshot, fake, _ = managed
    first = manager.prepare(snapshot, target(snapshot))
    second = manager.prepare(snapshot, target(snapshot, "u2"))
    page = manager.endpoints(limit=1)
    assert page[0]["target"] == first["target"]
    assert manager.endpoints(after=page[0]["sequence"], limit=1)[0]["target"] == second["target"]
    before = len(fake.calls)
    snapshot.spec = dataclasses.replace(
        snapshot.spec,
        communication=dataclasses.replace(snapshot.spec.communication, mode="observe"),
    )
    assert manager.observe(snapshot)["mode"] == "observe"
    assert len(fake.calls) == before
