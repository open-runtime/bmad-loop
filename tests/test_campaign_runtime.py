"""Zero-token production-driver qualification with actual Git and child PIDs.

Only the coding sessions and remote GitHub service are scripted. The campaign
supervisor, worktree allocation, immutable envelopes, worker process identity,
receipt validation, verification commands, merges, pushes and publication gates
are production code. The child script never imports or invokes a coding CLI.
"""

from __future__ import annotations

import contextlib
import dataclasses
import json
import subprocess
import sys
import threading
import time
import types
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from conftest import git, install_build_auto_skill

from bmad_loop import campaign_runtime, campaign_workers, verify
from bmad_loop.adapters import multiplexer
from bmad_loop.campaign_github import ApiResponse, GithubError, GithubGateway, UncertainMutation
from bmad_loop.campaign_model import (
    CampaignError,
    CampaignSpec,
    CheckpointSpec,
    GitHubSpec,
    IntakeSpec,
    PlanningSpec,
    ResourceSpec,
    RoleCandidate,
    RoleSpec,
    SourceSelector,
    UnitSpec,
    UnitState,
)
from bmad_loop.campaign_scheduler import CapacityObservation
from bmad_loop.campaign_store import CampaignStore
from bmad_loop.campaign_supervisor import CampaignSupervisor
from bmad_loop.platform_util import atomic_write_text


