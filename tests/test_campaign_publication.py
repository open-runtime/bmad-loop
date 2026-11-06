"""Every publication gate is tested against an otherwise publishable candidate."""

from __future__ import annotations

import copy
import subprocess
from dataclasses import replace

import pytest

from bmad_loop.campaign_github import ApiResponse, GithubError, GithubGateway
from bmad_loop.campaign_publication import (
    Candidate,
    MergeReceipt,
    PublicationController,
    PublicationEvidence,
    PublicationObservation,
    RemoteRules,
    RequiredCheck,
    read_rules,
)


class FakeTransport:
    def __init__(self):
        self.responses = {}
        self.calls = []

    def request(self, method, path, payload=None):
        self.calls.append((method, path, payload))
        response = self.responses[method, path]
        if isinstance(response, Exception):
            raise response
        return response


@pytest.fixture
def publishable():
    candidate = Candidate("a/b", "unit", 1, "h" * 40, "b" * 40, "s1", 1, "config1")
    evidence = PublicationEvidence(
        candidate,
        True,
        True,
        True,
        True,
        "openai",
        "anthropic",
        observed_model="fable",
        verification_receipts=("verify.json",),
        review_receipt="review.json",
    )
    rules = RemoteRules((RequiredCheck("CI", 7),), True, False, 1, True, True, "rules1")
    pr = {
        "number": 1,
        "head": {"sha": candidate.head},
        "base": {"ref": "main", "repo": {"full_name": "a/b"}},
        "state": "open",
        "draft": False,
        "merged": False,
        "mergeable": True,
        "mergeable_state": "clean",
        "user": {"login": "author"},
    }
    observation = PublicationObservation(
        "a/b",
        pr,
        candidate.base,
        rules,
        (
            {
                "id": 1,
                "name": "CI",
                "app": {"id": 7},
                "head_sha": candidate.head,
                "status": "completed",
                "conclusion": "success",
            },
        ),
        (),
        (
            {
                "id": 1,
                "user": {"login": "reviewer"},
                "state": "APPROVED",
                "commit_id": candidate.head,
            },
        ),
        (),
        {"labels": []},
        100,
        "APPROVED",
    )
    controller = PublicationController(GithubGateway(FakeTransport(), "a/b"), clock=lambda: 100)
    assert controller.evaluate(candidate, evidence, observation, independent_families=True).ready
    return controller, candidate, evidence, observation


@pytest.mark.parametrize(
    "change,expected",
    [
        ("head", "PR head changed"),
        ("base", "Canonical target moved"),
        ("age", "expired"),
        ("repo", "another repository"),
        ("dirty", "quiescent"),
        ("writer", "quiescent"),
        ("verification", "verification is missing"),
        ("review", "publication review is missing"),
        ("family", "provider-family"),
        ("finding", "findings remain"),
        ("hold", "active hold"),
        ("draft", "ready for review"),
        ("remotehold", "Remote owner"),
        ("rules", "up-to-date"),
        ("check", "pending/failed"),
        ("checkapp", "Required check missing"),
        ("approval", "current-head"),
        ("eligibility", "eligible approval"),
        ("thread", "Unresolved review"),
        ("dismissed", "current-head"),
    ],
)
def test_single_missing_gate_blocks_otherwise_valid_candidate(publishable, change, expected):
    controller, candidate, evidence, observation = publishable
    pr = copy.deepcopy(observation.pr)
    if change == "head":
        pr["head"]["sha"] = "changed"
    elif change == "base":
        observation = replace(observation, base_head="moved")
    elif change == "age":
        observation = replace(observation, observed_at=60)
    elif change == "repo":
        pr["base"]["repo"]["full_name"] = "foreign/repo"
    elif change == "dirty":
        evidence = replace(evidence, clean=False)
    elif change == "writer":
        evidence = replace(evidence, writer_quiescent=False)
    elif change == "verification":
        evidence = replace(evidence, verified=False)
    elif change == "review":
        evidence = replace(evidence, review_approved=False)
    elif change == "family":
        evidence = replace(evidence, reviewer_family="openai")
    elif change == "finding":
        evidence = replace(evidence, required_findings=("P1",))
    elif change == "hold":
        evidence = replace(evidence, held=True)
    elif change == "draft":
        pr["draft"] = True
    elif change == "remotehold":
        observation = replace(observation, issue={"labels": [{"name": "owner-hold"}]})
    elif change == "rules":
        observation = replace(observation, rules=replace(observation.rules, strict=False))
    elif change == "check":
        observation = replace(
            observation, checks=({**observation.checks[0], "conclusion": "failure"},)
        )
    elif change == "checkapp":
        observation = replace(observation, checks=({**observation.checks[0], "app": {"id": 8}},))
    elif change == "approval":
        observation = replace(
            observation, reviews=({**observation.reviews[0], "commit_id": "oldhead"},)
        )
    elif change == "eligibility":
        observation = replace(observation, review_decision="REVIEW_REQUIRED")
    elif change == "thread":
        observation = replace(observation, threads=({"isResolved": False, "isOutdated": True},))
    elif change == "dismissed":
        observation = replace(
            observation,
            reviews=observation.reviews
            + ({**observation.reviews[0], "id": 2, "state": "DISMISSED"},),
        )
    result = controller.evaluate(
        candidate, evidence, replace(observation, pr=pr), independent_families=True
    )
    assert not result.ready
    assert any(expected in reason for reason in result.reasons), result.reasons


