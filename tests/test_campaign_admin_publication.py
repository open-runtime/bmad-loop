"""Owner-authorized administrative publication retains product acceptance gates."""

import copy
import sys
from dataclasses import replace

import pytest
from test_campaign_publication import publishable as publishable

from bmad_loop.campaign_model import CampaignError, spec_from_dict
from bmad_loop.campaign_native_merge import execute, prepare
from bmad_loop.campaign_publication import MergeReceipt, PublicationRequirements
from bmad_loop.childrun import ChildRun


def admin(publishable):
    controller, candidate, evidence, observation = publishable
    controller.requirements = PublicationRequirements(
        allow_admin_merge=True, repository=candidate.repository
    )
    pr = copy.deepcopy(observation.pr)
    pr["mergeable_state"] = "blocked"
    observation = replace(
        observation,
        pr=pr,
        reviews=(),
        review_decision="REVIEW_REQUIRED",
        rules=replace(observation.rules, strict=False),
    )
    return controller, candidate, evidence, observation


def test_admin_policy_is_explicit_and_repository_scoped(publishable):
    controller, candidate, evidence, observation = admin(publishable)
    assert controller.evaluate(
        candidate, evidence, observation, merge_mode="direct-authorized", independent_families=True
    ).ready
    assert not controller.evaluate(
        candidate, evidence, observation, merge_mode="direct-strict", independent_families=True
    ).ready
    controller.requirements = replace(controller.requirements, repository="other/repo")
    assert not controller.evaluate(
        candidate, evidence, observation, merge_mode="direct-authorized"
    ).ready


@pytest.mark.parametrize(
    "defect", ["hold", "check", "changes", "request", "thread", "model", "conflict", "base"]
)
def test_administrative_permission_never_waives_real_acceptance(publishable, defect):
    controller, candidate, evidence, observation = admin(publishable)
    assert controller.evaluate(
        candidate, evidence, observation, merge_mode="direct-authorized", independent_families=True
    ).ready
    if defect == "hold":
        evidence = replace(evidence, held=True)
    elif defect == "check":
        observation = replace(observation, checks=())
    elif defect == "changes":
        observation = replace(
            observation,
            reviews=(
                {
                    "id": 4,
                    "state": "CHANGES_REQUESTED",
                    "user": {"login": "human"},
                    "commit_id": candidate.head,
                },
            ),
        )
    elif defect == "request":
        observation = replace(observation, requested_reviewers=("human",))
    elif defect == "thread":
        observation = replace(observation, threads=({"isResolved": False},))
    elif defect == "model":
        evidence = replace(evidence, reviewer_family=evidence.developer_family)
    elif defect == "conflict":
        observation = replace(observation, pr={**observation.pr, "mergeable": False})
    else:
        observation = replace(observation, base_head="moved")
    assert not controller.evaluate(
        candidate, evidence, observation, merge_mode="direct-authorized", independent_families=True
    ).ready


@pytest.mark.parametrize("override", [{"merge_mode": "direct-strict"}, {"repository": ""}])
def test_admin_requires_explicit_project_executor(override):
    raw = {
        "schema_version": 1,
        "name": "test",
        "planning": {"enabled": True},
        "github": {
            "allow_admin_merge": True,
            "repository": "a/b",
            "merge_mode": "direct-authorized",
            **override,
        },
    }
    with pytest.raises(CampaignError, match="allow_admin_merge|direct-authorized"):
        spec_from_dict(raw)


def native_case(publishable, monkeypatch):
    controller, candidate, evidence, observation = admin(publishable)
    monkeypatch.setattr(controller, "inspect", lambda n: observation)
    merged = [False]
    monkeypatch.setattr(
        controller,
        "merge_status",
        lambda *a, **k: MergeReceipt(
            "merged_pending_sync" if merged[0] else "unknown", merge_commit="m" * 40
        ),
    )
    intent = prepare(
        controller,
        candidate,
        evidence,
        action_id="native-1",
        generation=1,
        executable=sys.executable,
    )
    return controller, candidate, evidence, replace(intent, state="sent_unknown"), merged


