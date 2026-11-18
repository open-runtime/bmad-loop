"""Delivery close recovery preserves uncertainty, human scope and finite lineage."""

import copy
import json
from dataclasses import replace

import pytest
from test_campaign_parent_closure import submit
from test_campaign_runtime import diagnose
from test_campaign_runtime import production_case as production_case
from test_campaign_runtime import run_case
from test_campaign_tracking import HierarchyTransport, snapshot

from bmad_loop import campaign_tracking_recovery as recovery
from bmad_loop.campaign_action_history import action as historical_action
from bmad_loop.campaign_github import (
    ApiResponse,
    GithubError,
    GithubGateway,
    MutationIntent,
    UncertainMutation,
)
from bmad_loop.campaign_model import Hold, UnitState
from bmad_loop.campaign_runtime import CampaignRuntime
from bmad_loop.campaign_store import CampaignStore, add_action
from bmad_loop.campaign_supervisor import CampaignSupervisor
from bmad_loop.campaign_tracking import HierarchyCoordinator


class TrackingTransport(HierarchyTransport):
    def __init__(self):
        super().__init__()
        self.drop_ack = False

    def request(self, method, path, payload=None):
        response = super().request(method, path, payload)
        if method == "PATCH" and self.drop_ack:
            self.drop_ack = False
            raise UncertainMutation("Synthetic accepted close; acknowledgement lost")
        return ApiResponse(response.status, copy.deepcopy(response.data), response.headers)


@pytest.fixture
def tracking(tmp_path, monkeypatch):
    state = snapshot()
    unit = next(iter(state.units.values()))
    # A landing repair has no native source selector to publish; the actual
    # ordinary story source transaction is covered by the production fixture.
    unit.spec = replace(unit.spec, kind="landing_repair")
    unit.state = UnitState.MERGED_PENDING_SYNC
    unit.evidence.update(merge={"merge_commit": "a" * 40}, landing_acceptance={"state": "covered"})
    from bmad_loop.campaign_acceptance import ensure

    ensure(unit)
    prove_current(unit)
    state.spec = replace(state.spec, units=(unit.spec,))
    service = TrackingTransport()
    issue = {
        "id": 1001,
        "number": 1,
        "updated_at": "t0",
        "state": "open",
        "state_reason": None,
        "title": "Story",
        "body": "Accepted owner requirements",
        "labels": [],
        "assignees": [],
    }
    service.issues[1] = issue
    unit.bindings["issue"] = HierarchyCoordinator._binding(issue, "issue")
    clock = [100.0]
    state.bindings.update(
        inventory={"complete": True, "observed_at": 100},
        intake={"observed_at": 100, "pending_count": 0},
        capacity={"observed_at": 100},
    )
    store = CampaignStore(tmp_path / "state", clock=lambda: clock[0])
    runtime = CampaignRuntime(
        tmp_path, store, gateway=GithubGateway(service, "a/b"), clock=lambda: clock[0]
    )
    monkeypatch.setattr(runtime, "_role", lambda *args, **kwargs: None)
    return (
        runtime,
        CampaignSupervisor(store, runtime, clock=lambda: clock[0]),
        store.create(state),
        service,
        clock,
    )


def prepared(case):
    runtime, supervisor, state, service, clock = case
    chosen = tuple(row for row in runtime.proposals(state) if row.kind == "sync-tracking")
    assert len(chosen) == 1
    state = supervisor._queue(state, chosen)
    action = next(
        row for row in state.outbox if row.kind == "sync-tracking" and row.state == "prepared"
    )
    state = submit(supervisor, state, action)
    action = next(
        row
        for row in state.outbox
        if row.kind == "github" and row.payload.get("purpose") == "tracking"
    )
    return state, action