def test_newest_check_wins_and_comments_do_not_remove_approval(publishable):
    controller, candidate, evidence, observation = publishable
    observation = replace(
        observation,
        checks=({**observation.checks[0], "id": 0, "conclusion": "failure"},) + observation.checks,
        reviews=observation.reviews
        + ({"id": 2, "user": {"login": "reviewer"}, "state": "COMMENTED"},),
    )
    assert controller.evaluate(candidate, evidence, observation).ready


def test_queue_requires_enforced_combined_candidate_evidence(publishable):
    controller, candidate, evidence, observation = publishable
    observation = replace(
        observation, rules=replace(observation.rules, queue=True, queue_merge_method="MERGE")
    )
    failed = controller.evaluate(candidate, evidence, observation, merge_mode="queue")
    assert "merge_group" in " ".join(failed.reasons)
    assert controller.evaluate(
        candidate, evidence, observation, merge_mode="queue", queue_evidence_context="CI"
    ).ready


def test_prepare_merge_fresh_read_and_explicit_nonbypass(monkeypatch, publishable):
    controller, candidate, evidence, observation = publishable
    seen = []
    monkeypatch.setattr(controller, "inspect", lambda number: seen.append(number) or observation)
    intent = controller.prepare_merge(candidate, evidence, action_id="merge1", generation=2)
    assert seen == [1]
    assert intent.payload == {
        "sha": candidate.head,
        "merge_action": "direct_merge",
        "bypass_rules": False,
        "merge_method": "merge",
    }
    assert intent.path.endswith("/merge-async")
    assert intent.coordinates["base"] == candidate.base


def test_queued_is_not_merged_and_expired_request_is_unknown(publishable):
    controller, candidate, _, observation = publishable
    transport = controller.gateway.transport
    transport.responses["GET", "repos/a/b/pulls/1"] = ApiResponse(200, observation.pr, {})
    transport.responses["GET", "repos/a/b/pulls/1/merge-async/r1"] = ApiResponse(
        200, {"status": "enqueued", "details": {}}, {}
    )
    assert (
        controller.merge_status(1, request_id="r1", expected_head=candidate.head).state
        == "enqueued"
    )
    transport.responses["GET", "repos/a/b/pulls/1/merge-async/r1"] = GithubError(
        "expired", status=404
    )
    assert (
        controller.merge_status(1, request_id="r1", expected_head=candidate.head).state == "unknown"
    )
    transport.responses["GET", "repos/a/b/pulls/1"] = ApiResponse(
        200, {**observation.pr, "merged": True, "merge_commit_sha": "squash"}, {}
    )
    receipt = controller.merge_status(1, request_id="r1", expected_head=candidate.head)
    assert receipt.state == "merged_pending_sync"
    assert receipt.merge_commit == "squash"