def test_native_normal_merge_preserves_history_and_exact_head(
    project, tmp_path, publishable, monkeypatch
):
    controller, candidate, evidence, intent, merged = native_case(publishable, monkeypatch)
    calls = []

    def runner(argv, **kwargs):
        calls.append(argv)
        merged[0] = True
        return ChildRun(0, "", "")

    result = execute(
        intent,
        project.project,
        tmp_path / "native",
        controller,
        evidence=evidence,
        generation=1,
        runner=runner,
    )
    assert result.state == "confirmed"
    assert calls == [
        [
            intent.payload["binary"],
            "pr",
            "merge",
            "1",
            "--repo",
            "a/b",
            "--merge",
            "--match-head-commit",
            candidate.head,
        ]
    ]


def test_native_admin_only_after_definitive_policy_refusal(
    project, tmp_path, publishable, monkeypatch
):
    controller, candidate, evidence, intent, merged = native_case(publishable, monkeypatch)
    calls = []

    def runner(argv, **kwargs):
        calls.append(argv)
        if "--admin" not in argv:
            return ChildRun(
                1,
                "",
                "Pull request a/b#1 is not mergeable: the base branch policy prohibits the merge.",
            )
        merged[0] = True
        return ChildRun(0, "", "")

    result = execute(
        intent,
        project.project,
        tmp_path / "native",
        controller,
        evidence=evidence,
        generation=1,
        runner=runner,
    )
    assert result.state == "confirmed" and len(calls) == 2
    assert "--admin" not in calls[0] and calls[1] == calls[0] + ["--admin"]
    assert result.receipt["execution"]["attempts"][1]["mode"] == "admin"


@pytest.mark.parametrize(
    "returncode,timed_out,stderr",
    [
        (1, False, "network lost"),
        (1, True, "the base branch policy prohibits the merge"),
        (0, False, ""),
    ],
)
def test_unknown_native_result_never_replays_or_promotes(
    project, tmp_path, publishable, monkeypatch, returncode, timed_out, stderr
):
    controller, candidate, evidence, intent, merged = native_case(publishable, monkeypatch)
    calls = []

    def runner(argv, **kwargs):
        calls.append(argv)
        return ChildRun(returncode, "", stderr, timed_out=timed_out)

    directory = tmp_path / "native"
    result = execute(
        intent,
        project.project,
        directory,
        controller,
        evidence=evidence,
        generation=1,
        runner=runner,
    )
    assert result.state == "sent_unknown"
    result = execute(
        intent,
        project.project,
        directory,
        controller,
        evidence=evidence,
        generation=1,
        runner=runner,
        reconcile=True,
    )
    assert result.state == "sent_unknown" and len(calls) == 1
    merged[0] = True
    result = execute(
        intent,
        project.project,
        directory,
        controller,
        evidence=evidence,
        generation=1,
        runner=runner,
        reconcile=True,
    )
    assert result.state == "confirmed" and len(calls) == 1


def test_new_hold_blocks_admin_and_removal_resumes_known_unsent_attempt(
    project, tmp_path, publishable, monkeypatch
):
    controller, candidate, evidence, intent, merged = native_case(publishable, monkeypatch)
    held = [False]
    calls = []

    def runner(argv, **kwargs):
        calls.append(argv)
        if "--admin" not in argv:
            held[0] = True
            return ChildRun(
                1,
                "",
                "Pull request a/b#1 is not mergeable: the base branch policy prohibits the merge.",
            )
        merged[0] = True
        return ChildRun(0, "", "")

    kwargs = dict(
        evidence=evidence,
        evidence_provider=lambda: replace(evidence, held=held[0]),
        generation=1,
        runner=runner,
    )
    result = execute(intent, project.project, tmp_path / "native", controller, **kwargs)
    assert result.state == "sent_unknown" and len(calls) == 1
    held[0] = False
    result = execute(
        intent, project.project, tmp_path / "native", controller, reconcile=True, **kwargs
    )
    assert result.state == "confirmed" and len(calls) == 2