class ScriptedGithub:
    """A strict GitHub server whose canonical branch is a real bare clone."""

    def __init__(self, remote: Path, state_file: Path):
        self.remote, self.state_file = remote, state_file
        self.issues, self.pulls, self.milestones, self.parents = {}, {}, {}, {}
        self.calls, self.merge_requests, self.closed_units = [], {}, []
        self.counter = 0
        self.lock = threading.RLock()
        self.drop_merge_reply = False
        self.drop_create_reply = False
        self.delay_tracking_for = ""
        self.tracking_released = False
        self.moved_main = False
        self.planning_required = False
        self.move_before_first_merge = False
        self.merge_bases = {}
        self._save()

    def _save(self):
        atomic_write_text(
            self.state_file,
            json.dumps(
                {
                    "pulls": list(self.pulls.values()),
                    "closed_units": self.closed_units,
                    "planning_required": self.planning_required,
                }
            ),
        )

    def _base(self):
        return git(self.remote, "rev-parse", "refs/heads/main")

    def _pull(self, number):
        row = self.pulls[number]
        row["head"]["sha"] = git(self.remote, "rev-parse", "refs/heads/" + row["head"]["ref"])
        row["base"]["sha"] = self._base()
        return row

    def _response(self, data, status=200):
        return ApiResponse(status, copy_json(data), {})

    def request(self, method, path, payload=None):
        with self.lock:
            self.calls.append((method, path, copy_json(payload)))
            parsed = urlsplit(path)
            route = parsed.path.removeprefix("repos/a/b/")
            query = parse_qs(parsed.query)
            if path == "graphql":
                variables = payload["variables"]
                operation = payload["query"]
                if "reviewThreads" in operation:
                    return self._response(
                        {
                            "data": {
                                "repository": {
                                    "pullRequest": {
                                        "reviewThreads": {
                                            "nodes": [],
                                            "pageInfo": {"hasNextPage": False},
                                        }
                                    }
                                }
                            }
                        }
                    )
                if "reviewDecision" in operation:
                    return self._response(
                        {"data": {"repository": {"pullRequest": {"reviewDecision": "APPROVED"}}}}
                    )
                if (
                    "markPullRequestReadyForReview" in operation
                    or "convertPullRequestToDraft" in operation
                ):
                    node = variables["input"]["pullRequestId"]
                    pr = next(row for row in self.pulls.values() if row["node_id"] == node)
                    pr["draft"] = "convertPullRequestToDraft" in operation
                    self._save()
                    name = (
                        "convertPullRequestToDraft"
                        if pr["draft"]
                        else "markPullRequestReadyForReview"
                    )
                    return self._response(
                        {"data": {name: {"pullRequest": {"id": node, "isDraft": pr["draft"]}}}}
                    )
                if "node(id:" in operation:
                    pr = next(
                        row for row in self.pulls.values() if row["node_id"] == variables["id"]
                    )
                    return self._response(
                        {"data": {"node": {"id": pr["node_id"], "isDraft": pr["draft"]}}}
                    )
                raise AssertionError((method, path, payload))
            if method == "GET":
                if route == "rules/branches/main":
                    return self._response(
                        [
                            {
                                "type": "required_status_checks",
                                "parameters": {
                                    "strict_required_status_checks_policy": True,
                                    "required_status_checks": [
                                        {"context": "CI", "integration_id": 7}
                                    ],
                                },
                            },
                            {
                                "type": "pull_request",
                                "parameters": {
                                    "required_approving_review_count": 1,
                                    "require_last_push_approval": True,
                                    "required_review_thread_resolution": True,
                                },
                            },
                        ]
                    )
                if route == "branches/main/protection":
                    raise GithubError("No classic protection", status=404)
                if route == "branches/main":
                    return self._response({"name": "main", "commit": {"sha": self._base()}})
                if route == "git/ref/heads/main":
                    if self.move_before_first_merge and not self.moved_main:
                        before = self._base()
                        tree = git(self.remote, "rev-parse", before + "^{tree}")
                        advanced = git(
                            self.remote,
                            "-c",
                            "user.name=Scripted GitHub",
                            "-c",
                            "user.email=scripted@example.test",
                            "commit-tree",
                            tree,
                            "-p",
                            before,
                            "-m",
                            "Concurrent upstream progress",
                        )
                        git(self.remote, "update-ref", "refs/heads/main", advanced, before)
                        self.moved_main = True
                    return self._response({"object": {"sha": self._base()}})
                if route.startswith("commits/"):
                    head = route.split("/")[1]
                    if route.endswith("/check-runs"):
                        return self._response(
                            {
                                "check_runs": [
                                    {
                                        "id": 1,
                                        "name": "CI",
                                        "app": {"id": 7},
                                        "head_sha": head,
                                        "status": "completed",
                                        "conclusion": "success",
                                    }
                                ]
                            }
                        )
                    if route.endswith("/statuses"):
                        return self._response([])
                if route == "milestones":
                    return self._response(list(self.milestones.values()))
                if route == "issues":
                    rows = [
                        row
                        for row in self.issues.values()
                        if query.get("state", ["open"])[0] == "all"
                        or row["state"] == query.get("state", ["open"])[0]
                    ]
                    if "milestone" in query:
                        rows = [
                            row
                            for row in rows
                            if (row.get("milestone") or {}).get("number")
                            == int(query["milestone"][0])
                        ]
                    return self._response(rows)
                if route == "pulls":
                    # A GitHub list request reads one server-side snapshot, not
                    # two separate Git subprocesses per returned pull request.
                    refs = dict(
                        row.split(" ", 1)
                        for row in git(
                            self.remote,
                            "for-each-ref",
                            "--format=%(refname:short) %(objectname)",
                            "refs/heads",
                        ).splitlines()
                    )
                    for row in self.pulls.values():
                        row["head"]["sha"] = refs[row["head"]["ref"]]
                        row["base"]["sha"] = refs["main"]
                    return self._response(
                        [
                            self.pulls[number]
                            for number in self.pulls
                            if query.get("state", ["open"])[0] == "all"
                            or self.pulls[number]["state"] == query.get("state", ["open"])[0]
                        ]
                    )
                parts = route.split("/")
                if parts[0] == "milestones" and len(parts) == 2:
                    return self._response(self.milestones[int(parts[1])])
                if parts[0] == "issues":
                    number = int(parts[1])
                    if len(parts) == 2:
                        return self._response(self.issues[number])
                    if parts[2] == "comments":
                        return self._response([])
                    if parts[2] == "parent":
                        if number not in self.parents:
                            raise GithubError("No parent", status=404)
                        return self._response(self.issues[self.parents[number]])
                    if parts[2] == "sub_issues":
                        return self._response(
                            [
                                row
                                for number_, row in self.issues.items()
                                if self.parents.get(number_) == number
                            ]
                        )
                if parts[0] == "pulls":
                    number = int(parts[1])
                    pr = self._pull(number)
                    if len(parts) == 2:
                        return self._response(pr)
                    if parts[2] == "requested_reviewers":
                        return self._response(
                            {
                                "users": pr.get("requested_reviewers", []),
                                "teams": pr.get("requested_teams", []),
                            }
                        )
                    if parts[2] == "reviews":
                        return self._response(
                            []
                            if pr["draft"]
                            else [
                                {
                                    "id": 1,
                                    "state": "APPROVED",
                                    "commit_id": pr["head"]["sha"],
                                    "user": {"login": "reviewer"},
                                }
                            ]
                        )
                    if parts[2] == "files":
                        names = git(
                            self.remote,
                            "diff",
                            "--name-only",
                            pr["base"]["sha"] + "..." + pr["head"]["sha"],
                        ).splitlines()
                        return self._response(
                            [{"filename": name, "status": "added"} for name in names]
                        )
                    if parts[2] == "merge-async" and len(parts) == 4:
                        return self._response(self.merge_requests[parts[3]])
                raise AssertionError((method, path, payload))
            if method == "POST" and route in {"issues", "pulls", "milestones"}:
                if route == "milestones":
                    number = len(self.milestones) + 1
                    row = {
                        "id": 9000 + number,
                        "number": number,
                        "html_url": f"https://github.com/a/b/milestone/{number}",
                        "state": "open",
                        "updated_at": "2026-11-05T10:00:00Z",
                        **payload,
                    }
                    self.milestones[number] = row
                else:
                    self.counter += 1
                    number = self.counter
                    row = {
                        "id": 1000 + number,
                        "node_id": f"node-{number}",
                        "number": number,
                        "html_url": f"https://github.com/a/b/{'pull' if route == 'pulls' else 'issues'}/{number}",
                        "state": "open",
                        "labels": [],
                        "updated_at": "2026-11-05T10:00:00Z",
                        "user": {"login": "author"},
                        **payload,
                    }
                    if route == "pulls":
                        row.update(
                            head={
                                "ref": payload["head"],
                                "sha": git(
                                    self.remote, "rev-parse", "refs/heads/" + payload["head"]
                                ),
                            },
                            base={
                                "ref": payload["base"],
                                "sha": self._base(),
                                "repo": {"full_name": "a/b"},
                            },
                            merged=False,
                            mergeable=True,
                            mergeable_state="clean",
                            pull_request={},
                        )
                        self.pulls[number] = row
                    row["milestone"] = self.milestones.get(payload.get("milestone"))
                    self.issues[number] = row
                self._save()
                if self.drop_create_reply and route == "pulls":
                    self.drop_create_reply = False
                    raise UncertainMutation("Created PR but reply was lost")
                return self._response(row, 201)
            parts = route.split("/")
            if method == "POST" and parts[0] == "issues" and parts[-1] == "sub_issues":
                parent = int(parts[1])
                child = next(
                    row for row in self.issues.values() if row["id"] == payload["sub_issue_id"]
                )
                self.parents[child["number"]] = parent
                return self._response(child, 201)
            if method == "POST" and parts[0] == "issues" and parts[-1] == "comments":
                return self._response({"id": 20000 + len(self.calls), **payload}, 201)
            if method == "PATCH" and parts[0] in {"issues", "milestones", "pulls"}:
                rows = {"issues": self.issues, "milestones": self.milestones, "pulls": self.pulls}[
                    parts[0]
                ]
                row = rows[int(parts[1])]
                if (
                    payload.get("state") == "closed"
                    and self.delay_tracking_for in row.get("body", "")
                    and self.delay_tracking_for
                    and not self.tracking_released
                ):
                    raise GithubError("Tracking unavailable", status=503)
                row.update(payload)
                if "milestone" in payload:
                    row["milestone"] = self.milestones[payload["milestone"]]
                if payload.get("state") == "closed" and parts[0] == "issues":
                    self.closed_units.append(row["body"].split("Source: ")[-1].splitlines()[0])
                self._save()
                return self._response(row)
            if method == "PUT" and parts[0] == "pulls" and parts[-1] == "merge-async":
                number = int(parts[1])
                pr = self._pull(number)
                if pr["merged"]:
                    return self._response(
                        {"status": "merged", "details": {"sha": pr["merge_commit_sha"]}}
                    )
                assert payload["bypass_rules"] is False
                assert payload["merge_method"] == "merge"
                assert payload["sha"] == pr["head"]["sha"]
                base, head = self._base(), pr["head"]["sha"]
                self.merge_bases[number] = base
                assert verify.is_ancestor(
                    self.remote, base, head
                ), "server strict current-base gate rejected a stale candidate"
                tree = git(self.remote, "rev-parse", head + "^{tree}")
                merged = git(
                    self.remote,
                    "-c",
                    "user.name=Scripted GitHub",
                    "-c",
                    "user.email=scripted@example.test",
                    "commit-tree",
                    tree,
                    "-p",
                    base,
                    "-p",
                    head,
                    "-m",
                    f"Merge PR #{number}",
                )
                git(self.remote, "update-ref", "refs/heads/main", merged, base)
                pr.update(merged=True, state="closed", merge_commit_sha=merged)
                uuid = f"request-{number}"
                result = {
                    "status": "merged",
                    "details": {"uuid": uuid, "sha": merged, "expected_head_sha": head, **payload},
                }
                self.merge_requests[uuid] = result
                self._save()
                if self.drop_merge_reply:
                    self.drop_merge_reply = False
                    raise UncertainMutation("Merge landed but acknowledgement was lost")
                return self._response(
                    {
                        "status": "pending",
                        "details": {"uuid": uuid, "expected_head_sha": head, **payload},
                    },
                    202,
                )
            raise AssertionError((method, path, payload))


