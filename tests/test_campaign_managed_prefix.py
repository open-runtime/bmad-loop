"""Live managed output is warmed through owned adapter handles, then sealed once."""

import dataclasses
import json
import time
from pathlib import Path

import pytest
from test_campaign_handoff_flow import campaign as campaign
from test_campaign_headless import finish as finish_native
from test_campaign_headless import fixture_capability
from test_campaign_headless import prepared as prepared
from test_campaign_history_warmup import finish as finish_maps
from test_campaign_managed_history import binding_for

from bmad_loop import campaign_history_warmup as warmup
from bmad_loop import runs
from bmad_loop.adapters.base import SessionSpec
from bmad_loop.adapters.headless import HeadlessAdapter
from bmad_loop.adapters.profile import get_profile
from bmad_loop.campaign_headless import file_digest
from bmad_loop.campaign_managed_history import snapshot_managed_history, snapshot_managed_prefix
from bmad_loop.campaign_model import CampaignError, OutboxAction
from bmad_loop.campaign_reflection_flow import observe_adapter
from bmad_loop.campaign_transcripts import load_events
from bmad_loop.policy import Policy


def until(predicate):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    pytest.fail("scripted managed prefix did not reach its explicit checkpoint")


@pytest.fixture
def live(campaign, prepared, tmp_path, monkeypatch):
    runtime, snapshot, unit, _ = campaign
    runner, _, request, candidate, cap = prepared
    monkeypatch.setattr(warmup, "MIN_BYTES", 1)
    snapshot.generation = unit.claim.generation = 1
    unit.claim.quiescent = False
    unit.claim.run_id = "managed-run"
    unit.claim.branch = unit.bindings["branch"]
    root = tmp_path / "signals"
    root.mkdir()
    script = tmp_path / "stream.py"
    script.write_text("""import json,sys,time
from pathlib import Path
root=Path(sys.argv[1]); sys.stdin.buffer.read()
print(json.dumps({"type":"system","model":"gpt-test","session_id":"live-session"}),flush=True)
sys.stdout.buffer.write(b'{"type":"assistant","text":"caf\\xc3'); sys.stdout.buffer.flush()
(root/"prefix").touch()
def wait(name):
    deadline=time.monotonic()+12
    while not (root/name).exists():
        if time.monotonic()>deadline: raise RuntimeError("scripted gate expired: "+name)
        time.sleep(.01)
wait("append")
sys.stdout.buffer.write(b'\\xa9"}\\n'); sys.stdout.buffer.flush()
(root/"appended").touch()
wait("finish")
print(json.dumps({"type":"result","structured_output":{"done":True},"usage":{"input_tokens":12,"output_tokens":8}}),flush=True)
""")
    cap = fixture_capability(
        tmp_path,
        {
            **cap,
            "argv": [cap["binary"], str(script), str(root)],
            "dependencies": [{"path": str(script), "sha256": file_digest(script)}],
        },
    )
    directory = runtime.store.directory(snapshot.id) / "workers" / unit.claim.launch_id
    run_dir = runs.run_dir_for(Path(unit.claim.workspace), unit.claim.run_id)
    adapter = HeadlessAdapter(
        run_dir=run_dir, profile=get_profile("codex"), policy=Policy(), mux=runner.mux
    )
    adapter.set_native_job_root(directory / "native" / run_dir.name)
    adapter.set_capability(
        cap,
        None,
        {
            **request.coordinates,
            "campaign": snapshot.id,
            "unit": unit.id,
            "config_revision": snapshot.config_revision,
        },
    )
    observe_adapter(
        adapter,
        directory=directory,
        run_dir=run_dir,
        claim=unit.claim,
        campaign_id=snapshot.id,
        environment={},
    )
    handle = adapter.start_session(
        SessionSpec(
            "managed-task",
            "dev",
            request.prompt,
            Path(unit.claim.workspace),
            model=candidate.model,
            effort=candidate.effort,
            timeout_s=15,
        )
    )
    path = adapter.requests[handle.task_id]
    try:
        until(lambda: (root / "prefix").exists())
        yield runtime, snapshot, unit, adapter, handle, path, root
    finally:
        (root / "append").touch()
        (root / "finish").touch()
        finish_native(adapter.runner, path)


