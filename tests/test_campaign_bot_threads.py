"""The supervisor may recommend a bot conversation, only GitHub confirms resolution."""

import copy
import time
from dataclasses import asdict, replace

import pytest
from conftest import git
from test_campaign_publication import publishable as publishable

from bmad_loop import campaign_bot_threads as policy
from bmad_loop.campaign_acceptance import context, provenance
from bmad_loop.campaign_model import (
    CampaignSnapshot,
    CampaignSpec,
    CampaignUnit,
    GitHubSpec,
    OutboxAction,
    UnitSpec,
    UnitState,
    WorkerClaim,
    spec_from_dict,
    spec_to_dict,
)
from bmad_loop.campaign_publication import PublicationController, PublicationRequirements
from bmad_loop.campaign_remote_feedback import collect
from bmad_loop.campaign_runtime import CampaignRuntime
from bmad_loop.campaign_store import CampaignStore


def conversation():
    return {
        "id": "THREAD1",
        "path": "src.txt",
        "isResolved": False,
        "comments": {
            "totalCount": 1,
            "pageInfo": {"hasNextPage": False},
            "nodes": [
                {
                    "id": "C1",
                    "body": "Please exercise the edge case",
                    "updatedAt": "t1",
                    "author": {"__typename": "Bot", "id": "BOT1", "login": "app[bot]"},
                }
            ],
        },
    }


def test_bot_policy_is_absent_from_historical_frozen_bytes():
    raw = spec_to_dict(
        CampaignSpec("example", units=(UnitSpec("story", "source", "epic", "Story"),))
    )
    assert "verified_thread_bot_ids" not in raw["github"]
    assert spec_to_dict(spec_from_dict(raw)) == raw
    raw["github"]["verified_thread_bot_ids"] = ["BOT1"]
    spec = spec_from_dict(raw)
    assert spec.github.verified_thread_bot_ids == ("BOT1",)
    assert spec_from_dict(spec_to_dict(spec)) == spec


@pytest.mark.parametrize(
    "fault", ["human", "unknown-author", "other-bot", "missing-page", "duplicate", "edit"]
)
def test_only_complete_authentic_unchanged_bot_comments_survive(fault):
    thread = conversation()
    before = policy.revision(thread, ("BOT1",))
    assert before
    comments = thread["comments"]
    row = copy.deepcopy(comments["nodes"][0])
    if fault == "edit":
        comments["nodes"][0]["body"] = "Now an unrelated finding"
    elif fault == "missing-page":
        comments["totalCount"] += 1
    else:
        if fault != "duplicate":
            row["id"] = "C2"
            row["author"] = {
                "human": {"__typename": "User", "id": "BOT1", "login": "app[bot]"},
                "unknown-author": {"login": "app[bot]"},
                "other-bot": {"__typename": "Bot", "id": "OTHER", "login": "app[bot]"},
            }[fault]
        comments["nodes"].append(row)
        comments["totalCount"] += 1
    after = policy.revision(thread, ("BOT1",))
    if fault == "edit":
        assert after and after != before
    else:
        assert after is None


