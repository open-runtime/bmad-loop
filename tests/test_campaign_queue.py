"""Real synthetic G verification plus durable authenticated required-app checks."""

import copy
import json
import subprocess
import sys
from dataclasses import asdict, replace
from types import SimpleNamespace

import pytest
from conftest import git
from test_campaign_merge_adapter import source

from bmad_loop import policy as policy_mod
from bmad_loop import verify
from bmad_loop.campaign_execution import CampaignExecution
from bmad_loop.campaign_github import ApiResponse, GithubError, GithubGateway, UncertainMutation
from bmad_loop.campaign_merge_adapter import AdapterReceipt
from bmad_loop.campaign_model import (
    CampaignSpec,
    GitHubSpec,
    SourceSelector,
    spec_from_dict,
    spec_to_dict,
)
from bmad_loop.campaign_publication import Candidate, RemoteRules, RequiredCheck
from bmad_loop.campaign_queue import (
    CheckCapability,
    QueueGroup,
    allocate_checkout,
    capability,
    observe_group,
    observe_required_check,
    prepare_check,
    run_verification,
)
from bmad_loop.campaign_workers import common_directory, observe, prepare_launch


@pytest.fixture
def group(project, tmp_path):
    repo = project.project
    base = git(repo, "rev-parse", "HEAD").strip()
    git(repo, "checkout", "-b", "unit-feature")
    (repo / "feature.py").write_text("accepted feature\n")
    git(repo, "add", "feature.py")
    git(repo, "commit", "-qm", "accepted H")
    head = git(repo, "rev-parse", "HEAD").strip()
    git(repo, "checkout", "-b", "queue-base", base)
    (repo / "main.py").write_text("new main work\n")
    git(repo, "add", "main.py")
    git(repo, "commit", "-qm", "current queue B")
    current_base = git(repo, "rev-parse", "HEAD").strip()
    git(repo, "checkout", "-b", "combined-group", head)
    git(repo, "merge", "--no-edit", "queue-base")
    combined = git(repo, "rev-parse", "HEAD").strip()
    remote = tmp_path / "remote.git"
    subprocess.run(
        ["git", "clone", "--bare", str(repo), str(remote)], capture_output=True, check=True
    )
    git(repo, "remote", "add", "queue-test", str(remote))
    candidate = Candidate("a/b", "tickets:project:u", 1, head, base, "source1", 1, "config1")
    group = QueueGroup(
        "a/b", 1, "main", "QE_1", combined, current_base, head, "AWAITING_CHECKS", 100
    )
    return candidate, group


class Transport:
    def __init__(self, group):
        self.group = group
        self.checks = []
        self.calls = []
        self.drop = False
        self.wrong_app = False
        self.fault = False
        self.queued = True
        self.drop_dequeue = False

    def request(self, method, path, payload=None):
        self.calls.append((method, path, copy.deepcopy(payload)))
        if self.fault:
            raise GithubError("temporary API outage", status=503)
        if path == "graphql":
            group = self.group
            if "dequeuePullRequest" in payload.get("query", ""):
                self.queued = False
                if self.drop_dequeue:
                    raise UncertainMutation("dequeue acknowledgement lost")
                return ApiResponse(
                    200,
                    {
                        "data": {
                            "dequeuePullRequest": {
                                "pullRequest": {
                                    "id": "PR_1",
                                    "headRefOid": group.pr_head,
                                    "baseRefName": group.target,
                                    "mergeQueueEntry": None,
                                }
                            }
                        }
                    },
                    {},
                )
            return ApiResponse(
                200,
                {
                    "data": {
                        "repository": {
                            "nameWithOwner": group.repository,
                            "pullRequest": {
                                "id": "PR_1",
                                "number": group.pr_number,
                                "headRefOid": group.pr_head,
                                "baseRefName": group.target,
                                "merged": False,
                                "state": "OPEN",
                                "mergeQueueEntry": (
                                    {
                                        "id": group.entry_id,
                                        "state": group.state,
                                        "baseCommit": {"oid": group.base},
                                        "headCommit": {"oid": group.head},
                                        "pullRequest": {
                                            "number": group.pr_number,
                                            "headRefOid": group.pr_head,
                                            "baseRefName": group.target,
                                        },
                                    }
                                    if self.queued
                                    else None
                                ),
                            },
                        }
                    }
                },
                {},
            )
        if method == "GET" and "/pulls/" in path:
            return ApiResponse(
                200,
                {
                    "number": self.group.pr_number,
                    "node_id": "PR_1",
                    "merged": False,
                    "state": "open",
                    "head": {"sha": self.group.pr_head},
                    "base": {"ref": self.group.target},
                },
                {},
            )
        if method == "POST" and path.endswith("/check-runs"):
            row = {**payload, "id": len(self.checks) + 1, "app": {"id": 8 if self.wrong_app else 7}}
            self.checks.append(row)
            if self.drop:
                raise UncertainMutation("check POST acknowledgement lost")
            return ApiResponse(201, row, {})
        if method == "GET" and "/check-runs" in path:
            return ApiResponse(200, {"check_runs": self.checks}, {})
        raise AssertionError((method, path))