def copy_json(value):
    return json.loads(json.dumps(value))


SCRIPT = r"""
import hashlib, json, os, sys, threading, time
from pathlib import Path
from bmad_loop import campaign_workers as workers, verify
from bmad_loop.adapters import multiplexer
from bmad_loop.adapters.base import SessionResult
from bmad_loop.adapters.mock import MockAdapter
from bmad_loop.adapters.profile import get_profile
from bmad_loop.model import TokenUsage
from bmad_loop.platform_util import atomic_write_text
GH_FILE = Path(__GH_FILE__)
EVENTS = Path(__EVENTS__)
MARKERS = Path(__MARKERS__)

def event(kind, request):
    record = json.dumps({"event":kind,"unit":request["unit_id"],"time":time.monotonic(),"base":request.get("coordinates",{}).get("base"),"head":request.get("coordinates",{}).get("head")})+"\n"
    descriptor = os.open(EVENTS, os.O_CREAT|os.O_APPEND|os.O_WRONLY, 0o600)
    try: os.write(descriptor, record.encode())
    finally: os.close(descriptor)

def execution(request, session):
    remote=json.loads(GH_FILE.read_text())
    matches=[row for row in remote["pulls"] if row["head"]["ref"]==request["branch"]]
    assert len(matches)==1 and matches[0]["draft"] is True, "implementation ran without its early draft PR"
    suffix=request["unit_id"].rsplit(":",1)[-1]
    if remote.get("planning_required"):
        assert any(row["title"]=="Campaign planning" and row["merged"] for row in remote["pulls"]), "execution started before planning PR merged"
        assert any(item.endswith(":planning") for item in remote["closed_units"]), "execution started before planning tracking reconciled"
    if suffix=="dependent":
        assert any("first" in item for item in remote["closed_units"]), "dependency ran before source/issue tracking reconciled"
    event("execution-start",request)
    MARKERS.mkdir(exist_ok=True)
    (MARKERS/suffix).write_text("started")
    if suffix in {"first","second"}:
        # This is a synchronization barrier, not an admission-latency benchmark.
        # The first worker cannot finish until a second actually starts.
        deadline=time.monotonic()+120
        independent=("first","second","planned") if remote.get("planning_required") else ("first","second")
        while sum((MARKERS/name).exists() for name in independent)<2:
            if time.monotonic()>deadline: raise RuntimeError("independent units were serialized")
            threading.Event().wait(.01)
    workspace=Path(request["workspace"])
    product=workspace/"src"/(suffix+".txt")
    product.parent.mkdir(exist_ok=True)
    product.write_text("accepted "+suffix+"\n")
    spec=workspace/request["source"]["path"]/"stories"/(suffix+"-story.md")
    baseline=verify.rev_parse_head(workspace)
    planned_marker="planning-revision-marker:first" if suffix=="first" and remote.get("planning_required") else ""
    if planned_marker:
        assert planned_marker in spec.read_text(), "execution read a stale source plan from before planning delivery"
    spec.write_text("---\ntitle: 'Scripted story'\ntype: feature\nstatus: done\nbaseline_revision: '"+baseline+"'\n---\n\n## Intent\nAccepted "+suffix+" behavior.\n"+planned_marker+"\n")
    verify.commit_paths(workspace,"feat: implement "+suffix,[product,spec])
    event("execution-end",request)
    return SessionResult(status="completed",session_id="scripted-"+request["launch_id"],result_json={"workflow":"auto-dev","story_key":request["unit"]["selector"],"spec_file":str(spec),"baseline_commit":baseline,"tasks_total":1,"tasks_done":1,"verification":[],"escalations":[],"followup_review_recommended":False})

def stage(request,session):
    packet=json.loads(request["prompt"][request["prompt"].index("{"):])
    context=packet["context"]
    name=request["coordinates"]["stage"]
    assert name in {"publication_review","admission_review","requirements","architecture","decomposition","planning_review"}
    event("publication-review" if name=="publication_review" else "admission-review" if name=="admission_review" else "planning-"+name,request)
    artifact={"schema_version":1,"status":"done","coordinates":request["coordinates"],"artifacts":[],"verdict":"approved","findings":[],"source_artifact_digest":context.get("source_artifact_digest","")}
    if name=="publication_review":
        proof=context["verification"]
        assert proof["proof"]["head"]==request["coordinates"]["head"]
        artifact["acceptance_evidence"]=[{"criterion_id":row["id"],"status":"passed","verification_launch_id":proof["launch_id"],"command_indexes":list(range(len(proof["commands"]))),"reason":"Recorded verifier checks original content and every produced story artifact on this candidate."} for row in context["acceptance_ledger"]["criteria"]]
    if name in {"requirements","architecture","decomposition"}:
        workspace=Path(request["workspace"])
        marker=workspace/"docs"/"planning"/(name+".md")
        marker.parent.mkdir(exist_ok=True,parents=True)
        marker.write_text("# "+name+"\n\nReviewed brief: "+(workspace/"docs"/"brief.md").read_text()+"\nAll existing acceptance and dependencies are retained.\n")
        artifact["artifacts"]=[str(marker.relative_to(workspace))]
        if name=="decomposition":
            units=[row for row in context["existing_units"] if row["kind"]!="planning"]
            first=next(row for row in units if row["selector"]=="first")
            first["acceptance"]=["Accept first with planning marker"]
            first_spec=workspace/"specs"/"epic"/"stories"/"first-story.md"
            first_spec.write_text(first_spec.read_text()+"\nplanning-revision-marker:first\n")
            artifact["artifacts"].append(str(first_spec.relative_to(workspace)))
            added=dict(units[0])
            added.update(id="source:project:planned",title="Planned",selector="planned",acceptance=["Accept planned"],scopes=["src/planned.txt"],after=[request["unit_id"]],order=99)
            units.append(added)
            spec=workspace/"specs"/"epic"/"stories"/"planned-story.md"
            spec.write_text("---\nstatus: ready-for-dev\n---\n# Planned\n\nAccept planned.\n")
            manifest=workspace/"specs"/"epic"/"stories.yaml"
            manifest.write_text(manifest.read_text().replace("description: Accept first\n","description: Accept first with planning marker\n")+"- id: planned\n  title: Planned\n  description: Accept planned\n")
            artifact["artifacts"] += [str(spec.relative_to(workspace)),str(manifest.relative_to(workspace))]
            artifact.update(units=units,source_bindings={added["id"]:{"kind":"stories","path":str(spec.relative_to(workspace))},first["id"]:{"kind":"stories","path":str(first_spec.relative_to(workspace))}})
    if name=="planning_review":
        artifact["graph_digest"]=context["planning_graph_digest"]
    if name=="admission_review":
        artifact["overlap"]={"unit_id":request["unit_id"],"disposition":"new_work","inventory_digest":context["inventory_digest"],"evidence_ids":[],"acceptance_coverage":context["unit"]["acceptance"],"remaining":[],"exclusions":[],"reason":"No active or delivered work covers these scoped acceptance clauses","adopted_pr":None}
    return SessionResult(status="completed",session_id="scripted-"+request["launch_id"],result_json=artifact)
def adapters(request,envelope,project,run_dir,policy,**kwargs):
    effect=execution if request["kind"]=="execution" else stage
    model="gpt-6-sol" if request["kind"]=="execution" else request["candidate"]["model"]
    adapter=MockAdapter([lambda spec: effect(request,spec)],usage_per_session=TokenUsage(models=(model,)))
    adapter.profile=get_profile(policy.adapter.resolved("dev").name if request["kind"]=="execution" else request["candidate"]["profile"],project)
    return {role:adapter for role in ("dev","review","retro","triage")}
workers._adapters=adapters
multiplexer.configure_multiplexer=lambda *a,**k: None
raise SystemExit(workers.child_main(Path(sys.argv[1])))
"""