def retry_case(case, state, *, restart=False):
    runtime, supervisor, _, service, clock = case
    if restart:
        store = CampaignStore(runtime.store.root, clock=lambda: clock[0])
        runtime = CampaignRuntime(
            runtime.project, store, gateway=GithubGateway(service, "a/b"), clock=lambda: clock[0]
        )
        runtime._role = lambda *args, **kwargs: None
        supervisor = CampaignSupervisor(store, runtime, clock=lambda: clock[0])
        state = store.load(state.id)
    clock[0] += recovery.RETRY_SECONDS
    for key in ("inventory", "intake", "capacity"):
        state.bindings[key]["observed_at"] = clock[0]
    state = runtime.store.save(state, state.revision)
    pending = next(
        (
            row
            for row in state.outbox
            if row.kind == "tracking-reconcile" and row.state == "sent_unknown"
        ),
        None,
    )
    if pending:
        state = submit(supervisor, state, pending, reconcile=True)
    else:
        chosen = tuple(row for row in runtime.proposals(state) if row.kind == "tracking-reconcile")
        assert len(chosen) == 1, [(row.kind, row.payload) for row in runtime.proposals(state)]
        state = supervisor._queue(state, chosen)
        action = next(
            row
            for row in state.outbox
            if row.kind == "tracking-reconcile" and row.state == "prepared"
        )
        state = submit(supervisor, state, action)
    return (runtime, supervisor, state, service, clock)


def finish_close(case):
    runtime, supervisor, state, service, _ = case
    action = next(
        row
        for row in state.outbox
        if row.kind == "github"
        and row.payload.get("purpose") == "tracking"
        and row.state == "prepared"
    )
    state = submit(supervisor, state, action)
    return state


def finish_completion(case):
    runtime, supervisor, state, service, clock = case
    refresh = next(
        row
        for row in state.outbox
        if row.kind == "sync-tracking"
        and row.state == "prepared"
        and row.payload.get("completion_predecessor")
    )
    state = submit(supervisor, state, refresh)
    return finish_close((runtime, supervisor, state, service, clock))


def prove_current(unit):
    ledger = unit.evidence["acceptance"]
    ledger["current"] = {
        "revision": ledger["revision"],
        "complete": True,
        "criteria": [{**row, "status": "passed"} for row in ledger["criteria"]],
    }


def test_preflight_collision_restarts_replans_and_delivers_with_one_patch(tracking):
    runtime, supervisor, _, service, clock = tracking
    state, original = prepared(tracking)
    frozen = copy.deepcopy(original.payload)
    service.issues[1].update(updated_at="t1", labels=[{"name": "owner-priority"}])
    state = submit(supervisor, state, original)
    unit = next(iter(state.units.values()))
    assert unit.state == UnitState.MERGED_PENDING_SYNC and unit.hold is None
    assert service.effects == []
    row = next(row for row in state.outbox if row.id == original.id)
    assert row.state == "confirmed" and row.receipt["canceled_before_write"]
    assert row.payload == frozen
    resumed = retry_case(tracking, state, restart=True)
    state = finish_close(resumed)
    assert next(iter(state.units.values())).state == UnitState.DELIVERED
    assert service.effects == [
        ("PATCH", "issues/1", {"state": "closed", "state_reason": "completed"})
    ]
    assert service.issues[1]["body"] == "Accepted owner requirements"
    assert service.issues[1]["labels"] == [{"name": "owner-priority"}]
    assert historical_action(state, original.id).payload == frozen


def test_native_hold_present_before_prepare_blocks_write_then_real_removal_releases(tracking):
    runtime, supervisor, state, service, clock = tracking
    unit = next(iter(state.units.values()))
    accepted = copy.deepcopy(unit.evidence["acceptance"])
    spec = unit.spec
    service.issues[1]["labels"] = [{"name": "bmad:hold"}]
    proposals = tuple(row for row in runtime.proposals(state) if row.kind == "sync-tracking")
    assert len(proposals) == 1
    state = supervisor._queue(state, proposals)
    action = next(row for row in state.outbox if row.kind == "sync-tracking")
    state = submit(supervisor, state, action)
    pending = next(row for row in state.outbox if row.id == action.id)
    assert pending.state == "sent_unknown" and "hold label" in pending.error
    assert service.effects == [] and service.issues[1]["state"] == "open"
    assert not any(row.kind == "github" for row in state.outbox)
    service.issues[1].update(labels=[], updated_at="hold-removed")
    clock[0] = pending.next_retry_at
    state = submit(supervisor, state, pending, reconcile=True)
    completed = finish_close((runtime, supervisor, state, service, clock))
    result = completed.units[unit.id]
    assert result.state == UnitState.DELIVERED
    assert result.spec == spec and result.evidence["acceptance"] == accepted
    assert service.effects == [
        ("PATCH", "issues/1", {"state": "closed", "state_reason": "completed"})
    ]


