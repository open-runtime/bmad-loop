"""A resolved route condition belongs to one retained development task."""

import copy
import hashlib
import json
from pathlib import Path
from dataclasses import asdict, replace
from types import SimpleNamespace

import pytest

from bmad_loop.campaign_child_pause import build
from bmad_loop.campaign_interruptions import execution_interruption
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
    source_to_dict,
)
from bmad_loop.campaign_rearm import capture_binding, validate_binding
from bmad_loop.model import PauseCause, PauseKind, Phase, RunState, SessionRecord, StoryTask


@pytest.fixture
def paused_child():
    source = SourceSelector("tickets", id="source", initiative="initiative")
    spec = UnitSpec("source:initiative:1.1", "source", "epic", "Owned ticket", selector="1.1")
    unit = CampaignUnit(
        spec,
        UnitState.RUNNING,
        attempt=2,
        bindings={
            "workspace": "/synthetic/owned",
            "branch": "owned-branch",
            "head": "a" * 40,
            "base": "b" * 40,
            "pr": {"number": 41, "node_id": "owned-native-pr"},
            "source_revision": "private-source-revision",
        },
        evidence={
            "dev_attempts_spent": 2,
            "review_cycles_spent": 1,
            "developer_families": ["anthropic", "openai"],
            "developer_attribution_incomplete": True,
            "unresolved_approval_requirements": ["fresh-human-signoff"],
        },
    )
    snapshot = CampaignSnapshot(
        "campaign",
        "example/repo",
        CampaignSpec("Retained", sources=(source,), units=(spec,)),
        {unit.id: unit},
        common_dir="/synthetic/common",
        config_snapshot={"policy": {"limits": {"max_dev_attempts": 5, "max_review_cycles": 4}}},
    )
    claim = WorkerClaim(
        unit.id,
        2,
        snapshot.generation,
        "original-launch",
        workspace=unit.bindings["workspace"],
        branch=unit.bindings["branch"],
        run_id="original-run",
        run_dir="/synthetic/control/original-worker",
        role="execution",
        quiescent=True,
        status="finished",
        writes=True,
    )
    unit.claim = claim
    snapshot.outbox.append(
        OutboxAction(
            claim.launch_id,
            "launch",
            unit.id,
            {"kind": "execution", "candidate": {"profile": "codex", "model": "gpt-scripted"}},
            snapshot.generation,
            state="confirmed",
        )
    )
    state = RunState("original-run", unit.bindings["workspace"], "2026-11-17", source="tickets")
    task = StoryTask("1.1", 1, phase=Phase.DEV_VERIFY, attempt=2, review_cycle=1)
    task.sessions.append(
        SessionRecord(
            "1.1:dev:2",
            "dev",
            "crashed",
            session_id="original-native-session",
            models=("gpt-scripted",),
            ended="transport",
            native_binding={"outcome": "transport"},
        )
    )
    state.tasks[task.story_key] = task
    state.record_pause(
        "Actual transport producer",
        "development",
        task.story_key,
        cause=PauseCause(PauseKind.TRANSPORT_FAILURE, "engine.session", "transport"),
    )
    receipt = {
        "outcome": "paused",
        "campaign_id": snapshot.id,
        "unit_id": unit.id,
        "launch_id": claim.launch_id,
        "attempt": claim.attempt,
        "generation": claim.generation,
        "workspace": claim.workspace,
        "branch": claim.branch,
        "common_dir": snapshot.common_dir,
        "head": unit.bindings["head"],
        "run_id": claim.run_id,
        "run_dir": "/synthetic/native/original-run",
        "sessions": [session.to_dict() for session in task.sessions],
    }
    receipt["interruption"] = execution_interruption(receipt["sessions"])
    receipt["child_pause"] = build(
        state,
        {
            "unit": asdict(spec),
            "source": source_to_dict(source),
            "source_revision": unit.bindings["source_revision"],
        },
        SimpleNamespace(config_revision=snapshot.config_revision),
        receipt,
    )
    unit.evidence["worker_history"] = [receipt]
    unit.hold = Hold(
        "transport",
        state.paused_reason,
        id=claim.launch_id,
        evidence={"child_pause": receipt["child_pause"]},
    )
    return snapshot, unit


