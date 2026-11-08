"""Campaign gates keep frozen requirements through every fresh publication check."""

import time
from dataclasses import asdict, replace

import pytest
from test_campaign_publication import publishable as publishable

from bmad_loop.campaign_github import ApiResponse, GithubError
from bmad_loop.campaign_model import (
    CampaignSnapshot,
    CampaignSpec,
    CampaignUnit,
    GitHubSpec,
    OutboxAction,
    UnitSpec,
    UnitState,
)
from bmad_loop.campaign_publication import (
    Candidate,
    PublicationController,
    PublicationRequirements,
    RequiredCheck,
)
from bmad_loop.campaign_remote_feedback import collect, repair_current
from bmad_loop.campaign_runtime import CampaignRuntime
from bmad_loop.campaign_store import CampaignStore


def policy(controller, **kwargs):
    return PublicationController(
        controller.gateway,
        clock=controller.clock,
        requirements=PublicationRequirements.from_spec(GitHubSpec(**kwargs)),
    )


@pytest.mark.parametrize("label", ["bmad:hold", "custom:wait"])
def test_lost_ack_replay_uses_frozen_hold_labels_and_retains_readback(
    monkeypatch, publishable, label
):
    old, candidate, evidence, observation = publishable
    controller = policy(old, hold_labels=(label,))
    current = [observation]
    monkeypatch.setattr(controller, "inspect", lambda _: current[0])
    intent = replace(
        controller.prepare_merge(candidate, evidence, action_id="m", generation=1),
        state="sent_unknown",
    )
    transport = controller.gateway.transport
    transport.responses["GET", "repos/a/b/pulls/1"] = ApiResponse(200, observation.pr, {})
    transport.responses["PUT", intent.path] = ApiResponse(
        202, {"status": "pending", "details": {"uuid": "accepted"}}, {}
    )
    current[0] = replace(observation, issue={"labels": [{"name": label.upper()}]})
    with pytest.raises(GithubError, match="Remote owner/review hold"):
        controller.reconcile_request(intent, candidate, evidence, generation=1, allow_retry=True)
    assert not [call for call in transport.calls if call[0] == "PUT"]
    # A known remote merge is still observed, not resent or fabricated unmerged.
    transport.responses["GET", "repos/a/b/pulls/1"] = ApiResponse(
        200, {**observation.pr, "merged": True, "merge_commit_sha": "merge"}, {}
    )
    assert (
        controller.reconcile_request(
            intent, candidate, evidence, generation=1, allow_retry=True
        ).state
        == "confirmed"
    )
    transport.responses["GET", "repos/a/b/pulls/1"] = ApiResponse(200, observation.pr, {})
    current[0] = observation
    assert (
        controller.reconcile_request(
            intent, candidate, evidence, generation=1, allow_retry=True
        ).state
        == "confirmed"
    )
    assert len([call for call in transport.calls if call[0] == "PUT"]) == 1


def security(candidate, state="success", head=None):
    return {
        "id": 2,
        "name": "security-scan",
        "app": {"id": 8},
        "head_sha": head or candidate.head,
        "status": "in_progress" if state == "pending" else "completed",
        "conclusion": state,
        "completed_at": "now",
    }


@pytest.mark.parametrize("state", ["missing", "pending", "failure", "wrong-head", "success"])
def test_configured_checks_gate_exact_head_and_only_failures_authorize_repair(
    publishable, monkeypatch, state
):
    old, candidate, evidence, observation = publishable
    controller = policy(old, required_checks=("security-scan",))
    checks = (
        ()
        if state == "missing"
        else (
            security(
                candidate,
                "success" if state == "wrong-head" else state,
                head="old" if state == "wrong-head" else None,
            ),
        )
    )
    observed = replace(observation, checks=observation.checks + checks)
    monkeypatch.setattr(controller, "inspect", lambda _: observed)
    gate = controller.evaluate(candidate, evidence, observed)
    assert gate.ready == (state == "success")
    feedback = collect(candidate, observed, now=100, requirements=controller.requirements)
    assert (feedback is not None) == (state == "failure")
    if state == "success":
        assert (
            controller.prepare_merge(candidate, evidence, action_id="m", generation=1).kind
            == "merge"
        )
    else:
        assert "security-scan" in ";".join(gate.reasons)
        with pytest.raises(GithubError, match="security-scan"):
            controller.prepare_merge(candidate, evidence, action_id="m", generation=1)