def test_runtime_ack_then_owner_hold_reopen_release_requires_actual_completed_readback(tracking):
    runtime, supervisor, _, service, _ = tracking
    state, action = prepared(tracking)
    original = copy.deepcopy(action.payload)

    def late_hold(snapshot):
        # Script an authoritative owner hold arriving after I/O and before the
        # coordinator consumes it; exercise the real final apply guard.
        snapshot.units[action.unit].hold = Hold("owner", "Late owner hold", id="late-owner")
        return runtime.store.save(snapshot, snapshot.revision)

    state = submit(supervisor, state, action, before_harvest=late_hold)
    unit = state.units[action.unit]
    assert unit.state == UnitState.MERGED_PENDING_SYNC and unit.hold.id == "late-owner"
    acknowledged = next(row for row in state.outbox if row.id == action.id)
    assert acknowledged.state == "confirmed" and "intent" not in acknowledged.receipt
    assert acknowledged.receipt["uncredited_tracking_intent"]["state"] == "confirmed"
    assert len(service.effects) == 1
    service.issues[1].update(state="open", state_reason=None, updated_at="t0")
    unit.hold = None  # Script the owner's resolution of this hold only.
    state = runtime.store.save(state, state.revision)
    waiting = retry_case(tracking, state, restart=True)
    assert waiting[2].units[action.unit].state == UnitState.MERGED_PENDING_SYNC
    assert len(service.effects) == 1
    assert not any(
        row.kind == "sync-tracking" and row.state == "prepared" for row in waiting[2].outbox
    )
    service.issues[1].update(state="closed", state_reason="completed", updated_at="owner-completed")
    renewed = retry_case(waiting, waiting[2])
    completed = finish_completion(renewed)
    assert completed.units[action.unit].state == UnitState.DELIVERED
    assert len(service.effects) == 1
    assert historical_action(completed, action.id).payload == original


def test_runtime_ack_waits_for_late_current_proof_then_renews_completion_authority(tracking):
    runtime, supervisor, _, service, _ = tracking
    state, action = prepared(tracking)
    original = copy.deepcopy(action.payload)

    def late_criterion(snapshot):
        ledger = snapshot.units[action.unit].evidence["acceptance"]
        ledger["revision"] += 1
        ledger["criteria"].append(
            {
                "id": "late-required",
                "text": "Late required criterion",
                "required": True,
                "status": "not_run",
            }
        )
        return runtime.store.save(snapshot, snapshot.revision)

    state = submit(supervisor, state, action, before_harvest=late_criterion)
    assert state.units[action.unit].state == UnitState.MERGED_PENDING_SYNC
    assert len(service.effects) == 1
    waiting = retry_case(tracking, state, restart=True)
    assert not any(
        row.kind == "sync-tracking" and row.state == "prepared" for row in waiting[2].outbox
    )
    assert any(
        "current required acceptance revision" in row.error
        for row in waiting[2].outbox
        if row.kind == "tracking-reconcile"
    )
    unit = waiting[2].units[action.unit]
    prove_current(unit)  # New controller proof covers the actual current ledger.
    state = waiting[0].store.save(waiting[2], waiting[2].revision)
    renewed = retry_case(waiting, state)
    completed = finish_completion(renewed)
    result = completed.units[action.unit]
    assert result.state == UnitState.DELIVERED
    assert any(
        row["id"] == "late-required" and row["required"]
        for row in result.evidence["acceptance"]["criteria"]
    )
    assert len(service.effects) == 1
    assert historical_action(completed, action.id).payload == original


@pytest.mark.parametrize("lost_ack", [False, True])
def test_normal_close_and_lost_ack_use_one_write(tracking, lost_ack):
    runtime, supervisor, _, service, clock = tracking
    state, action = prepared(tracking)
    service.drop_ack = lost_ack
    state = submit(supervisor, state, action)
    if lost_ack:
        state = runtime.store.load(state.id)
        row = next(row for row in state.outbox if row.id == action.id)
        assert row.state == "sent_unknown"
        clock[0] = row.next_retry_at
        state = submit(supervisor, state, row, reconcile=True)
    assert next(iter(state.units.values())).state == UnitState.DELIVERED
    assert len(service.effects) == 1
    assert not next(iter(state.units.values())).evidence.get(recovery.KEY)