@pytest.fixture
def production_case(project, tmp_path, monkeypatch, request):
    from bmad_loop import platform_util
    from bmad_loop.adapters import profile as profile_module

    real_profile = profile_module.get_profile

    def scripted_profile(*args, **kwargs):
        return dataclasses.replace(real_profile(*args, **kwargs), binary=sys.executable)

    # The fixture launches scripted Python children, so readiness must prove
    # their real executable rather than depend on developer-installed CLIs.
    monkeypatch.setattr(profile_module, "get_profile", scripted_profile)
    monkeypatch.setattr(campaign_runtime, "get_profile", scripted_profile)

    if not platform_util.HANDLE_ANCHORED_WRITES:
        pytest.skip(
            "campaign runtime requires handle-anchored candidate/source reads; preflight refusal is tested separately"
        )
    repo = project.project
    config = repo / "_bmad" / "bmm" / "config.yaml"
    config.parent.mkdir(parents=True)
    config.write_text(
        "output_folder: _bmad-output\nimplementation_artifacts: _bmad-output/implementation-artifacts\nplanning_artifacts: _bmad-output/planning-artifacts\n"
    )
    stories = repo / "specs" / "epic"
    (stories / "stories").mkdir(parents=True)
    (stories / "SPEC.md").write_text("# Scripted epic\n")
    (stories / "stories.yaml").write_text(
        "".join(
            f"- id: {name}\n  title: {name.title()}\n  description: Accept {name}\n"
            for name in ("first", "second", "dependent")
        )
    )
    for name in ("first", "second", "dependent"):
        (stories / "stories" / f"{name}-story.md").write_text(
            f"---\nstatus: ready-for-dev\n---\n# {name}\n"
        )
    policy = repo / ".bmad-loop" / "policy.toml"
    policy.parent.mkdir(parents=True, exist_ok=True)
    policy.write_text(
        '[adapter]\nname="codex"\nmodel="gpt-6-sol"\n[gates]\nmode="none"\n[review]\nenabled=false\n[notify]\ndesktop=false\n'
    )
    install_build_auto_skill(repo)
    install_build_auto_skill(repo, tree=".agents/skills")
    planning_skills = tuple(
        (stage, "fixture-" + stage.replace("_", "-"))
        for stage in ("requirements", "architecture", "decomposition", "planning_review")
    )
    for stage, name in planning_skills:
        native = repo / ".agents" / "skills" / name / "SKILL.md"
        native.parent.mkdir(parents=True)
        native.write_text(f"# Scripted {stage}\nUse the approved packet in unattended mode.\n")
    planning = bool(getattr(request, "param", {}).get("planning"))
    if planning:
        (repo / "docs").mkdir(exist_ok=True)
        (repo / "docs" / "brief.md").write_text(
            "Implement the selected stories and add the planned acceptance story while preserving their independent execution.\n"
        )
    git(repo, "add", "_bmad", "specs", ".bmad-loop/policy.toml", ".claude/skills", ".agents/skills")
    if planning:
        git(repo, "add", "docs/brief.md")
    git(repo, "commit", "-qm", "test: seed BMAD config")
    remote = tmp_path / "remote.git"
    git(repo, "clone", "--bare", str(repo), str(remote))
    git(repo, "remote", "add", "origin", str(remote))
    git(repo, "fetch", "origin", "main")
    store = CampaignStore(tmp_path / "state")
    monkeypatch.setattr(campaign_runtime, "store_for", lambda _: store)
    units = tuple(
        UnitSpec(
            f"source:project:{name}",
            "source",
            "epic",
            name.title(),
            selector=name,
            acceptance=("Accept " + name,),
            scopes=("src/" + name + ".txt",),
            after=("source:project:first",) if name == "dependent" else (),
            order=index,
        )
        for index, name in enumerate(("first", "second", "dependent"))
    )
    roles = tuple(
        (role, RoleSpec(profile="claude", model="claude-fable-5-1", effort="high"))
        for role in (
            "planning",
            "planning_review",
            "triage",
            "escalation",
            "decision_review",
            "conflict_repair",
            "publication_review",
        )
    )
    roles = tuple(
        (role, RoleSpec(pool="reviewers") if role == "publication_review" else settings)
        for role, settings in roles
    )
    spec = CampaignSpec(
        "production",
        sources=(
            SourceSelector(
                "stories",
                path="specs/epic",
                units=("first", "second", "dependent"),
                independent=True,
                id="source",
            ),
        ),
        units=(),
        ownership=tuple((unit.id, unit.scopes) for unit in units),
        max_workers=2,
        max_active_epics=2,
        max_in_flight_units=6,
        max_review_sessions=2,
        resources=ResourceSpec(verify_slots=2, max_sessions=4),
        github=GitHubSpec(repository="a/b", require_independent_review=True),
        worktree_root=str(tmp_path / "workers"),
        roles=roles,
        role_pools=(
            (
                "reviewers",
                (
                    RoleCandidate("codex", "gpt-6-astra", "high", "openai"),
                    RoleCandidate("claude", "claude-fable-5-1", "high", "anthropic"),
                ),
            ),
        ),
        intake=IntakeSpec(
            issues=(
                "all-open-and-new" if getattr(request, "param", {}).get("intake") else "selected"
            ),
            allow_new_units=bool(getattr(request, "param", {}).get("intake")),
            allowed_scopes=("src",),
        ),
        planning=PlanningSpec(
            enabled=planning, brief="docs/brief.md" if planning else "", skills=planning_skills
        ),
        checkpoints=CheckpointSpec(target_interval_s=10**12),
        verify_commands=(
            (
                sys.executable,
                "-c",
                "from pathlib import Path; assert Path('src.txt').read_text().strip() == 'original'; assert all(p.read_text().startswith('accepted ') for p in Path('src').glob('*.txt'))",
            ),
        ),
        poll_interval_s=0.005,
    )
    _, snapshot = campaign_runtime.create(repo, spec)
    # Seed an accepted campaign graph revision over the real stories import.
    # The source's independent declaration frees first/second; this explicit
    # graph edge holds the dependent until delivery, including tracking debt.
    dependent = snapshot.units["source:project:dependent"]
    dependent.spec = dataclasses.replace(dependent.spec, after=("source:project:first",))
    snapshot.graph_revision += 1
    snapshot = store.save(snapshot, snapshot.revision)
    service = ScriptedGithub(remote, tmp_path / "github-state.json")
    service.planning_required = planning
    service._save()
    gateway = GithubGateway(service, "a/b")
    runtime = campaign_runtime.CampaignRuntime(
        repo,
        store,
        gateway=gateway,
        # Accelerate short waits without turning 15s inventory refreshes into
        # continuous 150ms Git/process storms under xdist.
        clock=lambda: time.monotonic() * 10,
        capacity_observer=lambda *a, **k: CapacityObservation(True, slots=8, model_access=True),
    )
    script = tmp_path / "scripted-coding-cli.py"
    events = tmp_path / "coding-events.jsonl"
    markers = tmp_path / "parallel-markers"
    script.write_text(
        SCRIPT.replace("__GH_FILE__", repr(str(service.state_file)))
        .replace("__EVENTS__", repr(str(events)))
        .replace("__MARKERS__", repr(str(markers))),
        encoding="utf-8",
    )
    processes = {}

    def scripted_process(argv, **options):
        request_path = Path(argv[-1])
        process = subprocess.Popen(
            [sys.executable, str(script), str(request_path)],
            **options,
        )
        processes[request_path.parent] = process
        return process

    # Exercise production spawn and observation, including owned child reaping.
    # Change only the child entrypoint, never subprocess's global Popen function.
    monkeypatch.setattr(
        campaign_workers,
        "subprocess",
        types.SimpleNamespace(
            Popen=scripted_process, DEVNULL=subprocess.DEVNULL, STDOUT=subprocess.STDOUT
        ),
    )
    monkeypatch.setattr(multiplexer, "configure_multiplexer", lambda *a, **k: None)
    monkeypatch.setattr(
        multiplexer, "get_multiplexer", lambda: types.SimpleNamespace(list_window_ids=lambda _: [])
    )
    yield types.SimpleNamespace(
        repo=repo,
        remote=remote,
        store=store,
        snapshot=snapshot,
        service=service,
        runtime=runtime,
        events=events,
        processes=processes,
    )
    for process in processes.values():
        if process.poll() is None:
            process.terminate()
        process.wait(timeout=10)
    for directory in processes:
        with contextlib.suppress(CampaignError):
            campaign_workers.observe(
                directory, expected=json.loads((directory / "request.json").read_text())
            )