def test_required_union_preserves_server_app_pins_and_latest_status_semantics(publishable):
    old, candidate, evidence, observation = publishable
    controller = policy(old, required_checks=("CI", "security-scan", "security-scan"))
    assert controller.requirements.checks(observation.rules.checks) == (
        RequiredCheck("CI", 7),
        RequiredCheck("security-scan"),
    )
    observed = replace(
        observation,
        checks=observation.checks
        + ({**observation.checks[0], "id": 20, "app": {"id": 99}, "conclusion": "failure"},),
        statuses=(
            {"id": 1, "context": "security-scan", "state": "failure"},
            {"id": 2, "context": "security-scan", "state": "success"},
        ),
    )
    assert controller.evaluate(candidate, evidence, observed).ready
    assert not controller.evaluate(
        candidate, evidence, replace(observed, statuses=observed.statuses[:1])
    ).ready
    assert not controller.evaluate(
        candidate, evidence, replace(observed, checks=observed.checks[1:])
    ).ready


@pytest.mark.parametrize(
    "field,value", [("requested_reviewers", ("reviewer",)), ("requested_teams", ("security",))]
)
def test_requested_reviews_block_even_with_current_approval_but_never_create_repair(
    publishable, field, value
):
    controller, candidate, evidence, observation = publishable
    pending = replace(observation, **{field: value})
    assert not controller.evaluate(candidate, evidence, pending).ready
    assert collect(candidate, pending, now=100, requirements=controller.requirements) is None
    assert controller.evaluate(candidate, evidence, observation).ready


def test_configured_failure_is_revalidated_before_repair(monkeypatch, publishable):
    old, candidate, _, observed = publishable
    requirements = PublicationRequirements(required_checks=("security-scan",))
    current = [replace(observed, checks=observed.checks + (security(candidate, "failure"),))]
    feedback = collect(candidate, current[0], now=100, requirements=requirements)
    assert feedback is not None
    monkeypatch.setattr(PublicationController, "inspect", lambda *args: current[0])
    assert repair_current(old.gateway, asdict(feedback), now=100, requirements=requirements)
    current[0] = replace(observed, checks=observed.checks + (security(candidate),))
    assert not repair_current(old.gateway, asdict(feedback), now=100, requirements=requirements)