@pytest.mark.parametrize("timing", ["before-prepare", "after-prepare"])
def test_human_noncompleted_close_is_retained_until_actual_completed_readback(tracking, timing):
    runtime, supervisor, _, service, _ = tracking
    if timing == "before-prepare":
        service.issues[1].update(state="closed", state_reason="not_planned")
    state, action = prepared(tracking)
    if timing == "after-prepare":
        service.issues[1].update(state="closed", state_reason="not_planned", updated_at="t1")
    state = submit(supervisor, state, action)
    waiting = retry_case(tracking, state)
    assert next(iter(waiting[2].units.values())).state == UnitState.MERGED_PENDING_SYNC
    assert service.issues[1]["state_reason"] == "not_planned" and service.effects == []
    service.issues[1].update(state_reason="completed", updated_at="t2")
    completed = retry_case(waiting, waiting[2])
    state = finish_close(completed)
    assert next(iter(state.units.values())).state == UnitState.DELIVERED
    assert service.effects == []


@pytest.mark.parametrize(
    "change",
    [
        "body",
        "title",
        "assignee",
        "comment",
        "copied-marker",
        "status-label",
        "hold-label",
        "parent",
    ],
)
def test_replan_retains_changed_human_scope_and_ownership(tracking, change):
    runtime, supervisor, _, service, _ = tracking
    state, action = prepared(tracking)
    issue = service.issues[1]
    issue["updated_at"] = "t1"
    if change in {"body", "title"}:
        issue[change] += " New owner requirement"
    elif change == "assignee":
        issue["assignees"] = [{"id": 12, "login": "owner"}]
    elif change in {"comment", "copied-marker"}:
        body = "New acceptance obligation"
        if change == "copied-marker":
            body += " <!-- bmad-campaign:v1:" + "a" * 64 + " -->"
        service.comments[1] = [
            {"id": 88, "body": body, "user": {"id": 7, "login": "same-controller-account"}}
        ]
    elif change in {"status-label", "hold-label"}:
        issue["labels"] = [
            {"name": "status:owner-review" if change == "status-label" else "bmad:hold"}
        ]
    else:
        service.issues[2] = {"id": 1002, "number": 2, "state": "open", "updated_at": "t1"}
        service.parents[1] = 2
    state = submit(supervisor, state, action)
    resumed = retry_case(tracking, state)
    unit = next(iter(resumed[2].units.values()))
    assert unit.state == UnitState.MERGED_PENDING_SYNC
    assert recovery.active(unit)
    assert service.effects == []
    assert not [
        row for row in resumed[2].outbox if row.kind == "github" and row.state == "prepared"
    ]


def test_later_no_send_refusal_does_not_erase_prior_unknown_attempt(tracking):
    runtime, _, _, service, clock = tracking
    state, action = prepared(tracking)
    intent = MutationIntent.from_dict(action.payload["intent"])
    earlier = replace(intent, state="sent_unknown", error="Earlier transport outcome unknown")
    action.receipt = {"intent": earlier.to_dict()}
    service.issues[1]["updated_at"] = "new"
    refused = runtime.gateway(state).execute(
        replace(intent, state="sent_unknown"), generation=state.generation
    )
    assert refused.state == "rejected" and refused.fault["certainty"] == "not_submitted"
    observed = recovery.observation(state, action, refused, now=clock[0])
    assert observed.state == "waiting"
    assert recovery.KEY not in observed.data and not observed.data.get("canceled_before_write")
    assert service.effects == []


