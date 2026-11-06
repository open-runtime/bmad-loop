"""A real campaign reaches canonical delivery through queued synthetic G checks."""

import sys
from dataclasses import replace
from urllib.parse import urlsplit

import pytest
from conftest import git
from test_campaign_merge_adapter import source
from test_campaign_runtime import diagnose
from test_campaign_runtime import production_case as production_case
from test_campaign_runtime import run_case

from bmad_loop import campaign_runtime, verify
from bmad_loop.campaign_github import ApiResponse, UncertainMutation
from bmad_loop.campaign_model import GitHubSpec, UnitState


class QueuedServer:
    """Extend the scripted authenticated service with a real merge-queue DAG."""

    def __init__(self, service, *, stacked=False):
        self.service = service
        self.original = service.request
        self.entries = {}
        self.checks = []
        self.groups = []
        self.group_numbers = set()
        self.drop_check_ack = True
        self.check_creates = 0
        self.stacked = stacked

    def _reply(self, data, status=200):
        return ApiResponse(status, data, {})

    def _build(self, number, base):
        service = self.service
        pr = service._pull(number)
        head = pr["head"]["sha"]
        tree = git(service.remote, "merge-tree", "--write-tree", base, head).splitlines()[0]
        group = git(
            service.remote,
            "-c",
            "user.name=GitHub Queue",
            "-c",
            "user.email=queue@example.test",
            "commit-tree",
            tree,
            "-p",
            base,
            "-p",
            head,
            "-m",
            f"Merge queue G for #{number}",
        )
        git(
            service.remote,
            "update-ref",
            f"refs/heads/gh-readonly-queue/main/pr-{number}-{group[:12]}",
            group,
        )
        entry = {
            "id": f"QE_{number}",
            "state": "AWAITING_CHECKS",
            "headCommit": {"oid": group},
            "baseCommit": {"oid": base},
            "pullRequest": {
                "id": pr["node_id"],
                "number": number,
                "headRefOid": head,
                "baseRefName": "main",
            },
        }
        self.entries[number] = entry
        self.groups.append((number, group, base, head))
        self.group_numbers.add(number)
        return entry

    def _entry(self, number):
        if number not in self.entries:
            return None
        if self.stacked:
            base = self.service._base()
            # Rebuild every affected successor after a dequeue or external main
            # update. A check on an old G cannot authorize its replacement.
            for current in self.entries:
                entry = self.entries[current]
                if entry["baseCommit"]["oid"] != base:
                    entry = self._build(current, base)
                if current == number:
                    return entry
                base = entry["headCommit"]["oid"]
            raise AssertionError("queued entry disappeared")
        entry = self.entries[number]
        base = self.service._base()
        if not verify.is_ancestor(self.service.remote, base, entry["headCommit"]["oid"]):
            entry = self._build(number, base)
        return entry

    def _land(self):
        for number in list(self.entries):
            entry = self._entry(number)
            head = entry["headCommit"]["oid"]
            check = next(
                (
                    row
                    for row in self.checks
                    if row["head_sha"] == head
                    and row["name"] == "CI"
                    and row["app"]["id"] == 7
                    and row["conclusion"] == "success"
                ),
                None,
            )
            if check is None:
                break
            base = self.service._base()
            if self.stacked:
                assert entry["baseCommit"]["oid"] == base, "queued predecessors must land first"
            assert verify.is_ancestor(self.service.remote, base, head)
            git(self.service.remote, "update-ref", "refs/heads/main", head, base)
            pr = self.service.pulls[number]
            pr.update(merged=True, state="closed", merge_commit_sha=head)
            self.service.merge_bases[number] = base
            self.entries.pop(number)
            self.service._save()

    def request(self, method, path, payload=None):
        service = self.service
        route = urlsplit(path).path.removeprefix("repos/a/b/")
        with service.lock:
            if path == "graphql" and "dequeuePullRequest" in payload.get("query", ""):
                node = payload["variables"]["input"]["pullRequestId"]
                pr = next(row for row in service.pulls.values() if row["node_id"] == node)
                self.entries.pop(pr["number"], None)
                return self._reply(
                    {
                        "data": {
                            "dequeuePullRequest": {
                                "pullRequest": {
                                    "id": node,
                                    "headRefOid": pr["head"]["sha"],
                                    "baseRefName": "main",
                                    "mergeQueueEntry": None,
                                }
                            }
                        }
                    }
                )
            if path == "graphql" and "mergeQueueEntry" in payload.get("query", ""):
                number = payload["variables"]["number"]
                pr = service._pull(number)
                return self._reply(
                    {
                        "data": {
                            "repository": {
                                "nameWithOwner": "a/b",
                                "pullRequest": {
                                    "id": pr["node_id"],
                                    "number": number,
                                    "headRefOid": pr["head"]["sha"],
                                    "baseRefName": "main",
                                    "merged": pr["merged"],
                                    "state": pr["state"].upper(),
                                    "mergeQueueEntry": self._entry(number),
                                },
                            }
                        }
                    }
                )
            if method == "GET" and route == "rules/branches/main":
                original = self.original(method, path, payload)
                return self._reply(
                    [
                        *original.data,
                        {"type": "merge_queue", "parameters": {"merge_method": "MERGE"}},
                    ]
                )
            if method == "PUT" and route.endswith("/merge-async"):
                assert payload["merge_action"] == "merge_queue" and payload["bypass_rules"] is False
                number = int(route.split("/")[1])
                assert payload["sha"] == service._pull(number)["head"]["sha"]
                assert any(
                    row["head_sha"] == payload["sha"]
                    and row["app"]["id"] == 7
                    and row["conclusion"] == "success"
                    for row in self.checks
                ), "PR-head App check must exist before queue submission"
                if self.stacked and number in self.entries:
                    self._entry(number)
                else:
                    base = service._base()
                    if self.stacked and self.entries:
                        predecessor = self._entry(next(reversed(self.entries)))
                        base = predecessor["headCommit"]["oid"]
                    self._build(number, base)
                service.calls.append((method, path, payload))
                return self._reply(
                    {
                        "status": "enqueued",
                        "details": {
                            "uuid": f"queue-{number}",
                            "expected_head_sha": payload["sha"],
                            "merge_action": "merge_queue",
                            "bypass_rules": False,
                        },
                    },
                    202,
                )
            if method == "GET" and route.startswith("pulls/") and "/merge-async/" in route:
                return self._reply({"status": "enqueued", "details": {"bypass_rules": False}})
            if method == "GET" and route.startswith("commits/") and route.endswith("/check-runs"):
                head = route.split("/")[1]
                return self._reply(
                    {"check_runs": [row for row in self.checks if row["head_sha"] == head]}
                )
            if method == "POST" and route == "check-runs":
                service.calls.append((method, path, payload))
                self.check_creates += 1
                row = {**payload, "id": 1000 + self.check_creates, "app": {"id": 7}}
                self.checks.append(row)
                self._land()
                if self.drop_check_ack:
                    self.drop_check_ack = False
                    raise UncertainMutation("lost check acknowledgement after canonical landing")
                return self._reply(row, 201)
            return self.original(method, path, payload)