def test_binding_preserves_original_state_and_obligations(paused_child):
    snapshot, unit = paused_child
    previous = snapshot.clone()
    binding = capture_binding(snapshot, unit)
    validate_binding(snapshot, unit, binding)
    assert snapshot == previous
    assert binding["remaining_dev_attempts"] == 3
    assert binding["remaining_review_cycles"] == 3
    assert binding["hold"]["id"] == unit.claim.launch_id
    assert binding["pause_id"] == unit.hold.evidence["child_pause"]["id"]
    assert binding["source"]["kind"] == "tickets"


def test_drained_pending_hold_queue_does_not_invent_an_obligation(paused_child):
    snapshot, unit = paused_child
    unit.evidence["pending_failure_holds"] = []
    binding = capture_binding(snapshot, unit)
    validate_binding(snapshot, unit, binding)
    assert unit.evidence["pending_failure_holds"] == []


def test_read_only_predecessor_cannot_mint_writable_rearm_authority(paused_child):
    snapshot, unit = paused_child
    assert capture_binding(snapshot, unit)["predecessor"]["writes"] is True
    unit.claim.writes = False
    with pytest.raises(CampaignError, match="quiescent development pause"):
        capture_binding(snapshot, unit)


@pytest.mark.parametrize(
    "change",
    [
        "owner_hold",
        "different_pause",
        "live_predecessor",
        "read_only_predecessor",
        "head",
        "branch",
        "pr",
        "source",
        "source_revision",
        "config",
        "graph",
        "generation",
        "attempt_allowance",
        "review_allowance",
        "exhausted",
        "unknown_accounting",
        "family_removed",
        "unknown_attribution_cleared",
        "approval_removed",
        "pending_hold",
        "uncertain_write",
        "route",
        "foreign_unit",
    ],
)
def test_changed_authority_refuses_old_resolution(paused_child, change):
    snapshot, unit = paused_child
    binding = capture_binding(snapshot, unit)
    validate_binding(snapshot, unit, binding)
    if change == "owner_hold":
        unit.hold = Hold("owner", "New owner decision", id="new-hold")
    elif change == "different_pause":
        unit.hold = replace(unit.hold, id="another-launch")
    elif change == "live_predecessor":
        unit.claim.quiescent = False
    elif change == "read_only_predecessor":
        unit.claim.writes = False
    elif change in {"head", "branch", "source_revision"}:
        unit.bindings[change] = "changed"
    elif change == "pr":
        unit.bindings["pr"]["number"] += 1
    elif change == "source":
        unit.bindings["effective_source"] = {"kind": "stories", "id": "foreign"}
    elif change in {"config", "graph", "generation"}:
        attr = change if change == "generation" else change + "_revision"
        setattr(snapshot, attr, getattr(snapshot, attr) + 1)
    elif change in {"attempt_allowance", "review_allowance", "exhausted"}:
        key = "review_cycles_spent" if change == "review_allowance" else "dev_attempts_spent"
        unit.evidence[key] += 10 if change == "exhausted" else 1
    elif change == "unknown_accounting":
        unit.evidence["usage"] = {"legacy_counters_unknown": True}
    elif change == "family_removed":
        unit.evidence["developer_families"] = ["openai"]
    elif change == "unknown_attribution_cleared":
        unit.evidence["developer_attribution_incomplete"] = False
    elif change == "approval_removed":
        unit.evidence["unresolved_approval_requirements"] = []
    elif change == "pending_hold":
        unit.evidence["pending_failure_hold"] = asdict(Hold("owner", "Preserved obligation"))
    elif change == "uncertain_write":
        snapshot.outbox.append(
            OutboxAction("uncertain", "github", unit.id, {}, snapshot.generation, "sent_unknown")
        )
    elif change == "route":
        snapshot.outbox[0].payload["candidate"]["profile"] = "claude"
    elif change == "foreign_unit":
        unit.spec = replace(unit.spec, id="foreign-unit", selector="2.1")
    with pytest.raises(CampaignError):
        validate_binding(snapshot, unit, binding)