@pytest.mark.parametrize("deadline", [None, 400])
def test_unknown_close_wait_retains_uncertainty_and_server_deadline(tracking, deadline):
    _, _, state, _, clock = tracking
    unit = next(iter(state.units.values()))
    intent = MutationIntent(
        "unknown-close",
        "close_issue",
        "PATCH",
        "repos/a/b/issues/1",
        {"state": "closed", "state_reason": "completed"},
        state="sent_unknown",
        fault={"category": "rate_limit", "retry_after_s": deadline} if deadline else None,
    )
    action = add_action(
        state,
        "github",
        unit.id,
        {"intent": intent.to_dict(), "purpose": "tracking"},
        action_id=intent.action_id,
    )
    observed = recovery.observation(state, action, intent, now=clock[0])
    assert observed.state == "waiting"
    assert observed.retry_at == clock[0] + (deadline or recovery.RETRY_SECONDS)
    assert observed.data["intent"]["state"] == "sent_unknown"
    assert recovery.KEY not in observed.data and not observed.data.get("canceled_before_write")


def test_fresh_owner_hold_blocks_even_completed_remote_readback(tracking):
    runtime, supervisor, _, service, _ = tracking
    state, action = prepared(tracking)
    service.issues[1]["updated_at"] = "t1"
    state = submit(supervisor, state, action)
    unit = next(iter(state.units.values()))
    unit.hold = Hold("owner", "Retain outstanding acceptance", id="new-owner-hold")
    state = runtime.store.save(state, state.revision)
    service.issues[1].update(state="closed", state_reason="completed")
    observed = retry_case(tracking, state)
    retained = next(iter(observed[2].units.values()))
    assert retained.hold.id == "new-owner-hold" and retained.state == UnitState.MERGED_PENDING_SYNC
    assert service.effects == []


@pytest.mark.parametrize("change", ["revision", "criterion", "approval", "decision"])
def test_late_current_acceptance_or_decision_authority_cannot_credit_old_close(tracking, change):
    runtime, _, state, service, _ = tracking
    unit = next(iter(state.units.values()))
    unit.evidence["acceptance"] = {
        "revision": 1,
        "criteria": [{"id": "original", "required": True, "status": "passed"}],
    }
    prove_current(unit)
    spec = unit.spec
    intent = recovery.prepare_close(
        runtime.gateway(state), state, unit, 1, action_id="old-close", source_confirmed=True
    )
    action = add_action(
        state,
        "github",
        unit.id,
        {"intent": intent.to_dict(), "purpose": "tracking"},
        action_id=intent.action_id,
    )
    confirmed = replace(
        intent,
        state="confirmed",
        receipt={**service.issues[1], "state": "closed", "state_reason": "completed"},
    )
    assert recovery.success_guard(state, action, confirmed) == ""
    if change == "revision":
        unit.evidence["acceptance"]["revision"] = 2
    elif change == "criterion":
        unit.evidence["acceptance"]["criteria"].append(
            {
                "id": "late",
                "required": True,
                "status": "not_run",
                "text": "Late required acceptance",
            }
        )
    elif change == "approval":
        unit.evidence["unresolved_approval_requirements"] = [
            {"id": "late-owner-approval", "head": "a" * 40}
        ]
    else:
        state.bindings["decision_receipts"] = {
            "decision": {
                "coordinates": {"unit": unit.id},
                "clear_hold": False,
                "disposition": "owner-required",
            }
        }
    if change in {"revision", "criterion"}:
        prove_current(unit)  # Isolate the old-close binding from the live-proof gate.
    assert unit.spec == spec
    assert "acceptance obligations changed" in recovery.success_guard(state, action, confirmed)
    assert service.effects == []


def test_new_close_requires_current_criterion_proof_then_releases_when_proven(tracking):
    runtime, _, state, service, _ = tracking
    unit = next(iter(state.units.values()))
    unit.evidence["acceptance"] = {
        "revision": 1,
        "criteria": [{"id": "first", "text": "First", "required": True, "status": "not_run"}],
    }
    prove_current(unit)
    original = recovery.prepare_close(
        runtime.gateway(state), state, unit, 1, action_id="first-close", source_confirmed=True
    )
    action = add_action(
        state,
        "github",
        unit.id,
        {"intent": original.to_dict(), "purpose": "tracking"},
        action_id=original.action_id,
    )
    assert recovery.success_guard(state, action, original) == ""
    unit.evidence["acceptance"]["revision"] = 2
    unit.evidence["acceptance"]["criteria"].append(
        {"id": "late", "text": "Late requirement", "required": True, "status": "not_run"}
    )
    fresh = recovery.prepare_close(
        runtime.gateway(state), state, unit, 1, action_id="fresh-close", source_confirmed=True
    )
    fresh_action = replace(
        action, id=fresh.action_id, payload={"intent": fresh.to_dict(), "purpose": "tracking"}
    )
    assert "current required acceptance revision" in recovery.success_guard(
        state, fresh_action, fresh
    )
    prove_current(unit)
    assert recovery.success_guard(state, fresh_action, fresh) == ""
    assert "acceptance obligations changed" in recovery.success_guard(state, action, original)
    assert all(row["status"] == "not_run" for row in unit.evidence["acceptance"]["criteria"])
    assert service.effects == []


