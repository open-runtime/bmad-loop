"""Parent scopes close independently while writes retain durable uncertainty."""

from dataclasses import replace

import pytest
from test_campaign_tracking import HierarchyTransport, snapshot

from bmad_loop import campaign_parent_closure as closure
from bmad_loop.campaign_github import ApiResponse, GithubGateway, UncertainMutation
from bmad_loop.campaign_model import CampaignUnit, UnitState
from bmad_loop.campaign_runtime import CampaignRuntime
from bmad_loop.campaign_store import CampaignStore
from bmad_loop.campaign_supervisor import CampaignSupervisor
from bmad_loop.campaign_tracking import HierarchyCoordinator


def configured(tmp_path, count=3):
    state = snapshot()
    template = next(iter(state.units.values())).spec
    state.units = {}
    transport = HierarchyTransport()
    bindings = {}
    for index in range(1, count + 1):
        spec = replace(template, id=f"unit-{index}", epic=f"epic-{index}")
        state.units[spec.id] = CampaignUnit(spec, UnitState.DELIVERED)
        issue = {"id": 1000 + index, "number": index, "state": "open", "updated_at": "t0"}
        transport.issues[index] = issue
        bindings[f"epic:tickets:initiative:{spec.epic}"] = HierarchyCoordinator._binding(
            issue, "issue"
        )
    state.bindings["github_hierarchy"] = bindings
    state.spec = replace(state.spec, units=tuple(unit.spec for unit in state.units.values()))
    runtime = CampaignRuntime(
        tmp_path, CampaignStore(tmp_path / "state"), gateway=GithubGateway(transport, "a/b")
    )
    state = runtime.store.create(state)
    return runtime, state, transport


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
        runtime.store.save(state, state.revision)
    else:
        transport.issues[99] = {"id": 1099, "number": 99, "state": "open", "updated_at": "t0"}
        transport.parents[99] = 1
    observed = runtime._perform_github(state, row)
    assert transport.effects == []
    assert observed.data.get("canceled_before_write") or observed.state == "waiting"
    if change == "native-child":
        transport.issues[99]["state"] = "closed"
        resumed = runtime.reconcile_action(state, row)
        assert resumed.state == "confirmed" and len(transport.effects) == 1


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
    uncertain = runtime._perform_github(state, row)
    assert uncertain.state == "unknown" and len(transport.effects) == 1
    row.state, row.receipt = "sent_unknown", uncertain.data
    state = runtime.store.save(state, state.revision)
    row = next(row for row in state.outbox if row.kind == "github")
    observed = runtime.reconcile_action(state, row)
    assert observed.state == "confirmed" and len(transport.effects) == 1


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
    result = runtime._perform_github(state, action)
    assert result.state == "waiting"
    action.state, action.receipt = "sent_unknown", result.data
    state = runtime.store.save(state, state.revision)
    state.bindings["intake"]["pending_count"] = 0
    state = runtime.store.save(state, state.revision)
    for _ in range(3):
        state = prepare(runtime, state)
        for action in state.outbox:
            if action.kind == "github" and action.state in {"prepared", "sent_unknown"}:
                result = runtime.reconcile_action(state, action)
                assert result.state == "confirmed"
                runtime._apply_effect(state, action, result)
                action.state, action.receipt = "confirmed", result.data
        state = runtime.store.save(state, state.revision)
    assert transport.issues[1]["state"] == "closed" and len(transport.effects) == 1
    assert state.bindings["hierarchy_finalized_graph"] == state.graph_revision