@pytest.mark.parametrize("defect", ["hold", "missing-check", "failed-check", "review-request"])
def test_runtime_prepare_uses_frozen_campaign_requirements(
    project, tmp_path, monkeypatch, publishable, defect
):
    from bmad_loop import campaign_ownership

    old, candidate, evidence, observed = publishable
    observed = replace(
        observed, observed_at=time.time(), checks=observed.checks + (security(candidate),)
    )
    if defect == "hold":
        observed = replace(observed, issue={"labels": [{"name": "custom:wait"}]})
    elif defect == "missing-check":
        observed = replace(observed, checks=observed.checks[:1])
    elif defect == "failed-check":
        observed = replace(observed, checks=observed.checks[:1] + (security(candidate, "failure"),))
    else:
        observed = replace(observed, requested_teams=("security",))
    unit = CampaignUnit(
        UnitSpec("unit", "s", "e", "Unit"),
        state=UnitState.READY_TO_PUBLISH,
        bindings={"workspace": str(project.project), "pr": {"number": 1}},
    )
    spec = CampaignSpec(
        "gate",
        units=(unit.spec,),
        github=GitHubSpec(hold_labels=("custom:wait",), required_checks=("security-scan",)),
    )
    snapshot = CampaignSnapshot("gate", "a/b", spec, {unit.id: unit})
    driver = CampaignRuntime(
        project.project, CampaignStore(tmp_path / "authority"), gateway=old.gateway
    )
    unit.bindings["source_revision"] = candidate.source_revision
    unit.evidence["integrated"] = {"head": candidate.head, "base": candidate.base}
    unit.evidence["review"] = {
        "head": candidate.head,
        "validated": {
            "verdict": "approved",
            "coordinates": {
                "head": candidate.head,
                "base": candidate.base,
                "source_revision": unit.bindings["source_revision"],
                "graph_revision": snapshot.graph_revision,
                "config_revision": snapshot.config_revision,
            },
        },
    }
    candidate = driver._candidate(snapshot, unit)
    evidence = replace(evidence, candidate=candidate)
    monkeypatch.setattr(driver, "_publication_evidence", lambda *args: evidence)
    monkeypatch.setattr(campaign_ownership, "canonical_unchanged", lambda *args: None)
    monkeypatch.setattr(PublicationController, "inspect", lambda *args: observed)
    result = driver._perform_prepare_merge(
        snapshot, OutboxAction("m", "prepare-merge", unit.id, {}, 0)
    )
    if defect == "failed-check":
        assert result.data["remote_feedback"]["defects"][0]["context"] == "security-scan"
    else:
        assert result.state == "waiting" and not result.data["gate"]["ready"]
    assert "intent" not in result.data and old.gateway.transport.calls == []

    # Removing only the remote condition reaches real merge preparation with
    # the same current local review, instead of bypassing freshness.
    observed = replace(
        publishable[3],
        observed_at=time.time(),
        checks=publishable[3].checks + (security(candidate),),
    )
    ready = driver._perform_prepare_merge(
        snapshot, OutboxAction("ready", "prepare-merge", unit.id, {}, 0)
    )
    assert ready.state == "confirmed" and ready.data["purpose"] == "merge"
    assert ready.data["intent"]["payload"]["sha"] == candidate.head
    assert old.gateway.transport.calls == []


@pytest.mark.parametrize("defect", ["hold", "missing-check", "review-request"])
def test_saved_uncertain_merge_uses_snapshot_requirements_after_resume(
    project, tmp_path, monkeypatch, publishable, defect
):
    from bmad_loop.platform_util import safe_segment

    old, candidate, evidence, observation = publishable
    observation = replace(
        observation, observed_at=time.time(), checks=observation.checks + (security(candidate),)
    )
    # This pre-upgrade intent has no new policy fields. The saved specification,
    # not absent intent metadata or controller defaults, governs its replay.
    monkeypatch.setattr(old, "inspect", lambda _: replace(observation, observed_at=100))
    intent = replace(
        old.prepare_merge(candidate, evidence, action_id="old-m", generation=1),
        state="sent_unknown",
    )
    unit = CampaignUnit(UnitSpec("unit", "s", "e", "Unit"), state=UnitState.READY_TO_PUBLISH)
    spec = CampaignSpec(
        "resume",
        units=(unit.spec,),
        github=GitHubSpec(hold_labels=("custom:wait",), required_checks=("security-scan",)),
    )
    action = OutboxAction(
        "old-m",
        "github",
        unit.id,
        {"intent": intent.to_dict(), "retry_authorized": True, "purpose": "merge"},
        1,
        state="sent_unknown",
    )
    store = CampaignStore(tmp_path / "authority")
    state = store.create(
        CampaignSnapshot("resume", "a/b", spec, {unit.id: unit}, outbox=[action], generation=1)
    )
    submitted = (
        store.directory(state.id) / "merge-submissions" / (safe_segment(action.id) + ".json")
    )
    submitted.parent.mkdir()
    submitted.write_text("{}")
    driver = CampaignRuntime(project.project, store, gateway=old.gateway)
    monkeypatch.setattr(driver, "_candidate", lambda *args: candidate)
    monkeypatch.setattr(driver, "_publication_evidence", lambda *args: evidence)
    current = [observation]
    if defect == "hold":
        current[0] = replace(observation, issue={"labels": [{"name": "custom:wait"}]})
    elif defect == "missing-check":
        current[0] = replace(observation, checks=observation.checks[:1])
    else:
        current[0] = replace(observation, requested_reviewers=("new-reviewer",))
    monkeypatch.setattr(PublicationController, "inspect", lambda *args: current[0])
    transport = old.gateway.transport
    transport.responses["GET", "repos/a/b/pulls/1"] = ApiResponse(200, observation.pr, {})
    transport.responses["PUT", intent.path] = ApiResponse(
        202, {"status": "pending", "details": {"uuid": "same-request"}}, {}
    )
    with pytest.raises(GithubError, match="Publication held"):
        driver.reconcile_action(state, action)
    assert not [row for row in transport.calls if row[0] == "PUT"]
    assert store.load(state.id).outbox[0].payload["intent"] == intent.to_dict()
    current[0] = observation
    assert driver.reconcile_action(state, action).state == "confirmed"
    assert len([row for row in transport.calls if row[0] == "PUT"]) == 1