def diagnose(snapshot):
    return {
        "phase": snapshot.phase,
        "units": {
            key: {
                "state": unit.state.value,
                "hold": dataclasses.asdict(unit.hold) if unit.hold else None,
                "claim": dataclasses.asdict(unit.claim) if unit.claim else None,
            }
            for key, unit in snapshot.units.items()
        },
        "unsettled": [
            {"kind": row.kind, "unit": row.unit, "state": row.state, "error": row.error}
            for row in snapshot.outbox
            if row.state != "confirmed"
        ],
    }


def run_case(case, *, ticks=None, timeout_s=None):
    """Wait for the real terminal predicate; ticks are only for intermediate-state tests."""
    from campaign_runtime_watchdog import ProgressWatchdog, TimedDriver, failure_details

    watchdog = ProgressWatchdog(
        case.store.load(case.snapshot.id), now=time.monotonic(), timeout_s=timeout_s
    )
    next_check = 0.0
    timed_driver = TimedDriver(case.runtime)

    def bounded_clock():
        nonlocal next_check
        now = time.monotonic()
        if now >= next_check:
            current = case.store.load(case.snapshot.id)
            reason = watchdog.check(current, now=now)
            if reason:
                details = diagnose(current)
                details.update(failure_details(case, current, supervisor._active))
                details.update(timed_driver.details())
                raise AssertionError(
                    f"campaign did not reach its {reason}: " + json.dumps(details, indent=2)
                )
            next_check = now + 0.25
        return case.runtime.clock()

    supervisor = CampaignSupervisor(
        case.store, timed_driver, clock=bounded_clock, progress=lambda _: None
    )
    return supervisor.run(case.snapshot.id, max_ticks=ticks)


