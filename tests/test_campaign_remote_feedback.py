"""Only actual required remote defect identities request bounded same-PR repair."""

import copy
from dataclasses import replace

import pytest
from test_campaign_publication import publishable as publishable

from bmad_loop.campaign_model import (
    CampaignSnapshot,
    CampaignSpec,
    CampaignUnit,
    UnitSpec,
    UnitState,
    WorkerClaim,
)
from bmad_loop.campaign_publication import PublicationRequirements
from bmad_loop.campaign_remote_feedback import apply, collect
from bmad_loop.campaign_resolution import CauseCode
from bmad_loop.campaign_resolution_flow import typed_failure


def observation(publishable, defect):
    controller, candidate, evidence, observed = publishable
    if defect == "check":
        observed = replace(
            observed,
            checks=(
                {
                    **observed.checks[0],
                    "conclusion": "failure",
                    "completed_at": "2026-11-05T00:00:00Z",
                },
            ),
        )
    elif defect == "changes":
        observed = replace(
            observed,
            reviews=(
                {
                    "id": 2,
                    "state": "CHANGES_REQUESTED",
                    "commit_id": candidate.head,
                    "user": {"login": "reviewer"},
                    "body": "Fix actual required behavior",
                    "submitted_at": "2026-11-05T00:00:00Z",
                },
            ),
        )
    elif defect == "thread":
        observed = replace(
            observed,
            threads=(
                {
                    "id": "T1",
                    "isResolved": False,
                    "isOutdated": False,
                    "path": "src/owned.py",
                    "comments": {"nodes": [{"id": "C1", "body": "Required correction"}]},
                },
            ),
        )
    return candidate, observed


@pytest.mark.parametrize("defect", ["check", "changes", "thread"])
def test_required_remote_defect_is_bound_to_exact_head_native_identity_and_rule_revision(
    publishable, defect
):
    candidate, observed = observation(publishable, defect)
    result = collect(
        candidate,
        observed,
        now=100,
        requirements=PublicationRequirements(hold_labels=("owner-hold",)),
    )
    assert result is not None and result.defects
    assert result.candidate == candidate and result.rules_fingerprint == observed.rules.fingerprint
    assert all(row["head"] == candidate.head for row in result.defects)
    assert all(row["id"] for row in result.defects)
    assert (
        result.digest
        == collect(
            candidate,
            replace(observed, observed_at=101),
            now=101,
            requirements=PublicationRequirements(hold_labels=()),
        ).digest
    )


@pytest.mark.parametrize(
    "case",
    [
        "pending-check",
        "missing-check",
        "missing-approval",
        "wrong-app",
        "remote-hold",
        "stale",
    ],
)
def test_waits_and_unbound_findings_do_not_create_repair_authority(publishable, case):
    candidate, observed = observation(publishable, "check")
    if case == "pending-check":
        observed = replace(
            observed, checks=({**observed.checks[0], "status": "in_progress", "conclusion": None},)
        )
    elif case == "missing-check":
        observed = replace(observed, checks=())
    elif case == "missing-approval":
        observed = replace(publishable[3], reviews=())
    elif case == "wrong-app":
        observed = replace(observed, checks=({**observed.checks[0], "app": {"id": 8}},))
    elif case == "remote-hold":
        observed = replace(observed, issue={"labels": [{"name": "owner-hold"}]})
    else:
        observed = replace(observed, observed_at=0)
    assert (
        collect(
            candidate,
            observed,
            now=100,
            requirements=PublicationRequirements(hold_labels=("owner-hold",)),
        )
        is None
    )


@pytest.mark.parametrize("later_state", ["COMMENTED", "PENDING"])
def test_later_comment_preserves_the_latest_binding_review_objection(publishable, later_state):
    controller, _, evidence, _ = publishable
    candidate, observed = observation(publishable, "changes")
    objection = observed.reviews[0]
    observed = replace(
        observed,
        reviews=(
            objection,
            {**objection, "id": 3, "state": later_state, "body": "Additional review context"},
        ),
    )
    feedback = collect(candidate, observed, now=100, requirements=PublicationRequirements())
    assert feedback is not None
    assert feedback.defects[0]["id"] == str(objection["id"])
    assert feedback.defects[0]["body"] == objection["body"]
    assert (
        "A reviewer requests changes" in controller.evaluate(candidate, evidence, observed).reasons
    )


@pytest.mark.parametrize("kind", ["old-head-review", "outdated-thread", "no-server-rule"])
def test_repair_context_keeps_every_known_publication_review_veto(publishable, kind):
    controller, _, evidence, _ = publishable
    candidate, observed = observation(
        publishable, "changes" if kind == "old-head-review" else "thread"
    )
    if kind == "old-head-review":
        observed = replace(
            observed, reviews=({**observed.reviews[0], "commit_id": "previous-head"},)
        )
    elif kind == "outdated-thread":
        observed = replace(observed, threads=({**observed.threads[0], "isOutdated": True},))
    else:
        observed = replace(observed, rules=replace(observed.rules, conversation_resolution=False))
    feedback = collect(candidate, observed, now=100, requirements=PublicationRequirements())
    assert feedback is not None and len(feedback.defects) == 1
    defect = feedback.defects[0]
    assert defect["head"] == candidate.head
    if kind == "old-head-review":
        assert defect["review_head"] == "previous-head"
        assert defect["body"] == observed.reviews[0]["body"]
    else:
        assert defect["comments"] == observed.threads[0]["comments"]
        assert defect["is_outdated"] == observed.threads[0]["isOutdated"]
    # Collecting a fix proposal does not dismiss or resolve the objection.
    reason = (
        "A reviewer requests changes"
        if kind == "old-head-review"
        else "Unresolved review threads remain"
    )
    assert reason in controller.evaluate(candidate, evidence, observed).reasons


def test_remote_repair_preserves_pr_and_attempt_budget_and_must_return_through_full_acceptance(
    publishable,
):
    candidate, observed = observation(publishable, "check")
    candidate = replace(candidate, config_revision="1", graph_revision=1)
    observed = replace(observed, pr=copy.deepcopy(observed.pr))
    feedback = collect(
        candidate, observed, now=100, requirements=PublicationRequirements(hold_labels=())
    )
    spec = UnitSpec(candidate.unit_id, "s", "e", "Unit")
    unit = CampaignUnit(
        spec,
        UnitState.READY_TO_PUBLISH,
        claim=WorkerClaim(spec.id, 1, 1, "review", quiescent=True),
        bindings={
            "source_revision": candidate.source_revision,
            "pr": {"number": candidate.pr_number},
        },
        evidence={
            "integrated": {"head": candidate.head, "base": candidate.base},
            "verification": {},
            "review": {},
        },
    )
    unit.resolution_attempts = 1
    snapshot = CampaignSnapshot(
        "remote", candidate.repository, CampaignSpec("Remote", units=(spec,)), {unit.id: unit}
    )
    apply(snapshot, unit.id, feedback)
    assert unit.state == UnitState.SYNCING and unit.resume_state == UnitState.SYNCING
    assert unit.hold.kind == "verification_failed"
    assert unit.bindings["pr"]["number"] == candidate.pr_number
    assert unit.resolution_attempts == 1
    assert "verification" not in unit.evidence and "review" not in unit.evidence
    assert typed_failure(snapshot, unit.id).code == CauseCode.VERIFICATION
    unit.hold = None
    unit.state = UnitState.READY_TO_PUBLISH
    unit.evidence["integrated"] = {"head": candidate.head, "base": candidate.base}
    apply(snapshot, unit.id, feedback)
    assert unit.hold.kind == "external_wait"
