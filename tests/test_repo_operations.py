"""Repository workflow through real qualified child transport; zero LLM tokens."""

from __future__ import annotations

import copy
import json
import sys
import time
from dataclasses import asdict, replace
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from test_campaign_headless import ChildMux, fixture_capability

from bmad_loop import cli
from bmad_loop.campaign_capacity import CapacityObservation
from bmad_loop.campaign_github import ApiResponse, GithubGateway, UncertainMutation
from bmad_loop.campaign_headless import HeadlessRunner, file_digest
from bmad_loop.campaign_jobs import digest
from bmad_loop.campaign_model import CampaignError, RoleCandidate
from bmad_loop.repo_actions import readback, semantic_details, validate_action, validate_review
from bmad_loop.repo_inventory import RepoInventory, intent_blocks
from bmad_loop.repo_jobs import RepoJobs
from bmad_loop.repo_policy import RepoPolicy, load_policy, policy_from_dict
from bmad_loop.repo_runtime import RepoRuntime
from bmad_loop.repo_store import RepoStore


class Github:
    def __init__(self):
        self.rows = {
            n: {
                "id": 100 + n,
                "number": n,
                "title": "same obligation",
                "body": "Preserve the exact requirement",
                "state": "open",
                "updated_at": "2026-11-01T00:00:00Z",
                "labels": [],
                "comments": 0,
                "html_url": f"https://github.com/a/b/issues/{n}",
            }
            for n in (1, 2)
        }
        self.comments = {1: [], 2: []}
        self.milestones = {}
        self.requests = []
        self.lose_ack = False

    def request(self, method, path, payload=None):
        self.requests.append((method, path, copy.deepcopy(payload)))
        clean = urlsplit(path).path
        if method == "GET":
            if clean == "repos/a/b":
                data = {"id": 9, "full_name": "a/b"}
            elif clean == "repos/a/b/issues":
                data = list(self.rows.values())
            elif clean == "repos/a/b/pulls":
                data = []
            elif clean == "repos/a/b/milestones":
                data = list(self.milestones.values())
            else:
                number = int(clean.split("/")[4])
                if "/milestones/" in clean:
                    data = self.milestones[number]
                elif clean.endswith("/comments"):
                    data = self.comments[number]
                elif clean.endswith("/events"):
                    data = []
                elif clean.endswith("/labels"):
                    data = self.rows[number]["labels"]
                else:
                    data = self.rows[number]
            return ApiResponse(200, copy.deepcopy(data), {})
        number = int(clean.split("/")[4])
        if clean.endswith("/comments"):
            data = {"id": 500 + len(self.comments[number]), "body": payload["body"]}
            self.comments[number].append(data)
            self.rows[number]["comments"] += 1
        elif clean.endswith("/labels"):
            self.rows[number]["labels"] += [{"name": name} for name in payload["labels"]]
            data = self.rows[number]["labels"]
        else:
            self.rows[number].update(payload)
            if "milestone" in payload:
                self.rows[number]["milestone"] = self.milestones[payload["milestone"]]
            data = self.rows[number]
        self.rows[number]["updated_at"] = "2026-11-02T00:00:00Z"
        if self.lose_ack:
            self.lose_ack = False
            raise UncertainMutation("lost acknowledgement")
        return ApiResponse(201, copy.deepcopy(data), {})