def test_plain_availability_or_forged_identity_is_not_resolution(paused_child):
    snapshot, unit = paused_child
    binding = capture_binding(snapshot, unit)
    validate_binding(snapshot, unit, binding)
    forged = copy.deepcopy(binding)
    forged["id"] = "not-the-original-binding"
    with pytest.raises(CampaignError):
        validate_binding(snapshot, unit, forged)
    with pytest.raises(CampaignError):
        validate_binding(snapshot, unit, {"available": True})


@pytest.fixture
def quota_child(paused_child):
    snapshot, unit = paused_child
    receipt = unit.evidence["worker_history"][-1]
    receipt["sessions"][-1]["native_binding"] = {"outcome": "quota"}
    receipt["sessions"][-1]["ended"] = "usage"
    receipt["interruption"] = execution_interruption(receipt["sessions"])
    state = RunState(receipt["run_id"], receipt["workspace"], "2026-11-17", source="tickets")
    task = StoryTask("1.1", 1, phase=Phase.DEV_VERIFY, attempt=2, review_cycle=1)
    task.sessions = [SessionRecord.from_dict(receipt["sessions"][-1])]
    state.tasks[task.story_key] = task
    state.record_pause(
        "Actual quota producer",
        "development",
        task.story_key,
        cause=PauseCause(PauseKind.QUOTA_WAIT, "engine.session", "usage_limit"),
    )
    receipt["child_pause"] = build(
        state,
        {
            "unit": asdict(unit.spec),
            "source": source_to_dict(snapshot.spec.sources[0]),
            "source_revision": unit.bindings["source_revision"],
        },
        SimpleNamespace(config_revision=snapshot.config_revision),
        receipt,
    )
    unit.hold = Hold(
        "quota",
        state.paused_reason,
        id=unit.claim.launch_id,
        evidence={"child_pause": receipt["child_pause"]},
    )
    snapshot.bindings["capacity_faults"] = {
        "codex\0gpt-scripted": {
            "observed_at": 80,
            "reason": "native quota",
            "quota_retry_at": 90,
            "quota_source": "agy-native-individual-quota-v1",
        }
    }
    return snapshot, unit


def condition_case(snapshot, unit, directory=Path("/synthetic/control")):
    from bmad_loop.campaign_scheduler import CapacityObservation
    from bmad_loop.campaign_supervisor import Observation

    binding = capture_binding(snapshot, unit)
    action = OutboxAction(
        "resolution-observation",
        "resolution-reprobe",
        unit.id,
        {"rearm_binding": binding},
        snapshot.generation,
        state="sent_unknown",
    )
    snapshot.outbox.append(action)
    runtime = SimpleNamespace(
        store=SimpleNamespace(directory=lambda _: directory),
        clock=lambda: 100,
        project="/synthetic/owned",
        capacity_observer=lambda *_: CapacityObservation(
            True, slots=2, model_access=True, observed_at=95, expires_at=115
        ),
        _launcher_readiness=lambda *_: {"ready": True, "transport": "bounded-headless"},
        _perform_observe_worker=lambda *_: Observation(
            "confirmed", {"state": "finished", "receipt": unit.evidence["worker_history"][-1]}
        ),
    )
    return runtime, action


def test_unsupported_transport_cannot_use_generic_available_capacity(paused_child):
    from bmad_loop.campaign_rearm import observe

    snapshot, unit = paused_child
    runtime, action = condition_case(snapshot, unit)
    calls = []
    runtime.capacity_observer = lambda *_: calls.append("capacity must not prove transport")
    observed = observe(runtime, snapshot, action)
    assert observed.data["available"] is False
    assert "no maintained condition-specific" in observed.data["reason"]
    assert calls == []