def local_receipt(project, tmp_path, group):
    candidate, queued = group
    workspace = allocate_checkout(
        project.project,
        tmp_path / "worktrees",
        tmp_path / "allocations",
        queued,
        remote="queue-test",
    )
    script = tmp_path / "queue-verifier.py"
    script.write_text(source())
    request = {
        "candidate": asdict(candidate),
        "queue_group": asdict(queued),
        "workspace": str(workspace),
        "common_dir": str(common_directory(project.project)),
        "commands": [sys.executable, str(script)],
        "check_context": "bmad-combined",
        "rules_fingerprint": "rules1",
    }
    result = run_verification(
        request, SimpleNamespace(unit_id=candidate.unit_id, directory=tmp_path / "worker-state")
    )
    assert result["outcome"] == "queue_verified"
    return AdapterReceipt.from_dict(result["verification"]), request, workspace


def producer():
    return CheckCapability(
        True,
        "a/b",
        "bmad-combined",
        7,
        "native",
        100,
        producer="qualified-app",
        rules_fingerprint="rules1",
    )


def test_authenticated_queue_observation_keeps_accepted_base_and_current_combined_base(group):
    candidate, queued = group
    gateway = GithubGateway(Transport(queued), "a/b")
    observed = observe_group(gateway, candidate, clock=lambda: 100)
    assert observed == queued
    assert queued.base != candidate.base
    assert queued.digest == replace(queued, state="MERGEABLE", observed_at=200).digest
    assert queued.digest != replace(queued, head=candidate.head).digest


def test_ordinary_pr_head_never_substitutes_for_combined_candidate(group):
    candidate, queued = group
    gateway = GithubGateway(Transport(replace(queued, head=candidate.head)), "a/b")
    assert observe_group(gateway, candidate) is None
    with pytest.raises(GithubError, match="identity changed"):
        observe_group(GithubGateway(Transport(replace(queued, target="other")), "a/b"), candidate)


def test_queue_observation_outage_stays_retryable_then_recovers(group):
    candidate, queued = group
    transport = Transport(queued)
    transport.fault = True
    gateway = GithubGateway(transport, "a/b")
    with pytest.raises(GithubError, match="temporary"):
        observe_group(gateway, candidate)
    transport.fault = False
    assert observe_group(gateway, candidate, clock=lambda: 100) == queued


def test_owned_detached_group_checkout_does_not_modify_unit_branch(project, tmp_path, group):
    _, queued = group
    before = git(project.project, "rev-parse", "refs/heads/unit-feature")
    path = allocate_checkout(
        project.project, tmp_path / "worktrees", tmp_path / "alloc", queued, remote="queue-test"
    )
    assert verify.rev_parse_head(path) == queued.head
    assert git(project.project, "rev-parse", "refs/heads/unit-feature") == before
    assert (path / "feature.py").is_file() and (path / "main.py").is_file()
    assert (
        allocate_checkout(
            project.project,
            tmp_path / "worktrees",
            tmp_path / "alloc",
            replace(queued, observed_at=101),
            remote="queue-test",
        )
        == path
    )
    (path / "foreign.txt").write_text("keep operator work")
    with pytest.raises(GithubError, match="checkout identity"):
        allocate_checkout(
            project.project, tmp_path / "worktrees", tmp_path / "alloc", queued, remote="queue-test"
        )
    assert (path / "foreign.txt").read_text() == "keep operator work"


