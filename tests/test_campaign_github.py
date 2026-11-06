"""GitHub side effects recover through receipts without duplicate creates."""

from __future__ import annotations

import json
import subprocess
from dataclasses import replace

import pytest

from bmad_loop.campaign_github import (
    API_VERSION,
    ApiResponse,
    GhTransport,
    GithubError,
    GithubGateway,
    MutationIntent,
    UncertainMutation,
    managed_body,
    managed_marker,
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


def test_cli_uses_json_stdin_and_pinned_api(monkeypatch):
    seen = {}

    def run(argv, **kwargs):
        seen.update(argv=argv, **kwargs)
        return subprocess.CompletedProcess(
            argv, 0, 'HTTP/2.0 201 Created\r\nContent-Type: application/json\r\n\r\n{"id": 9}', ""
        )

    monkeypatch.setattr(subprocess, "run", run)
    response = GhTransport().request("POST", "repos/a/b/issues", {"body": "`$(bad)`\ntext"})
    assert response.data == {"id": 9}
    assert json.loads(seen["input"])["body"] == "`$(bad)`\ntext"
    assert f"X-GitHub-Api-Version: {API_VERSION}" in seen["argv"]
    assert seen["timeout"] == 45
    assert "shell" not in seen


def test_mutation_timeout_is_uncertain(monkeypatch):
    def run(*args, **kwargs):
        raise subprocess.TimeoutExpired("gh", 45)

    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(UncertainMutation):
        GhTransport().request("POST", "repos/a/b/issues", {})
    with pytest.raises(GithubError) as error:
        GhTransport().request("GET", "repos/a/b/issues")
    assert not isinstance(error.value, UncertainMutation)


def test_paginated_list_and_pr_filtering():
    transport = FakeTransport()
    transport.responses[
        "GET", "repos/a/b/issues?state=open&sort=updated&direction=asc&per_page=100"
    ] = ApiResponse(
        200,
        [{"id": 1}, {"id": 2, "pull_request": {}}],
        {"link": '<https://api.github.com/repos/a/b/issues?page=2>; rel="next"'},
    )
    transport.responses["GET", "repos/a/b/issues?page=2"] = ApiResponse(200, [{"id": 3}], {})
    assert [row["id"] for row in GithubGateway(transport, "a/b").issues()] == [1, 3]


def test_pagination_escape_and_limit_are_not_partial_success():
    transport = FakeTransport()
    transport.responses["GET", "repos/a/b/issues?per_page=100"] = ApiResponse(
        200, [], {"link": '<https://evil.test/secrets>; rel="next"'}
    )
    with pytest.raises(GithubError, match="escaped"):
        GithubGateway(transport, "a/b").pages("repos/a/b/issues")
    transport.responses["GET", "repos/a/b/issues?per_page=100"] = ApiResponse(
        200, [], {"link": '<https://api.github.com/repos/a/b/issues?page=2>; rel="next"'}
    )
    with pytest.raises(GithubError, match="incomplete"):
        GithubGateway(transport, "a/b", max_pages=1).pages("repos/a/b/issues")


def test_lost_create_response_reconciles_exact_marker_without_recreate():
    transport = FakeTransport()
    gateway = GithubGateway(transport, "a/b")
    intent = gateway.prepare_create(
        "issue",
        action_id="a1",
        campaign_id="c",
        unit_id="u",
        title="Story",
        body="Human acceptance",
        generation=4,
    )
    transport.responses["POST", intent.path] = UncertainMutation("lost acknowledgement")
    result = gateway.execute(replace(intent, state="sent_unknown"), generation=4)
    assert result.state == "sent_unknown"
    transport.responses[
        "GET", "repos/a/b/issues?state=all&sort=updated&direction=asc&per_page=100"
    ] = ApiResponse(
        200,
        [
            {
                "id": 5,
                "number": 12,
                "title": "Story",
                "body": intent.payload["body"] + "\nHuman addition",
            }
        ],
        {},
    )
    recovered = gateway.reconcile_create(result)
    assert recovered.state == "confirmed"
    assert recovered.receipt["number"] == 12
    assert sum(method == "POST" for method, _, _ in transport.calls) == 1
    assert "Human addition" in recovered.receipt["body"]


@pytest.mark.parametrize("matches", [0, 2])
def test_absent_eventual_search_or_duplicate_marker_does_not_authorize_retry(matches):
    transport = FakeTransport()
    gateway = GithubGateway(transport, "a/b")
    intent = gateway.prepare_create(
        "issue", action_id="a", campaign_id="c", unit_id="u", title="Story", body="criteria"
    )
    transport.responses[
        "GET", "repos/a/b/issues?state=all&sort=updated&direction=asc&per_page=100"
    ] = ApiResponse(200, [{"title": "Story", "body": intent.payload["body"]}] * matches, {})
    result = gateway.reconcile_create(replace(intent, state="sent_unknown"))
    assert result.state == "sent_unknown"
    assert not any(method == "POST" for method, _, _ in transport.calls)


def test_stale_generation_and_unprepared_action_do_not_write():
    transport = FakeTransport()
    gateway = GithubGateway(transport, "a/b")
    intent = MutationIntent("a", "issue", "POST", "repos/a/b/issues", {}, generation=3)
    with pytest.raises(GithubError, match="Persist"):
        gateway.execute(intent, generation=3)
    with pytest.raises(GithubError, match="Stale"):
        gateway.execute(replace(intent, state="sent_unknown"), generation=4)
    assert transport.calls == []


def test_markers_are_stable_unique_and_cannot_inject_html():
    marker = managed_marker("c --> <script>", "u", "issue")
    assert marker == managed_marker("c --> <script>", "u", "issue")
    assert marker != managed_marker("c --> <script>", "other", "issue")
    assert "script" not in marker
    assert "Human text" in managed_body("Human text", marker)
    with pytest.raises(ValueError, match="closing"):
        managed_body("Fixes #12", marker)


def test_native_subissues_use_database_id_and_preserve_parent():
    transport = FakeTransport()
    gateway = GithubGateway(transport, "a/b")
    transport.responses["GET", "repos/a/b/issues/7"] = ApiResponse(
        200, {"id": 777, "number": 7}, {}
    )
    transport.responses["GET", "repos/a/b/issues/7/parent"] = GithubError("no parent", status=404)
    transport.responses["GET", "repos/a/b/issues/3/sub_issues?per_page=100"] = ApiResponse(
        200, [], {}
    )
    intent = gateway.prepare_subissue(3, 7, action_id="a", child_database_id=777)
    assert intent.payload == {"sub_issue_id": 777}
    transport.responses["GET", "repos/a/b/issues/7/parent"] = ApiResponse(200, {"number": 4}, {})
    with pytest.raises(GithubError, match="reparent"):
        gateway.prepare_subissue(3, 7, action_id="a", child_database_id=777)


def test_shared_field_human_edit_prevents_update():
    transport = FakeTransport()
    gateway = GithubGateway(transport, "a/b")
    transport.responses["GET", "repos/a/b/issues/7"] = ApiResponse(
        200, {"id": 777, "number": 7, "updated_at": "t1", "milestone": None}, {}
    )
    intent = gateway.milestone_intent(7, 3, action_id="a", generation=1)
    transport.responses["GET", "repos/a/b/issues/7"] = ApiResponse(
        200, {"id": 777, "number": 7, "updated_at": "t2", "milestone": {"number": 9}}, {}
    )
    with pytest.raises(GithubError, match="human edits"):
        gateway.execute(replace(intent, state="sent_unknown"), generation=1)
    assert all(method == "GET" for method, _, _ in transport.calls)


def test_issue_closure_requires_source_and_does_not_rewrite_body():
    transport = FakeTransport()
    gateway = GithubGateway(transport, "a/b")
    with pytest.raises(GithubError, match="Canonical"):
        gateway.close_issue_intent(7, action_id="a", generation=1, source_confirmed=False)
    transport.responses["GET", "repos/a/b/issues/7"] = ApiResponse(
        200, {"id": 777, "number": 7, "updated_at": "t1", "state": "open", "body": "Human"}, {}
    )
    intent = gateway.close_issue_intent(7, action_id="a", generation=1, source_confirmed=True)
    assert intent.payload == {"state": "closed", "state_reason": "completed"}


def test_pending_merge_ack_is_not_merge_completion_and_conflicting_409_held():
    transport = FakeTransport()
    gateway = GithubGateway(transport, "a/b")
    intent = MutationIntent(
        "a",
        "merge",
        "PUT",
        "repos/a/b/pulls/1/merge-async",
        {"sha": "head", "bypass_rules": False},
        state="sent_unknown",
    )
    transport.responses["PUT", intent.path] = ApiResponse(
        202,
        {
            "status": "pending",
            "details": {"uuid": "req", "expected_head_sha": "head", "bypass_rules": False},
        },
        {},
    )
    result = gateway.execute(intent, generation=0)
    assert result.state == "confirmed"
    assert result.receipt["status"] == "pending"
    transport.responses["PUT", intent.path] = ApiResponse(
        409,
        {
            "status": "pending",
            "details": {"uuid": "other", "expected_head_sha": "stale", "bypass_rules": False},
        },
        {},
    )
    assert gateway.execute(intent, generation=0).state == "sent_unknown"


def test_recent_closed_inventory_stops_at_declared_time_window():
    transport = FakeTransport()
    path = "repos/a/b/pulls?state=closed&sort=updated&direction=desc&per_page=100"
    transport.responses["GET", path] = ApiResponse(
        200,
        [{"number": 1, "updated_at": "2026-11-04"}, {"number": 2, "updated_at": "2026-09-01"}],
        {"link": '<https://api.github.com/repos/a/b/pulls?page=2>; rel="next"'},
    )
    rows = GithubGateway(transport, "a/b").recent_pulls("2026-11-01")
    assert [row["number"] for row in rows] == [1]
    assert len(transport.calls) == 1


def test_review_thread_comments_are_fully_paginated():
    class ThreadTransport:
        def request(self, method, path, payload=None):
            if "number" in payload["variables"]:
                return ApiResponse(
                    200,
                    {
                        "data": {
                            "repository": {
                                "pullRequest": {
                                    "reviewThreads": {
                                        "nodes": [
                                            {
                                                "id": "t1",
                                                "isResolved": False,
                                                "comments": {
                                                    "nodes": [{"id": "c1"}],
                                                    "pageInfo": {
                                                        "hasNextPage": True,
                                                        "endCursor": "cursor1",
                                                    },
                                                },
                                            }
                                        ],
                                        "pageInfo": {"hasNextPage": False},
                                    }
                                }
                            }
                        }
                    },
                    {},
                )
            assert payload["variables"] == {"id": "t1", "cursor": "cursor1"}
            return ApiResponse(
                200,
                {
                    "data": {
                        "node": {
                            "comments": {
                                "nodes": [{"id": "c2"}],
                                "pageInfo": {"hasNextPage": False},
                            }
                        }
                    }
                },
                {},
            )

    threads = GithubGateway(ThreadTransport(), "a/b").review_threads(1)
    assert [row["id"] for row in threads[0]["comments"]["nodes"]] == ["c1", "c2"]


def test_ready_mutation_requires_native_id_and_expected_state():
    transport = FakeTransport()
    gateway = GithubGateway(transport, "a/b")
    intent = replace(
        gateway.prepare_ready("PR_node", action_id="ready", generation=1), state="sent_unknown"
    )
    transport.responses["POST", "graphql"] = ApiResponse(
        200,
        {
            "data": {
                "markPullRequestReadyForReview": {
                    "pullRequest": {"id": "PR_node", "isDraft": False}
                }
            }
        },
        {},
    )
    assert gateway.execute(intent, generation=1).state == "confirmed"
    transport.responses["POST", "graphql"] = ApiResponse(
        200,
        {
            "data": {
                "markPullRequestReadyForReview": {
                    "pullRequest": {"id": "foreign", "isDraft": False}
                }
            }
        },
        {},
    )
    assert gateway.execute(intent, generation=1).state == "sent_unknown"
