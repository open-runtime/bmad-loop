"""A queued PR's external head is removal identity, never accepted evidence."""

import copy
from dataclasses import asdict, replace
from types import SimpleNamespace

import pytest
import test_campaign_remote_head

from bmad_loop import campaign_runtime_queue as flow
from bmad_loop.campaign_github import ApiResponse, GithubError, GithubGateway, MutationIntent
from bmad_loop.campaign_model import Hold, UnitState
from bmad_loop.campaign_remote_head import RemoteHeadPending
from bmad_loop.campaign_runtime import CampaignRuntime

remote_case = test_campaign_remote_head.case


class Transport:
    def __init__(self, case):
        self.case = case
        self.entry = {"id": "ENTRY", "state": "AWAITING_CHECKS"}
        self.member_changes = {}
        self.mutations = []
        self.drop = False

    def request(self, method, path, payload=None):
        pr = self.case.pr
        if method == "GET":
            return ApiResponse(200, copy.deepcopy(pr), {})
        assert payload is not None
        member = {
            "id": pr["node_id"],
            "number": pr["number"],
            "headRefOid": pr["head"]["sha"],
            "baseRefName": pr["base"]["ref"],
            "merged": pr["merged"],
            "state": pr["state"].upper(),
            "mergeQueueEntry": self.entry,
            **self.member_changes,
        }
        if "dequeuePullRequest" in payload["query"]:
            from bmad_loop.campaign_github import UncertainMutation

            self.mutations.append(copy.deepcopy(payload))
            self.entry = None
            if self.drop:
                raise UncertainMutation("lost acknowledgement")
            member["mergeQueueEntry"] = None
            return ApiResponse(200, {"data": {"dequeuePullRequest": {"pullRequest": member}}}, {})
        return ApiResponse(200, {"data": {"repository": {"pullRequest": member}}}, {})


@pytest.fixture
def case(remote_case):
    c = remote_case
    c.snapshot.generation = 1
    c.snapshot.spec = replace(
        c.snapshot.spec, github=replace(c.snapshot.spec.github, merge_mode="queue")
    )
    c.unit.state = UnitState.MERGE_REQUESTED
    c.unit.evidence.update(
        integrated={"head": "a" * 40, "base": "c" * 40},
        verification={"accepted": "original"},
        review={"accepted": "original"},
        native_review_pair={"accepted": "original"},
    )
    c.candidate = CampaignRuntime._candidate(c.runtime, c.snapshot, c.unit)
    c.unit.evidence["queue"] = {"candidate": asdict(c.candidate), "observed_at": 100}
    c.transport = Transport(c)
    c.gateway = GithubGateway(c.transport, c.snapshot.repository)
    c.runtime.gateway = lambda _: c.gateway
    c.runtime.clock = lambda: 100
    c.runtime._candidate = lambda snapshot, unit: CampaignRuntime._candidate(
        c.runtime, snapshot, unit
    )
    c.action = SimpleNamespace(
        id="remove", kind="queue-observe", unit=c.unit.id, payload={}, generation=1
    )
    c.runtime._queue_intent = lambda *args: None
    return c


def observe_and_apply(case):
    result = flow.observe_effect(case.runtime, case.snapshot, case.action)
    flow.apply_effect(case.runtime, case.snapshot, case.unit, case.action, result.data)
    return result


def prepare(case):
    case.action.kind = "queue-dequeue"
    result = flow.dequeue_effect(case.runtime, case.snapshot, case.action)
    return MutationIntent.from_dict(result.data["intent"])