def test_stacked_queue_requires_predecessor_checks_and_rebuilds_after_dequeue(production_case):
    case = production_case
    server = QueuedServer(case.service, stacked=True)
    server.drop_check_ack = False
    initial = case.service._base()
    numbers = []

    def success(head):
        server.request(
            "POST",
            "repos/a/b/check-runs",
            {"head_sha": head, "name": "CI", "conclusion": "success"},
        )

    for name in ("first-queued", "second-queued", "third-queued"):
        git(case.repo, "checkout", "-b", name, "main")
        (case.repo / (name + ".txt")).write_text(name)
        git(case.repo, "add", name + ".txt")
        git(case.repo, "commit", "-qm", "test: " + name)
        git(case.repo, "push", "origin", name)
        pr = case.service.request(
            "POST", "repos/a/b/pulls", {"head": name, "base": "main", "title": name}
        ).data
        numbers.append(pr["number"])
        success(pr["head"]["sha"])
        server.request(
            "PUT",
            f"repos/a/b/pulls/{pr['number']}/merge-async",
            {"sha": pr["head"]["sha"], "merge_action": "merge_queue", "bypass_rules": False},
        )
    first, second, third = numbers
    old_heads = [server._entry(number)["headCommit"]["oid"] for number in numbers]
    assert verify.is_ancestor(case.remote, old_heads[0], old_heads[1])
    assert verify.is_ancestor(case.remote, old_heads[1], old_heads[2])
    success(old_heads[2])
    assert case.service._base() == initial
    server.request(
        "POST",
        "graphql",
        {
            "query": "mutation dequeuePullRequest",
            "variables": {"input": {"pullRequestId": case.service.pulls[first]["node_id"]}},
        },
    )
    new_heads = [server._entry(number)["headCommit"]["oid"] for number in (second, third)]
    assert new_heads[0] != old_heads[1] and new_heads[1] != old_heads[2]
    files = git(case.remote, "ls-tree", "--name-only", new_heads[1]).splitlines()
    assert "first-queued.txt" not in files
    assert "second-queued.txt" in files and "third-queued.txt" in files
    success(new_heads[1])
    assert case.service._base() == initial
    success(new_heads[0])
    assert case.service._base() == new_heads[1]
    assert not case.service.pulls[first]["merged"]
    assert all(case.service.pulls[number]["merged"] for number in (second, third))
    assert case.service.merge_bases[second] == initial
    assert case.service.merge_bases[third] == new_heads[0]


