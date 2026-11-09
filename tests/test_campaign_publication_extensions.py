"""Reviewer recruitment and actual project submission preserve durable authority."""

import sys
from dataclasses import replace

import pytest
from test_campaign_publication import publishable as publishable

from bmad_loop.campaign_github import GithubError, UncertainMutation
from bmad_loop.campaign_merge_adapter import (
    MergeAdapterError,
    execute_project_merge,
    prepare_execution,
    reconcile_project_merge,
)
from bmad_loop.campaign_publication import MergeReceipt
from bmad_loop.campaign_review_requests import (
    execute_requests,
    prepare_requests,
    reconcile_requests,
)


@pytest.mark.parametrize(
    "reviewer,ready", [("openai", False), ("anthropic", False), ("google", True)]
)
def test_handoff_preserves_every_writer_for_independent_review(publishable, reviewer, ready):
    controller, candidate, evidence, observation = publishable
    evidence = replace(
        evidence,
        developer_family="openai",
        developer_families=("anthropic", "openai"),
        reviewer_family=reviewer,
    )
    assert (
        controller.evaluate(candidate, evidence, observation, independent_families=True).ready
        is ready
    )


def test_unknown_previous_writer_cannot_be_hidden_by_later_known_writer(publishable):
    controller, candidate, evidence, observation = publishable
    evidence = replace(evidence, developer_families=("openai",), attribution_incomplete=True)
    assert not controller.evaluate(
        candidate, evidence, observation, independent_families=True
    ).ready


def test_planning_and_repair_keep_prior_writer_and_missing_attribution():
    from types import SimpleNamespace

    from bmad_loop.campaign_publication_flow import record_stage_writer

    unit = SimpleNamespace(evidence={"developer_family": "anthropic"})
    record_stage_writer(unit, {"provider_family": "openai"})
    assert unit.evidence["developer_families"] == ["anthropic", "openai"]
    record_stage_writer(unit, {"provider_family": ""})
    record_stage_writer(unit, {"provider_family": "google"})
    assert unit.evidence["developer_attribution_incomplete"]
    assert unit.evidence["developer_families"] == ["anthropic", "google", "openai"]


def test_recruitment_preserves_existing_requests_and_current_head_responses(
    publishable, monkeypatch
):
    controller, candidate, _, observation = publishable
    observation = replace(
        observation, requested_reviewers=("human-added",), requested_teams=("existing",)
    )
    monkeypatch.setattr(controller, "inspect", lambda number: observation)
    intent = prepare_requests(
        controller,
        candidate,
        users=("reviewer", "new", "human-added"),
        teams=("existing", "security"),
        action_id="r1",
        generation=1,
    )
    assert intent.payload == {"reviewers": ["new"], "team_reviewers": ["security"]}
    assert intent.coordinates["candidate"]["head"] == candidate.head


def test_lost_recruitment_ack_is_read_back_without_another_post(publishable, monkeypatch):
    controller, candidate, _, observation = publishable
    observations = [observation]
    monkeypatch.setattr(controller, "inspect", lambda number: observations[-1])
    intent = prepare_requests(
        controller, candidate, users=("new",), teams=("security",), action_id="r1", generation=1
    )
    calls = []

    def request(method, path, payload):
        calls.append((method, path, payload))
        observations.append(
            replace(observation, requested_reviewers=("new",), requested_teams=("security",))
        )
        raise UncertainMutation("ack lost")

    monkeypatch.setattr(controller.gateway.transport, "request", request)
    result = execute_requests(controller, replace(intent, state="sent_unknown"), generation=1)
    assert result.state == "confirmed"
    assert len(calls) == 1
    assert reconcile_requests(controller, result).state == "confirmed"
    assert len(calls) == 1


def test_absent_request_after_lost_ack_stays_unknown(publishable, monkeypatch):
    controller, candidate, _, observation = publishable
    monkeypatch.setattr(controller, "inspect", lambda number: observation)
    intent = prepare_requests(controller, candidate, users=("new",), action_id="r1", generation=1)
    result = reconcile_requests(controller, replace(intent, state="sent_unknown"))
    assert result.state == "sent_unknown" and "no blind" in result.error


