"""Retained technical recovery through production supervision, with scripted CLIs."""

import copy
import dataclasses
import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from test_campaign_handoff_flow import campaign as campaign
from test_campaign_headless import prepared as prepared
from test_campaign_runtime import diagnose, run_case
from test_campaign_runtime import production_case as production_case

from bmad_loop.campaign_model import UnitState
from bmad_loop.campaign_scheduler import CapacityObservation


def test_retained_rearm_finishes_own_in_progress_ticket_and_keeps_dirty_history(
    project_v7, prepared, tmp_path
):
    from conftest import git, ticket_row
    from test_tickets_engine import _builds, _policy, _tree, _write_plan, make_engine
    from test_campaign_execution_qualification import fixture_writer_capability

    from bmad_loop import campaign_rearm, runsetup, verify
    from bmad_loop.adapters.mock import MockAdapter
    from bmad_loop.campaign_child_pause import build
    from bmad_loop.campaign_continuation import compose_continuation, prepare_permit
    from bmad_loop.campaign_execution import CampaignExecution, bind_run
    from bmad_loop.campaign_interruptions import execution_interruption
    from bmad_loop.campaign_model import (
        CampaignSnapshot,
        CampaignSpec,
        CampaignUnit,
        Hold,
        OutboxAction,
        RoleSpec,
        SourceSelector,
        UnitSpec,
        WorkerClaim,
        source_to_dict,
    )
    from bmad_loop.campaign_store import CampaignStore, consume_continuation_permit
    from bmad_loop.campaign_usage import legacy_receipt
    from bmad_loop.campaign_worker_authority import continuation_request, execution_envelope
    from bmad_loop.campaign_workers import common_directory
    from bmad_loop.engine import RunPaused
    from bmad_loop.journal import save_state
    from bmad_loop.model import PauseCause, PauseKind, Phase, SessionRecord, StoryTask, TokenUsage
    from bmad_loop.policy import StageAdapterPolicy, TicketsPolicy
    from bmad_loop.tickets_engine import TicketsEngine
    from bmad_loop.tickets_source import ticket_binding

    _, _, _, route, basecap = prepared
    capability = fixture_writer_capability(tmp_path, basecap, route, input_bytes=100000)
    configured = SimpleNamespace(
        spec=CampaignSpec(
            "Ticket route",
            roles=(("execution", RoleSpec(pool="writers")),),
            role_pools=(("writers", (route,)),),
        ),
        config_snapshot={
            "reasoning_capabilities": {
                route.profile + ":" + route.model: {"execution": capability},
            }
        },
    )
    layout, repo = project_v7, project_v7.project
    git(repo, "checkout", "-b", "owned-ticket-rearm")
    tree = _tree(layout, in_progress=(ticket_row("1.1", status="in-progress"),))
    policy = dataclasses.replace(
        _policy(),
        limits=dataclasses.replace(_policy().limits, max_dev_attempts=5, max_review_cycles=4),
        tickets=TicketsPolicy(mark_done="never"),
        adapter=dataclasses.replace(
            _policy().adapter,
            dev=StageAdapterPolicy(name=route.profile, model=route.model, effort=route.effort),
        ),
    )
    original, _ = make_engine(layout, tree, policy=policy, source="tickets", max_stories=1)
    with pytest.raises(RunPaused) as foreign:
        original._pick_next()
    assert foreign.value.cause.code == "foreign-active"
    assert original.state.tasks == {}
    unit_spec = UnitSpec(
        "source:initiative:1.1",
        "source",
        "epic",
        "Owned ticket",
        selector="1.1",
        acceptance=("Preserve the accepted behavior",),
    )
    source = SourceSelector("tickets", id="source", initiative=layout.initiative)
    store = CampaignStore(tmp_path / "ticket-authority")
    predecessor_directory = tmp_path / "predecessor-worker"
    template = CampaignExecution(
        "ticket-campaign",
        unit_spec.id,
        2,
        1,
        1,
        predecessor_directory,
        policy,
        runsetup.resolve_profiles(policy, repo),
    )
    baseline = verify.rev_parse_head(repo)
    task = StoryTask(
        "1.1",
        0,
        phase=Phase.DEV_VERIFY,
        attempt=2,
        review_cycle=1,
        generation=4,
        baseline_commit=baseline,
        ticket=ticket_binding(tree.finds["1.1"]),
        tokens=TokenUsage(input_tokens=31, output_tokens=17),
    )
    plan = Path(task.ticket.plan)
    _write_plan(plan, "ready-for-dev", baseline=baseline)
    task.dispatched_spec_file, task.dispatched_spec_snapshot = str(plan), plan.read_bytes()
    _write_plan(plan, "in-progress", baseline=baseline)
    task.spec_file = str(plan)
    task.baseline_untracked = ["retained-note.txt"]
    note = repo / "retained-note.txt"
    note.write_text("Existing owner's unfinished note\n")
    (repo / "src.txt").write_text("staged work\n")
    git(repo, "add", "src.txt")
    (repo / "src.txt").write_text("unstaged work\n")
    history = tmp_path / "ticket-native-history.jsonl"
    history.write_text(
        json.dumps({"type": "session_meta", "payload": {"id": "ticket-native"}})
        + "\n"
        + json.dumps(
            {
                "type": "event_msg",
                "payload": {
                    "type": "user_message",
                    "message": "Keep all unfinished ticket work and acceptance.",
                },
            }
        )
        + "\n"
    )
    task.sessions = [
        SessionRecord(
            "old-dev",
            "dev",
            "crashed",
            adapter=route.profile,
            model=route.model,
            session_id="ticket-native",
            transcript_path=str(history),
            ended="usage",
            models=(route.model,),
            native_binding={"outcome": "quota"},
        )
    ]
    original.state.tasks[task.story_key] = task
    original.state.record_pause(
        "native quota reset",
        "escalation",
        task.story_key,
        cause=PauseCause(PauseKind.QUOTA_WAIT, "engine.session", "usage_limit"),
    )
    original.state.campaign_binding = bind_run(repo, original.run_dir, template)
    save_state(original.run_dir, original.state)
    claim = WorkerClaim(
        unit_spec.id,
        2,
        1,
        "old-launch",
        workspace=str(repo),
        branch="owned-ticket-rearm",
        run_id=original.state.run_id,
        run_dir=str(predecessor_directory),
        role="execution",
        quiescent=True,
        status="finished",
        writes=True,
    )
    unit = CampaignUnit(
        unit_spec,
        UnitState.RUNNING,
        attempt=2,
        claim=claim,
        bindings={
            "workspace": str(repo),
            "branch": claim.branch,
            "head": baseline,
            "base": baseline,
            "pr": {"number": 7},
            "source_revision": "ticket-v1",
        },
        evidence={
            "dev_attempts_spent": 2,
            "review_cycles_spent": 1,
            "developer_families": ["openai"],
            "unresolved_approval_requirements": ["Existing human signoff remains required"],
        },
    )
    snapshot = CampaignSnapshot(
        template.campaign_id,
        "example/repo",
        CampaignSpec(
            "Ticket continuation",
            sources=(source,),
            units=(unit_spec,),
            roles=configured.spec.roles,
            role_pools=configured.spec.role_pools,
        ),
        {unit.id: unit},
        generation=2,
        config_revision=1,
        common_dir=str(common_directory(repo)),
        root=str(repo),
        config_snapshot={
            "policy": policy.to_dict(),
            "reasoning_capabilities": configured.config_snapshot["reasoning_capabilities"],
        },
    )
    frozen = dataclasses.replace(template, directory=tmp_path / "frozen-configuration")
    template_path, template_digest = frozen.save()
    snapshot.bindings["execution_template"] = {
        "path": str(template_path),
        "digest": template_digest,
    }
    snapshot.bindings["capacity_faults"] = {
        route.profile + "\0" + route.model: {
            "observed_at": time.time() - 10,
            "reason": "native quota",
            "quota_retry_at": time.time() - 5,
            "quota_source": "agy-native-individual-quota-v1",
        }
    }
    payload = {"kind": "execution", "role": "execution", "candidate": dataclasses.asdict(route)}
    snapshot.outbox.append(
        OutboxAction(claim.launch_id, "launch", unit.id, payload, 1, state="confirmed")
    )
    receipt = {
        "outcome": "paused",
        "campaign_id": snapshot.id,
        "unit_id": unit.id,
        "launch_id": claim.launch_id,
        "attempt": claim.attempt,
        "generation": claim.generation,
        "workspace": str(repo),
        "branch": claim.branch,
        "common_dir": snapshot.common_dir,
        "head": baseline,
        "run_id": original.state.run_id,
        "run_dir": str(original.run_dir),
        "sessions": [row.to_dict() for row in task.sessions],
        "accounting": legacy_receipt(original.state),
    }
    receipt["interruption"] = execution_interruption(receipt["sessions"])
    receipt["child_pause"] = build(
        original.state,
        {
            "source": source_to_dict(source),
            "unit": dataclasses.asdict(unit_spec),
            "source_revision": "ticket-v1",
        },
        template,
        receipt,
    )
    unit.evidence["worker_history"] = [receipt]
    unit.hold = Hold(
        "quota",
        original.state.paused_reason,
        id=claim.launch_id,
        evidence={"child_pause": receipt["child_pause"]},
    )
    snapshot = store.create(snapshot)
    unit = snapshot.units[unit.id]
    binding = campaign_rearm.capture_binding(snapshot, unit)
    runtime = SimpleNamespace(store=store, clock=time.time)
    sealed = campaign_rearm._capture(runtime, snapshot, unit, binding)
    now = time.time()
    condition = {
        "schema_version": 1,
        "kind": "quota",
        "ready": True,
        "binding_id": binding["id"],
        "action_id": "resolved-quota",
        "observed_at": now,
        "expires_at": now + 120,
        "quota_retry_at": binding["capacity_fault"]["quota_retry_at"],
        "producer": "campaign.capacity",
        "candidate": binding["candidate"],
        "launcher": {"ready": True},
    }
    observation = OutboxAction(
        "resolved-quota",
        "resolution-reprobe",
        unit.id,
        {"rearm_binding": binding},
        snapshot.generation,
    )
    campaign_rearm.apply(
        runtime,
        snapshot,
        unit,
        observation,
        {
            **sealed,
            "rearm_binding": binding,
            "action_id": observation.id,
            "available": True,
            "condition": condition,
            "capacity": {},
        },
    )
    snapshot = store.save(snapshot, snapshot.revision)
    unit = snapshot.units[unit.id]
    successor = dataclasses.replace(
        claim,
        attempt=3,
        generation=snapshot.generation,
        launch_id="successor-launch",
        run_id="successor-run",
        run_dir=str(store.directory(snapshot.id) / "workers" / "successor-launch"),
        quiescent=False,
        status="prepared",
    )
    launch_payload = {**payload, "rearm_id": unit.evidence["technical_rearm"]["id"]}
    snapshot.outbox.append(
        OutboxAction(successor.launch_id, "launch", unit.id, launch_payload, snapshot.generation)
    )
    unit.claim = successor
    unit.attempt = successor.attempt
    campaign_rearm.adopt_launch(snapshot, unit, launch_payload, successor)
    child = execution_envelope(
        frozen, snapshot, unit, successor, launch_payload, Path(successor.run_dir)
    )
    request = continuation_request(snapshot, unit, successor)
    permit = prepare_permit(request, child)
    consumed = consume_continuation_permit(snapshot, unit.id, permit)
    snapshot = store.save(snapshot, snapshot.revision)
    request.update(
        candidate=payload["candidate"],
        continuation_permit=permit,
        continuation_consumption=consumed["consumption"],
        continuation_authority=str(store.root),
    )
    adapter = MockAdapter([_builds(layout, tree, plan)])
    seen = []
    build_ticket = adapter.script[0]

    def retained_session(session):
        assert (repo / "src.txt").read_text() == "unstaged work\n"
        assert git(repo, "show", ":src.txt") == "staged work"
        assert note.read_text() == "Existing owner's unfinished note\n"
        seen.append(session)
        return build_ticket(session)

    adapter.script = [retained_session]

    def retained_engine(**kwargs):
        return TicketsEngine(**kwargs, ticket_tree_factory=lambda *_a, **_k: tree)

    composed = compose_continuation(
        request,
        child,
        make_adapters=lambda *_a, **_k: {
            role: adapter for role in ("dev", "review", "retro", "triage")
        },
        tickets_engine_cls=retained_engine,
    )
    before = composed.state.tasks["1.1"]
    assert before.attempt == 2 and before.review_cycle == 1 and before.generation == 5
    assert before.baseline_commit == baseline
    summary = composed.engine.run()
    assert not summary.paused, summary
    assert composed.state.tasks["1.1"].phase == Phase.DONE
    assert composed.state.tasks["1.1"].attempt == 3
    assert len(seen) == 1
    assert note.read_text() == "Existing owner's unfinished note\n"
    saved = store.load(snapshot.id).units[unit.id]
    assert saved.evidence["unresolved_approval_requirements"] == [
        "Existing human signoff remains required"
    ]
    assert saved.evidence["developer_families"] == ["openai"]


