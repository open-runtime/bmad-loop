"""Proven pre-request refusals release capacity without replaying uncertain launches."""

import dataclasses
import json
from concurrent.futures import Future
from types import SimpleNamespace

import pytest
from test_campaign_checkpoint_runtime import checkpoint_case as checkpoint_case
from test_campaign_runtime import production_case as production_case

from bmad_loop import campaign_runtime, campaign_stage_prompt
from bmad_loop.campaign_boundary_sync import before_proposal
from bmad_loop.campaign_model import CampaignError, Hold, OutboxAction, UnitState
from bmad_loop.campaign_supervisor import CampaignSupervisor, Proposal


@pytest.fixture
def pending(checkpoint_case):
    case = checkpoint_case
    case.unit.claim.quiescent = True
    for state in (
        UnitState.LOCALLY_COMPLETE,
        UnitState.SYNCING,
        UnitState.VERIFYING,
        UnitState.REVIEWING,
    ):
        case.unit.state = state
        case.snapshot = case.store.save(case.snapshot, case.snapshot.revision)
        case.unit = case.snapshot.units[case.unit.id]
    case.unit.claim = dataclasses.replace(
        case.unit.claim,
        launch_id="preparation-test",
        writes=False,
        status="prepared",
        role="publication_review",
        resource="review",
        quiescent=False,
    )
    action = OutboxAction(
        "preparation-test",
        "launch",
        case.unit.id,
        {
            "kind": "stage",
            "role": "triage",
            "stage": "triage",
            "candidate": {"profile": "claude", "model": "fixture"},
            "read_only": True,
            "stage_boundary": {"inputs": {}},
        },
        case.snapshot.generation,
        state="sent_unknown",
    )
    case.snapshot.outbox.append(action)
    return case, action


def fail_context(*args, **kwargs):
    raise CampaignError("required campaign obligations exceed the inline context bound")


def harvest(case, action, observed):
    supervisor = CampaignSupervisor(case.store, case.runtime)
    future = Future()
    future.set_result(observed)
    supervisor._active[action.id] = future
    case.snapshot = supervisor._harvest(case.snapshot)
    case.unit = case.snapshot.units[action.unit]
    return next(row for row in case.snapshot.outbox if row.id == action.id)


def test_pre_request_refusal_is_durable_quiescent_and_recovers_after_explicit_resume(
    pending, monkeypatch
):
    case, action = pending
    original = campaign_stage_prompt.prepare_context
    monkeypatch.setattr(campaign_stage_prompt, "prepare_context", fail_context)
    observed = case.runtime._perform_launch(case.snapshot, action)
    assert observed.state == "confirmed" and observed.data["not_launched"]
    refused = observed.data["preparation_refused"]
    assert refused["phase"] == "before_request"
    directory = case.store.directory(case.snapshot.id) / "workers" / action.id
    assert (directory / "preparation-refused.json").is_file()
    assert not (directory / "request.json").exists()
    # Lost acknowledgement is reconciled from proof, never by absence of startup.
    restored = case.runtime.reconcile_action(case.snapshot, action)
    assert restored == observed
    confirmed = harvest(case, action, restored)
    assert confirmed.state == "confirmed"
    assert case.unit.claim.quiescent and case.unit.claim.status == "not_launched"
    assert case.unit.hold.kind == "launch_preparation"
    assert any(row["kind"] == "launch-preparation-refused" for row in case.snapshot.events)
    # Active scheduling never automatically retries the bad inputs.
    assert not any(
        row.unit == case.unit.id and row.kind == "launch"
        for row in case.runtime.proposals(case.snapshot)
    )
    assert case.unit.hold.kind == "launch_preparation"
    old_proof = (directory / "preparation-refused.json").read_bytes()
    case.unit.evidence["stage_boundary_ready"] = {"old": True}
    monkeypatch.setattr(campaign_stage_prompt, "prepare_context", original)
    assert case.runtime.prepare_resume(case.snapshot)
    assert case.unit.hold is None
    assert "stage_boundary_ready" not in case.unit.evidence
    assert case.unit.evidence["launch_preparation_history"] == [refused]
    payload = {key: value for key, value in action.payload.items() if key != "stage_boundary"}
    proposal = before_proposal(
        case.runtime, case.snapshot, Proposal("launch", case.unit.id, payload)
    )
    assert proposal.kind == "stage-boundary-before"
    # The ordinary before-stage observer earns fresh input before a new action.
    boundary = OutboxAction(
        "fresh-boundary", proposal.kind, proposal.unit, proposal.payload, case.snapshot.generation
    )
    result = case.runtime.perform(case.snapshot, boundary)
    assert result.state == "confirmed"
    case.runtime.apply(case.snapshot, boundary, result)
    fresh = before_proposal(case.runtime, case.snapshot, Proposal("launch", case.unit.id, payload))
    assert fresh.kind == "launch" and fresh.payload["stage_boundary"]["id"] == boundary.id
    retry = OutboxAction(
        "fresh-launch",
        "launch",
        case.unit.id,
        fresh.payload,
        case.snapshot.generation,
        state="sent_unknown",
    )
    case.unit.claim = dataclasses.replace(
        case.unit.claim, launch_id=retry.id, quiescent=False, status="prepared"
    )
    launches = []
    monkeypatch.setattr(
        campaign_runtime,
        "spawn",
        lambda path, **kwargs: launches.append(path) or SimpleNamespace(pid=12345),
    )
    launched = case.runtime._perform_launch(case.snapshot, retry)
    assert launched.state == "confirmed" and launched.data["launched"]
    assert len(launches) == 1 and launches[0].parent.name == retry.id
    assert (directory / "preparation-refused.json").read_bytes() == old_proof