@pytest.mark.parametrize("stage", ["before", "after"])
@pytest.mark.parametrize("defect", ["hold", "check", "team"])
def test_project_gate_cannot_drop_requirements_before_or_after_command(
    project, tmp_path, monkeypatch, publishable, stage, defect
):
    from test_campaign_merge_adapter import script, source

    from bmad_loop.campaign_merge_adapter import check_project_gate

    old, candidate, evidence, observation = publishable
    controller = policy(old, hold_labels=("custom:wait",), required_checks=("security-scan",))
    good = replace(observation, checks=observation.checks + (security(candidate),))
    bad = (
        replace(good, issue={"labels": [{"name": "custom:wait"}]})
        if defect == "hold"
        else (
            replace(good, checks=observation.checks)
            if defect == "check"
            else replace(good, requested_teams=("team",))
        )
    )
    observations = [bad] if stage == "before" else [good, bad]
    monkeypatch.setattr(controller, "inspect", lambda _: observations.pop(0))
    result = check_project_gate(
        script(project, source()),
        project.project,
        tmp_path / "gate",
        candidate,
        evidence,
        controller,
        target="main",
        clock=lambda: 100,
    )
    assert result.state == "held" and not observations


@pytest.mark.parametrize(
    "fault", [None, "forbidden", "missing-teams", "cycle", "bound", "bad-user", "bad-team"]
)
def test_requested_review_observation_covers_users_teams_and_pages_or_fails(
    monkeypatch, publishable, fault
):
    from bmad_loop import campaign_publication

    controller, candidate, _, observed = publishable
    gateway = controller.gateway
    transport = gateway.transport
    monkeypatch.setattr(gateway, "pull", lambda _: observed.pr)
    monkeypatch.setattr(gateway, "issue", lambda _: observed.issue)
    monkeypatch.setattr(gateway, "reviews", lambda _: list(observed.reviews))
    monkeypatch.setattr(gateway, "review_threads", lambda _: [])
    monkeypatch.setattr(campaign_publication, "read_rules", lambda *args: observed.rules)
    transport.responses["GET", "repos/a/b/git/ref/heads/main"] = ApiResponse(
        200, {"object": {"sha": candidate.base}}, {}
    )
    transport.responses[
        "GET", f"repos/a/b/commits/{candidate.head}/check-runs?filter=all&per_page=100"
    ] = ApiResponse(200, {"check_runs": list(observed.checks)}, {})
    transport.responses["GET", f"repos/a/b/commits/{candidate.head}/statuses?per_page=100"] = (
        ApiResponse(200, [], {})
    )
    transport.responses["POST", "graphql"] = ApiResponse(
        200, {"data": {"repository": {"pullRequest": {"reviewDecision": "APPROVED"}}}}, {}
    )
    path = "repos/a/b/pulls/1/requested_reviewers?per_page=100"
    next_page = "repos/a/b/pulls/1/requested_reviewers?per_page=100&page=2"
    first = {"users": [{"login": "first"}], "teams": []}
    second = {"users": [{"login": "second"}], "teams": [{"slug": "security"}]}
    if fault == "missing-teams":
        del second["teams"]
    if fault == "bad-user":
        second["users"] = [{}]
    if fault == "bad-team":
        second["teams"] = [{}]
    if fault == "bound":
        gateway.max_pages = 1
    transport.responses["GET", path] = ApiResponse(
        403 if fault == "forbidden" else 200,
        first,
        {"link": f'<https://api.github.com/{next_page}>; rel="next"'},
    )
    transport.responses["GET", next_page] = ApiResponse(
        200,
        second,
        {"link": f'<https://api.github.com/{next_page}>; rel="next"'} if fault == "cycle" else {},
    )
    if fault:
        with pytest.raises(GithubError):
            controller.inspect(1)
    else:
        actual = controller.inspect(1)
        assert actual.requested_reviewers == ("first", "second")
        assert actual.requested_teams == ("security",)
        assert [row[1] for row in transport.calls if "requested_reviewers" in row[1]] == [
            path,
            next_page,
            path,
            next_page,
        ]


