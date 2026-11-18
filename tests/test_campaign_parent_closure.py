"""Parent scopes close independently while writes retain durable uncertainty."""

from dataclasses import replace

import pytest
from test_campaign_tracking import HierarchyTransport, snapshot

from bmad_loop import campaign_parent_closure as closure
from bmad_loop.campaign_github import ApiResponse, GithubGateway, UncertainMutation
from bmad_loop.campaign_model import CampaignUnit, Hold, UnitState
from bmad_loop.campaign_runtime import CampaignRuntime
from bmad_loop.campaign_store import CampaignStore
from bmad_loop.campaign_supervisor import CampaignSupervisor
from bmad_loop.campaign_tracking import HierarchyCoordinator


def configured(tmp_path, count=3, *, held=(), initiatives=None):
    state = snapshot()
    template = next(iter(state.units.values())).spec
    state.units = {}
    transport = HierarchyTransport()
    bindings = {}
    for index in range(1, count + 1):
        spec = replace(
            template,
            id=f"unit-{index}",
            epic=f"epic-{index}",
            initiative=(initiatives or {}).get(index, template.initiative),
        )
        state.units[spec.id] = CampaignUnit(
            spec,
            UnitState.MERGED_PENDING_SYNC if index in held else UnitState.DELIVERED,
            hold=(
                Hold("owner", "Independent required owner decision", id=f"owner-{index}")
                if index in held
                else None
            ),
        )
        if index not in held:
            from bmad_loop.campaign_acceptance import ensure

            ledger = ensure(state.units[spec.id])
            ledger["current"] = {
                "revision": ledger["revision"],
                "complete": True,
                "criteria": [{**row, "status": "passed"} for row in ledger["criteria"]],
            }
        issue = {"id": 1000 + index, "number": index, "state": "open", "updated_at": "t0"}
        transport.issues[index] = issue
        bindings[f"epic:tickets:{spec.initiative}:{spec.epic}"] = HierarchyCoordinator._binding(
            issue, "issue"
        )
    state.bindings["github_hierarchy"] = bindings
    state.spec = replace(state.spec, units=tuple(unit.spec for unit in state.units.values()))
    runtime = CampaignRuntime(
        tmp_path, CampaignStore(tmp_path / "state"), gateway=GithubGateway(transport, "a/b")
    )
    state = runtime.store.create(state)
    return runtime, state, transport


def submit(supervisor, state, action, *, reconcile=False, before_harvest=None):
    """Exercise persisted submission and observation without worker threads."""
    from concurrent.futures import Future

    class InlineExecutor:
        @staticmethod
        def submit(operation, *args):
            future = Future()
            try:
                future.set_result(operation(*args))
            except Exception as exc:
                future.set_exception(exc)
            return future

    state = supervisor._submit(state, action, InlineExecutor(), reconcile=reconcile)
    if before_harvest is not None:
        state = before_harvest(state)
    return supervisor._harvest(state)


def recover_parent(runtime, state):
    from bmad_loop import campaign_tracking_recovery as recovery

    deadline = max(
        row["next_retry_at"]
        for unit in state.units.values()
        for row in unit.evidence.get(recovery.KEY, {}).values()
        if row["state"] == "waiting"
    )
    runtime.clock = lambda: deadline
    supervisor = CampaignSupervisor(runtime.store, runtime, clock=runtime.clock)
    proposed = recovery.proposals(state, now=deadline)
    assert len(proposed) == 1
    state = supervisor._queue(state, proposed)
    action = next(
        row for row in state.outbox if row.kind == "tracking-reconcile" and row.state == "prepared"
    )
    state = submit(supervisor, state, action)
    action = next(row for row in state.outbox if row.kind == "github" and row.state == "prepared")
    return submit(supervisor, state, action)