def sealed_case(snapshot, unit, binding, tmp_path):
    from bmad_loop.campaign_transcripts import blob, digest, encoded

    receipt = unit.evidence["worker_history"][-1]
    state = RunState(receipt["run_id"], receipt["workspace"], "2026-11-17", source="tickets")
    task = StoryTask("1.1", 1, phase=Phase.DEV_VERIFY, attempt=2, review_cycle=1)
    task.sessions = [SessionRecord.from_dict(receipt["sessions"][-1])]
    state.tasks[task.story_key] = task
    pause = receipt["child_pause"]
    state.record_pause(
        pause["reason"], pause["stage"], task.story_key, cause=PauseCause.from_dict(pause["cause"])
    )
    state.campaign_binding = {
        "campaign_id": snapshot.id,
        "unit_id": unit.id,
        "config_revision": snapshot.config_revision,
    }
    root = tmp_path / "handoffs" / ("rearm-" + binding["id"][:24])
    current = {
        "original_run_id": state.run_id,
        "original_run_dir": receipt["run_dir"],
        "source_roots": [],
        "predecessor": binding["predecessor"],
        "unit": asdict(unit.spec),
        "head": binding["head"],
        "fingerprint": {"head": binding["head"]},
        "workspace": unit.bindings["workspace"],
        "branch": unit.bindings["branch"],
        "pr": unit.bindings["pr"],
        "stage": snapshot.outbox[0].payload,
    }
    for key, data in (("original_state", state.to_dict()), ("predecessor_receipt", receipt)):
        raw = encoded(data)
        current[key + "_path"] = str(blob(root / "predecessor", raw))
        current[key + "_sha256"] = hashlib.sha256(raw).hexdigest()
    raw = '{"role":"user","content":"Retain unfinished work and require independent review."}\n'
    source = {
        "source_id": "original-native-history",
        "raw_sha256": hashlib.sha256(raw.encode()).hexdigest(),
        "captured_bytes": len(raw),
        "complete_bytes": len(raw),
        "warnings": [],
    }
    from bmad_loop.campaign_acceptance import criteria

    captured = {
        "native_role": "dev",
        "current": current,
        "sources": [source],
        "gaps": [],
        "obligations": criteria(unit.id, unit.spec.acceptance),
    }
    captured["capture_digest"] = digest([current, captured["sources"], captured["obligations"]])
    content = {
        "purpose": "technical_rearm",
        "read_only": False,
        "pause_binding": binding,
        "current_state": current,
        "obligations": captured["obligations"],
        "exact_history": [{"source": source, "untrusted_original_content": raw}],
    }
    packet = encoded(content)
    path = blob(root / "packet", packet)
    return {
        "id": root.name,
        "capture": captured,
        "packet": {
            "purpose": "technical_rearm",
            "read_only": False,
            "path": str(path),
            "sha256": hashlib.sha256(packet).hexdigest(),
            "tokens": len(packet),
        },
        "original_payload": snapshot.outbox[0].payload,
    }


def test_resolved_quota_reaches_bound_ready_state_without_clearing_hold(
    quota_child, monkeypatch, tmp_path
):
    from bmad_loop import campaign_rearm as rearm

    snapshot, unit = quota_child
    runtime, action = condition_case(snapshot, unit, tmp_path)
    binding = action.payload["rearm_binding"]
    captured = sealed_case(snapshot, unit, binding, tmp_path)
    calls = []
    monkeypatch.setattr(rearm, "_capture", lambda *_: calls.append("immutable-capture") or captured)
    before = copy.deepcopy(unit.hold)
    observed = rearm.observe(runtime, snapshot, action)
    assert observed.data["available"] is True
    assert calls == ["immutable-capture"]
    rearm.apply(runtime, snapshot, unit, action, observed.data)
    assert unit.hold == before
    assert unit.evidence["technical_rearm"]["phase"] == "ready"
    assert unit.evidence["technical_rearm"]["binding"] == binding
    assert unit.evidence["dev_attempts_spent"] == 2


