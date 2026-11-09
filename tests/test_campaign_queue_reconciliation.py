"""Queue state changes between independent reads cannot strand a prepared dequeue."""

from dataclasses import asdict
from types import SimpleNamespace

import pytest
from conftest import git
from test_campaign_queue import Transport
from test_campaign_queue import group as group

from bmad_loop import campaign_runtime_queue as flow
from bmad_loop.campaign_github import GithubError, GithubGateway
from bmad_loop.campaign_model import (
    CampaignSnapshot,
    CampaignSpec,
    CampaignUnit,
    GitHubSpec,
    UnitSpec,
    UnitState,
)
from bmad_loop.campaign_publication import (
    PublicationObservation,
    PublicationRequirements,
    RemoteRules,
)
from bmad_loop.campaign_runtime import CampaignRuntime
from bmad_loop.campaign_store import CampaignStore


class TerminalTransport(Transport):
    def __init__(self, queued):
        super().__init__(queued)
        self.merged = False
        self.closed = False
        self.merge_commit = queued.head
        self.pr_changes = {}

    def request(self, method, path, payload=None):
        result = super().request(method, path, payload)
        if method == "GET" and "/pulls/" in path:
            pr = result.data
            pr.update(
                merged=self.merged,
                state="closed" if self.merged or self.closed else "open",
                merge_commit_sha=self.merge_commit,
            )
            pr.update(self.pr_changes)
        elif path == "graphql" and "dequeuePullRequest" not in payload["query"]:
            pr = result.data["data"]["repository"]["pullRequest"]
            pr.update(merged=self.merged, state="CLOSED" if self.merged or self.closed else "OPEN")
            if self.merged or self.closed:
                pr["mergeQueueEntry"] = None
        return result


@pytest.fixture
def case(project, tmp_path, group, monkeypatch):
    candidate, queued = group
    transport = TerminalTransport(queued)
    gateway = GithubGateway(transport, "a/b")
    spec = UnitSpec(candidate.unit_id, "tickets", "e", "Unit", scopes=("feature.py",))
    selected = CampaignSpec(
        "Queue",
        units=(spec,),
        remote="queue-test",
        github=GitHubSpec(repository="a/b", merge_mode="queue"),
    )
    unit = CampaignUnit(
        spec,
        UnitState.MERGE_REQUESTED,
        bindings={
            "workspace": str(project.project),
            "branch": "unit-feature",
            "head": candidate.head,
            "source_revision": candidate.source_revision,
            "pr": {"number": 1, "node_id": "PR_1"},
        },
        evidence={
            "integrated": {"head": candidate.head, "base": candidate.base},
            "queue": {
                "group": asdict(queued),
                "group_digest": queued.digest,
                "needs_dequeue": True,
                "gate_reasons": ["late hold"],
                "observed_at": 100,
            },
        },
    )
    snapshot = CampaignSnapshot("queue-race", "a/b", selected, {unit.id: unit})
    runtime = CampaignRuntime(
        project.project, CampaignStore(tmp_path / "state"), gateway=gateway, clock=lambda: 100
    )
    candidate = runtime._candidate(snapshot, unit)
    rules = RemoteRules((), False, True, 0, False, False, "rules", queue_merge_method="MERGE")
    monkeypatch.setattr(runtime, "_publication_evidence", lambda *args: None)
    action = SimpleNamespace(
        id="durable-dequeue", kind="queue-dequeue", unit=unit.id, payload={}, generation=1
    )

    def land():
        git(tmp_path / "remote.git", "update-ref", "refs/heads/main", queued.head)
        transport.merged = True

    return SimpleNamespace(
        flow=flow,
        runtime=runtime,
        snapshot=snapshot,
        unit=unit,
        transport=transport,
        gateway=gateway,
        candidate=candidate,
        queued=queued,
        rules=rules,
        action=action,
        land=land,
        remote=tmp_path / "remote.git",
    )


def assert_no_dequeue(case):
    assert not any(
        path == "graphql" and "dequeuePullRequest" in (payload or {}).get("query", "")
        for _, path, payload in case.transport.calls
    )


def test_late_merge_after_membership_enters_canonical_tracking_path(case, monkeypatch):
    # First REST and membership reads are open, then the check/queue lands before
    # the controller's later inspection, exactly as in the full regression.
    def group_lands(*args, **kwargs):
        case.land()
        return None

    monkeypatch.setattr(case.flow, "observe_group", group_lands)

    def observation():
        return PublicationObservation(
            "a/b", case.gateway.pull(1), case.queued.base, case.rules, (), (), (), (), {}, 100
        )

    controller = SimpleNamespace(
        inspect=lambda _: observation(),
        evaluate=lambda *args, **kwargs: SimpleNamespace(
            reasons=("Already merged; reconcile tracking instead of resubmitting",)
        ),
        requirements=PublicationRequirements(),
    )
    monkeypatch.setattr(case.flow, "PublicationController", lambda *args, **kwargs: controller)
    result = case.flow.observe_effect(case.runtime, case.snapshot, case.action)
    assert result.state == "confirmed" and result.data.get("merged"), result
    case.flow.apply_effect(case.runtime, case.snapshot, case.unit, case.action, result.data)
    assert case.unit.state == UnitState.MERGED_PENDING_SYNC
    assert case.unit.evidence["merge"]["merge_commit"] == case.queued.head
    assert case.unit.evidence["merge"]["canonical_head"] == case.queued.head
    assert case.unit.bindings["head"] == case.candidate.head
    assert_no_dequeue(case)


