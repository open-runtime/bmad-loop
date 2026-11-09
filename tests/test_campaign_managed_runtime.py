"""Managed public actors reach real native jobs and production effect boundaries."""

import dataclasses
from pathlib import Path
from types import SimpleNamespace

from test_campaign_headless import finish
from test_campaign_headless import prepared as prepared
from test_campaign_message_bridge import FakeHcom, seed_room
from test_campaign_reasoning_jobs import case as reasoning_case
from test_campaign_runtime import production_case as production_case

from bmad_loop import campaign_message_bridge as bridge
from bmad_loop.campaign_headless import read_json
from bmad_loop.campaign_managed_communication import ManagedCommunication
from bmad_loop.campaign_messages import MessageLedger
from bmad_loop.campaign_model import CommunicationSpec, OutboxAction, ReasoningSpec


def managed_runtime(runtime, snapshot, tmp_path, monkeypatch):
    room = tmp_path / "public-hcom"
    seed_room(room)
    fake = FakeHcom()
    monkeypatch.setattr(
        bridge,
        "get_process_host",
        lambda: SimpleNamespace(identity=lambda pid: 42.5, liveness_of=lambda *args: "dead"),
    )
    snapshot.spec = dataclasses.replace(
        snapshot.spec, communication=CommunicationSpec(mode="managed", room=str(room))
    )
    runtime.communication_manager = lambda directory: ManagedCommunication(
        directory, executable="fake-hcom", runner=fake
    )
    return fake


def test_normal_reasoning_claim_freezes_public_bridge_and_native_private_room(
    prepared, tmp_path, monkeypatch
):
    def configure(snapshot):
        snapshot.spec = dataclasses.replace(
            snapshot.spec,
            communication=CommunicationSpec(mode="managed", room=str(tmp_path / "public-hcom")),
        )

    snapshot, runtime, supervisor, _ = reasoning_case(
        tmp_path, prepared, count=1, configure=configure
    )
    fake = managed_runtime(runtime, snapshot, tmp_path, monkeypatch)
    unit = snapshot.jobs["job-0"]["request"]["unit"]
    ledger = MessageLedger(runtime.store.directory(snapshot.id))
    ledger.accept(
        message_id="job-mail", unit=unit, job="job-0", text="Preserve original owner obligations"
    )
    ledger.accept(message_id="unit-mail", unit=unit, text="Keep for the next coding stage")
    snapshot = runtime.store.save(snapshot, snapshot.revision)
    snapshot = supervisor._queue(snapshot, runtime.observe_workers(snapshot))
    action = next(row for row in snapshot.outbox if row.kind == "reasoning-launch")
    launched = runtime.perform(snapshot, action)
    assert launched.data["state"] == "running", launched
    runtime.apply(snapshot, action, launched)
    job = snapshot.jobs["job-0"]
    path = Path(job["claim"]["run_dir"]) / "request.json"
    envelope = read_json(path)
    public = envelope["managed_binding"]
    assert set(public) == {"kind", "name", "room", "target"}
    assert public["target"]["launch_id"] == action.id and public["name"] == "boma"
    assert [row["id"] for row in envelope["steering"]] == ["job-mail"]
    assert ledger.list(job="job-0")[0]["state"] == "queued"
    receipt = finish(runtime.reasoning_runner, path)
    assert receipt["outcome"] == "completed", receipt
    assert receipt["managed_communication"] == public
    private_room = Path(receipt["native_communication_room"])
    assert private_room != Path(public["room"])
    assert private_room.is_relative_to(path.parent) and private_room.is_dir()
    observe = dataclasses.replace(action, kind="reasoning-observe")
    completed = runtime.perform(snapshot, observe)
    runtime.apply(snapshot, observe, completed)
    assert snapshot.jobs["job-0"]["state"] == "validated"
    assert ledger.list(job="job-0")[0]["state"] == "applied"
    assert ledger.pending(unit)[0]["id"] == "unit-mail"
    communication = OutboxAction(
        "communication", "communication", "", {}, generation=snapshot.generation
    )
    observed = runtime.perform(snapshot, communication)
    assert observed.data["available"], observed
    runtime.apply(snapshot, communication, observed)
    assert observed.data["counts"] == {"closed": 1}
    assert fake.count("start") == fake.count("stop") == 1
    assert not runtime.communication_manager(runtime.store.directory(snapshot.id)).has_open()