@pytest.fixture
def routes(tmp_path):
    caps = {}
    candidates = {}
    for role, model, family in (
        ("proposer", "gpt-test", "openai"),
        ("reviewer", "claude-test", "anthropic"),
    ):
        script = tmp_path / f"{role}.py"
        script.write_text(
            """import json,sys
text=sys.stdin.read()
source=next(json.loads(line) for line in text.splitlines() if line.startswith('{"source_id"'))
value=json.loads(source['content'])
if 'shard' in value:
    actions=[]
    if 'issue:2' in value['shard']:
        if 'milestone:3' in value.get('index', {}):
            actions=[dict(kind='assign_milestone',source='issue:2',target='milestone:3',labels=[],reason='Existing milestone organizes this unassigned obligation')]
        else:
            actions=[dict(kind='close_duplicate',source='issue:2',target='issue:1',labels=[],reason='All original requirements and discussion are retained at the open successor')]
    result=dict(actions=actions,planning_required=[])
else:
    result=dict(accepted=True,action_digest=value['action_digest'],evidence_digest=value['evidence_digest'],covered_blocks=list(value['intent_blocks']),reason='Exact obligations preserved')
print(json.dumps(dict(type='system',model=sys.argv[1],session_id='scripted-repo')))
print(json.dumps(dict(type='result',structured_output=result,usage=dict(input_tokens=100,output_tokens=30))))
""",
            encoding="utf-8",
        )
        candidate = RoleCandidate(
            "codex" if role == "proposer" else "claude", model, "high", family
        )
        binary = Path(sys.executable).resolve()
        cap = {
            "schema_version": 1,
            "platform": sys.platform,
            "profile": candidate.profile,
            "model": model,
            "protocol": "bmad-test-v1",
            "cli_version": "fixture-v1",
            "binary": str(binary),
            "binary_sha256": file_digest(binary),
            "dependencies": [{"path": str(script), "sha256": file_digest(script)}],
            "argv": [str(binary), str(script), model],
            "efforts": ["high"],
            "permissions": "no-tools",
            "process_containment": "foreground-contained",
            "input_mode": "text-stdin",
            "tokenizer": "utf8-byte-upper-bound",
            "context_tokens": 200000,
            "input_tokens": 100000,
            "output_tokens": 4096,
            "reserved_tokens": 4096,
            "max_turns": 1,
        }
        caps[role] = fixture_capability(tmp_path, cap, candidate)
        candidates[role] = candidate
    return caps, candidates


@pytest.fixture
def workflow(project, routes, monkeypatch):
    project = project.project
    caps, candidates = routes
    policy = RepoPolicy(
        "a/b",
        "/qualified/capabilities.json",
        candidates["proposer"],
        candidates["reviewer"],
        "owner",
        labels=("organized",),
    )
    transport = Github()
    gateway = GithubGateway(transport, "a/b")
    store = RepoStore(project)
    state = store.create(json.loads(json.dumps(asdict(policy))), caps, digest(asdict(policy)))
    collector = RepoInventory(project, gateway, policy)
    real_git = collector._git

    def git(*args):
        if args[:2] == ("remote", "get-url"):
            return "https://github.com/a/b.git"
        if args[0] == "ls-remote":
            return ""
        return real_git(*args)

    monkeypatch.setattr(collector, "_git", git)
    runner = HeadlessRunner(ChildMux())

    def capacity(*args):
        now = time.time()
        return CapacityObservation(
            True, slots=1, model_access=True, observed_at=now, expires_at=now + 60
        )

    jobs = RepoJobs(store, policy, runner=runner, capacity=capacity)
    runtime = RepoRuntime(store, state, gateway=gateway, inventory=collector, jobs=jobs)
    yield runtime, transport
    for process in runner.mux.processes.values():
        process.wait(timeout=10)


def test_actual_scripted_audit_review_reconcile_and_retained_history(workflow):
    runtime, transport = workflow
    runtime.audit()
    assert runtime.state["phase"] == "ready", runtime.state
    assert len(runtime.state["jobs"]) == 2
    assert all(row["state"] == "completed" for row in runtime.state["jobs"].values())
    assert not any(method != "GET" for method, _, _ in transport.requests)
    assert runtime.state["actions"][0]["state"] == "approved", runtime.state["actions"]
    runtime.reconcile()
    assert runtime.state["phase"] == "complete", runtime.state
    assert transport.rows[2]["state"] == "closed"
    assert transport.rows[1]["state"] == "open"
    assert "Preserve the exact requirement" in transport.comments[1][0]["body"]
    assert runtime.state["mutations"] == 2 and runtime.state["closures"] == 1
    assert runtime.store.load(runtime.state["id"])["actions"] == runtime.state["actions"]
    history = runtime.store.history(runtime.state["id"])
    assert len([row for row in history if row["kind"] == "action-submitting"]) == 2
    assert all(
        not any(process.poll() is None for process in runtime.jobs.runner.mux.processes.values())
        for _ in [0]
    )


def approved(workflow):
    runtime, transport = workflow
    # Seam tests obtain approval through real proposal/review jobs, never by
    # manufacturing a state-only authorization receipt.
    runtime.audit()
    assert runtime.state["actions"][0]["state"] == "approved"
    return runtime, transport