def test_durable_dequeue_reconciles_already_merged_instead_of_replaying(case):
    case.land()
    result = case.flow.dequeue_effect(case.runtime, case.snapshot, case.action)
    assert result.state == "confirmed" and result.data.get("merged"), result
    case.flow.apply_effect(case.runtime, case.snapshot, case.unit, case.action, result.data)
    assert case.unit.state == UnitState.MERGED_PENDING_SYNC
    assert case.unit.hold is None and "integrated" in case.unit.evidence
    assert_no_dequeue(case)


def test_durable_dequeue_reads_back_already_removed_before_authorizing_repair(case):
    case.transport.queued = False
    result = case.flow.dequeue_effect(case.runtime, case.snapshot, case.action)
    assert result.state == "confirmed" and result.data.get("dequeued"), result
    assert result.data["candidate"] == asdict(case.candidate)
    case.flow.apply_effect(case.runtime, case.snapshot, case.unit, case.action, result.data)
    assert case.unit.state == UnitState.SYNCING and case.unit.hold.kind == "remote_wait"
    assert case.unit.hold.evidence["dequeue_confirmed"] is True
    assert_no_dequeue(case)


def test_closed_without_merge_is_external_hold_not_delivery_or_repair(case):
    case.transport.closed = True
    result = case.flow.dequeue_effect(case.runtime, case.snapshot, case.action)
    case.flow.apply_effect(case.runtime, case.snapshot, case.unit, case.action, result.data)
    assert case.unit.state == UnitState.MERGE_REQUESTED
    assert case.unit.hold.kind == "external_wait"
    assert "integrated" in case.unit.evidence and "merge" not in case.unit.evidence
    assert_no_dequeue(case)


@pytest.mark.parametrize("fault", ["node", "head", "target", "number", "unreachable-merge"])
def test_terminal_readback_cannot_release_another_identity_or_unlanded_commit(case, fault):
    case.land()
    if fault == "node":
        case.transport.pr_changes["node_id"] = "OTHER"
    elif fault == "head":
        case.transport.pr_changes["head"] = {"sha": "f" * 40}
    elif fault == "target":
        case.transport.pr_changes["base"] = {"ref": "other"}
    elif fault == "number":
        case.transport.pr_changes["number"] = 2
    else:
        case.transport.merge_commit = "f" * 40
    with pytest.raises(GithubError):
        case.flow.dequeue_effect(case.runtime, case.snapshot, case.action)
    assert case.unit.state == UnitState.MERGE_REQUESTED and "merge" not in case.unit.evidence
    assert_no_dequeue(case)


def test_reported_merge_must_preserve_accepted_head(case):
    case.land()
    git(case.remote, "update-ref", "refs/heads/main", case.candidate.base)
    case.transport.merge_commit = case.candidate.base
    result = case.flow.dequeue_effect(case.runtime, case.snapshot, case.action)
    assert (
        result.state == "rejected" and result.reason == "merged history omits the accepted H head"
    )
    assert case.unit.state == UnitState.MERGE_REQUESTED and "merge" not in case.unit.evidence
    assert_no_dequeue(case)


def test_reported_merge_must_preserve_published_checkpoints(case, monkeypatch):
    case.land()
    # An actual detached commit exists, but it was not included in this landing.
    tree = git(case.remote, "rev-parse", case.candidate.base + "^{tree}")
    checkpoint = git(
        case.remote,
        "-c",
        "user.name=Checkpoint",
        "-c",
        "user.email=checkpoint@example.test",
        "commit-tree",
        tree,
        "-p",
        case.candidate.base,
        "-m",
        "published checkpoint",
    )
    monkeypatch.setattr(
        case.runtime,
        "_pusher",
        lambda *args: SimpleNamespace(
            observations=lambda: [SimpleNamespace(state="confirmed", head=checkpoint)]
        ),
    )
    result = case.flow.dequeue_effect(case.runtime, case.snapshot, case.action)
    assert (
        result.state == "rejected" and result.reason == "merged history omits a campaign checkpoint"
    )
    assert case.unit.state == UnitState.MERGE_REQUESTED and "merge" not in case.unit.evidence
    assert_no_dequeue(case)


def test_removed_membership_must_match_owned_native_pr(case):
    case.transport.queued = False
    case.unit.bindings["pr"]["node_id"] = "DIFFERENT"
    with pytest.raises(GithubError, match="native identity changed"):
        case.flow.dequeue_effect(case.runtime, case.snapshot, case.action)
    assert case.unit.state == UnitState.MERGE_REQUESTED and case.unit.hold is None
    assert_no_dequeue(case)