@pytest.mark.parametrize("defect", [None, "hold", "check", "team"])
def test_queue_observation_retains_h_requirements_alongside_successful_g(
    monkeypatch, publishable, defect
):
    from types import SimpleNamespace

    from bmad_loop import campaign_runtime_queue as queue
    from bmad_loop.campaign_queue import QueueGroup

    old, candidate, evidence, observation = publishable
    good = replace(
        observation,
        checks=observation.checks + (security(candidate),),
        rules=replace(observation.rules, queue=True, queue_merge_method="MERGE"),
    )
    observed = (
        replace(good, issue={"labels": [{"name": "custom:wait"}]})
        if defect == "hold"
        else (
            replace(good, checks=observation.checks)
            if defect == "check"
            else replace(good, requested_teams=("team",)) if defect == "team" else good
        )
    )
    unit = CampaignUnit(UnitSpec("unit", "s", "e", "Unit"), state=UnitState.MERGE_REQUESTED)
    spec = CampaignSpec(
        "queue",
        units=(unit.spec,),
        github=GitHubSpec(
            merge_mode="queue",
            candidate_check_context="CI",
            hold_labels=("custom:wait",),
            required_checks=("security-scan",),
        ),
    )
    snapshot = CampaignSnapshot("queue", "a/b", spec, {unit.id: unit})
    runtime = SimpleNamespace(
        gateway=lambda _: old.gateway,
        _candidate=lambda *args: candidate,
        _publication_evidence=lambda *args: evidence,
        clock=lambda: 100,
    )
    group = QueueGroup(
        "a/b", 1, "main", "entry", "g" * 40, candidate.base, candidate.head, "AWAITING_CHECKS", 100
    )
    monkeypatch.setattr(old.gateway, "pull", lambda _: observation.pr)
    monkeypatch.setattr(
        queue,
        "membership",
        lambda *args: {
            "merged": False,
            "state": "OPEN",
            "mergeQueueEntry": {"state": "AWAITING_CHECKS"},
        },
    )
    monkeypatch.setattr(queue, "observe_group", lambda *args, **kwargs: group)
    monkeypatch.setattr(
        queue,
        "observe_check",
        lambda *args: {"head_sha": group.head, "status": "completed", "conclusion": "success"},
    )
    monkeypatch.setattr(PublicationController, "inspect", lambda *args: observed)
    action = OutboxAction("observe", "queue-observe", unit.id, {}, 0)
    result = queue.observe_effect(runtime, snapshot, action)
    assert result.data["hosted_check"]["head_sha"] == group.head != candidate.head
    assert bool(result.data["gate_reasons"]) is bool(defect)
    queue.apply_effect(runtime, snapshot, unit, action, result.data)
    assert bool(unit.evidence["queue"].get("needs_dequeue")) is bool(defect)


