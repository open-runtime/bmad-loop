"""Only actual required remote defect identities request bounded same-PR repair."""

import copy
import hashlib
import json
from dataclasses import asdict, replace

import pytest
from test_campaign_publication import publishable as publishable

from bmad_loop.campaign_model import (
    CampaignSnapshot,
    CampaignSpec,
    CampaignUnit,
    GitHubSpec,
    OutboxAction,
    UnitSpec,
    UnitState,
    WorkerClaim,
)
from bmad_loop.campaign_publication import PublicationController, PublicationRequirements
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


@pytest.mark.parametrize(
    "variant",
    [
        "neutral",
        "skipped",
        "real-failure",
        "different-head",
        "foreign-source",
        "tampered",
        "active",
    ],
)
def test_resume_reobserves_only_proven_inconclusive_legacy_feedback(
    publishable, tmp_path, monkeypatch, variant
):
    from bmad_loop.campaign_runtime import CampaignRuntime
    from bmad_loop.campaign_store import CampaignStore

    controller, candidate, _, observed = publishable
    candidate = replace(candidate, config_revision="1", graph_revision=1)
    requirements = PublicationRequirements(allow_admin_merge=True, repository=candidate.repository)
    failed = replace(observed, checks=({**observed.checks[0], "conclusion": "failure"},))
    original = collect(candidate, failed, now=100, requirements=requirements)
    assert original is not None
    # Build the exact older producer's typed feedback and pass it through the real hold writer.
    conclusion = variant if variant in {"neutral", "skipped"} else "skipped"
    defects = [{**original.defects[0], "conclusion": conclusion}]
    if variant == "real-failure":
        defects.append(original.defects[0])
    original_bytes = json.dumps(
        {"candidate": asdict(candidate), "rules": original.rules_fingerprint, "defects": defects},
        sort_keys=True,
    ).encode()
    old = replace(
        original, defects=tuple(defects), digest=hashlib.sha256(original_bytes).hexdigest()
    )
    spec = UnitSpec(candidate.unit_id, "s", "e", "Unit")
    unit = CampaignUnit(
        spec,
        UnitState.READY_TO_PUBLISH,
        claim=WorkerClaim(spec.id, 1, 1, "review", quiescent=True),
        bindings={
            "head": candidate.head,
            "source_revision": candidate.source_revision,
            "pr": {"number": 1},
        },
        evidence={"integrated": {"head": candidate.head, "base": candidate.base}},
        resolution_attempts=1,
    )
    github = GitHubSpec(
        merge_mode="direct-authorized", allow_admin_merge=True, repository=candidate.repository
    )
    snapshot = CampaignSnapshot(
        "remote",
        candidate.repository,
        CampaignSpec("Remote", units=(spec,), github=github),
        {unit.id: unit},
    )
    apply(snapshot, unit.id, old)
    if variant == "different-head":
        unit.bindings["head"] = "different"
    elif variant == "foreign-source":
        unit.evidence["failure"]["producer"] = "local_verification"
    elif variant == "tampered":
        for saved in (unit.evidence["remote_feedback"], unit.hold.evidence["feedback"]):
            saved["rules_fingerprint"] = "changed"
    elif variant == "active":
        unit.claim.quiescent = False
    store = CampaignStore(tmp_path / "campaigns")
    store.create(snapshot)
    snapshot = store.load(snapshot.id)
    unit = snapshot.units[unit.id]
    original_hold = copy.deepcopy(unit.hold)
    driver = CampaignRuntime(tmp_path, store, gateway=controller.gateway, clock=controller.clock)
    driver.prepare_resume(snapshot)
    if variant not in {"neutral", "skipped"}:
        assert unit.hold == original_hold
        return
    assert unit.hold.id == original_hold.id and unit.hold.kind == "external_wait"
    assert unit.hold.evidence["feedback"] == original_hold.evidence["feedback"]
    assert "remote_feedback" not in unit.evidence and "failure" not in unit.evidence
    store.save(snapshot, snapshot.revision)
    snapshot = store.load(snapshot.id)
    unit = snapshot.units[unit.id]
    restored = unit.evidence["inconclusive_check_history"][old.digest]
    assert restored["hold"] == asdict(original_hold) and restored["failure"]["id"] == old.digest
    assert not driver.prepare_resume(snapshot)
    current = replace(observed, checks=({**observed.checks[0], "conclusion": conclusion},))
    monkeypatch.setattr(PublicationController, "inspect", lambda *args: current)
    action = OutboxAction(
        "wait", "remote-wait", unit.id, {"hold_id": unit.hold.id}, snapshot.generation
    )
    waiting = driver._perform_remote_wait(snapshot, action)
    assert not waiting.data["ready_to_reaccept"]
    driver.apply(snapshot, action, waiting)
    assert unit.hold.id == original_hold.id
    current = observed
    driver.apply(snapshot, action, driver._perform_remote_wait(snapshot, action))
    assert unit.hold is None and unit.state == UnitState.SYNCING
    assert unit.resolution_attempts == 1 and unit.evidence["remote_feedback_attempted"] == [
        old.digest
    ]
    assert controller.gateway.transport.calls == []