@pytest.fixture
def run(project, tmp_path, publishable, monkeypatch):
    controller, _, _, obs = publishable
    repo = project.project
    base = git(repo, "rev-parse", "HEAD").strip()
    (repo / "src.txt").write_text("edge case fixed\n")
    git(repo, "commit", "-qam", "fix edge case")
    fix = git(repo, "rev-parse", "HEAD").strip()
    spec = CampaignSpec(
        "example", github=GitHubSpec(repository="a/b", verified_thread_bot_ids=("BOT1",))
    )
    unit = CampaignUnit(
        UnitSpec("story", "source", "epic", "Story", acceptance=("Exercise the edge case",)),
        state=UnitState.READY_TO_PUBLISH,
        claim=WorkerClaim("story", 1, 1, "review", quiescent=True),
    )
    unit.bindings = {
        "workspace": str(repo),
        "pr": {"number": 1},
        "source_revision": "source",
        "ready_for_review": True,
    }
    snap = CampaignSnapshot("C", "a/b", spec, {unit.id: unit})
    unit.evidence.update(
        integrated={"head": fix, "base": base},
        developer_families=["openai"],
        developer_family="openai",
    )
    runtime = CampaignRuntime(
        repo,
        CampaignStore(tmp_path / "store"),
        gateway=controller.gateway,
    )
    candidate = runtime._candidate(snap, unit)
    before = replace(candidate, head=base)
    thread = conversation()
    observed = replace(
        obs,
        pr={**obs.pr, "head": {"sha": base}},
        base_head=base,
        observed_at=time.time(),
        threads=(thread,),
    )
    remote = collect(
        before, observed, now=observed.observed_at, requirements=PublicationRequirements()
    )
    unit.evidence["remote_feedback"] = asdict(remote)
    unit.evidence["required_findings"] = list(remote.defects)
    unit.evidence["accepted_resolution"] = {"id": "R", "cause_id": remote.digest}
    unit.evidence["repair_candidate"] = {
        "resolution_id": "R",
        "parent": base,
        "head": fix,
        "receipt": "writer",
        "changed_paths": ["src.txt"],
    }
    policy.record_repair(
        snap,
        unit,
        {"proposal_id": "R", "head": fix, "parent_head": base, "recovery_push_confirmed": True},
    )
    link = policy.review_context(unit)["bot_thread_repairs"][0]
    cmd = ["pytest", "test_edge_case"]
    unit.evidence["verification"] = {
        "outcome": "verified",
        "head": fix,
        "launch_id": "V",
        "commands": [{"argv": cmd, "returncode": 0}],
        "proof": provenance(repo, fix, [cmd], config_revision=candidate.config_revision),
    }
    unit.evidence["review"] = {
        "head": fix,
        "launch_id": "PR",
        "observed_model": "claude-test",
        "provider_family": "anthropic",
        "validated": {"coordinates": asdict(candidate), "verdict": "approved"},
        "artifact": {
            "findings": [
                {
                    **link,
                    "resolved": True,
                    "verification_launch_id": "V",
                    "command_index": 0,
                    "reason": "The edge case regression passes against the repaired method.",
                }
            ],
            "acceptance_evidence": [
                {
                    "criterion_id": context(unit)["criteria"][0]["id"],
                    "status": "passed",
                    "command_indexes": [0],
                    "reason": "The edge case passes.",
                    "verification_launch_id": "V",
                }
            ],
        },
    }
    latest = [
        replace(
            observed,
            pr={**observed.pr, "head": {"sha": fix}},
            reviews=tuple({**r, "commit_id": fix} for r in observed.reviews),
            checks=tuple({**r, "head_sha": fix} for r in observed.checks),
        )
    ]
    monkeypatch.setattr(PublicationController, "inspect", lambda self, number: latest[0])
    return runtime, snap, unit, latest


def test_verified_bot_repair_waits_for_maintainer_then_advances(run):
    runtime, snap, unit, observed = run
    prepare = OutboxAction("P", "prepare-merge", unit.id, {}, snap.generation)
    pending = runtime._perform_prepare_merge(snap, prepare)
    assert (
        pending.state == "confirmed"
        and pending.data["bot_thread_wait"]["findings"][0]["id"] == "THREAD1"
    )
    runtime._apply_effect(snap, prepare, pending)
    assert unit.hold and "Maintainer" in unit.hold.reason
    watch = OutboxAction(
        "watch", "remote-wait", unit.id, {"hold_id": unit.hold.id}, snap.generation
    )
    unchanged = runtime._perform_remote_wait(snap, watch)
    assert unchanged.data["ready_to_reaccept"] is False
    observed[0] = replace(observed[0], threads=({**observed[0].threads[0], "isResolved": True},))
    wait = runtime._perform_remote_wait(snap, watch)
    assert wait.data["ready_to_reaccept"], wait.data
    runtime._apply_effect(snap, watch, wait)
    finished = runtime._perform_prepare_merge(snap, prepare)
    runtime._apply_effect(snap, prepare, finished)
    assert unit.hold is None and not unit.evidence["required_findings"]
    assert unit.evidence["bot_thread_dispositions"][0]["observed_resolved"] is True
    assert not snap.outbox
    next_stage = runtime._perform_prepare_merge(snap, prepare)
    assert next_stage.data.get("purpose") == "merge", next_stage

    # A new reply can appear on an already resolved GitHub conversation.
    changed = copy.deepcopy(observed[0].threads[0])
    changed["comments"]["nodes"].append(
        {
            "id": "C3",
            "author": {"__typename": "User", "id": "U"},
            "body": "Reproduces again",
            "updatedAt": "t3",
        }
    )
    changed["comments"]["totalCount"] += 1
    observed[0] = replace(observed[0], threads=(changed,))
    retry = runtime._perform_prepare_merge(snap, prepare)
    assert retry.data.get("acceptance_stale")
    unit.evidence["review"]["launch_id"] = "fresh-review"
    dispute = runtime._perform_prepare_merge(snap, prepare)
    assert dispute.data.get("bot_thread_objection") == changed["id"]
    runtime._apply_effect(snap, prepare, dispute)
    assert unit.hold and "reopen" in unit.hold.reason