def test_qualified_coding_workers_deliver_with_managed_public_bridges(
    production_case, prepared, tmp_path, monkeypatch
):
    from campaign_scripted_native import inject_execution_messages, qualify_case
    from test_campaign_runtime import run_case

    from bmad_loop import campaign_runtime

    case = production_case
    manifest = qualify_case(case, prepared, tmp_path)
    fake = managed_runtime(case.runtime, case.snapshot, tmp_path, monkeypatch)
    configured = dataclasses.replace(
        case.snapshot.spec,
        units=(),
        reasoning=ReasoningSpec(enabled=False, capabilities=str(manifest)),
    )
    _, case.snapshot = campaign_runtime.create(case.repo, configured)
    dependent = case.snapshot.units["source:project:dependent"]
    dependent.spec = dataclasses.replace(dependent.spec, after=("source:project:first",))
    case.snapshot.graph_revision += 1
    case.snapshot = case.store.save(case.snapshot, case.snapshot.revision)
    ledger = MessageLedger(case.store.directory(case.snapshot.id))
    ledger.accept(
        message_id="before-stage",
        unit="source:project:first",
        text="Preserve the original acceptance and report its recorded evidence.",
    )
    original_proposals = case.runtime.proposals
    sent = set()

    def proposals(snapshot):
        inject_execution_messages(case, snapshot, fake, sent)
        return original_proposals(snapshot)

    case.runtime.proposals = proposals
    result = run_case(case, timeout_s=240)
    assert result.phase == "delivered"
    manager = case.runtime.communication_manager(case.store.directory(case.snapshot.id))
    assert not manager.has_open()
    endpoints = manager.endpoints(limit=100)
    assert len(endpoints) >= 6 and all(row["state"] == "closed" for row in endpoints)
    assert fake.count("start") == fake.count("stop") == len(endpoints)
    assert ledger.list(unit="source:project:first")[0]["state"] == "applied"
    injected = [row for row in ledger.list() if row["id"] != "before-stage"]
    assert sent and {row["unit"] for row in injected} == sent
    assert all(row["state"] == "applied" for row in injected)
    requests = list((case.store.directory(case.snapshot.id) / "workers").glob("*/request.json"))
    coding = [
        read_json(path) for path in requests if read_json(path)["kind"] in {"execution", "stage"}
    ]
    assert coding and all(row.get("managed_communication") for row in coding)
    by_launch = {row["launch_id"]: row for row in coding}
    for message in injected:
        target = message["source"]["target"]
        execution = by_launch[target["launch_id"]]
        assert execution["kind"] == "execution"
        assert execution["generation"] == target["generation"]
        successors = [
            row
            for row in coding
            if any(item["id"] == message["id"] for item in row.get("steering", []))
        ]
        assert successors and all(row["kind"] == "stage" for row in successors)
    for row in coding:
        binding = row["managed_communication"]
        assert set(binding) == {"kind", "name", "room", "target"}
        assert binding["target"]["launch_id"] == row["launch_id"]
    steered = next(
        row
        for row in coding
        if any(item["id"] == "before-stage" for item in row.get("steering", []))
    )
    assert steered["kind"] == "stage" and steered["stage"] == "admission_review"
    stage_receipt = read_json(
        case.store.directory(case.snapshot.id) / "workers" / steered["launch_id"] / "receipt.json"
    )
    assert stage_receipt["outcome"] == "stage_complete"