@pytest.mark.parametrize("restart_at", ["pause", "ready", "expired_ready", "unlaunched_stop"])
def test_quota_rearm_survives_restart_and_delivers_with_reasoning_disabled(
    campaign, production_case, tmp_path, restart_at
):
    _, configured, _, _ = campaign
    case = production_case
    initial = case.store.load(case.snapshot.id)
    writer = configured.spec.candidates("execution")[0]
    roles = dict(initial.spec.roles)
    roles["execution"] = dataclasses.replace(
        dict(configured.spec.roles)["execution"], max_sessions=2
    )
    routes = copy.deepcopy(configured.config_snapshot["reasoning_capabilities"])
    manifest = tmp_path / "rearm-capabilities.json"
    manifest.write_text(json.dumps({"schema_version": 1, "routes": routes}))
    pools = tuple(
        (name, (writer,) if name == "writers" else pool)
        for name, pool in configured.spec.role_pools
    )
    spec = dataclasses.replace(
        initial.spec,
        units=(),
        roles=tuple(roles.items()),
        role_pools=(*initial.spec.role_pools, *pools),
        reasoning=dataclasses.replace(
            configured.spec.reasoning, enabled=False, capabilities=str(manifest)
        ),
    )
    from bmad_loop.campaign_runtime import CampaignRuntime, create

    _, snapshot = create(case.repo, spec)
    dependent = snapshot.units["source:project:dependent"]
    dependent.spec = dataclasses.replace(dependent.spec, after=("source:project:first",))
    snapshot.graph_revision += 1
    snapshot = case.store.save(snapshot, snapshot.revision)
    case.snapshot = snapshot

    ttl = [30 if restart_at == "expired_ready" else 120]

    def capacity(*_):
        now = time.time()
        return CapacityObservation(
            True, slots=8, model_access=True, observed_at=now, expires_at=now + ttl[0]
        )

    case.runtime.clock = time.time
    case.runtime.capacity_observer = capacity
    script = tmp_path / "scripted-coding-cli.py"
    body = script.read_text()
    injection = """
    if request["unit_id"].endswith(":first"):
        workspace=Path(request["workspace"])
        partial=workspace/"src"/"first.txt"
        if not request.get("rearm_id"):
            event("execution-start",request)
            MARKERS.mkdir(exist_ok=True)
            (MARKERS/"first").write_text("started")
            partial.parent.mkdir(exist_ok=True)
            partial.write_text("preserved staged work")
            assert verify.git_bytes(workspace,"add","src/first.txt").returncode==0
            partial.write_text("preserved unstaged work")
            history=Path(request["execution"]).parent/"quota-history.jsonl"
            messages=[{"type":"session_meta","payload":{"id":"quota-native"}},{"type":"event_msg","payload":{"type":"user_message","message":"Preserve partial work and every unfinished acceptance obligation."}}]
            history.write_text("\\n".join(json.dumps(row) for row in messages)+"\\n")
            event("quota",request)
            return SessionResult(status="crashed",session_id="quota-native",transcript_path=str(history),produced_work=True,env_fault=False,native_binding={"outcome":"quota","quota_retry_at":time.time()+2,"quota_source":"agy-native-individual-quota-v1","observed_model":request["candidate"]["model"],"provider_family":"openai"})
        assert request["rearm_state"]["purpose"]=="technical_rearm"
        assert request["continuation_permit"]["schema_version"]==3
        assert "handoff_state" not in request and "handoff_id" not in request
        packet=Path(request["rearm_packet"]["path"])
        assert hashlib.sha256(packet.read_bytes()).hexdigest()==request["rearm_packet"]["sha256"]
        retained=json.loads(packet.read_text())
        assert retained["purpose"]=="technical_rearm" and retained["exact_history"]
        assert all("source" in item for item in retained["exact_history"])
        assert retained["obligations"]
        assert partial.read_text()=="preserved unstaged work"
        assert verify.git_bytes(workspace,"show",":src/first.txt").stdout==b"preserved staged work"
        predecessor=json.loads((Path(request["native_predecessor"]["run_dir"])/"request.json").read_text())
        assert request["candidate"]==predecessor["candidate"]
        assert request["native_predecessor"]["launch_id"]!=request["launch_id"]
        event("retained-successor",request)
"""
    body = body.replace(
        "def execution(request, session):\n", "def execution(request, session):\n" + injection
    ).replace(
        'model="gpt-6-sol" if request["kind"]=="execution" else request["candidate"]["model"]',
        'model=request["candidate"]["model"]',
    )
    body = body.replace(
        'return SessionResult(status="completed",session_id="scripted-"+request["launch_id"],result_json=artifact)',
        'history=Path(request["execution"]).parent/"stage-history.jsonl"\n    history.write_text(json.dumps({"type":"user","sessionId":"scripted-"+request["launch_id"],"isMeta":True,"message":{"role":"user","content":request["prompt"]}})+"\\n")\n    return SessionResult(status="completed",session_id="scripted-"+request["launch_id"],transcript_path=str(history),native_binding={"profile":request["candidate"]["profile"]},result_json=artifact)',
    )
    script.write_text(body)

    ready_before_restart = None

    class CoordinatorRestart(Exception):
        """Crash after the real pause/ready transaction, before successor admission."""

    original_save = case.store.save

    def crash_after_boundary(*args, **kwargs):
        saved = original_save(*args, **kwargs)
        first = saved.units["source:project:first"]
        reached = (
            first.hold is not None and first.hold.kind == "quota"
            if restart_at == "pause"
            else first.evidence.get("technical_rearm", {}).get("phase")
            == ("continuing" if restart_at == "unlaunched_stop" else "ready")
        )
        if reached:
            if restart_at == "unlaunched_stop":
                assert next(row for row in saved.outbox if row.id == first.claim.launch_id).state == "prepared"
                case.store.enqueue(saved.id, "stop", reason="Scripted stop before retained launch submission")
            raise CoordinatorRestart()
        return saved

    case.store.save = crash_after_boundary
    try:
        with pytest.raises(CoordinatorRestart):
            run_case(case)
    finally:
        case.store.save = original_save
    intermediate = case.store.load(snapshot.id)
    first = intermediate.units["source:project:first"]
    if restart_at in {"ready", "expired_ready"}:
        ready_before_restart = copy.deepcopy(first.evidence["technical_rearm"])
        assert first.claim.quiescent and first.hold.kind == "quota"
        if restart_at == "expired_ready":
            # Real bounded time, shared with the child validator; no forged
            # expired receipt or different controller/worker clock is used.
            until = ready_before_restart["condition"]["expires_at"] + 0.01
            while time.time() < until:
                threading.Event().wait(min(0.25, until - time.time()))
            ttl[0] = 120
    old_launch = (
        first.evidence["technical_rearm"]["predecessor"]["launch_id"]
        if restart_at == "unlaunched_stop" else first.claim.launch_id
    )
    canceled_permits = copy.deepcopy(first.evidence.get("continuation_permits", {}))
    reserved_attempt = first.attempt
    old_branch = first.bindings["branch"]
    old_pr = first.bindings["pr"]["number"]
    spent = first.evidence.get("dev_attempts_spent", 0)
    # Reconstruct both controller and runtime from the durable store. No old
    # in-memory task or prepared permit is supplied to the replacement.
    from bmad_loop.campaign_store import CampaignStore

    case.store = CampaignStore(case.store.root)
    case.runtime = CampaignRuntime(
        case.repo,
        case.store,
        gateway=case.runtime.gateway(intermediate),
        clock=time.time,
        capacity_observer=capacity,
    )
    if restart_at == "unlaunched_stop":
        drained = run_case(case)
        held = drained.units["source:project:first"]
        assert drained.phase == "stopped"
        assert held.evidence["technical_rearm"]["phase"] == "canceled"
        assert held.evidence["technical_rearm"]["cancellation"]["certainty"] == "not-submitted"
        assert held.claim.quiescent and held.claim.launch_id == old_launch
        assert held.attempt == reserved_attempt
        assert held.evidence["continuation_permits"] == canceled_permits
    result = run_case(case)
    assert result.phase == "delivered", diagnose(result)
    first = result.units["source:project:first"]
    assert first.state == UnitState.DELIVERED
    assert first.bindings["branch"] == old_branch
    assert first.bindings["pr"]["number"] == old_pr
    assert first.evidence["technical_rearm"]["predecessor"]["launch_id"] == old_launch
    assert first.evidence["technical_rearm"]["phase"] == "finished"
    if ready_before_restart is not None:
        assert ready_before_restart in first.evidence["technical_rearm_history"]
    assert first.evidence["dev_attempts_spent"] >= spent
    assert first.evidence["developer_families"] == ["openai"]
    assert len(first.evidence["continuation_permits"]) == (2 if restart_at == "unlaunched_stop" else 1)
    if restart_at == "unlaunched_stop":
        assert all(first.evidence["continuation_permits"][key] == value for key, value in canceled_permits.items())
        assert any(row["phase"] == "canceled" for row in first.evidence["technical_rearm_history"])
    assert not result.jobs
    assert all(unit.state == UnitState.DELIVERED for unit in result.units.values())
    events = [json.loads(line) for line in case.events.read_text().splitlines()]
    assert sum(row["event"] == "quota" for row in events) == 1
    assert sum(row["event"] == "retained-successor" for row in events) == 1
    assert len(case.service.pulls) == 3
    assert all(pr["merged"] for pr in case.service.pulls.values())