def test_runtime_closes_delivered_epic_while_independent_epic_keeps_owner_hold(
    tmp_path, monkeypatch
):
    runtime, state, transport = configured(tmp_path, count=2, held=(2,))
    monkeypatch.setattr(runtime, "clock", lambda: 100)
    monkeypatch.setattr(runtime, "_role", lambda *args, **kwargs: None)
    state.bindings.update(
        inventory={"complete": True, "observed_at": 100},
        intake={"observed_at": 100, "pending_count": 0},
        capacity={"observed_at": 100},
    )
    for number, parent, closed in ((101, 1, True), (102, 2, False)):
        transport.issues[number] = {
            "id": 1000 + number,
            "number": number,
            "state": "closed" if closed else "open",
            "state_reason": "completed" if closed else None,
            "updated_at": "t0",
        }
        transport.parents[number] = parent
    state = runtime.store.save(state, state.revision)
    supervisor = CampaignSupervisor(runtime.store, runtime)
    parent_key = "epic:tickets:initiative:epic-1"
    proposed = [row for row in runtime.proposals(state) if row.kind == "hierarchy-close"]
    assert [(row.unit, row.payload) for row in proposed] == [("unit-1", {"parent_key": parent_key})]
    state = supervisor._queue(state, tuple(proposed))
    action = next(row for row in state.outbox if row.kind == "hierarchy-close")
    state = submit(supervisor, state, action)
    action = next(row for row in state.outbox if row.kind == "github")
    assert action.unit == "unit-1"
    state = submit(supervisor, state, action)
    assert transport.issues[1]["state"] == "closed"
    assert len(transport.effects) == 1
    # Re-read and project the acknowledged native close before reconsidering scope.
    state = supervisor._queue(state, closure.proposals(state))
    action = next(
        row for row in state.outbox if row.kind == "hierarchy-close" and row.state == "prepared"
    )
    state = submit(supervisor, state, action)
    assert state.bindings["github_hierarchy"][parent_key]["closed_confirmed"]
    assert state.units["unit-2"].hold.id == "owner-2"
    assert state.units["unit-2"].state == UnitState.MERGED_PENDING_SYNC
    assert transport.issues[2]["state"] == transport.issues[102]["state"] == "open"
    assert not closure.proposals(state)
    assert "hierarchy_finalized_graph" not in state.bindings


def test_alias_of_same_native_parent_retains_other_source_member_obligations(tmp_path):
    runtime, state, transport = configured(tmp_path, count=2, held=(2,))
    bindings = state.bindings["github_hierarchy"]
    bindings["epic:tickets:initiative:epic-2"] = dict(bindings["epic:tickets:initiative:epic-1"])
    state = runtime.store.save(state, state.revision)
    assert closure.proposals(state) == ()
    direct = HierarchyCoordinator(runtime.gateway(state)).close_parents(
        state, only_key="epic:tickets:initiative:epic-1"
    )
    assert direct.intent is None
    assert transport.effects == []


def test_fresh_parent_guard_observes_hold_on_a_member_but_not_an_unrelated_unit(tmp_path):
    runtime, state, transport = configured(tmp_path, count=2, held=(2,))
    state = prepare(runtime, state)
    action = next(row for row in state.outbox if row.kind == "github")
    state.units["unit-1"].hold = Hold("owner", "Retain this parent", id="new-owner-hold")
    state = runtime.store.save(state, state.revision)
    supervisor = CampaignSupervisor(runtime.store, runtime)
    state = submit(supervisor, state, action)
    canceled = next(row for row in state.outbox if row.id == action.id)
    assert canceled.state == "confirmed" and canceled.receipt["canceled_before_write"]
    assert canceled.receipt["intent"]["fault"]["certainty"] == "not_submitted"
    assert transport.effects == []
    assert runtime.store.load(state.id).units["unit-1"].hold.id == "new-owner-hold"
    state.units["unit-1"].hold = None  # Script resolution of this owner's condition.
    state = runtime.store.save(state, state.revision)
    state = recover_parent(runtime, state)
    assert transport.issues[1]["state"] == "closed" and len(transport.effects) == 1
    assert state.units["unit-2"].hold.id == "owner-2"
    assert transport.issues[2]["state"] == "open"