@pytest.mark.parametrize(
    "change",
    [
        "unknown_reset",
        "before_reset",
        "stale_capacity",
        "unavailable_capacity",
        "expired_capacity",
        "launcher",
        "live_predecessor",
    ],
)
def test_quota_requires_actual_deadline_capacity_launcher_and_quiescence(
    quota_child, monkeypatch, change
):
    from bmad_loop import campaign_rearm as rearm
    from bmad_loop.campaign_scheduler import CapacityObservation
    from bmad_loop.campaign_supervisor import Observation

    snapshot, unit = quota_child
    if change == "unknown_reset":
        snapshot.bindings["capacity_faults"]["codex\0gpt-scripted"].pop("quota_retry_at")
        snapshot.bindings["capacity_faults"]["codex\0gpt-scripted"].pop("quota_source")
    runtime, action = condition_case(snapshot, unit)
    if change == "before_reset":
        runtime.clock = lambda: 85
    elif change in {"stale_capacity", "unavailable_capacity", "expired_capacity"}:
        runtime.capacity_observer = lambda *_: CapacityObservation(
            change != "unavailable_capacity",
            slots=2,
            model_access=True,
            observed_at=89 if change == "stale_capacity" else 95,
            expires_at=99 if change == "expired_capacity" else 115,
        )
    elif change == "launcher":
        runtime._launcher_readiness = lambda *_: {"ready": False, "reason": "unqualified route"}
    elif change == "live_predecessor":
        runtime._perform_observe_worker = lambda *_: Observation("waiting", {"state": "running"})
    calls = []
    monkeypatch.setattr(rearm, "_capture", lambda *_: calls.append("unsafe-capture") or {})
    observed = rearm.observe(runtime, snapshot, action)
    assert observed.data["available"] is False
    assert calls == []
    assert unit.hold is not None


def test_late_available_reprobe_cannot_erase_new_owner_hold(quota_child, monkeypatch, tmp_path):
    from bmad_loop import campaign_rearm as rearm

    snapshot, unit = quota_child
    runtime, action = condition_case(snapshot, unit, tmp_path)
    binding = action.payload["rearm_binding"]
    sealed = sealed_case(snapshot, unit, binding, tmp_path)
    monkeypatch.setattr(rearm, "_capture", lambda *_: sealed)
    observed = rearm.observe(runtime, snapshot, action)
    assert observed.data["available"] is True
    owner = Hold("owner", "New owner decision", id="new-owner-hold")
    unit.hold = owner
    with pytest.raises(CampaignError, match="quiescent development pause"):
        rearm.apply(runtime, snapshot, unit, action, observed.data)
    assert unit.hold == owner
    assert "technical_rearm" not in unit.evidence


@pytest.mark.parametrize(
    "change",
    [
        "task_history",
        "obligation",
        "omitted_native_history",
        "altered_native_history",
        "head",
        "foreign_pr",
    ],
)
def test_sealed_continuation_preserves_exact_task_context_and_obligations(
    quota_child, tmp_path, change
):
    from bmad_loop.campaign_rearm import validate_capture
    from bmad_loop.campaign_transcripts import blob, digest, encoded

    snapshot, unit = quota_child
    unit.spec = replace(unit.spec, acceptance=("Keep exact acceptance",))
    binding = capture_binding(snapshot, unit)
    sealed = sealed_case(snapshot, unit, binding, tmp_path)
    value = {**sealed, "binding": binding}
    validate_capture(snapshot, unit, value)
    captured = value["capture"]
    current = captured["current"]
    packet = value["packet"]
    content = json.loads(Path(packet["path"]).read_text())
    if change == "task_history":
        state = json.loads(Path(current["original_state_path"]).read_text())
        state["tasks"][unit.spec.selector]["attempt"] = 0
        data = encoded(state)
        current["original_state_path"] = str(
            blob(Path(current["original_state_path"]).parent, data)
        )
        current["original_state_sha256"] = hashlib.sha256(data).hexdigest()
    elif change == "obligation":
        captured["obligations"] = []
        content["obligations"] = []
    elif change == "omitted_native_history":
        content["exact_history"] = []
    elif change == "altered_native_history":
        content["exact_history"][0]["untrusted_original_content"] = "Invented permission"
    elif change == "head":
        current["head"] = "c" * 40
        current["fingerprint"]["head"] = "c" * 40
    elif change == "foreign_pr":
        current["pr"] = {"number": 999}
    # Rehash the outer containers so the regression checks retained authority,
    # not merely a mismatched checksum caused by the test's edit.
    captured["capture_digest"] = digest([current, captured["sources"], captured["obligations"]])
    content["current_state"] = current
    data = encoded(content)
    packet.update(
        path=str(blob(Path(packet["path"]).parent, data)),
        sha256=hashlib.sha256(data).hexdigest(),
        tokens=len(data),
    )
    with pytest.raises(CampaignError):
        validate_capture(snapshot, unit, value)