def test_rules_combine_rulesets_and_classic_without_interpreting_403_as_absence():
    transport = FakeTransport()
    gateway = GithubGateway(transport, "a/b")
    transport.responses["GET", "repos/a/b/rules/branches/main?per_page=100"] = ApiResponse(
        200,
        [
            {
                "type": "required_status_checks",
                "parameters": {
                    "strict_required_status_checks_policy": False,
                    "required_status_checks": [{"context": "native", "integration_id": 42}],
                },
            }
        ],
        {},
    )
    transport.responses["GET", "repos/a/b/branches/main/protection"] = ApiResponse(
        200,
        {"required_status_checks": {"strict": True, "checks": [{"context": "unit", "app_id": 7}]}},
        {},
    )
    rules = read_rules(gateway, "main")
    assert rules.strict
    assert set(rules.checks) == {RequiredCheck("native", 42), RequiredCheck("unit", 7)}
    transport.responses["GET", "repos/a/b/branches/main/protection"] = GithubError(
        "forbidden", status=403
    )
    with pytest.raises(GithubError, match="forbidden"):
        read_rules(gateway, "main")


def test_squash_merge_canonical_confirmation_uses_merge_commit_not_worker_head(project):
    repo = project.project

    def git(*args):
        return subprocess.run(
            ["git", *args], cwd=repo, check=True, capture_output=True, text=True
        ).stdout.strip()

    git("remote", "add", "origin", str(repo))
    canonical = git("rev-parse", "HEAD")
    controller = PublicationController(GithubGateway(FakeTransport(), "a/b"))
    candidate = Candidate("a/b", "u", 1, "not-an-ancestor", canonical, "s", 1, "c")
    receipt = controller.confirm_canonical(
        candidate, MergeReceipt("merged_pending_sync", merge_commit=canonical), repo
    )
    assert receipt.canonical_head == canonical
    assert receipt.state == "merged_pending_sync"


def test_lost_async_reply_recovers_existing_uuid_only_after_fresh_acceptance(
    monkeypatch, publishable
):
    controller, candidate, evidence, observation = publishable
    monkeypatch.setattr(controller, "inspect", lambda _: observation)
    intent = controller.prepare_merge(candidate, evidence, action_id="m1", generation=1)
    intent = replace(intent, state="sent_unknown")
    transport = controller.gateway.transport
    transport.responses["GET", "repos/a/b/pulls/1"] = ApiResponse(200, observation.pr, {})
    transport.responses["PUT", intent.path] = ApiResponse(
        409,
        {
            "status": "pending",
            "details": {
                "uuid": "existing",
                "expected_head_sha": candidate.head,
                "merge_action": "direct_merge",
                "merge_method": "merge",
                "bypass_rules": False,
            },
        },
        {},
    )
    unapproved = controller.reconcile_request(intent, candidate, evidence, generation=1)
    assert unapproved.state == "sent_unknown"
    assert all(method != "PUT" for method, _, _ in transport.calls)
    recovered = controller.reconcile_request(
        intent, candidate, evidence, generation=1, allow_retry=True
    )
    assert recovered.state == "confirmed"
    assert recovered.receipt["details"]["uuid"] == "existing"
    assert [payload for method, _, payload in transport.calls if method == "PUT"] == [
        intent.payload
    ]
    monkeypatch.setattr(controller, "inspect", lambda _: replace(observation, base_head="moved"))
    with pytest.raises(GithubError, match="Canonical target moved"):
        controller.reconcile_request(intent, candidate, evidence, generation=1, allow_retry=True)
    assert sum(method == "PUT" for method, _, _ in transport.calls) == 1


def test_publication_refuses_history_rewriting_merge_modes(monkeypatch, publishable):
    controller, candidate, evidence, observation = publishable
    monkeypatch.setattr(controller, "inspect", lambda _: observation)
    with pytest.raises(ValueError, match="preserve every commit"):
        controller.prepare_merge(
            candidate, evidence, action_id="m1", generation=1, merge_method="squash"
        )
    with pytest.raises(ValueError, match="preserve every commit"):
        controller.prepare_merge(
            candidate, evidence, action_id="m1", generation=1, merge_method="rebase"
        )
    queued = replace(
        observation, rules=replace(observation.rules, queue=True, queue_merge_method="SQUASH")
    )
    verdict = controller.evaluate(
        candidate, evidence, queued, merge_mode="queue", queue_evidence_context="CI"
    )
    assert not verdict.ready
    assert "preserve every commit" in " ".join(verdict.reasons)