def test_drained_initiative_milestone_closes_while_another_initiative_is_held(
    tmp_path, monkeypatch
):
    runtime, state, transport = configured(tmp_path, count=2, held=(2,), initiatives={2: "other"})
    milestone = {"id": 9001, "number": 1, "state": "open", "updated_at": "t0"}
    transport.milestones[1] = milestone
    transport.issues[1]["milestone"] = milestone
    state.bindings["github_hierarchy"]["initiative:tickets:initiative"] = (
        HierarchyCoordinator._binding(milestone, "milestone")
    )
    original = transport.request

    def filtered(method, path, payload=None):
        if method == "GET" and path.startswith("repos/a/b/issues?state=open&milestone=1"):
            return ApiResponse(
                200,
                [
                    row
                    for row in transport.issues.values()
                    if row.get("milestone", {}).get("id") == 9001 and row["state"] == "open"
                ],
                {},
            )
        return original(method, path, payload)

    monkeypatch.setattr(transport, "request", filtered)
    state = runtime.store.save(state, state.revision)
    for _ in range(4):
        state = prepare(runtime, state)
        for action in tuple(state.outbox):
            if action.kind == "github" and action.state == "prepared":
                supervisor = CampaignSupervisor(runtime.store, runtime)
                state = submit(supervisor, state, action)
    assert milestone["state"] == "closed"
    assert transport.issues[1]["state"] == "closed" and transport.issues[2]["state"] == "open"
    assert state.units["unit-2"].hold.id == "owner-2"
    assert "hierarchy_finalized_graph" not in state.bindings
    assert [route for _, route, _ in transport.effects] == ["issues/1", "milestones/1"]


def test_legacy_aggregate_target_uses_native_parent_scope_not_unrelated_anchor(tmp_path):
    from bmad_loop import campaign_tracking_recovery as recovery
    from bmad_loop.campaign_github import MutationIntent
    from bmad_loop.campaign_store import add_action

    runtime, state, transport = configured(tmp_path, count=2, held=(2,))
    intent = MutationIntent(
        "legacy-parent",
        "close_issue",
        "PATCH",
        "repos/a/b/issues/1",
        {"state": "closed", "state_reason": "completed"},
        coordinates={"id": 1001, "updated_at": "t0", "source_confirmed": True},
    )
    action = add_action(
        state,
        "github",
        "unit-2",
        {"intent": intent.to_dict(), "purpose": "parent-tracking"},
        action_id=intent.action_id,
    )
    assert recovery.target_members(state, action, intent) == ("unit-1",)
    assert recovery.success_guard(state, action, intent) == ""
    confirmed = replace(
        intent,
        state="confirmed",
        receipt={**transport.issues[1], "state": "closed", "state_reason": "completed"},
    )
    assert recovery.success_guard(state, action, confirmed) == ""
    # A second namespace for the same native issue brings its obligations back
    # into this scope; a matching title or the action anchor never creates it.
    bindings = state.bindings["github_hierarchy"]
    bindings["epic:tickets:initiative:epic-2"] = dict(bindings["epic:tickets:initiative:epic-1"])
    assert recovery.target_members(state, action, intent) == ("unit-1", "unit-2")
    assert "holds" in recovery.success_guard(state, action, intent)
    assert state.units["unit-2"].hold.id == "owner-2"


@pytest.mark.parametrize("change", ["native-id", "repository", "parent-key"])
def test_parent_completion_rejects_unknown_native_target_scope(tmp_path, change):
    from bmad_loop import campaign_tracking_recovery as recovery
    from bmad_loop.campaign_github import MutationIntent
    from bmad_loop.campaign_store import add_action

    _, state, _ = configured(tmp_path, count=1)
    intent = MutationIntent(
        "parent",
        "close_issue",
        "PATCH",
        "repos/a/b/issues/1",
        {"state": "closed", "state_reason": "completed"},
        coordinates={"id": 1001},
    )
    action = add_action(
        state,
        "github",
        "unit-1",
        {"intent": intent.to_dict(), "purpose": "parent-tracking"},
        action_id=intent.action_id,
    )
    assert recovery.success_guard(state, action, intent) == ""
    if change == "native-id":
        intent = replace(intent, coordinates={"id": 9999})
    elif change == "repository":
        intent = replace(intent, path="repos/foreign/repository/issues/1")
    else:
        action.payload["parent_key"] = "epic:foreign:foreign:foreign"
    assert recovery.target_members(state, action, intent) == ()
    assert "unproven" in recovery.success_guard(state, action, intent)


