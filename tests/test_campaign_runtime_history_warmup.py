"""Live native history warmup reaches handoff through the normal supervisor."""

import copy
import dataclasses
import json
import time
from pathlib import Path

import pytest
from test_campaign_handoff_flow import campaign as campaign
from test_campaign_handoff_runtime import ScriptedReasoning
from test_campaign_headless import prepared as prepared
from test_campaign_runtime import diagnose
from test_campaign_runtime import production_case as production_case

from bmad_loop import runs
from bmad_loop.campaign_headless import read_json
from bmad_loop.campaign_runtime import create
from bmad_loop.campaign_supervisor import CampaignSupervisor

PREFIX = "Preserve the original acceptance and partial implementation."
TAIL = "Also preserve the later owner correction added while implementation was live."
UNIT = "source:project:first"


class ObservedReasoning(ScriptedReasoning):
    def __init__(self, case, campaign_id):
        super().__init__()
        self.case = case
        self.campaign_id = campaign_id
        self.launches = []

    def launch(self, path):
        request = read_json(path)["request"]
        coordinates = request["coordinates"]
        if coordinates.get("history_warmup"):
            current = self.case.store.load(self.campaign_id)
            unit = current.units[UNIT]
            assert unit.claim and not unit.claim.quiescent
            assert not unit.evidence.get("handoff")
            pid, identity = runs.read_named_pid_identity(Path(unit.claim.run_dir) / "engine.pid")
            assert runs.probe_liveness(pid, identity) == "alive"
        self.launches.append(copy.deepcopy(request))
        return super().launch(path)