def test_live_managed_warmup_uses_normal_handle_and_reuses_exact_prefix(live, tmp_path):
    runtime, snapshot, unit, adapter, handle, path, signals = live
    proposals = warmup.proposals(runtime, snapshot)
    assert len(proposals) == 1
    proposal = proposals[0]
    action = OutboxAction("warm", proposal.kind, unit.id, proposal.payload, snapshot.generation)
    observation = warmup.perform(runtime, snapshot, action)
    assert observation.state == "confirmed" and observation.data["requests"], observation
    warmup.apply(runtime, snapshot, action, observation)
    finish_maps(runtime, snapshot, unit)
    index = (
        json.loads((Path(observation.data["index_path"])).read_text())
        if "index_path" in observation.data
        else None
    )
    prefix = snapshot_managed_prefix(
        path, adapter.live_history_binding(handle), tmp_path / "prefix"
    )
    first = load_events(prefix)
    assert len(first) == 3 and not prefix.owner_event_ids
    assert not any("café" in row.record.text for row in first)
    assert unit.claim and not unit.claim.quiescent and not unit.evidence.get("handoff")
    (signals / "append").touch()
    until(lambda: (signals / "appended").exists())
    second = snapshot_managed_prefix(
        path, adapter.live_history_binding(handle), tmp_path / "second"
    )
    assert [e.id for e in load_events(second)][:3] == [e.id for e in first]
    assert "café" in load_events(second)[-1].record.text
    (signals / "finish").touch()
    receipt = finish_native(adapter.runner, path)
    final = snapshot_managed_history(
        path, binding_for(path, receipt), tmp_path / "final", session_id="live-session"
    )
    assert [e.id for e in load_events(final)][:4] == [e.id for e in load_events(second)]
    assert not final.owner_event_ids
    reused, rejected = warmup.reusable(runtime, snapshot, unit, [dataclasses.asdict(final)])
    assert reused and not rejected, (index, rejected)


@pytest.mark.parametrize("fault", ["rewrite", "request", "startup", "archive"])
def test_live_prefix_refuses_changed_owned_evidence(live, tmp_path, fault):
    _, _, _, adapter, handle, path, _ = live
    binding = adapter.live_history_binding(handle)
    snapshot_managed_prefix(path, binding, tmp_path / "first")
    target = path.parent / {
        "rewrite": "stdout.jsonl",
        "request": "request.json",
        "startup": "startup.json",
    }.get(fault, "history-prefix.json")
    original = target.read_bytes()
    try:
        if fault == "rewrite":
            target.write_bytes(original.replace(b"gpt-test", b"bad-test"))
        elif fault == "request":
            target.write_bytes(original + b" ")
        else:
            value = json.loads(original)
            value["launch_id" if fault == "startup" else "events_sha256"] = "other"
            target.write_text(json.dumps(value))
        with pytest.raises((CampaignError, FileNotFoundError)):
            snapshot_managed_prefix(path, binding, tmp_path / "changed")
    finally:
        target.write_bytes(original)


def test_live_capture_cannot_claim_a_terminated_process(live, tmp_path):
    _, _, _, adapter, handle, path, signals = live
    binding = adapter.live_history_binding(handle)
    snapshot_managed_prefix(path, binding, tmp_path / "first")
    (signals / "append").touch()
    (signals / "finish").touch()
    finish_native(adapter.runner, path)
    with pytest.raises(CampaignError, match="confirmed alive"):
        snapshot_managed_prefix(path, binding, tmp_path / "ended")


def test_final_capture_refuses_identical_bytes_from_replaced_stream(live, tmp_path):
    _, _, _, adapter, handle, path, signals = live
    snapshot_managed_prefix(path, adapter.live_history_binding(handle), tmp_path / "first")
    (signals / "append").touch()
    (signals / "finish").touch()
    receipt = finish_native(adapter.runner, path)
    stream = path.parent / "stdout.jsonl"
    replacement = path.parent / "replacement.jsonl"
    replacement.write_bytes(stream.read_bytes())
    replacement.replace(stream)
    with pytest.raises(CampaignError, match="stream identity differs"):
        snapshot_managed_history(path, binding_for(path, receipt), tmp_path / "final")