def test_successful_peer_stage_does_not_rearm_held_launch(pending, monkeypatch):
    case, action = pending
    monkeypatch.setattr(campaign_stage_prompt, "prepare_context", fail_context)
    observed = case.runtime._perform_launch(case.snapshot, action)
    harvest(case, action, observed)
    held = case.unit.hold
    refusal = case.unit.evidence["launch_preparation_refusal"]
    case.unit.evidence["stage_boundary_ready"] = {"old": True}

    peer = case.snapshot.units["source:project:second"]
    peer.state = UnitState.DRAFT
    peer.claim = dataclasses.replace(
        case.unit.claim,
        unit=peer.id,
        launch_id="peer-admission",
        role="admission_review",
        status="running",
        quiescent=False,
    )
    receipt = {
        "launch_id": peer.claim.launch_id,
        "outcome": "stage_complete",
        "stage": "admission_review",
        "head": case.unit.bindings["head"],
        "validated_admission": {"unit_id": peer.id, "disposition": "new_work"},
    }
    observation = OutboxAction(
        "observe-peer",
        "observe-worker",
        peer.id,
        {"launch_id": peer.claim.launch_id},
        case.snapshot.generation,
    )
    case.runtime._apply_worker(
        case.snapshot, peer, observation, {"state": "finished", "receipt": receipt}
    )
    assert peer.state == UnitState.READY and peer.claim.quiescent
    assert peer.evidence["admission"] == receipt["validated_admission"]
    assert case.unit.hold == held and held.kind == "launch_preparation"
    assert case.unit.evidence["launch_preparation_refusal"] == refusal
    assert "launch_preparation_history" not in case.unit.evidence
    assert not any(row["kind"] == "launch-preparation-rearmed" for row in case.snapshot.events)

    # An explicit operator resume still retries only after a fresh stage boundary.
    assert case.runtime.prepare_resume(case.snapshot)
    assert case.unit.hold is None
    assert case.unit.evidence["launch_preparation_history"] == [refusal]
    assert "stage_boundary_ready" not in case.unit.evidence
    payload = {key: value for key, value in action.payload.items() if key != "stage_boundary"}
    fresh = before_proposal(case.runtime, case.snapshot, Proposal("launch", case.unit.id, payload))
    assert fresh.kind == "stage-boundary-before"


@pytest.mark.parametrize(
    "field,value",
    [("generation", 999), ("schema_version", True), ("unit_id", "other"), ("attempt", 999)],
)
def test_preparation_refusal_requires_exact_claim_binding(pending, monkeypatch, field, value):
    case, action = pending
    monkeypatch.setattr(campaign_stage_prompt, "prepare_context", fail_context)
    case.runtime._perform_launch(case.snapshot, action)
    path = (
        case.store.directory(case.snapshot.id) / "workers" / action.id / "preparation-refused.json"
    )
    record = json.loads(path.read_text(encoding="utf-8"))
    record[field] = value
    path.write_text(json.dumps(record), encoding="utf-8")
    observed = case.runtime.reconcile_action(case.snapshot, action)
    assert observed.state == "unknown"
    assert case.unit.claim.quiescent is False


@pytest.mark.parametrize("artifact", ["request.json", "startup.json", "receipt.json"])
@pytest.mark.parametrize("dangling", [False, True])
def test_preparation_proof_cannot_override_possible_launch(
    pending, monkeypatch, artifact, dangling
):
    case, action = pending
    monkeypatch.setattr(campaign_stage_prompt, "prepare_context", fail_context)
    case.runtime._perform_launch(case.snapshot, action)
    directory = case.store.directory(case.snapshot.id) / "workers" / action.id
    if dangling:
        (directory / artifact).symlink_to(directory / "missing-target")
    else:
        (directory / artifact).write_text("{}", encoding="utf-8")
    assert case.runtime.reconcile_action(case.snapshot, action).state == "unknown"
    assert case.runtime._perform_launch(case.snapshot, action).state == "unknown"
    assert not case.unit.claim.quiescent