def prepare(runtime, state):
    supervisor = CampaignSupervisor(runtime.store, runtime)
    state = supervisor._queue(state, closure.proposals(state))
    observations = [
        (row.id, runtime._perform_hierarchy_close(state.clone(), row))
        for row in state.outbox
        if row.kind == "hierarchy-close" and row.state == "prepared"
    ]
    for identifier, observation in observations:
        row = next(row for row in state.outbox if row.id == identifier)
        runtime._apply_effect(state, row, observation)
        row.state, row.receipt = "confirmed", observation.data
    return runtime.store.save(state, state.revision)


def test_parent_keys_have_independent_durable_writes_and_confirmation_fences(tmp_path):
    runtime, state, transport = configured(tmp_path)
    state = prepare(runtime, state)
    mutations = [row for row in state.outbox if row.kind == "github"]
    assert len(mutations) == 3 and len({row.payload["parent_key"] for row in mutations}) == 3
    assert transport.effects == []
    assert closure.proposals(state) == ()  # No duplicate plans while their writes are pending.
    for row in mutations:
        observed = runtime._perform_github(state, row)
        assert observed.state == "confirmed"
        runtime._apply_effect(state, row, observed)
        row.state, row.receipt = "confirmed", observed.data
    state = runtime.store.save(state, state.revision)
    state = prepare(runtime, state)
    assert all(row.get("closed_confirmed") for row in state.bindings["github_hierarchy"].values())
    assert not any(event["kind"] == "hierarchy-stale-observation" for event in state.events)
    assert "hierarchy_finalized_graph" not in state.bindings
    state = prepare(runtime, state)
    assert state.bindings["hierarchy_finalized_graph"] == state.graph_revision
    assert len(transport.effects) == 3


def test_closure_admission_bound_includes_pending_parent_scopes(tmp_path):
    runtime, state, _ = configured(tmp_path, count=40)
    chosen = closure.proposals(state)
    assert len(chosen) == closure.LIMIT
    supervisor = CampaignSupervisor(runtime.store, runtime)
    state = supervisor._queue(state, chosen)
    assert len([row for row in state.outbox if row.kind == "hierarchy-close"]) == closure.LIMIT
    assert closure.proposals(state) == ()


@pytest.mark.parametrize("change", ["graph", "native-child"])
def test_fresh_scope_and_native_dependency_block_a_prepared_parent_write(tmp_path, change):
    runtime, state, transport = configured(tmp_path, count=1)
    state = prepare(runtime, state)
    row = next(row for row in state.outbox if row.kind == "github")
    if change == "graph":
        state.graph_revision += 1
        state = runtime.store.save(state, state.revision)
    else:
        transport.issues[99] = {"id": 1099, "number": 99, "state": "open", "updated_at": "t0"}
        transport.parents[99] = 1
    supervisor = CampaignSupervisor(runtime.store, runtime)
    state = submit(supervisor, state, row)
    canceled = next(action for action in state.outbox if action.id == row.id)
    assert transport.effects == []
    assert canceled.state == "confirmed" and canceled.receipt["canceled_before_write"]
    if change == "native-child":
        assert canceled.receipt["intent"]["fault"]["certainty"] == "not_submitted"
        transport.issues[99]["state"] = "closed"
        state = recover_parent(runtime, state)
        assert transport.issues[1]["state"] == "closed" and len(transport.effects) == 1


def test_lost_parent_write_ack_reconciles_without_duplicate_mutation(tmp_path, monkeypatch):
    runtime, state, transport = configured(tmp_path, count=1)
    state = prepare(runtime, state)
    row = next(row for row in state.outbox if row.kind == "github")
    original = transport.request

    def dropped(method, path, payload=None):
        result = original(method, path, payload)
        if method == "PATCH":
            raise UncertainMutation("accepted, acknowledgement lost")
        return result

    monkeypatch.setattr(transport, "request", dropped)
    supervisor = CampaignSupervisor(runtime.store, runtime)
    state = submit(supervisor, state, row)
    row = next(row for row in state.outbox if row.kind == "github")
    assert row.state == "sent_unknown" and row.receipt["intent"]["state"] == "sent_unknown"
    assert row.receipt["intent"]["fault"]["certainty"] == "unknown"
    assert len(transport.effects) == 1
    state = submit(supervisor, state, row, reconcile=True)
    assert next(action for action in state.outbox if action.id == row.id).state == "confirmed"
    assert len(transport.effects) == 1