@pytest.mark.parametrize("initial_profile", ["codex", "claude"])
def test_live_prefix_maps_are_reused_after_append_and_quota_handoff(
    campaign, production_case, tmp_path, monkeypatch, initial_profile
):
    from bmad_loop import campaign_history_warmup as warmup

    monkeypatch.setattr(warmup, "INTERVAL_S", 0.2)
    monkeypatch.setattr(warmup, "MIN_BYTES", 1)
    _, configured, _, _ = campaign
    case = production_case
    snapshot = case.store.load(case.snapshot.id)
    roles = dict(snapshot.spec.roles)
    roles.update(dict(configured.spec.roles))
    roles["execution"] = dataclasses.replace(roles["execution"], max_sessions=2)
    capabilities = tmp_path / "warmup-capabilities.json"
    capabilities.write_text(
        json.dumps(
            {"schema_version": 1, "routes": configured.config_snapshot["reasoning_capabilities"]}
        )
    )
    spec = dataclasses.replace(
        snapshot.spec,
        units=(),
        roles=tuple(roles.items()),
        role_pools=(*snapshot.spec.role_pools, *configured.spec.role_pools),
        reasoning=dataclasses.replace(
            configured.spec.reasoning,
            capabilities=str(capabilities),
            purposes=("handoff_map", "handoff_reduce", "handoff_audit"),
        ),
    )
    _, snapshot = create(case.repo, spec)
    dependent = snapshot.units["source:project:dependent"]
    dependent.spec = dataclasses.replace(dependent.spec, after=(UNIT,))
    snapshot.graph_revision += 1
    snapshot = case.store.save(snapshot, snapshot.revision)
    reasoner = ObservedReasoning(case, snapshot.id)
    case.runtime.reasoning_runner = reasoner
    original_role = case.runtime._role

    def initial_route(current, role, **options):
        # Constrain only the initial writer's eligible pool; keep production
        # capacity/budget checks and permit the other route after quota.
        if role == "execution" and not current.units[UNIT].evidence.get("handoff"):
            allowed = options.get("allowed_candidates") or current.spec.candidates(role)
            options["allowed_candidates"] = tuple(
                candidate for candidate in allowed if candidate.profile == initial_profile
            )
        return original_role(current, role, **options)

    monkeypatch.setattr(case.runtime, "_role", initial_route)

    script = tmp_path / "scripted-coding-cli.py"
    body = script.read_text().replace(
        "def execution(request, session):", "def base_execution(request, session):"
    )
    wrapper = (
        r"""
def execution(request, session):
    if not request["unit_id"].endswith(":first"):
        return base_execution(request, session)
    MARKERS.mkdir(exist_ok=True)
    lease=MARKERS/"single-writer"
    descriptor=os.open(lease,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
    os.close(descriptor)
    try:
        partial=Path(request["workspace"])/"src"/"first.txt"
        if not request.get("handoff_id"):
            assert request["candidate"]["profile"]==__INITIAL_PROFILE__
            event("execution-start",request)
            (MARKERS/"first").write_text("started")
            partial.parent.mkdir(exist_ok=True)
            partial.write_text("preserved partial work")
            history=Path(request["execution"]).parent/"live-history.jsonl"
            codex=request["candidate"]["profile"]=="codex"
            def owner_message(text):
                return {"type":"event_msg","payload":{"type":"user_message","message":text}} if codex else {"type":"user","sessionId":"warmup-native","message":{"role":"user","content":text}}
            rows=([{"type":"session_meta","payload":{"id":"warmup-native"}}] if codex else [])
            rows.append(owner_message(__PREFIX__))
            rows.append({"type":"response_item","payload":{"type":"message","role":"assistant","content":"Implementation is still in progress."}} if codex else {"type":"assistant","sessionId":"warmup-native","message":{"role":"assistant","content":"Implementation is still in progress."}})
            history.write_text("\n".join(json.dumps(row) for row in rows)+"\n")
            events=Path(session.env["BMAD_LOOP_EVENTS_DIR"])
            events.mkdir(parents=True,exist_ok=True)
            atomic_write_text(events/"warmup-start.json",json.dumps({"event":"SessionStart","task_id":session.task_id,"session_id":"warmup-native","transcript_path":str(history),"ts":time.time_ns(),"lineage":"match"}))
            event("history-ready",request)
            deadline=time.monotonic()+120
            while not (MARKERS/"append-history").exists():
                if time.monotonic()>deadline: raise RuntimeError("live warmup never applied")
                threading.Event().wait(.01)
            with history.open("a") as stream:
                stream.write(json.dumps(owner_message(__TAIL__))+"\n")
            event("history-appended",request)
            event("quota",request)
            return SessionResult(status="crashed",session_id="warmup-native",transcript_path=str(history),env_fault=True,env_fault_evidence="scripted quota",native_binding={"outcome":"quota","observed_model":request["candidate"]["model"],"provider_family":request["candidate"]["provider_family"]})
        packet_path=Path(request["handoff_packet"]["path"])
        assert hashlib.sha256(packet_path.read_bytes()).hexdigest()==request["handoff_packet"]["sha256"]
        packet=json.loads(packet_path.read_text())
        assert packet["resolved_constraints"]
        assert partial.read_text()=="preserved partial work"
        from bmad_loop import runs
        predecessor=Path(request["native_predecessor"]["run_dir"])
        assert runs.probe_liveness(*runs.read_named_pid_identity(predecessor/"engine.pid"))=="dead"
        event("successor-context-delivered",request)
        return base_execution(request, session)
    finally:
        lease.unlink()

""".replace("__PREFIX__", repr(PREFIX))
        .replace("__TAIL__", repr(TAIL))
        .replace("__INITIAL_PROFILE__", repr(initial_profile))
    )
    body = body.replace("def stage(request,session):", wrapper + "def stage(request,session):")
    body = body.replace(
        'model="gpt-6-sol" if request["kind"]=="execution" else request["candidate"]["model"]',
        'model=request["candidate"]["model"]',
    )
    body = body.replace(
        "lambda spec: effect(request,spec)",
        "lambda spec: effect(request,adapter.sessions[-1])",
    )
    body = body.replace(
        'return SessionResult(status="completed",session_id="scripted-"+request["launch_id"],result_json=artifact)',
        'history=Path(request["execution"]).parent/"stage-history.jsonl"\n'
        '    history.write_text(json.dumps({"type":"user","sessionId":"scripted-"+request["launch_id"],"isMeta":True,"message":{"role":"user","content":request["prompt"]}})+"\\n")\n'
        '    return SessionResult(status="completed",session_id="scripted-"+request["launch_id"],transcript_path=str(history),native_binding={"profile":request["candidate"]["profile"]},result_json=artifact)',
    )
    # Preserve actual adapter-handle observation; the fake coding process writes
    # its native hook stream rather than seeding warmup state or completed jobs.
    body = body.replace(
        '    return {role:adapter for role in ("dev","review","retro","triage")}',
        "    from bmad_loop.campaign_reflection_flow import observe_adapter\n"
        "    from bmad_loop.campaign_model import WorkerClaim\n"
        '    observe_adapter(adapter,directory=envelope.directory,run_dir=run_dir,claim=WorkerClaim(**request["claim"]),campaign_id=envelope.campaign_id,environment=request.get("reflection_environment",{}))\n'
        '    return {role:adapter for role in ("dev","review","retro","triage")}',
    )
    script.write_text(body)

    prefix_jobs = set()
    original_launch = ""
    original_pr = None
    stop_requested = False
    deadline = time.monotonic() + 180

    def observe_progress():
        nonlocal prefix_jobs, original_launch, original_pr, stop_requested
        current = case.store.load(snapshot.id)
        unit = current.units[UNIT]
        if not prefix_jobs:
            applied = [
                row
                for row in case.store.jobs(snapshot.id, unit=UNIT)
                if row["state"] == "applied" and row["request"]["coordinates"].get("history_warmup")
            ]
            if applied:
                assert unit.claim and not unit.claim.quiescent
                assert not unit.evidence.get("handoff")
                assert (
                    runs.probe_liveness(
                        *runs.read_named_pid_identity(Path(unit.claim.run_dir) / "engine.pid")
                    )
                    == "alive"
                )
                prefix_jobs = {row["id"] for row in applied}
                original_launch, original_pr = unit.claim.launch_id, unit.bindings["pr"]
                (tmp_path / "parallel-markers" / "append-history").touch()
        elif (
            unit.evidence.get("session_lineage")
            and any(
                row.get("outcome") == "locally_complete"
                for row in unit.evidence.get("worker_history", [])
            )
            and not stop_requested
        ):
            case.store.enqueue(snapshot.id, "stop")
            stop_requested = True
        assert time.monotonic() < deadline, json.dumps(diagnose(current), indent=2)
        return case.runtime.clock()

    result = CampaignSupervisor(
        case.store, case.runtime, clock=observe_progress, progress=lambda _: None
    ).run(snapshot.id)
    unit = result.units[UNIT]
    handoff = unit.evidence["handoff"]
    assert prefix_jobs
    assert prefix_jobs <= set(handoff["all_jobs"])
    assert unit.bindings["pr"] == original_pr
    assert handoff["predecessor"]["launch_id"] == original_launch
    assert unit.claim and unit.claim.launch_id != original_launch
    warm_requests = [row for row in reasoner.launches if row["coordinates"].get("history_warmup")]
    assert prefix_jobs <= {row["id"] for row in warm_requests}
    assert all(sum(row["id"] == job for row in reasoner.launches) == 1 for job in prefix_jobs)
    audits = [row for row in reasoner.launches if row["purpose"] == "handoff_audit"]
    captured = next(
        source
        for source in handoff["capture"]["sources"]
        if source["session_id"] == "warmup-native"
    )
    original = Path(handoff["predecessor"]["run_dir"]) / "live-history.jsonl"
    assert Path(captured["raw_path"]).read_bytes() == original.read_bytes()
    assert captured["captured_bytes"] == original.stat().st_size
    assert set(captured["owner_event_ids"]) <= {
        identity for row in audits for identity in row["coordinates"]["owner_ids"]
    }
    audited_text = "\n".join(
        span["text"] for row in audits for span in row["coordinates"]["owner_spans"]
    )
    assert PREFIX in audited_text and TAIL in audited_text
    events = [json.loads(line) for line in case.events.read_text().splitlines()]
    own = [row["event"] for row in events if row["unit"] == UNIT]
    assert own.index("history-ready") < own.index("history-appended") < own.index("quota")
    assert own.count("successor-context-delivered") == 1
    assert not (tmp_path / "parallel-markers" / "single-writer").exists()