def test_production_delivery_parallel_drafts_review_merge_and_tracking(production_case):
    case = production_case
    result = run_case(case)
    assert result.phase == "delivered", diagnose(result)
    assert all(unit.state == UnitState.DELIVERED for unit in result.units.values())
    rows = [json.loads(line) for line in case.events.read_text().splitlines()]
    starts = {row["unit"]: row["time"] for row in rows if row["event"] == "execution-start"}
    ends = {row["unit"]: row["time"] for row in rows if row["event"] == "execution-end"}
    assert max(starts[key] for key in starts if not key.endswith("dependent")) < min(
        ends[key] for key in ends if not key.endswith("dependent")
    )
    assert len([row for row in rows if row["event"] == "publication-review"]) >= 3
    canonical = git(case.remote, "rev-parse", "refs/heads/main")
    for unit in result.units.values():
        assert unit.evidence["merge"]["canonical_head"]
        assert unit.bindings["pr"]["number"] in case.service.pulls
        from bmad_loop.campaign_action_history import actions

        for action in actions(result, unit=unit.id):
            if action.unit == unit.id and action.kind in {"push", "checkpoint"}:
                assert verify.is_ancestor(case.remote, action.receipt["head"], canonical)
    assert len(case.service.pulls) == 3
    assert all(pr["merged"] for pr in case.service.pulls.values())