@pytest.mark.parametrize("defect", [None, "hold", "check", "team"])
def test_queue_without_group_keeps_fresh_publication_veto(monkeypatch, publishable, defect):
    from types import SimpleNamespace

    from bmad_loop import campaign_runtime_queue as queue

    old, candidate, evidence, observation = publishable
    good = replace(
        observation,
        checks=observation.checks + (security(candidate),),
        rules=replace(observation.rules, queue=True, queue_merge_method="MERGE"),
    )
    observed = (
        replace(good, issue={"labels": [{"name": "custom:wait"}]})
        if defect == "hold"
        else (
            replace(good, checks=observation.checks)
            if defect == "check"
            else replace(good, requested_teams=("security",)) if defect == "team" else good
        )
    )
    unit = CampaignUnit(UnitSpec("unit", "s", "e", "Unit"), state=UnitState.MERGE_REQUESTED)
    spec = CampaignSpec(
        "queue-wait",
        units=(unit.spec,),
        github=GitHubSpec(
            merge_mode="queue",
            candidate_check_context="CI",
            hold_labels=("custom:wait",),
            required_checks=("security-scan",),
        ),
    )
    snapshot = CampaignSnapshot("queue-wait", "a/b", spec, {unit.id: unit})
    runtime = SimpleNamespace(
        gateway=lambda _: old.gateway,
        _candidate=lambda *args: candidate,
        _publication_evidence=lambda *args: evidence,
        clock=lambda: 100,
    )
    monkeypatch.setattr(old.gateway, "pull", lambda _: observation.pr)
    monkeypatch.setattr(
        queue,
        "membership",
        lambda *args: {
            "merged": False,
            "state": "OPEN",
            "mergeQueueEntry": {"state": "AWAITING_CHECKS"},
        },
    )
    monkeypatch.setattr(queue, "observe_group", lambda *args, **kwargs: None)
    monkeypatch.setattr(PublicationController, "inspect", lambda *args: observed)
    action = OutboxAction("observe", "queue-observe", unit.id, {}, 0)
    result = queue.observe_effect(runtime, snapshot, action)
    queue.apply_effect(runtime, snapshot, unit, action, result.data)
    assert result.data["group"] is None
    assert bool(unit.evidence["queue"].get("needs_dequeue")) is bool(defect)
    proposals = queue.proposals(runtime, snapshot, unit, lambda _: True)
    assert [row.kind for row in proposals] == (["queue-dequeue"] if defect else [])
    assert unit.evidence["queue"]["candidate"] == asdict(candidate)


