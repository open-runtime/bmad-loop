"""Concurrent normal hierarchy effects preserve unrelated confirmed bindings."""

from dataclasses import replace

from test_campaign_tracking import HierarchyTransport, snapshot

from bmad_loop.campaign_github import GithubGateway
from bmad_loop.campaign_model import CampaignUnit, OutboxAction
from bmad_loop.campaign_runtime import CampaignRuntime
from bmad_loop.campaign_store import CampaignStore
from bmad_loop.campaign_tracking import HierarchyCoordinator, TrackingStep
from bmad_loop.campaign_tracking_delta import apply, observation


def configured(tmp_path):
    state = snapshot()
    first = next(iter(state.units.values()))
    second = CampaignUnit(replace(first.spec, id=first.id + "-second"))
    state.units[second.id] = second
    transport = HierarchyTransport()
    milestone = {"id": 9001, "number": 1, "state": "open"}
    transport.milestones[1] = milestone
    for number in (1, 2, 3):
        transport.issues[number] = {
            "id": 1000 + number,
            "number": number,
            "state": "open",
            "milestone": milestone,
        }
    binding = HierarchyCoordinator._binding
    state.bindings["github_hierarchy"] = {
        "initiative:tickets:initiative": binding(milestone, "milestone"),
        "epic:tickets:initiative:epic": {
            **binding(transport.issues[1], "issue"),
            "milestone_confirmed": True,
        },
        "story:" + first.id: binding(transport.issues[2], "issue"),
        "story:" + second.id: binding(transport.issues[3], "issue"),
    }
    runtime = CampaignRuntime(
        tmp_path, CampaignStore(tmp_path / "state"), gateway=GithubGateway(transport, "a/b")
    )
    return runtime, state, first, second


def test_concurrent_runtime_hierarchy_observations_preserve_each_others_confirmations(tmp_path):
    runtime, state, first, second = configured(tmp_path)
    one = OutboxAction("one", "hierarchy", first.id, {}, state.generation)
    two = OutboxAction("two", "hierarchy", second.id, {}, state.generation)
    observed_one = runtime._perform_hierarchy(state.clone(), one)
    observed_two = runtime._perform_hierarchy(state.clone(), two)
    first.bindings["head"] = "a-newer-unrelated-observation"
    runtime._apply_effect(state, one, observed_one)
    runtime._apply_effect(state, two, observed_two)
    hierarchy = state.bindings["github_hierarchy"]
    assert hierarchy["story:" + first.id]["milestone_confirmed"]
    assert hierarchy["story:" + second.id]["milestone_confirmed"]
    assert first.bindings["head"] == "a-newer-unrelated-observation"
    assert len(observed_one.data["binding_changes"]) == 1
    assert "bindings" not in observed_one.data


def test_new_owner_closure_discards_stale_observation_without_partial_flag_changes(tmp_path):
    runtime, state, first, _ = configured(tmp_path)
    action = OutboxAction("one", "hierarchy", first.id, {}, state.generation)
    observed = runtime._perform_hierarchy(state.clone(), action)
    state.bindings["github_hierarchy"]["initiative:tickets:initiative"]["state"] = "closed"
    runtime._apply_effect(state, action, observed)
    assert state.bindings["github_hierarchy"]["initiative:tickets:initiative"]["state"] == "closed"
    assert not state.bindings["github_hierarchy"]["story:" + first.id].get("milestone_confirmed")
    assert state.events[-1]["kind"] == "hierarchy-stale-observation"


def test_fresh_reopening_removes_old_closed_confirmation_but_preserves_concurrent_links(tmp_path):
    _, state, first, second = configured(tmp_path)
    key = "initiative:tickets:initiative"
    state.bindings["github_hierarchy"][key].update(state="closed", closed_confirmed=True)
    after = state.clone().bindings["github_hierarchy"]
    after[key]["state"] = "open"
    after[key].pop("closed_confirmed")
    value = observation(state, TrackingStep("", None, after, dict(first.bindings)), first.id)
    state.bindings["github_hierarchy"]["story:" + second.id]["parent_confirmed"] = True
    assert apply(state, value, first.id)
    assert "closed_confirmed" not in state.bindings["github_hierarchy"][key]
    assert state.bindings["github_hierarchy"]["story:" + second.id]["parent_confirmed"]