@pytest.mark.parametrize("fault", ["missing-current", "criterion-identity", "incomplete"])
def test_required_criteria_without_exact_current_proof_remain_unknown(tracking, fault):
    _, _, state, _, _ = tracking
    unit = next(iter(state.units.values()))
    unit.evidence["acceptance"] = {
        "revision": 1,
        "criteria": [{"id": "first", "text": "Accepted requirement", "required": True}],
    }
    prove_current(unit)
    assert recovery.acceptance_wait(unit) == ""
    if fault == "missing-current":
        unit.evidence["acceptance"].pop("current")
    elif fault == "criterion-identity":
        unit.evidence["acceptance"]["current"]["criteria"][0]["text"] = "Different requirement"
    else:
        unit.evidence["acceptance"]["current"]["complete"] = False
    assert recovery.acceptance_wait(unit)


@pytest.mark.parametrize("human_state", ["open", "not_planned"])
def test_acknowledged_close_held_then_human_changed_is_readback_only(tracking, human_state):
    runtime, _, state, service, _ = tracking
    unit = next(iter(state.units.values()))
    intent = recovery.prepare_close(
        runtime.gateway(state), state, unit, 1, action_id="close", source_confirmed=True
    )
    action = add_action(
        state,
        "github",
        unit.id,
        {"intent": intent.to_dict(), "purpose": "tracking"},
        action_id=intent.action_id,
    )
    acknowledged = runtime.gateway(state).execute(
        replace(intent, state="sent_unknown"), generation=state.generation
    )
    assert acknowledged.state == "confirmed" and len(service.effects) == 1
    unit.hold = Hold("owner", "Retain work", id="late-owner-hold")
    assert recovery.success_guard(state, action, acknowledged)
    current = recovery.readback(runtime, state, action, acknowledged)
    assert current.state == "confirmed"
    unit.hold = None  # Script the owner resolving only this hold.
    service.issues[1].update(
        state="open" if human_state == "open" else "closed",
        state_reason=None if human_state == "open" else "not_planned",
        # GitHub timestamps are not an atomic lifecycle precondition. Keep the
        # same timestamp to isolate the permanent no-replay guard.
        updated_at="t0",
    )
    directly_observed = runtime.gateway(state).reconcile_update(
        current, allow_retry=True, generation=state.generation
    )
    assert directly_observed.state == "sent_unknown" and len(service.effects) == 1
    pending = recovery.readback(runtime, state, action, current)
    assert pending.state == "sent_unknown"
    assert pending.coordinates["tracking_acknowledgement"]["receipt"] == acknowledged.receipt
    with pytest.raises(GithubError, match="readback-only"):
        runtime.gateway(state).execute(pending, generation=state.generation)
    observed = runtime.gateway(state).reconcile_update(
        pending, allow_retry=True, generation=state.generation
    )
    assert observed.state == "sent_unknown" and len(service.effects) == 1
    service.issues[1].update(state="closed", state_reason="completed", updated_at="resolved")
    completed = recovery.readback(runtime, state, action, pending)
    assert completed.state == "confirmed" and recovery.success_guard(state, action, completed) == ""
    assert len(service.effects) == 1


def test_path_empty_already_closed_intent_needs_fresh_readback(tracking):
    runtime, _, state, service, _ = tracking
    unit = next(iter(state.units.values()))
    service.issues[1].update(state="closed", state_reason="completed")
    intent = recovery.prepare_close(
        runtime.gateway(state), state, unit, 1, action_id="noop", source_confirmed=True
    )
    assert intent.state == "confirmed" and intent.path == ""
    action = add_action(
        state,
        "github",
        unit.id,
        {"intent": intent.to_dict(), "purpose": "tracking"},
        action_id=intent.action_id,
    )
    service.issues[1].update(state="open", state_reason=None, updated_at="reopened")
    observed = recovery.readback(runtime, state, action, intent)
    assert observed.state == "sent_unknown" and observed.path == "repos/a/b/issues/1"
    service.issues[1].update(state="closed", state_reason="completed", updated_at="resolved")
    assert recovery.readback(runtime, state, action, observed).state == "confirmed"
    assert service.effects == []


