"""The last successful queue check is a publication authority boundary."""

from dataclasses import asdict, replace
from types import SimpleNamespace

import pytest
from test_campaign_publication import publishable as publishable

from bmad_loop import campaign_queue_publication as publication
from bmad_loop.campaign_github import GithubError, MutationIntent
from bmad_loop.campaign_model import GitHubSpec, OutboxAction
from bmad_loop.campaign_queue import QueueGroup
from bmad_loop.campaign_runtime import CampaignRuntime
from bmad_loop.campaign_runtime_queue import proposals


@pytest.mark.parametrize(
    "change", ["none", "hold", "review", "group", "rules", "draining", "outage"]
)
def test_final_queue_success_gate_checks_current_authority(publishable, monkeypatch, change):
    controller, candidate, evidence, observed = publishable
    observed = replace(
        observed, rules=replace(observed.rules, queue=True, queue_merge_method="MERGE")
    )
    group = QueueGroup(
        "a/b", 1, "main", "entry", "g" * 40, candidate.base, candidate.head, "AWAITING_CHECKS", 100
    )
    intent = MutationIntent(
        "check",
        "queue_check",
        "POST",
        "repos/a/b/check-runs",
        {"conclusion": "success"},
        coordinates={
            "candidate": asdict(candidate),
            "group_digest": group.digest,
            "rules_fingerprint": observed.rules.fingerprint,
        },
    )
    unit = SimpleNamespace(id=candidate.unit_id)
    snapshot = SimpleNamespace(
        id="campaign",
        phase="draining" if change == "draining" else "active",
        units={unit.id: unit},
        spec=SimpleNamespace(
            github=GitHubSpec(
                hold_labels=("bmad:hold",),
                required_checks=(),
                require_independent_review=True,
                candidate_check_context="CI",
            )
        ),
    )
    runtime = SimpleNamespace(
        store=SimpleNamespace(load=lambda _: snapshot),
        clock=controller.clock,
        gateway=lambda _: controller.gateway,
        _candidate=lambda *_: candidate,
        _publication_evidence=lambda *_: evidence,
    )
    if change == "hold":
        observed = replace(observed, issue={"labels": [{"name": "bmad:hold"}]})
    elif change == "review":
        observed = replace(observed, requested_reviewers=("late-reviewer",))
    elif change == "group":
        group = replace(group, head="z" * 40)
    elif change == "rules":
        observed = replace(observed, rules=replace(observed.rules, fingerprint="changed"))
    monkeypatch.setattr(controller, "inspect", lambda _: observed)
    if change == "outage":

        def unavailable(_):
            raise GithubError("temporary read outage")

        monkeypatch.setattr(controller, "inspect", unavailable)
    monkeypatch.setattr(publication, "PublicationController", lambda *args, **kwargs: controller)
    monkeypatch.setattr(publication, "observe_group", lambda *args, **kwargs: group)
    action = OutboxAction("check", "github", unit.id, {}, 0)
    result = publication.guard_success(runtime, snapshot, action, intent)
    if change == "none":
        assert result is None
    else:
        assert result.state == "confirmed"
        assert result.data["queue_success_blocked"]
        assert result.data["needs_dequeue"] is (change not in {"group", "outage"})
        if change == "outage":
            # A known-unsent failed read releases the publication reservation;
            # the restored observation permits a later guarded attempt.
            monkeypatch.setattr(controller, "inspect", lambda _: observed)
            assert publication.guard_success(runtime, snapshot, action, intent) is None


@pytest.mark.parametrize("purpose", ["queue-check", "queue-dequeue"])
def test_suppressed_queue_write_rearms_after_operator_release(purpose):
    group = QueueGroup("a/b", 1, "main", "entry", "g", "b", "h", "AWAITING_CHECKS", 100)
    state = {
        "group": asdict(group),
        "group_digest": group.digest,
        "observed_at": 100,
        "workspace": "workspace",
        "verification": {"proof": True},
        "check_submitted": True,
        "dequeue_submitted": True,
    }
    if purpose == "queue-dequeue":
        state["needs_dequeue"] = True
    unit = SimpleNamespace(id="unit", evidence={"queue": state})
    snapshot = SimpleNamespace(
        units={unit.id: unit},
        spec=SimpleNamespace(github=SimpleNamespace(candidate_publish="native")),
        events=[],
        revision=0,
        generation=0,
    )
    action = OutboxAction(
        "effect",
        "github",
        unit.id,
        {"purpose": purpose, "intent": {"coordinates": {"group_digest": group.digest}}},
        0,
    )
    CampaignRuntime._apply_effect(
        None,
        snapshot,
        action,
        SimpleNamespace(data={"operator_suppressed": True}, state="confirmed"),
    )
    # A fresh unchanged queue observation after release must allow the same G's
    # write to be proposed; it must not remain permanently 'submitted'.
    state["observed_at"] = 100
    proposed = proposals(SimpleNamespace(clock=lambda: 100), snapshot, unit, lambda _: True)
    assert [row.kind for row in proposed] == [
        "queue-publish" if purpose == "queue-check" else "queue-dequeue"
    ]