def test_changed_queued_head_requires_removal_before_integration(case):
    result = observe_and_apply(case)
    assert result.state == "confirmed" and result.data["dequeued"] is False
    assert case.unit.state == UnitState.MERGE_REQUESTED
    assert case.unit.evidence["integrated"]["head"] == case.candidate.head
    assert case.unit.evidence["review"] == {"accepted": "original"}
    assert "remote_head_sync" not in case.unit.evidence
    state = case.unit.evidence["queue"]
    assert state["candidate"] == asdict(case.candidate)
    assert state["removal_candidate"]["head"] == case.pr["head"]["sha"]
    assert [
        p.kind for p in flow.proposals(case.runtime, case.snapshot, case.unit, lambda _: True)
    ] == ["queue-dequeue"]
    intent = prepare(case)
    assert intent.coordinates is not None
    assert intent.coordinates["accepted_candidate"] == asdict(case.candidate)
    assert intent.coordinates["candidate"]["head"] == case.pr["head"]["sha"]
    assert case.transport.mutations == []

    submitted = case.gateway.execute(replace(intent, state="sent_unknown"), generation=1)
    assert submitted.state == "confirmed"
    result = observe_and_apply(case)
    assert result.data["dequeued"] is True
    assert case.unit.state == UnitState.SYNCING
    assert case.unit.evidence["remote_head_sync"]["head"] == case.pr["head"]["sha"]
    assert all(
        key not in case.unit.evidence
        for key in ("integrated", "review", "verification", "native_review_pair")
    )
    assert state["candidate"] == asdict(case.candidate)
    assert state["dequeue_confirmed"]["head"] == case.pr["head"]["sha"]


@pytest.mark.parametrize("new_entry", [False, True])
def test_lost_dequeue_ack_after_another_bot_commit_never_replays(case, new_entry):
    observe_and_apply(case)
    intent = prepare(case)
    case.transport.drop = True
    submitted = case.gateway.execute(replace(intent, state="sent_unknown"), generation=1)
    assert submitted.state == "sent_unknown"
    case.pr["head"]["sha"] = "d" * 40
    case.ancestor.add(("a" * 40, "d" * 40))
    case.ancestor.add(("b" * 40, "d" * 40))
    if new_entry:
        case.transport.entry = {"id": "NEW_ENTRY", "state": "AWAITING_CHECKS"}
    reconciled = case.gateway.reconcile_update(submitted, allow_retry=True, generation=1)
    assert len(case.transport.mutations) == 1
    assert reconciled.state == ("sent_unknown" if new_entry else "confirmed")
    assert reconciled.coordinates == submitted.coordinates
    if not new_entry:
        result = observe_and_apply(case)
        assert result.data["dequeued"] and case.unit.state == UnitState.SYNCING
        assert case.unit.evidence["remote_head_sync"]["head"] == "d" * 40
    else:
        assert case.unit.state == UnitState.MERGE_REQUESTED
        assert "remote_head_sync" not in case.unit.evidence


@pytest.mark.parametrize("fault", ["node", "branch", "repository", "target"])
def test_unknown_dequeue_cannot_use_another_pr_identity(case, fault):
    intent = replace(prepare(case), state="sent_unknown")
    case.transport.entry = None
    if fault == "node":
        case.pr["node_id"] = "OTHER"
    elif fault == "branch":
        case.pr["head"]["ref"] = "OTHER"
    elif fault == "repository":
        case.pr["head"]["repo"]["full_name"] = "other/repo"
    else:
        case.pr["base"]["ref"] = "OTHER"
    with pytest.raises(GithubError, match="lost owned"):
        case.gateway.reconcile_update(intent, allow_retry=True, generation=1)
    assert case.transport.mutations == []


def test_changed_head_observation_preserves_operator_hold_and_live_claim(case):
    case.transport.entry = None
    case.unit.hold = Hold("operator", "owner pause", id="owner")
    case.unit.claim.quiescent = False
    observe_and_apply(case)
    assert case.unit.state == UnitState.MERGE_REQUESTED
    assert "integrated" in case.unit.evidence
    case.unit.claim.quiescent = True
    observe_and_apply(case)
    assert case.unit.state == UnitState.SYNCING
    assert case.unit.hold.id == "owner"


def test_membership_head_race_does_not_authorize_integration(case):
    case.transport.entry = None
    case.transport.member_changes["headRefOid"] = "e" * 40
    with pytest.raises(GithubError, match="PR/head/target changed"):
        observe_and_apply(case)
    assert case.unit.state == UnitState.MERGE_REQUESTED
    assert case.unit.evidence["integrated"]["head"] == case.candidate.head