@pytest.mark.parametrize("change", ["head", "draft", "author", "generation"])
def test_recruitment_refuses_changed_identity_or_self_review(publishable, monkeypatch, change):
    controller, candidate, _, observation = publishable
    monkeypatch.setattr(controller, "inspect", lambda number: observation)
    intent = prepare_requests(controller, candidate, users=("new",), action_id="r1", generation=1)
    pr = dict(observation.pr)
    if change == "head":
        pr["head"] = {"sha": "other"}
    if change == "draft":
        pr["draft"] = True
    observation = replace(observation, pr=pr)
    with pytest.raises(GithubError):
        if change == "author":
            prepare_requests(controller, candidate, users=("author",), action_id="r2", generation=1)
        else:
            execute_requests(
                controller,
                replace(intent, state="sent_unknown"),
                generation=2 if change == "generation" else 1,
            )
    assert not controller.gateway.transport.calls


def executor(tmp_path, *, drop=False, merged=False):
    log = tmp_path / "operations.jsonl"
    path = tmp_path / "executor.py"
    path.write_text(
        """import argparse,json
from pathlib import Path
p=argparse.ArgumentParser()
p.add_argument('--bmad-campaign-request')
p.add_argument('--bmad-campaign-result')
a=p.parse_args()
r=json.loads(Path(a.bmad_campaign_request).read_text())
with Path(LOG).open('a') as f:f.write(r['operation']+'\\n')
result={**r,'ready':True,'submission':SUBMISSION}
if not (DROP and r['operation']=='merge-submit'):
 Path(a.bmad_campaign_result).write_text(json.dumps(result))
""".replace("LOG", repr(str(log)))
        .replace("SUBMISSION", repr("merged" if merged else "pending"))
        .replace("DROP", repr(drop))
    )
    return (sys.executable, str(path)), log


def test_actual_executor_submits_once_then_reconciles_same_request(
    project, tmp_path, publishable, monkeypatch
):
    controller, candidate, evidence, observation = publishable
    monkeypatch.setattr(controller, "inspect", lambda number: observation)
    monkeypatch.setattr(controller, "merge_status", lambda *a, **k: MergeReceipt("unknown"))
    command, log = executor(tmp_path, drop=True)
    intent = prepare_execution(
        command, project.project, candidate, evidence, controller, action_id="merge-1", generation=1
    )
    result = execute_project_merge(
        replace(intent, state="sent_unknown"),
        project.project,
        tmp_path / "state",
        controller,
        evidence=evidence,
        generation=1,
    )
    assert result.state == "sent_unknown"
    reconciled = reconcile_project_merge(
        result, project.project, tmp_path / "state", controller, generation=1
    )
    assert reconciled.state == "confirmed"
    assert reconciled.receipt["project_executor"] is True
    assert log.read_text().splitlines() == ["merge-submit", "merge-reconcile"]
    assert not controller.gateway.transport.calls  # no native merge fallback


def test_command_merged_claim_requires_independent_github_confirmation(
    project, tmp_path, publishable, monkeypatch
):
    controller, candidate, evidence, observation = publishable
    monkeypatch.setattr(controller, "inspect", lambda number: observation)
    monkeypatch.setattr(controller, "merge_status", lambda *a, **k: MergeReceipt("unknown"))
    command, log = executor(tmp_path, merged=True)
    intent = prepare_execution(
        command, project.project, candidate, evidence, controller, action_id="merge-1", generation=1
    )
    result = execute_project_merge(
        replace(intent, state="sent_unknown"),
        project.project,
        tmp_path / "state",
        controller,
        evidence=evidence,
        generation=1,
    )
    assert result.state == "sent_unknown"
    monkeypatch.setattr(
        controller,
        "merge_status",
        lambda *a, **k: MergeReceipt("merged_pending_sync", merge_commit="m" * 40),
    )
    result = reconcile_project_merge(
        result, project.project, tmp_path / "state", controller, generation=1
    )
    assert result.state == "confirmed" and result.receipt["details"]["sha"] == "m" * 40
    assert log.read_text().splitlines() == ["merge-submit"]