def test_unknown_post_readback_does_not_replay_and_finishes_same_action(workflow):
    runtime, transport = approved(workflow)
    transport.lose_ack = True
    runtime.reconcile()
    assert runtime.state["phase"] == "held"
    assert runtime.state["actions"][0]["steps"][0]["state"] == "sent_unknown"
    runtime.reconcile()
    assert runtime.state["phase"] == "complete"
    assert len([row for row in transport.requests if row[0] == "POST"]) == 1


@pytest.mark.parametrize("change", ["body", "updated_at", "protected", "identity"])
def test_fresh_changed_object_prevents_all_mutations(workflow, change):
    runtime, transport = approved(workflow)
    if change == "protected":
        transport.rows[2]["labels"] = [{"name": "bmad:hold"}]
    elif change == "identity":
        transport.rows[2]["id"] = 999
    else:
        transport.rows[2][change] = "new owner content"
    runtime.reconcile()
    assert runtime.state["actions"][0]["state"] == "held"
    assert not any(row[0] != "GET" for row in transport.requests)


def test_stop_then_resume_preserves_job_and_action_identity(workflow):
    runtime, transport = workflow
    runtime.store.stop(runtime.state["id"])
    runtime.audit()
    assert runtime.state["phase"] == "stopped" and not runtime.state["jobs"]
    runtime.store.stop(runtime.state["id"], False)
    runtime.audit()
    job_ids = set(runtime.state["jobs"])
    runtime.store.stop(runtime.state["id"])
    runtime.reconcile()
    assert runtime.state["phase"] == "stopped"
    assert not any(row[0] != "GET" for row in transport.requests)
    runtime.store.stop(runtime.state["id"], False)
    runtime.reconcile()
    assert runtime.state["phase"] == "complete" and set(runtime.state["jobs"]) == job_ids


def test_budget_has_no_partial_closure_after_exhaustion(workflow):
    runtime, transport = approved(workflow)
    runtime.policy = replace(runtime.policy, max_mutations=1, max_closures=1)
    runtime.reconcile()
    assert runtime.state["mutations"] == 1
    assert transport.rows[2]["state"] == "open"
    assert runtime.state["actions"][0]["state"] == "held"


@pytest.mark.parametrize("bad", [0, -1, True, 1.5, "100"])
def test_policy_refuses_invalid_budget(bad):
    policy = RepoPolicy(
        "a/b",
        "/cap.json",
        RoleCandidate("codex", "gpt-test", "high", "openai"),
        RoleCandidate("claude", "claude-test", "high", "anthropic"),
        "owner",
    )
    raw = asdict(policy)
    raw["max_mutations"] = bad
    with pytest.raises(CampaignError):
        policy_from_dict(raw)


def test_full_public_cli_parser_remains_usable(capsys):
    with pytest.raises(SystemExit) as raised:
        cli.main(["--help"])
    assert raised.value.code == 0
    assert "repo" in capsys.readouterr().out
    with pytest.raises(SystemExit) as raised:
        cli.main(["repo", "audit", "--help"])
    assert raised.value.code == 0
    assert "--policy" in capsys.readouterr().out