def test_acknowledgement_survives_failed_fresh_readback(tracking, monkeypatch):
    runtime, _, state, service, clock = tracking
    unit = next(iter(state.units.values()))
    intent = recovery.prepare_close(
        runtime.gateway(state), state, unit, 1, action_id="close", source_confirmed=True
    )
    action = add_action(
        state,
        "github",
        unit.id,
        {"intent": intent.to_dict(), "purpose": "tracking"},
        action_id=intent.action_id,
    )
    acknowledged = runtime.gateway(state).execute(
        replace(intent, state="sent_unknown"), generation=state.generation
    )
    assert acknowledged.state == "confirmed" and len(service.effects) == 1
    original = service.request

    def expired(method, path, payload=None):
        if method == "GET" and path == "repos/a/b/issues/1":
            raise GithubError("Synthetic authentication expired", status=401)
        return original(method, path, payload)

    monkeypatch.setattr(service, "request", expired)
    pending = recovery.readback(runtime, state, action, acknowledged)
    assert pending.state == "sent_unknown"
    assert pending.coordinates["tracking_acknowledgement"]["receipt"] == acknowledged.receipt
    assert pending.receipt == acknowledged.receipt
    observed = recovery.observation(state, action, pending, now=clock[0])
    assert observed.retry_at == clock[0] + 300
    assert recovery.KEY not in observed.data
    monkeypatch.setattr(service, "request", original)
    assert recovery.readback(runtime, state, action, pending).state == "confirmed"
    assert len(service.effects) == 1


@pytest.mark.parametrize("change", ["revision", "criterion"])
def test_replan_preserves_late_ledger_obligations_without_changing_unit_spec(tracking, change):
    runtime, supervisor, _, service, _ = tracking
    state, action = prepared(tracking)
    service.issues[1]["updated_at"] = "t1"
    state = submit(supervisor, state, action)
    unit = next(iter(state.units.values()))
    original_spec = unit.spec
    ledger = unit.evidence["acceptance"]
    if change == "revision":
        ledger["revision"] += 1
    else:
        ledger["criteria"].append({"id": "late-required", "required": True, "status": "not_run"})
    state = runtime.store.save(state, state.revision)
    resumed = retry_case(tracking, state, restart=True)
    retained = next(iter(resumed[2].units.values()))
    assert retained.spec == original_spec
    assert retained.state == UnitState.MERGED_PENDING_SYNC and recovery.active(retained)
    assert any(
        "acceptance obligations changed" in row.error
        for row in resumed[2].outbox
        if row.kind == "tracking-reconcile"
    )
    assert service.effects == []


@pytest.mark.parametrize("edited", [False, True])
def test_only_receipt_proven_controller_comment_is_excluded_from_replan(tracking, edited):
    runtime, supervisor, _, service, _ = tracking
    state, action = prepared(tracking)
    body = "State: merged_pending_sync"
    comment = {"id": 88, "body": body, "user": {"id": 7, "login": "controller"}}
    intent = MutationIntent(
        "own-comment",
        "comment",
        "POST",
        "repos/a/b/issues/1/comments",
        {"body": body},
        state="confirmed",
        receipt=copy.deepcopy(comment),
    )
    own = add_action(
        state,
        "github",
        action.unit,
        {"intent": intent.to_dict(), "purpose": "transition"},
        action_id="own-comment",
    )
    own.state, own.receipt = "confirmed", {"intent": intent.to_dict()}
    state = runtime.store.save(state, state.revision)
    service.comments[1] = [{**comment, "body": body + " Owner edit" if edited else body}]
    service.issues[1]["updated_at"] = "t1"
    state = submit(supervisor, state, next(row for row in state.outbox if row.id == action.id))
    resumed = retry_case(tracking, state)
    if edited:
        assert next(iter(resumed[2].units.values())).state == UnitState.MERGED_PENDING_SYNC
        assert service.effects == []
    else:
        completed = finish_close(resumed)
        assert next(iter(completed.units.values())).state == UnitState.DELIVERED
        assert len(service.effects) == 1