@pytest.mark.parametrize("change", ["expiry", "reset", "route", "producer", "binding"])
def test_available_result_requires_exact_fresh_condition_proof(
    quota_child, monkeypatch, tmp_path, change
):
    from bmad_loop import campaign_rearm as rearm

    snapshot, unit = quota_child
    runtime, action = condition_case(snapshot, unit, tmp_path)
    sealed = sealed_case(snapshot, unit, action.payload["rearm_binding"], tmp_path)
    monkeypatch.setattr(rearm, "_capture", lambda *_: sealed)
    observed = rearm.observe(runtime, snapshot, action)
    assert observed.data["available"] is True
    baseline = snapshot.clone()
    rearm.apply(runtime, baseline, baseline.units[unit.id], action, observed.data)
    assert baseline.units[unit.id].evidence["technical_rearm"]["phase"] == "ready"
    bad = copy.deepcopy(observed.data)
    condition = bad["condition"] = copy.deepcopy(bad["condition"])
    if change == "expiry":
        condition["expires_at"] = 99
    elif change == "reset":
        condition["quota_retry_at"] = 89
    elif change == "route":
        condition["candidate"]["model"] = "different-provider-model"
    elif change == "producer":
        condition["producer"] = "model-prose"
    elif change == "binding":
        condition["binding_id"] = "another-pause"
    with pytest.raises(CampaignError):
        rearm.apply(runtime, snapshot, unit, action, bad)
    assert "technical_rearm" not in unit.evidence
    assert unit.hold.kind == "quota"


def test_schema_three_request_binds_original_task_condition_and_successor(
    quota_child, monkeypatch, tmp_path
):
    from bmad_loop import campaign_rearm as rearm
    from bmad_loop.adapters.profile import CLIProfile, HookSpec
    from bmad_loop.campaign_continuation import prepare_permit
    from bmad_loop.campaign_execution import CampaignExecution
    from bmad_loop.campaign_worker_authority import continuation_request, execution_envelope
    from bmad_loop.policy import Policy, StageAdapterPolicy

    snapshot, unit = quota_child
    runtime, action = condition_case(snapshot, unit, tmp_path)
    sealed = sealed_case(snapshot, unit, action.payload["rearm_binding"], tmp_path)
    monkeypatch.setattr(rearm, "_capture", lambda *_: sealed)
    observed = rearm.observe(runtime, snapshot, action)
    rearm.apply(runtime, snapshot, unit, action, observed.data)
    predecessor = unit.claim
    claim = replace(
        predecessor,
        launch_id="successor-launch",
        run_id="successor-run",
        run_dir=str(tmp_path / "workers" / "successor-launch"),
        attempt=3,
        quiescent=False,
        status="prepared",
        writes=True,
    )
    payload = {**snapshot.outbox[0].payload, "rearm_id": unit.evidence["technical_rearm"]["id"]}
    unit.claim = claim
    unit.attempt = claim.attempt
    rearm.adopt_launch(snapshot, unit, payload, claim)
    policy = Policy()
    policy = replace(
        policy,
        adapter=replace(policy.adapter, dev=StageAdapterPolicy(name="codex", model="gpt-scripted")),
    )
    profile = CLIProfile("codex", "scripted-unused", HookSpec("none", "", {}))
    template = CampaignExecution(
        snapshot.id,
        unit.id,
        0,
        0,
        snapshot.config_revision,
        tmp_path / "frozen",
        policy,
        {"dev": profile},
    )
    envelope = execution_envelope(template, snapshot, unit, claim, payload, Path(claim.run_dir))
    request = continuation_request(snapshot, unit, claim)
    permit = prepare_permit(request, envelope)
    assert prepare_permit(request, envelope) == permit
    assert permit["schema_version"] == 3
    assert permit["binding"]["purpose"] == "technical_rearm"
    assert "handoff_id" not in permit["binding"]
    assert permit["binding"]["pause_binding"] == action.payload["rearm_binding"]
    assert permit["binding"]["resolution"]["action_id"] == action.id
    assert permit["binding"]["predecessor"]["launch_id"] == predecessor.launch_id
    assert permit["binding"]["successor"]["launch_id"] == claim.launch_id
    assert permit["binding"]["remaining_dev_attempts"] == 3
    assert permit["binding"]["remaining_review_cycles"] == 3
    assert permit["binding"]["packet"] == sealed["packet"]
    mixed = {**request, "handoff_id": "unaudited-handoff"}
    with pytest.raises(CampaignError, match="purpose"):
        prepare_permit(mixed, envelope)