@pytest.mark.parametrize("defect", [None, "hold", "check", "team"])
def test_external_merge_canonical_proof_does_not_waive_campaign_requirements(
    project, monkeypatch, publishable, defect
):
    from types import SimpleNamespace

    from conftest import git

    from bmad_loop import campaign_external_merge
    from bmad_loop.campaign_model import WorkerClaim
    from bmad_loop.campaign_publication import MergeReceipt

    old, original, original_evidence, observation = publishable
    repo = project.project
    base = git(repo, "rev-parse", "HEAD")
    (repo / "product.txt").write_text("accepted product")
    git(repo, "add", "product.txt")
    git(repo, "commit", "-qm", "accepted change")
    head = git(repo, "rev-parse", "HEAD")
    tree = git(repo, "rev-parse", "HEAD^{tree}")
    merge = git(repo, "commit-tree", tree, "-p", base, "-p", head, "-m", "external merge")
    candidate = replace(original, head=head, base=base, config_revision="1")
    evidence = replace(original_evidence, candidate=candidate)
    pr = {
        **observation.pr,
        "head": {"sha": head},
        "merged": True,
        "state": "closed",
        "merge_commit_sha": merge,
    }
    checks = ({**observation.checks[0], "head_sha": head}, security(candidate))
    observed = replace(
        observation,
        pr=pr,
        base_head=merge,
        observed_at=time.time(),
        checks=checks,
        reviews=({**observation.reviews[0], "commit_id": head},),
    )
    if defect == "hold":
        observed = replace(observed, issue={"labels": [{"name": "custom:wait"}]})
    if defect == "check":
        observed = replace(observed, checks=checks[:1])
    if defect == "team":
        observed = replace(observed, requested_teams=("team",))
    unit = CampaignUnit(
        UnitSpec("unit", "s", "e", "Unit"),
        state=UnitState.READY_TO_PUBLISH,
        claim=WorkerClaim("unit", 1, 0, "review", quiescent=True),
        bindings={"source_revision": "s1"},
        evidence={
            "integrated": {"head": head, "base": base},
            "verification": {"outcome": "verified", "head": head, "launch_id": "verified"},
            "review": {
                "launch_id": "review",
                "validated": {
                    "verdict": "approved",
                    "coordinates": {
                        "head": head,
                        "base": base,
                        "source_revision": "s1",
                        "graph_revision": 1,
                        "config_revision": 1,
                    },
                },
            },
        },
    )
    spec = CampaignSpec(
        "external",
        units=(unit.spec,),
        github=GitHubSpec(hold_labels=("custom:wait",), required_checks=("security-scan",)),
    )
    snapshot = CampaignSnapshot("external", "a/b", spec, {unit.id: unit})
    runtime = SimpleNamespace(
        project=repo,
        gateway=lambda _: old.gateway,
        _candidate=lambda *args: candidate,
        _publication_evidence=lambda *args: evidence,
        _pusher=lambda *args: SimpleNamespace(observations=lambda: ()),
        clock=time.time,
    )
    receipt = MergeReceipt("merged_pending_sync", merge_commit=merge, canonical_head=merge)
    monkeypatch.setattr(PublicationController, "merge_status", lambda *args, **kwargs: receipt)
    monkeypatch.setattr(PublicationController, "confirm_canonical", lambda *args, **kwargs: receipt)
    monkeypatch.setattr(PublicationController, "inspect", lambda *args: observed)
    result = campaign_external_merge.inspect(runtime, snapshot, unit, observed)
    assert ("external_followup_hold" in result.data) is bool(defect)
    assert ("external_merge" in result.data) is (not defect)