@pytest.mark.parametrize("late_hold", [False, True, "check-hold", "check-review"])
def test_production_campaign_queue_checks_exact_g_and_preserves_h_on_lost_ack(
    production_case, tmp_path, monkeypatch, late_hold
):
    case = production_case
    server = QueuedServer(case.service)
    held_number = None
    removed = []

    def request(method, path, payload=None):
        nonlocal held_number
        result = server.request(method, path, payload)
        if (
            late_hold is True
            and held_number is None
            and method == "PUT"
            and path.endswith("/merge-async")
        ):
            held_number = int(path.split("/")[-2])
            case.service.issues[held_number]["labels"] = [{"name": "bmad:hold"}]
        if path == "graphql" and "dequeuePullRequest" in payload.get("query", ""):
            removed.append(held_number)
            case.service.issues[held_number]["labels"] = []
            case.service.pulls[held_number]["requested_reviewers"] = []
        return result

    monkeypatch.setattr(case.service, "request", request)
    submit = case.runtime._perform_github_submission

    def late_submission(snapshot, action):
        nonlocal held_number
        intent = action.payload["intent"]
        if (
            late_hold in {"check-hold", "check-review"}
            and held_number is None
            and action.payload.get("purpose") == "queue-check"
            and intent["payload"]["conclusion"] == "success"
        ):
            held_number = intent["coordinates"]["candidate"]["pr_number"]
            if late_hold == "check-hold":
                case.service.issues[held_number]["labels"] = [{"name": "bmad:hold"}]
            else:
                case.service.pulls[held_number]["requested_reviewers"] = [
                    {"login": "late-reviewer"}
                ]
        result = submit(snapshot, action)
        if held_number is not None and held_number not in removed:
            assert not case.service.pulls[held_number][
                "merged"
            ], "late publication gate was bypassed"
        return result

    monkeypatch.setattr(case.runtime, "_perform_github_submission", late_submission)
    script = tmp_path / "qualified-combined-verifier.py"
    script.write_text(source())
    probe = tmp_path / "authenticated-app-producer.py"
    probe.write_text(
        "import json,time\nprint(json.dumps({'schema_version':1,'repository':'a/b','context':'CI','app_id':7,'mode':'native','authenticated':True,'producer':'scripted-github-app','checks_write':True,'observed_at':time.monotonic()*10}))\n"
    )
    settings = GitHubSpec(
        repository="a/b",
        merge_mode="queue",
        candidate_command=(sys.executable, str(script)),
        candidate_check_context="CI",
        candidate_check_app_id=7,
        candidate_producer_probe=(sys.executable, str(probe)),
    )
    spec = replace(case.snapshot.spec, name="queued-production", units=(), github=settings)
    _, snapshot = campaign_runtime.create(case.repo, spec)
    dependent = snapshot.units["source:project:dependent"]
    dependent.spec = replace(dependent.spec, after=("source:project:first",))
    snapshot.graph_revision += 1
    snapshot = case.store.save(snapshot, snapshot.revision)
    case.snapshot = snapshot
    result = run_case(case)
    assert result.phase == "delivered", diagnose(result)
    if late_hold:
        assert removed == [held_number]
    assert all(unit.state == UnitState.DELIVERED for unit in result.units.values())
    assert len(server.group_numbers) >= 3
    assert server.checks and all(row["app"]["id"] == 7 for row in server.checks)
    assert len({row["external_id"] for row in server.checks}) == len(server.checks)
    for unit in result.units.values():
        if unit.spec.kind in {"planning", "tracking"}:
            continue
        state = unit.evidence["queue"]
        assert unit.bindings["head"] == unit.evidence["integrated"]["head"]
        assert state["verification"]["group_head"] != unit.bindings["head"]
        assert verify.is_ancestor(
            case.remote, unit.bindings["head"], git(case.remote, "rev-parse", "main")
        )
    assert not [
        payload
        for method, path, payload in case.service.calls
        if method == "PUT" and path.endswith("merge-async") and payload.get("bypass_rules")
    ]