def ready_case(quota_child, monkeypatch, tmp_path):
    from bmad_loop import campaign_rearm as rearm

    snapshot, unit = quota_child
    runtime, action = condition_case(snapshot, unit, tmp_path)
    monkeypatch.setattr(
        rearm,
        "_capture",
        lambda _runtime, _snapshot, _unit, binding: sealed_case(
            _snapshot, _unit, binding, tmp_path
        ),
    )
    observed = rearm.observe(runtime, snapshot, action)
    rearm.apply(runtime, snapshot, unit, action, observed.data)
    action.state, action.receipt = "confirmed", observed.data
    runtime._role = lambda *_a, **_k: rearm._candidate(action.payload["rearm_binding"])
    return snapshot, unit, runtime


@pytest.mark.parametrize("boundary", ["generation", "expiry"])
def test_ready_rearm_refreshes_only_observation_without_resetting_history(
    quota_child, monkeypatch, tmp_path, boundary
):
    from bmad_loop import campaign_rearm as rearm
    from bmad_loop.campaign_scheduler import CapacityObservation

    snapshot, unit, runtime = ready_case(quota_child, monkeypatch, tmp_path)
    previous = copy.deepcopy(unit.evidence["technical_rearm"])
    counters = (
        unit.attempt,
        unit.evidence["dev_attempts_spent"],
        unit.evidence["review_cycles_spent"],
    )
    if boundary == "generation":
        snapshot.generation += 1
    else:
        runtime.clock = lambda: 130
    proposed = rearm.resume_proposal(snapshot, unit, runtime)
    assert proposed.kind == "resolution-reprobe"
    assert proposed.payload["prior_ready"]["id"] == previous["id"]
    action = OutboxAction(
        "fresh-observation",
        proposed.kind,
        unit.id,
        proposed.payload,
        snapshot.generation,
        state="sent_unknown",
    )
    snapshot.outbox.append(action)
    now = runtime.clock()
    runtime.capacity_observer = lambda *_: CapacityObservation(
        True, slots=2, model_access=True, observed_at=now, expires_at=now + 30
    )
    observed = rearm.observe(runtime, snapshot, action)
    assert observed.data["available"] is True
    rearm.apply(runtime, snapshot, unit, action, observed.data)
    action.state = "confirmed"
    assert unit.evidence["technical_rearm_history"] == [previous]
    assert unit.evidence["technical_rearm"]["refresh_of"] == proposed.payload["prior_ready"]
    assert unit.evidence["technical_rearm"]["condition"]["action_id"] == action.id
    assert (
        unit.attempt,
        unit.evidence["dev_attempts_spent"],
        unit.evidence["review_cycles_spent"],
    ) == counters
    assert unit.hold.kind == "quota"
    assert rearm.resume_proposal(snapshot, unit, runtime).kind == "launch"