def test_actual_worker_process_verifies_g_with_scripted_zero_token_command(
    project, tmp_path, group
):
    candidate, queued = group
    receipt, fields, workspace = local_receipt(project, tmp_path, group)
    assert receipt.group_head == queued.head and receipt.group_base == queued.base
    assert receipt.candidate.base == candidate.base
    context = CampaignExecution(
        "queue-campaign", candidate.unit_id, 1, 1, 1, tmp_path / "child", policy_mod.loads(""), {}
    )
    path = prepare_launch(
        context,
        {**fields, "kind": "queue_verify", "branch": "unit-feature", "launch_id": "queue-launch"},
    )
    process = subprocess.run(
        [sys.executable, "-m", "bmad_loop.campaign_workers", str(path)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert process.returncode == 0, process.stderr
    request = json.loads(path.read_text())
    observed = observe(context.directory, expected=request)
    assert observed["state"] == "finished", observed
    assert observed["receipt"]["outcome"] == "queue_verified"
    assert observed["receipt"]["head"] == queued.head
    assert not observed["receipt"].get("sessions")


def test_local_g_success_is_not_hosted_check_until_exact_app_readback(project, tmp_path, group):
    candidate, queued = group
    receipt, _, _ = local_receipt(project, tmp_path, group)
    gateway = GithubGateway(Transport(queued), "a/b")
    assert observe_required_check(gateway, queued, "bmad-combined", 7) is None
    intent = prepare_check(
        candidate,
        queued,
        receipt,
        producer(),
        gateway,
        action_id="publish-G",
        generation=2,
        now=100,
    )
    confirmed = gateway.execute(replace(intent, state="sent_unknown"), generation=2)
    assert confirmed.state == "confirmed"
    observed = observe_required_check(gateway, queued, "bmad-combined", 7)
    assert observed["head_sha"] == queued.head and observed["app"]["id"] == 7
    assert observe_required_check(gateway, queued, "bmad-combined", 8) is None


def test_lost_check_post_ack_reconciles_same_external_id_without_duplicate(
    project, tmp_path, group
):
    candidate, queued = group
    receipt, _, _ = local_receipt(project, tmp_path, group)
    transport = Transport(queued)
    transport.drop = True
    gateway = GithubGateway(transport, "a/b")
    intent = prepare_check(
        candidate,
        queued,
        receipt,
        producer(),
        gateway,
        action_id="publish-G",
        generation=2,
        now=100,
    )
    unknown = gateway.execute(replace(intent, state="sent_unknown"), generation=2)
    assert unknown.state == "sent_unknown"
    confirmed = gateway.reconcile_create(unknown)
    assert confirmed.state == "confirmed"
    assert len(transport.checks) == 1
    assert (
        len(
            [
                call
                for call in transport.calls
                if call[0] == "POST" and call[1].endswith("check-runs")
            ]
        )
        == 1
    )
    transport.checks.append(copy.deepcopy(transport.checks[0]))
    with pytest.raises(GithubError, match="multiple check runs"):
        gateway.reconcile_create(unknown)


def test_wrong_app_or_changed_group_cannot_publish_green(project, tmp_path, group):
    candidate, queued = group
    receipt, _, _ = local_receipt(project, tmp_path, group)
    transport = Transport(queued)
    transport.wrong_app = True
    gateway = GithubGateway(transport, "a/b")
    intent = prepare_check(
        candidate,
        queued,
        receipt,
        producer(),
        gateway,
        action_id="publish-G",
        generation=2,
        now=100,
    )
    unknown = gateway.execute(replace(intent, state="sent_unknown"), generation=2)
    assert unknown.state != "confirmed"
    with pytest.raises(GithubError, match="identity conflicts"):
        gateway.reconcile_create(unknown)
    with pytest.raises(GithubError, match="stale or incomplete"):
        prepare_check(
            candidate,
            replace(queued, head="f" * 40),
            receipt,
            producer(),
            gateway,
            action_id="other-G",
            generation=2,
            now=100,
        )


def test_missing_check_post_ack_with_no_matching_remote_check_remains_unknown(
    project, tmp_path, group
):
    candidate, queued = group
    receipt, _, _ = local_receipt(project, tmp_path, group)
    gateway = GithubGateway(Transport(queued), "a/b")
    intent = prepare_check(
        candidate,
        queued,
        receipt,
        producer(),
        gateway,
        action_id="publish-G",
        generation=2,
        now=100,
    )
    unknown = gateway.reconcile_create(replace(intent, state="sent_unknown"))
    assert unknown.state == "sent_unknown"
    assert "no blind duplicate" in unknown.error
    assert not [call for call in gateway.transport.calls if call[0] == "POST"]


def test_producer_probe_needs_authentication_fresh_permission_and_exact_app(project, tmp_path):
    rules = RemoteRules(
        (RequiredCheck("bmad-combined", 7),),
        False,
        True,
        0,
        False,
        False,
        "rules1",
        queue_merge_method="MERGE",
    )
    settings = GitHubSpec(
        merge_mode="queue", candidate_check_context="bmad-combined", candidate_check_app_id=7
    )
    assert capability(settings, "a/b", project.project, rules).ready is False
    raw = {
        "schema_version": 1,
        "repository": "a/b",
        "context": "bmad-combined",
        "app_id": 7,
        "mode": "native",
        "authenticated": True,
        "producer": "actual-app-identity",
        "observed_at": 100,
        "checks_write": True,
    }
    probe = tmp_path / "producer.py"
    probe.write_text("print(" + repr(json.dumps(raw)) + ")\n")
    settings = replace(settings, candidate_producer_probe=(sys.executable, str(probe)))
    assert capability(settings, "a/b", project.project, rules, clock=lambda: 100).ready is True
    assert capability(settings, "a/b", project.project, rules, clock=lambda: 300).ready is False
    assert (
        capability(
            replace(settings, candidate_check_app_id=8),
            "a/b",
            project.project,
            rules,
            clock=lambda: 100,
        ).ready
        is False
    )


def test_queue_producer_settings_roundtrip_and_forbid_history_bypass():
    settings = GitHubSpec(
        merge_mode="queue",
        candidate_command=("qualified-tests",),
        candidate_check_context="combined",
        candidate_check_app_id=7,
        candidate_producer_probe=("producer-status", "--json"),
        candidate_publish="external",
    )
    spec = CampaignSpec(
        "Queue", sources=(SourceSelector("stories", id="s", path="specs"),), github=settings
    )
    assert spec_from_dict(spec_to_dict(spec)) == spec
    raw = spec_to_dict(spec)
    raw["github"]["candidate_check_app_id"] = True
    with pytest.raises(ValueError, match="nonnegative integer"):
        spec_from_dict(raw)


def test_failed_g_dequeues_before_h_repair_and_lost_removal_ack_is_reconciled(
    project, tmp_path, group, monkeypatch
):
    from bmad_loop import campaign_runtime_queue as flow
    from bmad_loop.campaign_model import (
        CampaignSnapshot,
        CampaignUnit,
        UnitSpec,
        UnitState,
        WorkerClaim,
    )
    from bmad_loop.campaign_queue import prepare_dequeue
    from bmad_loop.campaign_runtime import CampaignRuntime
    from bmad_loop.campaign_store import CampaignStore

    candidate, queued = group
    candidate = replace(candidate, config_revision="1")
    receipt, request, workspace = local_receipt(project, tmp_path, (candidate, queued))
    failed_receipt = replace(
        receipt, state="held", hosted_check_pending=False, reason="combined tests failed"
    )
    transport = Transport(queued)
    gateway = GithubGateway(transport, "a/b")
    h_workspace = tmp_path / "owned-h"
    git(project.project, "worktree", "add", str(h_workspace), "unit-feature")
    spec = UnitSpec(candidate.unit_id, "tickets", "e", "Unit", selector="u", scopes=("feature.py",))
    selected = CampaignSpec(
        "Queue",
        units=(spec,),
        github=GitHubSpec(
            repository="a/b",
            merge_mode="queue",
            candidate_check_context="bmad-combined",
            candidate_check_app_id=7,
        ),
        worktree_root=str(tmp_path / "worktrees"),
    )
    unit = CampaignUnit(
        spec,
        UnitState.MERGE_REQUESTED,
        claim=WorkerClaim(
            spec.id,
            1,
            1,
            "verify-G",
            role="queue_verify",
            resource="verify",
            workspace=str(workspace),
        ),
        bindings={
            "workspace": str(h_workspace),
            "branch": "unit-feature",
            "head": candidate.head,
            "source_revision": candidate.source_revision,
            "pr": {"number": 1},
        },
        evidence={
            "integrated": {"head": candidate.head, "base": candidate.base},
            "queue": {
                "group": asdict(queued),
                "group_digest": queued.digest,
                "observed_at": 100,
                "workspace": str(workspace),
                "rules_fingerprint": "rules1",
            },
        },
    )
    snapshot = CampaignSnapshot(
        "queue-test",
        "a/b",
        selected,
        {spec.id: unit},
        common_dir=str(common_directory(project.project)),
    )
    runtime = CampaignRuntime(
        project.project, CampaignStore(tmp_path / "state"), gateway=gateway, clock=lambda: 100
    )
    action = SimpleNamespace(id="observe-G", payload={"launch_id": "verify-G"})
    runtime._apply_worker(
        snapshot,
        unit,
        action,
        {
            "state": "finished",
            "receipt": {
                "outcome": "queue_verification_waiting",
                "head": queued.head,
                "queue_group": asdict(queued),
                "verification": asdict(failed_receipt),
                "reason": "combined tests failed",
            },
        },
    )
    assert unit.bindings["head"] == candidate.head
    assert unit.state == UnitState.MERGE_REQUESTED and unit.hold is None
    rules = RemoteRules(
        (RequiredCheck("bmad-combined", 7),),
        False,
        True,
        0,
        False,
        False,
        "rules1",
        queue_merge_method="MERGE",
    )
    monkeypatch.setattr(
        flow,
        "PublicationController",
        lambda *a, **k: SimpleNamespace(
            inspect=lambda _: SimpleNamespace(rules=rules, base_head=queued.base),
            evaluate=lambda *a, **k: SimpleNamespace(reasons=()),
        ),
    )
    monkeypatch.setattr(runtime, "_publication_evidence", lambda *a: None)
    observe_action = SimpleNamespace(id="observe-removal", kind="queue-observe", unit=unit.id)
    before_removal = flow.observe_effect(runtime, snapshot, observe_action)
    assert not before_removal.data.get("dequeued")
    unit.evidence["queue"]["failure_check_attempted"] = True
    assert flow.proposals(runtime, snapshot, unit, lambda _: True)[0].kind == "queue-dequeue"
    intent = prepare_dequeue(gateway, candidate, action_id="remove-owned", generation=1)
    transport.drop_dequeue = True
    unknown = gateway.execute(replace(intent, state="sent_unknown"), generation=1)
    assert unknown.state == "sent_unknown"
    assert unit.state == UnitState.MERGE_REQUESTED
    observed = gateway.reconcile_update(unknown)
    assert observed.state == "confirmed"
    removal = flow.observe_effect(runtime, snapshot, observe_action)
    assert removal.data["dequeued"] is True
    flow.apply_effect(runtime, snapshot, unit, observe_action, removal.data)
    assert unit.state == UnitState.SYNCING and unit.resume_state == UnitState.SYNCING
    assert (
        unit.hold.kind == "verification_failed" and unit.hold.evidence["dequeue_confirmed"] is True
    )
    assert verify.rev_parse_head(h_workspace) == candidate.head
    assert verify.rev_parse_head(workspace) == queued.head
    assert (
        len(
            [
                call
                for call in transport.calls
                if call[1] == "graphql" and "dequeuePullRequest" in (call[2] or {}).get("query", "")
            ]
        )
        == 1
    )


def test_rebuilt_group_invalidates_local_and_hosted_receipts_before_new_verification(
    project, tmp_path, group
):
    from bmad_loop import campaign_runtime_queue as flow
    from bmad_loop.campaign_model import CampaignSnapshot, CampaignUnit, UnitSpec

    candidate, queued = group
    spec = UnitSpec(candidate.unit_id, "tickets", "e", "Unit")
    unit = CampaignUnit(
        spec,
        evidence={
            "queue": {
                "group": asdict(queued),
                "group_digest": queued.digest,
                "workspace": "/owned/old-G",
                "verification": {"state": "ready"},
                "hosted_check": {"id": 8},
                "rules_fingerprint": "old-rules",
            }
        },
    )
    snapshot = CampaignSnapshot(
        "queue-test", "a/b", CampaignSpec("Queue", units=(spec,)), {spec.id: unit}
    )
    rebuilt = replace(queued, head="f" * 40)
    action = SimpleNamespace(id="new-G", kind="queue-observe")
    flow.apply_effect(
        None,
        snapshot,
        unit,
        action,
        {
            "group": asdict(rebuilt),
            "group_digest": rebuilt.digest,
            "rules_fingerprint": "new-rules",
            "observed_at": 100,
            "hosted_check": None,
            "gate_reasons": [],
        },
    )
    assert unit.evidence["queue"]["group"]["head"] == rebuilt.head
    assert "verification" not in unit.evidence["queue"]
    assert not unit.evidence["queue"].get("hosted_check")
    assert unit.evidence["queue_history"][0]["workspace"] == "/owned/old-G"
