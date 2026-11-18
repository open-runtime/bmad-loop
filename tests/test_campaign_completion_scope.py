"""Native parent scope joins never discard another member's obligations."""

from dataclasses import replace

import pytest
from test_campaign_tracking import snapshot

from bmad_loop.campaign_completion_scope import parent_keys, parent_members, parent_wait_reason
from bmad_loop.campaign_model import CampaignUnit, Hold, UnitState


def scoped():
    state = snapshot()
    unit = next(iter(state.units.values()))
    unit.state = UnitState.DELIVERED
    state.bindings["github_hierarchy"] = {
        "epic:tickets:initiative:epic": {"kind": "issue", "database_id": 1001, "number": 1}
    }
    return state, unit, "epic:tickets:initiative:epic"


def test_native_alias_joins_members_and_declared_descendants_across_namespaces():
    state, unit, key = scoped()
    alias = "epic:second:other:other"
    state.bindings["github_hierarchy"][alias] = dict(state.bindings["github_hierarchy"][key])
    peer = replace(unit.spec, id="peer", source="second", initiative="other", epic="other")
    child = replace(unit.spec, id="split-child", source="third", epic="third", parent=unit.id)
    state.units[peer.id] = CampaignUnit(peer, UnitState.DELIVERED)
    state.units[child.id] = CampaignUnit(child, UnitState.READY)
    assert set(parent_keys(state, key)) == {key, alias}
    assert set(parent_members(state, key)) == {unit.id, peer.id, child.id}
    assert "split-child" in parent_wait_reason(state, key)
    state.units[child.id].state = UnitState.DELIVERED
    assert parent_wait_reason(state, key) == ""


def test_matching_title_is_not_native_parent_alias_authority():
    state, unit, key = scoped()
    state.bindings["github_hierarchy"][key]["title"] = "Same title"
    alias = "epic:second:other:other"
    state.bindings["github_hierarchy"][alias] = {
        "kind": "issue",
        "database_id": 1002,
        "number": 2,
        "title": "Same title",
    }
    peer = replace(unit.spec, id="peer", source="second", initiative="other", epic="other")
    state.units[peer.id] = CampaignUnit(peer, UnitState.READY)
    assert parent_keys(state, key) == (key,)
    assert parent_wait_reason(state, key) == ""


def test_delivered_member_owner_hold_retains_its_parent():
    state, unit, key = scoped()
    unit.hold = Hold("owner", "Retain acceptance obligation", id="owner-hold")
    assert "member hold" in parent_wait_reason(state, key)
    assert unit.hold.id == "owner-hold"


def test_unknown_parent_namespace_does_not_mean_empty_complete_scope():
    state, _, _ = scoped()
    assert parent_wait_reason(state, "epic:unknown:unknown:unknown")


@pytest.mark.parametrize(
    "field",
    [
        "unresolved_approval_requirements",
        "pending_failure_hold",
        "pending_failure_holds",
        "operator_prior_hold",
    ],
)
def test_delivered_member_retains_unpromoted_obligations(field):
    state, unit, key = scoped()
    unit.evidence[field] = [{"id": "unresolved-obligation"}]
    assert "pending member obligations" in parent_wait_reason(state, key)
    assert unit.evidence[field] == [{"id": "unresolved-obligation"}]