def test_git_and_native_head_disagreement_remains_unknown(case):
    case.pusher.remote_head = lambda: "e" * 40
    with pytest.raises(RemoteHeadPending, match="disagree"):
        observe_and_apply(case)
    assert case.unit.state == UnitState.MERGE_REQUESTED
    assert "integrated" in case.unit.evidence


def test_foreign_changed_head_becomes_visible_hold_without_queue_write(case):
    case.pr["head"]["ref"] = "OTHER"
    result = observe_and_apply(case)
    assert "remote_head_error" in result.data
    assert case.unit.hold.kind == "remote_head_error"
    assert "integrated" in case.unit.evidence
    assert case.transport.mutations == []


def test_observed_remote_contribution_cannot_disappear_into_old_acceptance(case):
    observe_and_apply(case)
    case.pr["head"]["sha"] = case.candidate.head
    case.transport.entry = None
    result = observe_and_apply(case)
    assert "disappeared" in result.data["remote_head_error"]
    assert case.unit.hold.kind == "remote_head_error"
    assert case.unit.state == UnitState.MERGE_REQUESTED
    assert case.unit.evidence["integrated"]["head"] == case.candidate.head


def test_stale_queue_removal_readback_cannot_release_changed_acceptance(case):
    case.transport.entry = None
    result = flow.observe_effect(case.runtime, case.snapshot, case.action)
    case.snapshot.graph_revision += 1
    flow.apply_effect(case.runtime, case.snapshot, case.unit, case.action, result.data)
    assert case.unit.state == UnitState.MERGE_REQUESTED
    assert "remote_head_sync" not in case.unit.evidence
    assert case.snapshot.events[-1]["kind"] == "queue-removal-readback-stale"


@pytest.mark.parametrize("merged", [False, True])
def test_lost_removal_ack_terminal_external_head_is_visible_not_delivered(case, merged):
    observe_and_apply(case)
    intent = prepare(case)
    case.transport.drop = True
    submitted = case.gateway.execute(replace(intent, state="sent_unknown"), generation=1)
    case.pr.update(state="closed", merged=merged)
    case.pr["head"]["sha"] = "d" * 40
    reconciled = case.gateway.reconcile_update(submitted, allow_retry=True, generation=1)
    assert reconciled.state == "confirmed"
    assert len(case.transport.mutations) == 1
    observe_and_apply(case)
    assert case.unit.state == UnitState.MERGE_REQUESTED
    assert case.unit.hold.kind == "external_wait"
    assert case.unit.evidence["queue"]["external_head_terminal"] == {
        "candidate": asdict(case.candidate),
        "head": "d" * 40,
        "merged": merged,
    }
    assert case.unit.evidence["integrated"]["head"] == case.candidate.head
    assert "merge" not in case.unit.evidence


def test_terminal_external_observation_cannot_replace_operator_hold(case):
    case.pr.update(state="closed", merged=True)
    case.unit.hold = Hold("operator", "pause", id="owner")
    observe_and_apply(case)
    assert case.unit.hold.id == "owner"


@pytest.mark.parametrize("storage", ["remote_head_observation", "remote_head_pending"])
@pytest.mark.parametrize("removed", [False, True])
def test_queue_rewrite_cannot_drop_previously_observed_remote_contribution(case, storage, removed):
    observe_and_apply(case)
    state = case.unit.evidence["queue"]
    previous = copy.deepcopy(state.pop("remote_head_observation"))
    state[storage] = previous
    case.pr["head"]["sha"] = "d" * 40
    # R2 still descends from the owned checkpoint, but does not retain R1.
    case.ancestor.add(("a" * 40, "d" * 40))
    if removed:
        case.transport.entry = None
    result = observe_and_apply(case)
    assert "previously observed" in result.data["remote_head_error"]
    assert case.unit.hold.kind == "remote_head_error"
    assert state[storage] == previous
    assert case.unit.state == UnitState.MERGE_REQUESTED
    assert case.unit.evidence["integrated"]["head"] == case.candidate.head
