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
    result = collect(candidate, observed, now=100, hold_labels=("owner-hold",))
    assert result is not None and result.defects
    assert result.candidate == candidate and result.rules_fingerprint == observed.rules.fingerprint
    assert all(row["head"] == candidate.head for row in result.defects)
    assert all(row["id"] for row in result.defects)
    assert (
        result.digest
        == collect(candidate, replace(observed, observed_at=101), now=101, hold_labels=()).digest
    )


@pytest.mark.parametrize(
    "case",
    [
        "pending-check",
        "missing-check",
        "missing-approval",
        "old-head-review",
        "outdated-thread",
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
    elif case == "old-head-review":
        candidate, observed = observation(publishable, "changes")
        observed = replace(observed, reviews=({**observed.reviews[0], "commit_id": "old"},))
    elif case == "outdated-thread":
        candidate, observed = observation(publishable, "thread")
        observed = replace(observed, threads=({**observed.threads[0], "isOutdated": True},))
    elif case == "wrong-app":
        observed = replace(observed, checks=({**observed.checks[0], "app": {"id": 8}},))
    elif case == "remote-hold":
        observed = replace(observed, issue={"labels": [{"name": "owner-hold"}]})
    else:
        observed = replace(observed, observed_at=0)
    assert collect(candidate, observed, now=100, hold_labels=("owner-hold",)) is None


def test_remote_repair_preserves_pr_and_attempt_budget_and_must_return_through_full_acceptance(
    publishable,
):
    candidate, observed = observation(publishable, "check")
    candidate = replace(candidate, config_revision="1", graph_revision=1)
    observed = replace(observed, pr=copy.deepcopy(observed.pr))
    feedback = collect(candidate, observed, now=100, hold_labels=())
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