@pytest.mark.parametrize(
    "fault", ["same-family", "wrong-fix", "wrong-command", "stale-head", "different-finding"]
)
def test_normal_merge_preparation_preserves_unverified_objections(run, fault):
    runtime, snap, unit, observed = run
    review = unit.evidence["review"]
    finding = review["artifact"]["findings"][0]
    if fault == "same-family":
        review.update(provider_family="openai", observed_model="gpt-fixed")
    elif fault == "wrong-fix":
        finding["fix_head"] = "another-fix"
    elif fault == "wrong-command":
        finding["command_index"] = 42
    elif fault == "stale-head":
        review["head"] = "old"
    else:
        finding["id"] = "another-finding"
    result = runtime._perform_prepare_merge(
        snap, OutboxAction("P", "prepare-merge", unit.id, {}, snap.generation)
    )
    assert result.data["remote_feedback"]["defects"][0]["id"] == "THREAD1"


def test_concurrent_human_reply_requires_fresh_review_even_if_manually_resolved(run):
    runtime, snap, unit, observed = run
    prepare = OutboxAction("P", "prepare-merge", unit.id, {}, snap.generation)
    runtime._apply_effect(snap, prepare, runtime._perform_prepare_merge(snap, prepare))
    wait = OutboxAction("watch", "remote-wait", unit.id, {"hold_id": unit.hold.id}, snap.generation)
    thread = copy.deepcopy(observed[0].threads[0])
    thread["comments"]["nodes"].append(
        {
            "id": "C2",
            "author": {"__typename": "User", "id": "U1"},
            "body": "Still broken",
            "updatedAt": "t2",
        }
    )
    thread["comments"]["totalCount"] += 1
    thread["isResolved"] = True
    observed[0] = replace(observed[0], threads=(thread,))
    result = runtime._perform_remote_wait(snap, wait)
    assert result.data["bot_thread_review_changed"]
    runtime._apply_effect(snap, wait, result)
    assert unit.state == UnitState.SYNCING and "review" not in unit.evidence
    assert unit.evidence["required_findings"][0]["id"] == "THREAD1"


def test_opt_in_gateway_collects_native_actor_types_and_complete_comment_pages():
    from bmad_loop.campaign_github import ApiResponse, GithubGateway

    thread = conversation()
    first = copy.deepcopy(thread["comments"]["nodes"][0])
    last = {**first, "id": "C2", "author": {"__typename": "User", "login": "human"}}

    class Pages:
        def request(self, method, path, payload):
            assert method == "POST" and path == "graphql"
            assert "mutation" not in payload["query"]
            assert all(
                field in payload["query"]
                for field in ("__typename", "... on Bot", "updatedAt", "totalCount")
            )
            if "reviewThreads" in payload["query"]:
                connection = {
                    "totalCount": 1,
                    "nodes": [
                        {
                            **thread,
                            "comments": {
                                "nodes": [first],
                                "totalCount": 2,
                                "pageInfo": {"hasNextPage": True, "endCursor": "C1"},
                            },
                        }
                    ],
                    "pageInfo": {"hasNextPage": False},
                }
                return ApiResponse(
                    200,
                    {"data": {"repository": {"pullRequest": {"reviewThreads": connection}}}},
                    {},
                )
            return ApiResponse(
                200,
                {
                    "data": {
                        "node": {
                            "comments": {
                                "nodes": [last],
                                "totalCount": 2,
                                "pageInfo": {"hasNextPage": False},
                            }
                        }
                    }
                },
                {},
            )

    [actual] = GithubGateway(Pages(), "a/b", thread_provenance=True).review_threads(1)
    assert actual["comments"]["nodes"] == [first, last]
    assert actual["comments"]["pageInfo"]["hasNextPage"] is False
    assert policy.revision(actual, ("BOT1",)) is None