def test_main_moving_after_policy_refusal_returns_to_integration(
    project, tmp_path, publishable, monkeypatch
):
    controller, candidate, evidence, intent, merged = native_case(publishable, monkeypatch)
    original = controller.inspect(1)
    calls = []

    def runner(argv, **kwargs):
        calls.append(argv)
        monkeypatch.setattr(
            controller, "inspect", lambda n: replace(original, base_head="new-main")
        )
        return ChildRun(
            1,
            "",
            "Pull request a/b#1 is not mergeable: the base branch policy prohibits the merge.",
        )

    result = execute(
        intent,
        project.project,
        tmp_path / "native",
        controller,
        evidence=evidence,
        generation=1,
        runner=runner,
    )
    assert result.state == "rejected" and result.receipt["needs_integration"]
    assert result.receipt["submission"] == "not-submitted" and len(calls) == 1


def test_independent_check_suites_with_same_context_must_all_pass(publishable):
    controller, candidate, evidence, observed = publishable
    first = {**observed.checks[0], "id": 100, "check_suite": {"id": 10}, "conclusion": "failure"}
    second = {**first, "id": 200, "check_suite": {"id": 20}, "conclusion": "success"}
    assert not controller.evaluate(
        candidate, evidence, replace(observed, checks=(first, second))
    ).ready
    retry = {**first, "id": 300, "conclusion": "success"}
    assert controller.evaluate(
        candidate, evidence, replace(observed, checks=(first, second, retry))
    ).ready


@pytest.mark.parametrize("conclusion", ["neutral", "skipped"])
def test_native_required_checks_require_real_success(publishable, conclusion):
    from bmad_loop.campaign_remote_feedback import collect

    controller, candidate, evidence, observed = admin(publishable)
    checks = ({**observed.checks[0], "conclusion": conclusion},)
    observed = replace(observed, checks=checks)
    assert not controller.evaluate(
        candidate, evidence, observed, merge_mode="direct-authorized"
    ).ready
    feedback = collect(
        candidate, observed, now=controller.clock(), requirements=controller.requirements
    )
    assert feedback is not None and feedback.defects[0]["conclusion"] == conclusion


def test_remote_repair_cannot_ignore_failed_sibling_check_suite(publishable):
    from bmad_loop.campaign_remote_feedback import collect

    controller, candidate, _, observed = publishable
    first = {**observed.checks[0], "id": 100, "check_suite": {"id": 10}, "conclusion": "failure"}
    second = {**first, "id": 200, "check_suite": {"id": 20}, "conclusion": "success"}
    feedback = collect(
        candidate,
        replace(observed, checks=(first, second)),
        now=controller.clock(),
        requirements=controller.requirements,
    )
    assert feedback is not None and feedback.defects[0]["id"] == "100"


@pytest.mark.parametrize("moved", [False, True])
def test_actual_landing_tree_controls_tracking_debt(project, publishable, moved):
    from conftest import git

    from bmad_loop.campaign_landing_acceptance import assess

    root = project.project
    branch = git(root, "branch", "--show-current").strip()
    base = git(root, "rev-parse", "HEAD").strip()
    git(root, "checkout", "-b", "landing-candidate")
    (root / "landing-story.txt").write_text("accepted story")
    git(root, "add", "landing-story.txt")
    git(root, "commit", "-m", "story")
    head = git(root, "rev-parse", "HEAD").strip()
    git(root, "checkout", branch)
    if moved:
        (root / "other-work.txt").write_text("concurrent accepted work")
        git(root, "add", "other-work.txt")
        git(root, "commit", "-m", "other")
    git(root, "merge", "--no-ff", "landing-candidate", "-m", "landing")
    merged = git(root, "rev-parse", "HEAD").strip()
    candidate = replace(publishable[1], head=head, base=base)
    debt = assess(
        root,
        candidate,
        MergeReceipt("merged_pending_sync", merge_commit=merged, canonical_head=merged),
    )
    assert debt["state"] == ("pending" if moved else "covered")
    assert debt["candidate"]["head"] == head and debt["merge_commit"] == merged