@pytest.mark.parametrize("has_group", [True, False])
@pytest.mark.parametrize("change_during_removal", ["before", "after", "head", "none"])
def test_failed_h_check_waits_for_queue_removal_then_uses_bounded_fresh_remote_repair(
    monkeypatch, publishable, has_group, change_during_removal
):
    from types import SimpleNamespace

    from bmad_loop import campaign_runtime_queue as queue
    from bmad_loop.campaign_model import CampaignError, WorkerClaim
    from bmad_loop.campaign_queue import QueueGroup
    from bmad_loop.campaign_remote_feedback import RemoteFeedback, apply_after_queue_removal

    old, initial, initial_evidence, observation = publishable
    candidate = replace(initial, config_revision="1")
    evidence = replace(initial_evidence, candidate=candidate)
    current = [
        replace(
            observation,
            checks=observation.checks + (security(candidate, "failure"),),
            rules=replace(observation.rules, queue=True, queue_merge_method="MERGE"),
        )
    ]
    unit = CampaignUnit(
        UnitSpec("unit", "s", "e", "Unit"),
        state=UnitState.MERGE_REQUESTED,
        claim=WorkerClaim("unit", 1, 0, "review", quiescent=True),
        bindings={"source_revision": "s1", "pr": {"number": 1}},
        evidence={
            "integrated": {"head": candidate.head, "base": candidate.base},
            "verification": {"head": candidate.head},
            "review": {"verdict": "approved"},
        },
    )
    spec = CampaignSpec(
        "queue-repair",
        units=(unit.spec,),
        github=GitHubSpec(
            merge_mode="queue", candidate_check_context="CI", required_checks=("security-scan",)
        ),
    )
    snapshot = CampaignSnapshot("queue-repair", "a/b", spec, {unit.id: unit})
    runtime = SimpleNamespace(
        gateway=lambda _: old.gateway,
        _candidate=lambda *args: candidate,
        _publication_evidence=lambda *args: evidence,
        clock=lambda: 100,
    )
    group = QueueGroup(
        "a/b", 1, "main", "entry", "g" * 40, candidate.base, candidate.head, "AWAITING_CHECKS", 100
    )
    member = {"merged": False, "state": "OPEN", "mergeQueueEntry": {"state": "AWAITING_CHECKS"}}
    monkeypatch.setattr(old.gateway, "pull", lambda _: observation.pr)
    monkeypatch.setattr(queue, "membership", lambda *args: member)
    monkeypatch.setattr(
        queue, "observe_group", lambda *args, **kwargs: group if has_group else None
    )
    monkeypatch.setattr(
        queue,
        "observe_check",
        lambda *args: {"head_sha": group.head, "status": "completed", "conclusion": "success"},
    )
    monkeypatch.setattr(PublicationController, "inspect", lambda *args: current[0])
    action = OutboxAction("observe", "queue-observe", unit.id, {}, 0)
    observed = queue.observe_effect(runtime, snapshot, action)
    queue.apply_effect(runtime, snapshot, unit, action, observed.data)
    raw = observed.data["remote_feedback"]
    feedback = RemoteFeedback(
        Candidate(**raw["candidate"]),
        raw["rules_fingerprint"],
        tuple(raw["defects"]),
        raw["digest"],
        raw["observed_at"],
    )
    with pytest.raises(CampaignError, match="confirmed removal"):
        apply_after_queue_removal(snapshot, unit.id, feedback)
    assert unit.state == UnitState.MERGE_REQUESTED and unit.hold is None
    assert [row.kind for row in queue.proposals(runtime, snapshot, unit, lambda _: True)] == [
        "queue-dequeue"
    ]
    assert not unit.evidence["queue"].get("failure")  # Never publish a fabricated G failure.
    unit.evidence["queue"]["dequeue_submitted"] = True
    assert queue.proposals(runtime, snapshot, unit, lambda _: True) == ()
    if change_during_removal == "before":
        snapshot.config_revision += 1
        candidate = replace(candidate, config_revision=str(snapshot.config_revision))
    member["mergeQueueEntry"] = None
    removed = queue.observe_effect(runtime, snapshot, action)
    assert removed.data["dequeued"]
    if change_during_removal == "after":
        snapshot.config_revision += 1
        candidate = replace(candidate, config_revision=str(snapshot.config_revision))
    elif change_during_removal == "head":
        candidate = replace(candidate, head="f" * 40)
    queue.apply_effect(runtime, snapshot, unit, action, removed.data)
    if change_during_removal == "head":
        assert unit.state == UnitState.MERGE_REQUESTED and unit.hold is None
        assert not unit.evidence.get("remote_feedback_attempted")
        assert [row.kind for row in queue.proposals(runtime, snapshot, unit, lambda _: True)] == [
            "queue-observe"
        ]
        return
    if change_during_removal != "none":
        assert unit.state == UnitState.SYNCING and unit.hold is None
        assert not unit.evidence.get("remote_feedback_attempted")
        assert "integrated" not in unit.evidence
        return
    assert unit.state == UnitState.SYNCING and unit.hold.kind == "verification_failed"
    assert unit.evidence["remote_feedback"]["digest"] == feedback.digest
    assert unit.evidence["remote_feedback_attempted"] == [feedback.digest]
    assert unit.bindings["pr"]["number"] == 1
    requirements = PublicationRequirements.from_spec(spec.github)
    assert repair_current(
        old.gateway, unit.evidence["remote_feedback"], now=100, requirements=requirements
    )
    current[0] = replace(current[0], issue={"labels": [{"name": "bmad:hold"}]})
    assert not repair_current(
        old.gateway, unit.evidence["remote_feedback"], now=100, requirements=requirements
    )
    current[0] = replace(observation, checks=observation.checks + (security(candidate),))
    assert not repair_current(
        old.gateway, unit.evidence["remote_feedback"], now=100, requirements=requirements
    )