def test_lost_draft_create_ack_does_not_launch_duplicate_pr_or_worker(production_case):
    case = production_case
    case.service.drop_create_reply = True
    result = run_case(case)
    assert result.phase == "delivered", diagnose(result)
    assert len(case.service.pulls) == 3
    rows = [json.loads(line) for line in case.events.read_text().splitlines()]
    assert len([row for row in rows if row["event"] == "execution-start"]) == 3


def test_lost_merge_ack_reconciles_remote_merge_without_repeating_delivery(production_case):
    case = production_case
    case.service.drop_merge_reply = True
    result = run_case(case)
    assert result.phase == "delivered", diagnose(result)
    assert len(case.service.merge_requests) == 3
    assert len(case.service.pulls) == 3
    assert all(unit.state == UnitState.DELIVERED for unit in result.units.values())


def test_moving_main_reintegrates_and_rereviews_before_publication(production_case):
    case = production_case
    case.service.move_before_first_merge = True
    result = run_case(case)
    assert result.phase == "delivered", diagnose(result)
    assert case.service.moved_main
    rows = [json.loads(line) for line in case.events.read_text().splitlines()]
    reviews = [row for row in rows if row["event"] == "publication-review"]
    assert len(reviews) > 3
    for unit in result.units.values():
        reviewed = [row for row in reviews if row["unit"] == unit.id]
        assert reviewed[-1]["base"] == case.service.merge_bases[unit.bindings["pr"]["number"]]
        assert reviewed[-1]["head"] == unit.evidence["integrated"]["head"]