def test_initiative_waits_for_epic_receipts_then_closes_with_final_aggregate(tmp_path, monkeypatch):
    runtime, state, transport = configured(tmp_path)
    milestone = {"id": 9001, "number": 1, "state": "open", "updated_at": "t0"}
    transport.milestones[1] = milestone
    state.bindings["github_hierarchy"]["initiative:tickets:initiative"] = (
        HierarchyCoordinator._binding(milestone, "milestone")
    )
    original = transport.request

    def filtered(method, path, payload=None):
        if method == "GET" and path.startswith("repos/a/b/issues?"):
            return ApiResponse(
                200, [row for row in transport.issues.values() if row["state"] != "closed"], {}
            )
        return original(method, path, payload)

    monkeypatch.setattr(transport, "request", filtered)
    state = runtime.store.save(state, state.revision)
    assert len(closure.proposals(state)) == 3
    for _ in range(5):
        state = prepare(runtime, state)
        mutations = [
            row for row in state.outbox if row.kind == "github" and row.state == "prepared"
        ]
        for row in mutations:
            result = runtime._perform_github(state, row)
            assert result.state == "confirmed"
            runtime._apply_effect(state, row, result)
            row.state, row.receipt = "confirmed", result.data
        state = runtime.store.save(state, state.revision)
    assert milestone["state"] == "closed"
    assert state.bindings["hierarchy_finalized_graph"] == state.graph_revision
    assert [route for _, route, _ in transport.effects][-1] == "milestones/1"


def test_unmanaged_parent_scope_does_not_serialize_unrelated_parent_closure(tmp_path):
    runtime, state, transport = configured(tmp_path)
    transport.issues[99] = {"id": 1099, "number": 99, "state": "open", "updated_at": "t0"}
    transport.parents[99] = 1
    state = prepare(runtime, state)
    mutations = [row for row in state.outbox if row.kind == "github"]
    assert len(mutations) == 2
    assert list(state.bindings["hierarchy_deferred"]["parents"]) == [
        "epic:tickets:initiative:epic-1"
    ]
    for row in mutations:
        result = runtime._perform_github(state, row)
        runtime._apply_effect(state, row, result)
        row.state, row.receipt = "confirmed", result.data
    state = runtime.store.save(state, state.revision)
    state = prepare(runtime, state)
    state = prepare(runtime, state)
    assert state.bindings["hierarchy_finalized_graph"] == state.graph_revision
    assert "open required or unmanaged" in state.bindings["hierarchy_remaining_scope"]
    assert transport.issues[1]["state"] == "open"
    assert transport.issues[2]["state"] == transport.issues[3]["state"] == "closed"


def test_parent_closure_resumes_after_same_graph_temporary_intake_wait(tmp_path):
    runtime, state, transport = configured(tmp_path, count=1)
    state = prepare(runtime, state)
    state.bindings["intake"] = {"pending_count": 1}
    state = runtime.store.save(state, state.revision)
    action = next(row for row in state.outbox if row.kind == "github")
    original_graph = state.graph_revision
    supervisor = CampaignSupervisor(runtime.store, runtime)
    state = submit(supervisor, state, action)
    canceled = next(row for row in state.outbox if row.id == action.id)
    assert canceled.state == "confirmed" and canceled.receipt["canceled_before_write"]
    assert canceled.receipt["intent"]["fault"]["certainty"] == "not_submitted"
    assert transport.effects == []
    state.bindings["intake"]["pending_count"] = 0
    state = runtime.store.save(state, state.revision)
    state = recover_parent(runtime, state)
    for _ in range(2):
        state = prepare(runtime, state)
    assert transport.issues[1]["state"] == "closed" and len(transport.effects) == 1
    assert state.graph_revision == original_graph
    assert state.bindings["hierarchy_finalized_graph"] == state.graph_revision