@pytest.mark.parametrize("change", ["hold", "base", "rules", "command", "generation"])
def test_project_executor_rechecks_before_effect(
    project, tmp_path, publishable, monkeypatch, change
):
    controller, candidate, evidence, observation = publishable
    monkeypatch.setattr(controller, "inspect", lambda number: observation)
    monkeypatch.setattr(controller, "merge_status", lambda *a, **k: MergeReceipt("unknown"))
    command, log = executor(tmp_path)
    intent = prepare_execution(
        command, project.project, candidate, evidence, controller, action_id="merge-1", generation=1
    )
    if change == "hold":
        observation = replace(observation, issue={"labels": [{"name": "bmad:hold"}]})
    elif change == "base":
        observation = replace(observation, base_head="changed")
    elif change == "rules":
        observation = replace(
            observation, rules=replace(observation.rules, fingerprint="new-rules")
        )
    elif change == "command":
        from pathlib import Path

        Path(command[1]).write_text("raise Exception('changed command')")
    if change == "rules":
        result = execute_project_merge(
            replace(intent, state="sent_unknown"),
            project.project,
            tmp_path / "state",
            controller,
            evidence=evidence,
            generation=1,
        )
        assert result.state == "rejected"
    else:
        with pytest.raises(MergeAdapterError):
            execute_project_merge(
                replace(intent, state="sent_unknown"),
                project.project,
                tmp_path / "state",
                controller,
                evidence=evidence,
                generation=2 if change == "generation" else 1,
            )
    assert not log.exists()


def test_bare_rem_gate_is_not_silently_repurposed_as_executor(project, tmp_path, publishable):
    controller, candidate, evidence, _ = publishable
    path = tmp_path / "merge_pr.py"
    path.write_text("raise Exception('must not run')")
    with pytest.raises(MergeAdapterError, match="qualified"):
        prepare_execution(
            (sys.executable, str(path)),
            project.project,
            candidate,
            evidence,
            controller,
            action_id="m",
            generation=1,
        )


def test_pr_head_check_is_verified_published_and_reconciled_before_enqueue(project, tmp_path):
    from conftest import git
    from test_campaign_merge_adapter import source
    from test_campaign_queue import Transport

    from bmad_loop.campaign_github import GithubGateway
    from bmad_loop.campaign_merge_adapter import verify_pr_candidate
    from bmad_loop.campaign_publication import Candidate
    from bmad_loop.campaign_queue import CheckCapability, head_candidate, prepare_pr_check

    head = git(project.project, "rev-parse", "HEAD").strip()
    candidate = Candidate("a/b", "u", 1, head, head, "source", 1, "config")
    script = tmp_path / "verify-head.py"
    script.write_text(source())
    receipt = verify_pr_candidate(
        (sys.executable, str(script)),
        project.project,
        tmp_path / "state",
        candidate,
        context="bmad-combined",
        rules_fingerprint="rules",
        clock=lambda: 100,
    )
    assert receipt.state == "ready" and receipt.group_head == candidate.head
    transport = Transport(head_candidate(candidate, observed_at=100))
    transport.drop = True
    gateway = GithubGateway(transport, "a/b")
    capability = CheckCapability(
        True, "a/b", "bmad-combined", 7, "native", 100, rules_fingerprint="rules"
    )
    intent = prepare_pr_check(
        candidate, receipt, capability, gateway, action_id="H-check", generation=1, now=100
    )
    assert intent.coordinates["candidate_kind"] == "pr-head"
    unknown = gateway.execute(replace(intent, state="sent_unknown"), generation=1)
    assert unknown.state == "sent_unknown"
    assert gateway.reconcile_create(unknown).state == "confirmed"
    assert len([call for call in transport.calls if call[0] == "POST"]) == 1
    assert transport.checks[0]["head_sha"] == head
    assert transport.checks[0]["app"]["id"] == 7
    with pytest.raises(GithubError, match="stale or incomplete"):
        prepare_pr_check(
            candidate,
            replace(receipt, group_base="wrong"),
            capability,
            gateway,
            action_id="bad",
            generation=1,
            now=100,
        )