def test_lost_child_launch_ack_adopts_existing_process_without_relaunch(
    production_case, monkeypatch
):
    case = production_case
    original = campaign_runtime.spawn
    lost = []

    def spawn_with_lost_ack(request_path, *, env=None):
        process = original(request_path, env=env)
        request = json.loads(request_path.read_text())
        if request["kind"] == "execution" and not lost:
            lost.append(request["launch_id"])
            raise RuntimeError("Child started but launch acknowledgement was lost")
        return process

    monkeypatch.setattr(campaign_runtime, "spawn", spawn_with_lost_ack)
    result = run_case(case)
    assert result.phase == "delivered", diagnose(result)
    assert len(lost) == 1
    rows = [json.loads(line) for line in case.events.read_text().splitlines()]
    assert len([row for row in rows if row["event"] == "execution-start"]) == 3
    assert len(case.service.pulls) == 3


def test_tracking_debt_blocks_only_affected_dependent_until_reconciled(
    production_case, monkeypatch
):
    case = production_case
    case.service.delay_tracking_for = "source:project:first"
    original_proposals = case.runtime.proposals

    def observer(snapshot):
        if (
            snapshot.units["source:project:second"].state == UnitState.DELIVERED
            and snapshot.units["source:project:first"].state == UnitState.MERGED_PENDING_SYNC
        ):
            case.store.enqueue(snapshot.id, "stop", request_id="observed-independent-delivery")
        return original_proposals(snapshot)

    # An operator pauses once unrelated delivery is observed. Waiting thousands
    # of extra ticks adds no overlap evidence and only repeats the held API read.
    with monkeypatch.context() as patch:
        patch.setattr(case.runtime, "proposals", observer)
        pending = run_case(case)
    assert pending.phase == "stopped", diagnose(pending)
    assert pending.units["source:project:first"].state == UnitState.MERGED_PENDING_SYNC, diagnose(
        pending
    )
    assert pending.units["source:project:second"].state == UnitState.DELIVERED, diagnose(pending)
    assert pending.units["source:project:dependent"].state in {
        UnitState.PENDING,
        UnitState.PLANNING,
        UnitState.DRAFT,
        UnitState.READY,
    }
    rows = [json.loads(line) for line in case.events.read_text().splitlines()]
    assert not any(
        row["event"] == "execution-start" and row["unit"].endswith("dependent") for row in rows
    )
    case.service.tracking_released = True
    completed = run_case(case)
    assert completed.phase == "delivered", diagnose(completed)
    assert len(case.service.pulls) == 3


@pytest.mark.parametrize("production_case", [{"planning": True}], indirect=True)
def test_brief_to_reviewed_planning_pr_activates_new_story_then_parallel_delivery(production_case):
    case = production_case
    result = run_case(case)
    assert result.phase == "delivered", diagnose(result)
    assert "source:project:planned" in result.units
    assert result.units["source:project:planned"].state == UnitState.DELIVERED
    plan = [unit for unit in result.units.values() if unit.spec.kind == "planning"]
    assert len(plan) == 1
    assert plan[0].state == UnitState.DELIVERED
    assert plan[0].evidence["planning_flow"]["state"] == "activated"
    assert len(case.service.pulls) == 5
    rows = [json.loads(line) for line in case.events.read_text().splitlines()]
    phases = [row["event"] for row in rows if row["event"].startswith("planning-")]
    assert phases == [
        "planning-requirements",
        "planning-architecture",
        "planning-decomposition",
        "planning-planning_review",
    ]
    stage_requests = [
        json.loads(path.read_text())
        for path in case.store.directory(result.id).rglob("request.json")
    ]
    native_requests = [row for row in stage_requests if row.get("native_planning_skill")]
    assert {row["stage"] for row in native_requests} == {
        "requirements",
        "architecture",
        "decomposition",
        "planning_review",
    }
    for row in native_requests:
        assert row["native_planning_skill"]["name"] == "fixture-" + row["stage"].replace("_", "-")
        assert "# Campaign planning" in row["prompt"]
        assert "Never invent CLI flags" in row["prompt"]
    assert len([row for row in rows if row["event"] == "execution-start"]) == 4
    planned_source = git(case.remote, "show", "refs/heads/main:specs/epic/stories/planned-story.md")
    assert verify.status_of(verify.parse_frontmatter(planned_source)) == "done"

    assert "planning-revision-marker:first" in git(
        case.remote, "show", "refs/heads/main:specs/epic/stories/first-story.md"
    )
    assert result.units["source:project:first"].spec.acceptance == (
        "Accept first with planning marker",
    )