def test_three_successor_limit_survives_restarts_without_resetting_lineage(tracking):
    runtime, supervisor, _, service, _ = tracking
    state, action = prepared(tracking)
    root_id = action.id
    case = tracking
    for count in range(recovery.MAX_REPLANS + 1):
        service.issues[1]["updated_at"] = f"conflict-{count}"
        state = submit(case[1], state, action)
        case = retry_case(case, state, restart=True)
        state = case[2]
        prepared_actions = [
            row
            for row in state.outbox
            if row.kind == "github"
            and row.payload.get("purpose") == "tracking"
            and row.state == "prepared"
        ]
        if count < recovery.MAX_REPLANS:
            assert len(prepared_actions) == 1
            action = prepared_actions[0]
            plan = action.payload["intent"]["coordinates"]["tracking_replan"]
            assert plan["root_action"] == root_id and plan["replans"] == count + 1
        else:
            assert not prepared_actions
            assert any(
                "allowance exhausted" in row.error
                for row in state.outbox
                if row.kind == "tracking-reconcile"
            )
    assert next(iter(state.units.values())).state == UnitState.MERGED_PENDING_SYNC
    assert service.effects == []


@pytest.mark.parametrize("production_case", [{"intake_mode": "off"}], indirect=True)
def test_complete_story_lifecycle_recovers_preflight_collision_and_releases_dependent(
    production_case, monkeypatch
):
    """Real Git/source publication and scripted workers, then durable stop/resume."""
    from conftest import git

    case = production_case
    gateway = case.runtime.gateway(case.snapshot)
    original_execute = gateway.execute
    collided = []

    def collide(intent, **kwargs):
        if intent.kind == "close_issue" and not collided:
            number = int(intent.path.rsplit("/", 1)[-1])
            issue = case.service.issues[number]
            if "source:project:first" in issue.get("body", ""):
                issue.update(updated_at="2026-11-05T10:01:00Z", labels=[{"name": "owner-priority"}])
                collided.append((intent.action_id, number, issue["body"]))
                case.service._save()
        return original_execute(intent, **kwargs)

    monkeypatch.setattr(gateway, "execute", collide)
    original_proposals = case.runtime.proposals

    def stop_after_no_send(snapshot):
        if recovery.active(snapshot.units["source:project:first"]):
            case.store.enqueue(snapshot.id, "stop", request_id="tracking-preflight-observed")
        return original_proposals(snapshot)

    with monkeypatch.context() as patch:
        patch.setattr(case.runtime, "proposals", stop_after_no_send)
        pending = run_case(case)
    assert pending.phase == "stopped", diagnose(pending)
    assert pending.units["source:project:first"].state == UnitState.MERGED_PENDING_SYNC
    assert len(collided) == 1
    events = [json.loads(line) for line in case.events.read_text().splitlines()]
    assert not any(
        row["event"] == "execution-start" and row["unit"].endswith("dependent") for row in events
    )
    completed = run_case(case)
    assert completed.phase == "delivered", diagnose(completed)
    assert all(unit.state == UnitState.DELIVERED for unit in completed.units.values())
    assert len(case.service.pulls) == 3 and all(pr["merged"] for pr in case.service.pulls.values())
    _, number, body = collided[0]
    closes = [
        payload
        for method, path, payload in case.service.calls
        if method == "PATCH"
        and path == f"repos/a/b/issues/{number}"
        and payload.get("state") == "closed"
    ]
    assert closes == [{"state": "closed", "state_reason": "completed"}]
    assert case.service.issues[number]["body"] == body
    assert case.service.issues[number]["labels"] == [{"name": "owner-priority"}]
    for name in ("first", "second", "dependent"):
        assert "status: done" in git(
            case.remote, "show", f"refs/heads/main:specs/epic/stories/{name}-story.md"
        )
    events = [json.loads(line) for line in case.events.read_text().splitlines()]
    assert len([row for row in events if row["event"] == "execution-start"]) == 3