def test_project_executor_intent_cannot_fall_back_to_native_submission(
    project, tmp_path, publishable, monkeypatch
):
    controller, candidate, evidence, observation = publishable
    monkeypatch.setattr(controller, "inspect", lambda number: observation)
    command, _ = executor(tmp_path)
    intent = prepare_execution(
        command, project.project, candidate, evidence, controller, action_id="m1", generation=1
    )
    with pytest.raises(GithubError, match="cannot use native"):
        controller.gateway.execute(replace(intent, state="sent_unknown"), generation=1)
    assert not controller.gateway.transport.calls


def test_acknowledged_team_recruitment_survives_immediate_fulfillment(publishable, monkeypatch):
    from bmad_loop.campaign_github import ApiResponse

    controller, candidate, _, observation = publishable
    monkeypatch.setattr(controller, "inspect", lambda _: observation)
    intent = prepare_requests(
        controller, candidate, teams=("security",), action_id="r1", generation=1
    )
    calls = []

    def request(method, path, payload):
        calls.append((method, path))
        return ApiResponse(201, {**observation.pr, "requested_teams": [{"slug": "security"}]}, {})

    monkeypatch.setattr(controller.gateway.transport, "request", request)
    result = execute_requests(controller, replace(intent, state="sent_unknown"), generation=1)
    assert not observation.requested_teams
    assert result.state == "confirmed"
    assert reconcile_requests(controller, result).state == "confirmed"
    assert len(calls) == 1


@pytest.mark.parametrize("change", ["head", "repository", "number", "teams", "status"])
def test_unrelated_ack_cannot_confirm_absent_recruitment(publishable, monkeypatch, change):
    controller, candidate, _, observation = publishable
    monkeypatch.setattr(controller, "inspect", lambda _: observation)
    intent = prepare_requests(
        controller, candidate, teams=("security",), action_id="r1", generation=1
    )
    receipt = {**observation.pr, "http_status": 201, "requested_teams": [{"slug": "security"}]}
    if change == "head":
        receipt["head"] = {"sha": "different"}
    elif change == "repository":
        receipt["base"] = {"ref": candidate.target, "repo": {"full_name": "other/repo"}}
    elif change == "number":
        receipt["number"] = candidate.pr_number + 1
    elif change == "teams":
        receipt["requested_teams"] = [{"slug": "other"}]
    else:
        receipt["http_status"] = 409
    result = reconcile_requests(controller, replace(intent, state="sent_unknown", receipt=receipt))
    assert result.state == "sent_unknown"
    assert not controller.gateway.transport.calls


def test_observed_recruitment_remains_confirmed_after_team_answers(publishable, monkeypatch):
    controller, candidate, _, observation = publishable
    current = [observation]
    monkeypatch.setattr(controller, "inspect", lambda _: current[0])
    intent = prepare_requests(
        controller, candidate, teams=("security",), action_id="r1", generation=1
    )
    current[0] = replace(observation, requested_teams=("security",))
    observed = reconcile_requests(controller, replace(intent, state="sent_unknown"))
    current[0] = observation
    assert reconcile_requests(controller, observed).state == "confirmed"


def test_successful_recruitment_does_not_depend_on_another_get(publishable, monkeypatch):
    from bmad_loop.campaign_github import ApiResponse

    controller, candidate, _, observation = publishable
    monkeypatch.setattr(controller, "inspect", lambda _: observation)
    intent = prepare_requests(
        controller, candidate, teams=("security",), action_id="r1", generation=1
    )

    def unavailable(_):
        raise GithubError("readback temporarily unavailable")

    def request(*args):
        monkeypatch.setattr(controller, "inspect", unavailable)
        return ApiResponse(201, {**observation.pr, "requested_teams": [{"slug": "security"}]}, {})

    monkeypatch.setattr(controller.gateway.transport, "request", request)
    result = execute_requests(controller, replace(intent, state="sent_unknown"), generation=1)
    assert result.state == "confirmed"