@pytest.mark.parametrize(
    "change",
    [
        "head",
        "source",
        "config",
        "graph",
        "approval",
        "allowance",
        "pending_successor",
        "consumed_permit",
        "late_prior_identity",
    ],
)
def test_ready_refresh_cannot_rebind_changed_work_or_consumed_authority(
    quota_child, monkeypatch, tmp_path, change
):
    from bmad_loop import campaign_rearm as rearm

    snapshot, unit, runtime = ready_case(quota_child, monkeypatch, tmp_path)
    snapshot.generation += 1
    proposed = rearm.resume_proposal(snapshot, unit, runtime)
    assert proposed.kind == "resolution-reprobe"
    previous = copy.deepcopy(unit.evidence["technical_rearm"])
    if change == "head":
        unit.bindings["head"] = "d" * 40
    elif change == "source":
        unit.bindings["source_revision"] = "other-source"
    elif change in {"config", "graph"}:
        setattr(snapshot, change + "_revision", getattr(snapshot, change + "_revision") + 1)
    elif change == "approval":
        unit.evidence["unresolved_approval_requirements"] = []
    elif change == "allowance":
        unit.evidence["dev_attempts_spent"] += 1
    elif change == "pending_successor":
        snapshot.outbox.append(
            OutboxAction(
                "new-writer", "launch", unit.id, {"kind": "execution"}, snapshot.generation
            )
        )
    elif change == "consumed_permit":
        unit.evidence["continuation_permits"] = {
            "already-consumed": {
                "permit": {"binding": {"rearm_id": previous["id"]}},
                "consumption": {"state": "consumed"},
            }
        }
    elif change == "late_prior_identity":
        unit.evidence["technical_rearm"]["condition"]["action_id"] = "newer-observation"
    action = OutboxAction("refresh", proposed.kind, unit.id, proposed.payload, snapshot.generation)
    with pytest.raises(CampaignError):
        rearm.observe(runtime, snapshot, action)
    assert unit.hold.kind == "quota"
    assert "technical_rearm_history" not in unit.evidence


def test_ready_refresh_refuses_changed_dirty_fingerprint(quota_child, monkeypatch, tmp_path):
    from bmad_loop import campaign_rearm as rearm

    snapshot, unit, runtime = ready_case(quota_child, monkeypatch, tmp_path)
    runtime.clock = lambda: 130
    proposal = rearm.resume_proposal(snapshot, unit, runtime)
    action = OutboxAction(
        "refresh-dirty", proposal.kind, unit.id, proposal.payload, snapshot.generation
    )
    sealed = sealed_case(snapshot, unit, proposal.payload["rearm_binding"], tmp_path)
    sealed["capture"]["current"]["fingerprint"]["worktree"] = "changed-dirty-work"
    monkeypatch.setattr(rearm, "_capture", lambda *_: sealed)
    from bmad_loop.campaign_scheduler import CapacityObservation

    runtime.capacity_observer = lambda *_: CapacityObservation(
        True, slots=2, model_access=True, observed_at=130, expires_at=150
    )
    with pytest.raises(CampaignError, match="changed retained work"):
        rearm.observe(runtime, snapshot, action)
    assert "technical_rearm_history" not in unit.evidence


@pytest.mark.parametrize("change", ["approval", "remaining_allowance"])
def test_refresh_does_not_mint_new_binding_for_changed_live_constraints(quota_child, monkeypatch, tmp_path, change):
    from bmad_loop import campaign_rearm as rearm

    snapshot, unit, runtime = ready_case(quota_child, monkeypatch, tmp_path)
    snapshot.generation += 1
    assert rearm.resume_proposal(snapshot, unit, runtime).kind == "resolution-reprobe"
    if change == "approval":
        unit.evidence["unresolved_approval_requirements"] = []
    else:
        unit.evidence["dev_attempts_spent"] += 1
    with pytest.raises(CampaignError, match="beyond generation"):
        rearm.resume_proposal(snapshot, unit, runtime)
    assert unit.hold.kind == "quota"