def test_status_history_stop_and_stale_revision_through_real_cli(workflow, capsys):
    runtime, _ = workflow
    project, identity = str(runtime.store.project), runtime.state["id"]
    assert cli.main(["repo", "status", identity, "--project", project, "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["id"] == identity and report["revision"] == 1
    assert (
        cli.main(
            [
                "repo",
                "reconcile",
                identity,
                "--project",
                project,
                "--expected-revision",
                "42",
                "--json",
            ]
        )
        == 1
    )
    assert not capsys.readouterr().out
    assert cli.main(["repo", "stop", identity, "--project", project, "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["stop_requested"]
    assert cli.main(["repo", "history", identity, "--project", project, "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["events"][-1]["kind"] == "stop-requested"


@pytest.fixture
def decision():
    transport = Github()
    source = {
        "kind": "issue",
        "record": transport.rows[2],
        "comments": [{"id": 1, "body": "Do not lose this owner request"}],
        "events": [],
    }
    target = {"kind": "issue", "record": transport.rows[1], "comments": [], "events": []}
    action = {
        "kind": "close_duplicate",
        "source": "issue:2",
        "target": "issue:1",
        "labels": [],
        "reason": "Duplicate intent preserved",
    }
    evidence = {"source": source, "target": target, "catalog_complete": True, "pulls": []}
    policy = RepoPolicy(
        "a/b",
        "/cap.json",
        RoleCandidate("codex", "gpt-test", "high", "openai"),
        RoleCandidate("claude", "claude-test", "high", "anthropic"),
        "owner",
    )
    review = {
        "accepted": True,
        "action_digest": digest(action),
        "evidence_digest": digest(evidence),
        "covered_blocks": list(intent_blocks(source)),
        "reason": "verified",
    }
    return action, evidence, policy, review


@pytest.mark.parametrize(
    "change",
    ["missing_block", "duplicate_block", "foreign_block", "action", "evidence", "rejected"],
)
def test_exact_independent_coverage_refusals(decision, change):
    action, evidence, _, review = decision
    if change == "missing_block":
        review["covered_blocks"].pop()
    elif change == "duplicate_block":
        review["covered_blocks"].append(review["covered_blocks"][0])
    elif change == "foreign_block":
        review["covered_blocks"][0] = "invented"
    elif change == "action":
        review["action_digest"] = "other"
    elif change == "evidence":
        review["evidence_digest"] = "other"
    else:
        review["accepted"] = False
    with pytest.raises(CampaignError, match="Independent review"):
        validate_review(action, evidence, review)


@pytest.mark.parametrize(
    "change", ["incomplete", "protected", "target_protected", "open_pr", "foreign_label", "delete"]
)
def test_deterministic_action_authority(decision, change):
    action, evidence, policy, _ = decision
    if change == "incomplete":
        evidence["catalog_complete"] = False
    elif change in {"protected", "target_protected"}:
        evidence["source" if change == "protected" else "target"]["record"]["labels"] = [
            {"name": "bmad:hold"}
        ]
    elif change == "open_pr":
        evidence["pulls"] = [{"record": {"state": "open", "body": "Implements #2"}}]
    elif change == "foreign_label":
        action.update(kind="label", target="", labels=["unauthorized"])
    else:
        action["kind"] = "delete"
    with pytest.raises(CampaignError):
        validate_action(action, evidence, policy)


@pytest.mark.parametrize(
    "proof",
    [
        None,
        {"complete": False},
        {"complete": True, "provider": {}},
        {"complete": True, "provider": {"name": "native"}, "services": []},
    ],
)
def test_existing_pr_without_positive_release_is_held(decision, proof):
    action, evidence, policy, _ = decision
    evidence["source"]["kind"] = "pr"
    with pytest.raises(CampaignError, match="release|release proof"):
        validate_action(action, evidence, policy, owners=proof)


def test_milestone_requires_complete_closed_members(decision):
    action, evidence, policy, _ = decision
    action.update(kind="close_completed_milestone", target="")
    evidence.update(target=None)
    evidence["source"].update(kind="milestone", members=[{"state": "closed"}])
    evidence["source"]["record"].update(closed_issues=1, open_issues=0)
    validate_action(action, evidence, policy)
    evidence["source"]["members"].append({"state": "open"})
    with pytest.raises(CampaignError, match="membership"):
        validate_action(action, evidence, policy)


def test_forged_marker_cannot_be_stripped_from_fresh_evidence(decision):
    _, evidence, _, _ = decision
    old = evidence["source"]
    changed = copy.deepcopy(old)
    changed["comments"].append({"id": 77, "body": "<!-- bmad-repo:fake:fake --> Owner requirement"})
    assert semantic_details(changed, []) != semantic_details(old, [])
    assert semantic_details(changed, [{"id": 77, "body": "different"}]) != semantic_details(old, [])


def test_unknown_absence_never_confirms_an_effect(decision):
    transport = Github()
    gateway = GithubGateway(transport, "a/b")
    intent = {
        "kind": "repo_comment",
        "path": "repos/a/b/issues/1/comments",
        "marker": "unique",
        "payload": {"body": "unique body"},
        "coordinates": {"object_path": "repos/a/b/issues/2", "id": 102},
    }
    assert readback(intent, gateway) is None
    transport.comments[1].append({"id": 5, "body": "unique body"})
    assert readback(intent, gateway)["id"] == 5
    transport.rows[2]["id"] = 900
    with pytest.raises(CampaignError, match="identity"):
        readback(intent, gateway)


def test_policy_load_validates_real_qualified_routes_and_family_separation(tmp_path, routes):
    caps, candidates = routes
    capability_path = tmp_path / "capabilities.json"
    capability_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "routes": {cap["profile"] + ":" + cap["model"]: cap for cap in caps.values()},
            }
        ),
        encoding="utf-8",
    )
    policy = RepoPolicy(
        "a/b", str(capability_path), candidates["proposer"], candidates["reviewer"], "owner"
    )
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(asdict(policy)), encoding="utf-8")
    raw, selected, sha = load_policy(path)
    assert selected == caps and sha == digest(raw)
    path.write_text(
        json.dumps(asdict(replace(policy, reviewer=candidates["proposer"]))), encoding="utf-8"
    )
    with pytest.raises(CampaignError, match="distinct"):
        load_policy(path)


def test_cas_and_immutable_evidence_are_real_sqlite_authority(workflow):
    runtime, _ = workflow
    old = copy.deepcopy(runtime.state)
    runtime.store.save(runtime.state, "changed")
    with pytest.raises(CampaignError, match="revision"):
        runtime.store.save(old, "stale")
    reference = runtime.store.artifact(runtime.state["id"], "proof", {"a": 1})
    Path(reference["path"]).write_text('{"a": 2}', encoding="utf-8")
    with pytest.raises(CampaignError, match="changed"):
        runtime.store.read(reference)


def test_batch_runs_independent_shards_with_one_durable_writer_and_shared_limit(
    workflow, monkeypatch
):
    runtime, _ = workflow
    jobs = runtime.jobs
    jobs.policy = replace(jobs.policy, max_sessions=2)

    def capacity(*args):
        now = time.time()
        return CapacityObservation(
            True, slots=3, model_access=True, observed_at=now, expires_at=now + 60
        )

    jobs.capacity = capacity
    mux = jobs.runner.mux
    start = mux.new_window
    peaks = []

    def tracked(*args):
        result = start(*args)
        peaks.append(sum(process.poll() is None for process in mux.processes.values()))
        return result

    monkeypatch.setattr(mux, "new_window", tracked)
    from bmad_loop.repo_actions import PROPOSAL_SCHEMA

    tasks = [
        (f"shard-{number}", "proposer", {"shard": {}}, "Inspect this shard", PROPOSAL_SCHEMA)
        for number in range(5)
    ]
    results = jobs.run_many(runtime.state, tasks)
    assert len(results) == 5 and max(peaks) == 2
    assert len(runtime.store.load(runtime.state["id"])["jobs"]) == 5


def test_unknown_capacity_wait_resumes_without_spending_native_attempt(workflow):
    runtime, _ = workflow
    capacity = runtime.jobs.capacity
    runtime.jobs.capacity = lambda *args: CapacityObservation(False, reason="quota unknown")
    runtime.audit()
    assert runtime.state["phase"] == "capacity_wait"
    assert not runtime.jobs.runner.mux.processes
    first = list(runtime.state["jobs"].values())[0]
    assert first["attempt"] == 1
    runtime.jobs.capacity = capacity
    runtime.state["resume_epoch"] = 1
    runtime.audit()
    assert runtime.state["phase"] == "ready"
    assert first["attempt"] == 1


def test_quiescent_quota_attempts_preserved_and_capped_across_resume(workflow, monkeypatch):
    runtime, _ = workflow
    original = runtime.jobs.runner.observe

    def quota(path):
        result = original(path)
        if result["state"] == "finished":
            result["receipt"]["outcome"] = "quota"
        return result

    monkeypatch.setattr(runtime.jobs.runner, "observe", quota)
    runtime.audit()
    row = list(runtime.state["jobs"].values())[0]
    first_path = row["path"]
    assert runtime.state["phase"] == "capacity_wait" and row["attempt"] == 1
    runtime.state["resume_epoch"] = 1
    runtime.audit()
    assert runtime.state["phase"] == "capacity_wait" and row["attempt"] == 2
    assert row["path"] != first_path and row["history"][0]["path"] == first_path
    runtime.state["resume_epoch"] = 2
    runtime.audit()
    assert runtime.state["phase"] == "held" and row["attempt"] == 2
    assert len(runtime.jobs.runner.mux.processes) == 2


@pytest.mark.parametrize("models", [["gpt-test", "unknown-author"], ["gpt-test", "claude-test"]])
def test_auxiliary_author_debt_preserved_without_granting_independent_review(
    workflow, monkeypatch, models
):
    runtime, transport = workflow
    original = runtime.jobs.runner.observe

    def changed(path):
        result = original(path)
        if result["state"] == "finished":
            result["receipt"]["observed_models"] = models
        return result

    monkeypatch.setattr(runtime.jobs.runner, "observe", changed)
    runtime.audit()
    assert runtime.state["phase"] == "ready"
    assert all(row["state"] == "completed" for row in runtime.state["jobs"].values())
    assert runtime.state["actions"][0]["state"] == "held"
    assert not any(method != "GET" for method, _, _ in transport.requests)


def test_known_auxiliary_proposer_succeeds_with_independent_reviewer(workflow, monkeypatch):
    runtime, _ = workflow
    original = runtime.jobs.runner.observe

    def changed(path):
        result = original(path)
        if result["state"] == "finished" and result["receipt"]["observed_model"] == "gpt-test":
            result["receipt"]["observed_models"] = ["gpt-test", "gemini-test"]
        return result

    monkeypatch.setattr(runtime.jobs.runner, "observe", changed)
    runtime.audit()
    assert runtime.state["actions"][0]["state"] == "approved"
    proposer = next(row for row in runtime.state["jobs"].values() if row["role"] == "proposer")
    assert proposer["families"] == ["google", "openai"]


def test_reviewer_auxiliary_cannot_review_its_own_family_work(workflow, monkeypatch):
    runtime, _ = workflow
    original = runtime.jobs.runner.observe

    def changed(path):
        result = original(path)
        if result["state"] == "finished" and result["receipt"]["observed_model"] == "claude-test":
            result["receipt"]["observed_models"] = ["claude-test", "gpt-test"]
        return result

    monkeypatch.setattr(runtime.jobs.runner, "observe", changed)
    runtime.audit()
    assert runtime.state["actions"][0]["state"] == "held"
    assert "intersects" in runtime.state["actions"][0]["reason"]


def test_unknown_primary_native_model_refuses_proposal(workflow, monkeypatch):
    runtime, _ = workflow
    original = runtime.jobs.runner.observe

    def changed(path):
        result = original(path)
        if result["state"] == "finished":
            result["receipt"]["observed_model"] = "unknown-primary"
        return result

    monkeypatch.setattr(runtime.jobs.runner, "observe", changed)
    runtime.audit()
    assert runtime.state["phase"] == "held" and not runtime.state["actions"]


def test_existing_milestone_assignment_through_independent_native_review(workflow):
    runtime, transport = workflow
    transport.milestones[3] = {
        "id": 303,
        "number": 3,
        "title": "Existing milestone",
        "state": "open",
        "updated_at": "2026-11-01T00:00:00Z",
        "html_url": "https://github.com/a/b/milestone/3",
        "open_issues": 0,
        "closed_issues": 0,
    }
    runtime.audit()
    assert runtime.state["actions"][0]["state"] == "approved", runtime.state["actions"]
    runtime.reconcile()
    assert runtime.state["phase"] == "complete", runtime.state
    assert transport.rows[2]["milestone"]["id"] == 303
    assert transport.rows[2]["body"] == "Preserve the exact requirement"
    assert runtime.state["mutations"] == 1 and runtime.state["closures"] == 0


def test_milestone_assignment_cannot_replace_existing_owner_choice(decision):
    action, evidence, policy, _ = decision
    action.update(kind="assign_milestone", target="milestone:3")
    evidence["target"].update(kind="milestone")
    evidence["source"]["record"]["milestone"] = {"number": 4}
    with pytest.raises(CampaignError, match="unassigned"):
        validate_action(action, evidence, policy)


def test_real_cli_audit_and_reconcile_use_qualified_child_workflow(
    workflow, tmp_path, monkeypatch, capsys
):
    from bmad_loop import repo_cli

    original, transport = workflow
    manifest = tmp_path / "routes.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "routes": {
                    cap["profile"] + ":" + cap["model"]: cap
                    for cap in original.state["capabilities"].values()
                },
            }
        ),
        encoding="utf-8",
    )
    policy = replace(original.policy, capabilities=str(manifest))
    policy_path = tmp_path / "repo-policy.json"
    policy_path.write_text(json.dumps(asdict(policy)), encoding="utf-8")

    def runtime(store, state):
        jobs = RepoJobs(store, policy, runner=original.jobs.runner, capacity=original.jobs.capacity)
        return RepoRuntime(
            store, state, gateway=original.gateway, inventory=original.inventory, jobs=jobs
        )

    monkeypatch.setattr(repo_cli, "RepoRuntime", runtime)
    project = str(original.store.project)
    assert (
        cli.main(["repo", "audit", "--project", project, "--policy", str(policy_path), "--json"])
        == 0
    )
    report = json.loads(capsys.readouterr().out)
    assert report["phase"] == "ready" and report["id"] != original.state["id"]
    assert all(method == "GET" for method, _, _ in transport.requests)
    assert (
        cli.main(
            [
                "repo",
                "reconcile",
                report["id"],
                "--project",
                project,
                "--expected-revision",
                str(report["revision"]),
                "--json",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["phase"] == "complete"


def test_stop_arriving_during_fresh_observation_wins_admission(workflow, monkeypatch):
    runtime, transport = approved(workflow)
    fresh = runtime._fresh

    def stop_after_read(row):
        value = fresh(row)
        runtime.store.stop(runtime.state["id"])
        return value

    monkeypatch.setattr(runtime, "_fresh", stop_after_read)
    runtime.reconcile()
    assert runtime.state["phase"] == "stopped"
    assert runtime.state["mutations"] == 0
    assert not any(method != "GET" for method, _, _ in transport.requests)


@pytest.mark.parametrize(
    "field",
    [
        "liveness",
        "ownership",
        "fence_revision",
        "binding_revision",
        "head",
        "pr_node_id",
        "admissions_open",
        "active_workers",
    ],
)
def test_exact_writer_release_refuses_each_missing_coordinate(decision, project, field):
    from bmad_loop.verify import git_bytes

    action, evidence, policy, _ = decision
    head = git_bytes(project.project, "rev-parse", "HEAD").stdout.decode().strip()
    for name in ("source", "target"):
        evidence[name].update(kind="pr")
        evidence[name]["record"].update(
            head={"sha": head, "ref": "owned"},
            base={"sha": head, "ref": "main"},
            node_id="native-pr",
        )
    details = {
        "repository": "a/b",
        "pr_number": 2,
        "pr_node_id": "native-pr",
        "head": head,
        "base": head,
        "branch": "owned",
        "fence_revision": "fence1",
        "binding_revision": "binding1",
        "admissions_open": False,
        "active_workers": 0,
        "preserved_clean": True,
        "resumable_binding_verified": True,
    }
    actor = {"revision": "actor1", "liveness": "dead", "ownership": "unowned", "details": details}
    owners = {
        "complete": True,
        "provider": {"name": "installed"},
        "services": [{"items": [actor]}],
        "items": [actor],
    }
    validate_action(action, evidence, policy, owners=owners, project=project.project)
    if field in {"liveness", "ownership"}:
        actor[field] = "unknown"
    else:
        details.pop(field)
    with pytest.raises(CampaignError, match="released"):
        validate_action(action, evidence, policy, owners=owners, project=project.project)


def test_unknown_launch_is_observed_without_replacement(workflow, monkeypatch):
    runtime, _ = workflow
    runner = runtime.jobs.runner
    actual_start, actual_observe = runner.launch, runner.observe

    def unknown_start(*args, **kwargs):
        actual_start(*args, **kwargs)
        return {"state": "unknown"}

    monkeypatch.setattr(runner, "launch", unknown_start)
    monkeypatch.setattr(runner, "observe", lambda path: {"state": "unknown"})
    runtime.audit()
    assert runtime.state["phase"] == "held"
    runtime.audit()
    assert len(runner.mux.processes) == 1 and not runtime.state["actions"]
    for process in runner.mux.processes.values():
        process.wait(timeout=10)
    monkeypatch.setattr(runner, "launch", actual_start)
    monkeypatch.setattr(runner, "observe", actual_observe)
    runtime.audit()
    assert runtime.state["phase"] == "ready"
    assert len(runner.mux.processes) == 2


@pytest.mark.parametrize("binding", ["direct", "shared"])
def test_native_issue_owner_without_pr_is_retained_and_blocks_only_its_issue(
    workflow, decision, binding
):
    from bmad_loop.campaign_model import CampaignSnapshot, CampaignSpec, CampaignUnit, UnitSpec
    from bmad_loop.campaign_runtime import store_for

    runtime, _ = workflow
    spec = UnitSpec("s:project:u", "s", "epic", "Owned work")
    unit = CampaignUnit(spec)
    snapshot = CampaignSnapshot(
        "repo-owned-fixture",
        "a/b",
        CampaignSpec("Existing", units=(spec,)),
        {spec.id: unit},
        common_dir=str(runtime.store.common),
        root=str(runtime.store.project),
        phase="stopped",
    )
    issue = {
        "kind": "issue",
        "number": 2,
        "database_id": 102,
        "url": "https://github.com/a/b/issues/2",
    }
    if binding == "direct":
        unit.bindings["issue"] = issue
    else:
        snapshot.bindings["github_hierarchy"] = {"story:" + unit.id: issue}
    campaign_store = store_for(runtime.store.project)
    campaign_store.create(snapshot)
    database = campaign_store.root / snapshot.id / "campaign.sqlite"
    before = file_digest(database)
    observed = runtime.inventory.collect()
    entry = next(row for row in observed["owners"]["items"] if row["kind"] == "native-unit")
    assert entry["details"]["shared_issue" if binding == "shared" else "issue"]["number"] == 2
    assert entry["details"]["pr"] is None
    action, evidence, policy, _ = decision
    with pytest.raises(CampaignError, match="campaign owns"):
        validate_action(action, evidence, policy, owners=observed["owners"])
    unowned = copy.deepcopy(evidence)
    unowned["source"]["record"].update(id=107, number=7)
    validate_action({**action, "source": "issue:7"}, unowned, policy, owners=observed["owners"])
    assert file_digest(database) == before


def test_partial_audit_with_approved_subset_cannot_reconcile(workflow):
    runtime, transport = approved(workflow)
    runtime.state.update(audit_complete=False, phase="held", reason="remaining shard not reviewed")
    with pytest.raises(CampaignError, match="Incomplete audit"):
        runtime.reconcile()
    assert not any(method != "GET" for method, _, _ in transport.requests)


@pytest.mark.skipif(
    sys.platform != "darwin", reason="Real read-only fixture uses maintained macOS sandbox"
)
def test_qualified_readonly_route_prepares_outside_its_evidence_workspace(workflow, tmp_path):
    import test_campaign_execution_qualification as writer_module
    import test_campaign_readonly_qualification as readonly_module

    from bmad_loop.campaign_execution_qualification import qualify
    from bmad_loop.campaign_headless import read_json
    from bmad_loop.repo_actions import PROPOSAL_SCHEMA

    runtime, _ = workflow
    cap = runtime.state["capabilities"]["proposer"]
    candidate = runtime.policy.proposer
    base = (None, None, None, candidate, cap)
    writer = writer_module.writer.__wrapped__(base, tmp_path)
    proposal, candidate, _ = readonly_module.readonly.__wrapped__(writer, tmp_path)
    result = qualify(
        proposal,
        tmp_path / "readonly-proof",
        input_bytes=4096,
        output_tokens=4096,
        timeout_s=20,
        runner=HeadlessRunner(ChildMux()),
    )
    assert result["ok"], result
    runtime.state["capabilities"]["proposer"] = read_json(Path(result["capability_path"]))
    _, row = runtime.jobs._prepare(
        runtime.state,
        (
            "readonly",
            "proposer",
            {"shard": {}},
            "Read this complete bounded evidence",
            PROPOSAL_SCHEMA,
        ),
    )
    envelope = read_json(Path(row["path"]))
    workspace = Path(envelope["grants"]["workspace"])
    assert not Path(row["path"]).is_relative_to(workspace)
    assert envelope["permissions"] == "read-only-tools"
    assert envelope["grants"]["scopes"] == []
    assert envelope["grants"]["read_only_paths"] == [envelope["request"]["inputs"][0]["path"]]


def test_large_repository_index_uses_bounded_related_candidates_without_losing_sources():
    from bmad_loop.repo_candidates import batches, wire_size

    objects = {
        f"issue:{number}": {
            "kind": "issue",
            "record": {
                "number": number,
                "id": number,
                "title": f"Obligation unique{number} navigation",
                "body": "See #1999" if number == 1 else "Complete requirement",
                "state": "open",
            },
        }
        for number in range(1, 2001)
    }
    assert wire_size(objects) > 131072
    views = batches(objects, 131072, "a/b")
    assert 1 < len(views) < 128
    sources = [key for view in views for key in view["shard"]]
    assert len(sources) == len(set(sources)) == len(objects)
    assert all(
        wire_size(view["shard"]) <= 131072 // 2 and wire_size(view["index"]) <= 131072 // 5
        for view in views
    )
    first = next(view for view in views if "issue:1" in view["shard"])
    assert "issue:1999" in first["index"]
    assert all(
        view["candidate_selection"]["mode"] == "related_subset_not_exhaustive" for view in views
    )