@pytest.mark.parametrize("hold", ["operator", "owner"])
def test_explicit_resume_preserves_owner_holds(pending, monkeypatch, hold):
    case, action = pending
    monkeypatch.setattr(campaign_stage_prompt, "prepare_context", fail_context)
    observed = case.runtime._perform_launch(case.snapshot, action)
    case.unit.hold = Hold(hold, "Owner pause", id="owner-pause")
    harvest(case, action, observed)
    case.runtime.prepare_resume(case.snapshot)
    assert case.unit.hold.kind == hold and case.unit.hold.id == "owner-pause"
    assert case.unit.claim.quiescent


@pytest.mark.parametrize("phase", ["prepare_request", "spawn"])
def test_request_or_spawn_failure_remains_uncertain(pending, monkeypatch, phase):
    case, action = pending
    directory = case.store.directory(case.snapshot.id) / "workers" / action.id

    def fail(*args, **kwargs):
        raise CampaignError("unknown child launch outcome")

    monkeypatch.setattr(
        campaign_runtime, "prepare_launch" if phase == "prepare_request" else "spawn", fail
    )
    with pytest.raises(CampaignError, match="unknown child launch"):
        case.runtime._perform_launch(case.snapshot, action)
    assert not (directory / "preparation-refused.json").exists()
    assert case.runtime.reconcile_action(case.snapshot, action).state == "unknown"
    assert not case.unit.claim.quiescent


@pytest.mark.parametrize("shape", ["link", "dangling", "directory", "oversized"])
def test_refusal_authority_must_be_bounded_regular_and_not_redirected(pending, monkeypatch, shape):
    case, action = pending
    monkeypatch.setattr(campaign_stage_prompt, "prepare_context", fail_context)
    case.runtime._perform_launch(case.snapshot, action)
    path = (
        case.store.directory(case.snapshot.id) / "workers" / action.id / "preparation-refused.json"
    )
    if shape in {"link", "dangling"}:
        target = path.with_name("other.json")
        if shape == "link":
            target.write_bytes(path.read_bytes())
        path.unlink()
        path.symlink_to(target)
    elif shape == "directory":
        path.unlink()
        path.mkdir()
    else:
        path.write_text(" " * 65537, encoding="utf-8")
    with pytest.raises(CampaignError):
        case.runtime.reconcile_action(case.snapshot, action)
    assert not case.unit.claim.quiescent


def test_unchanged_inputs_rearm_once_per_explicit_resume(pending, monkeypatch):
    case, action = pending
    monkeypatch.setattr(campaign_stage_prompt, "prepare_context", fail_context)
    harvest(case, action, case.runtime._perform_launch(case.snapshot, action))
    assert case.runtime.prepare_resume(case.snapshot)
    retry = dataclasses.replace(action, id="second-refusal", state="sent_unknown", receipt={})
    case.snapshot.outbox.append(retry)
    case.unit.claim = dataclasses.replace(case.unit.claim, launch_id=retry.id, quiescent=False)
    harvest(case, retry, case.runtime._perform_launch(case.snapshot, retry))
    assert case.unit.hold.kind == "launch_preparation"
    assert case.unit.hold.id == retry.id
    assert len(case.unit.evidence["launch_preparation_history"]) == 1
    assert not any(
        p.unit == case.unit.id and p.kind == "launch" for p in case.runtime.proposals(case.snapshot)
    )


def test_prepared_managed_endpoint_closes_normally_after_environment_refusal(
    pending, monkeypatch, tmp_path
):
    from test_campaign_message_bridge import FakeHcom, seed_room

    from bmad_loop import campaign_message_bridge as bridge
    from bmad_loop import campaign_reflection_flow
    from bmad_loop.campaign_managed_communication import ManagedCommunication
    from bmad_loop.campaign_model import CommunicationSpec

    case, action = pending
    room = tmp_path / "fake-room"
    seed_room(room)
    host = SimpleNamespace(identity=lambda pid: 42.5, liveness_of=lambda *args: "dead")
    monkeypatch.setattr(bridge, "get_process_host", lambda: host)
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
    case.snapshot.spec = dataclasses.replace(
        case.snapshot.spec, communication=CommunicationSpec(mode="managed", room=str(room))
    )
    case.snapshot.generation = action.generation = case.unit.claim.generation = 1
    original = campaign_reflection_flow.sanitized_actor_env

    def fail_environment(env, **kwargs):
        if env is campaign_runtime.os.environ:
            raise CampaignError("cannot prepare launch environment")
        return original(env, **kwargs)

    monkeypatch.setattr(campaign_reflection_flow, "sanitized_actor_env", fail_environment)
    result = case.runtime._perform_launch(case.snapshot, action)
    assert result.state == "confirmed" and result.data["not_launched"]
    assert manager.has_open()
    case.runtime.apply(case.snapshot, action, result)
    observed = manager.observe(case.snapshot)
    assert observed["available"], observed
    assert not manager.has_open()
    assert manager.endpoints()[0]["state"] == "closed"
