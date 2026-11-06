"""Externally merged work needs canonical history and already-covered product evidence."""

import copy
from dataclasses import replace
from urllib.parse import urlsplit

import pytest
from conftest import git
from test_campaign_publication import publishable as publishable
from test_campaign_runtime import diagnose
from test_campaign_runtime import production_case as production_case
from test_campaign_runtime import run_case

from bmad_loop import verify
from bmad_loop.campaign_external_merge import ExternalMergeEvidence
from bmad_loop.campaign_model import (
    CampaignSnapshot,
    CampaignSpec,
    CampaignUnit,
    UnitSpec,
    UnitState,
    WorkerClaim,
)
from bmad_loop.campaign_store import reconcile_external_merge
from bmad_loop.statemachine import IllegalTransition, check_campaign_external_merge


def test_typed_external_landing_has_no_fake_native_submission_states():
    spec = UnitSpec("source:project:u", "source", "e", "Unit")
    unit = CampaignUnit(
        spec,
        UnitState.READY_TO_PUBLISH,
        claim=WorkerClaim(spec.id, 1, 1, "review", quiescent=True),
        bindings={"source_revision": "s1"},
        evidence={
            "integrated": {"head": "h", "base": "b"},
            "verification": {"launch_id": "verify"},
            "review": {"launch_id": "review"},
        },
    )
    snapshot = CampaignSnapshot(
        "external", "a/b", CampaignSpec("External", units=(spec,)), {spec.id: unit}
    )
    proof = ExternalMergeEvidence(
        "a/b",
        spec.id,
        "h",
        "b",
        "main",
        "m",
        "c",
        ("b", "h"),
        ("h",),
        "verify",
        "review",
        "s1",
        1,
        1,
        True,
        True,
    )
    reconcile_external_merge(snapshot, unit.id, proof)
    assert unit.state == UnitState.MERGED_PENDING_SYNC
    assert [row["kind"] for row in snapshot.events] == ["external-merge-reconciled"]
    assert not [row for row in snapshot.events if row.get("target") == "merge_requested"]


@pytest.mark.parametrize(
    "phase", ["pending", "running", "locally_complete", "syncing", "reviewing", "delivered"]
)
def test_external_merge_reconciliation_cannot_bypass_acceptance(phase):
    with pytest.raises(IllegalTransition, match="ready-to-publish"):
        check_campaign_external_merge("unit", phase)


def test_canonical_merge_observation_retains_real_closed_state_and_native_remote_gates(publishable):
    from bmad_loop.campaign_publication import MergeReceipt

    controller, candidate, evidence, observation = publishable
    closed = copy.deepcopy(observation.pr)
    closed.update(state="closed", merged=True, merge_commit_sha="m" * 40, mergeable=None)
    observed = replace(observation, pr=closed, base_head="m" * 40)
    proof = MergeReceipt("merged_pending_sync", merge_commit="m" * 40, canonical_head="m" * 40)
    assert controller.evaluate(
        candidate, evidence, observed, independent_families=True, canonical_merge=proof
    ).ready
    held = replace(observed, issue={"labels": [{"name": "bmad:hold"}]})
    assert not controller.evaluate(
        candidate, evidence, held, independent_families=True, canonical_merge=proof
    ).ready
    assert closed["state"] == "closed" and closed["merged"] is True


class ExternalMerger:
    def __init__(self, service, *, uncovered=False):
        self.service, self.original = service, service.request
        self.uncovered = uncovered
        self.merged = set()

    def request(self, method, path, payload=None):
        service = self.service
        with service.lock:
            urlsplit(path).path.removeprefix("repos/a/b/").split("/")
            if (
                method == "POST"
                and path == "graphql"
                and "markPullRequestReadyForReview" in payload.get("query", "")
            ):
                response = self.original(method, path, payload)
                node = payload["variables"]["input"]["pullRequestId"]
                pr = next(row for row in service.pulls.values() if row["node_id"] == node)
                number, head = pr["number"], pr["head"]["sha"]
                base = service._base()
                if self.uncovered:
                    tree = git(service.remote, "rev-parse", base + "^{tree}")
                    base = git(
                        service.remote,
                        "-c",
                        "user.name=External",
                        "-c",
                        "user.email=external@example.test",
                        "commit-tree",
                        tree,
                        "-p",
                        base,
                        "-m",
                        "unreviewed intervening canonical history",
                    )
                    git(service.remote, "update-ref", "refs/heads/main", base)
                if not verify.is_ancestor(service.remote, base, head) and not self.uncovered:
                    return response
                tree = git(service.remote, "rev-parse", head + "^{tree}")
                merged = git(
                    service.remote,
                    "-c",
                    "user.name=External",
                    "-c",
                    "user.email=external@example.test",
                    "commit-tree",
                    tree,
                    "-p",
                    base,
                    "-p",
                    head,
                    "-m",
                    "External reviewed landing",
                )
                git(service.remote, "update-ref", "refs/heads/main", merged, base)
                pr.update(merged=True, state="closed", merge_commit_sha=merged)
                service.merge_bases[number] = base
                self.merged.add(number)
                service._save()
                return response
            return self.original(method, path, payload)


def test_external_ready_pr_merge_reconciles_canonical_delivery_without_native_merge_intent(
    production_case, monkeypatch
):
    from bmad_loop.campaign_action_history import actions

    case = production_case
    external = ExternalMerger(case.service)
    monkeypatch.setattr(case.service, "request", external.request)
    result = run_case(case, ticks=2500)
    assert result.phase == "delivered", diagnose(result)
    assert external.merged
    reconciled = [row for row in result.events if row["kind"] == "external-merge-reconciled"]
    assert reconciled
    for row in reconciled:
        unit = result.units[row["unit"]]
        assert unit.state == UnitState.DELIVERED
        assert not [
            action
            for action in actions(result)
            if action.unit == unit.id and action.payload.get("purpose") == "merge"
        ]
        assert verify.is_ancestor(case.remote, row["head"], git(case.remote, "rev-parse", "main"))


def test_uncovered_external_merge_is_visible_followup_hold_and_never_delivered(
    production_case, monkeypatch
):
    case = production_case
    external = ExternalMerger(case.service, uncovered=True)
    monkeypatch.setattr(case.service, "request", external.request)
    original_proposals = case.runtime.proposals
    hold_observed = False

    def observe_hold(snapshot):
        nonlocal hold_observed
        if not hold_observed and any(
            unit.hold and unit.hold.kind == "postmerge_acceptance"
            for unit in snapshot.units.values()
        ):
            hold_observed = True
            case.store.enqueue(snapshot.id, "stop", request_id="observed-postmerge-hold")
        return original_proposals(snapshot)

    # Wait for the actual hold, then drain outstanding effects. A tick budget
    # races real Git subprocesses when the suite runs under parallel load.
    monkeypatch.setattr(case.runtime, "proposals", observe_hold)
    result = run_case(case)
    assert hold_observed, diagnose(result)
    assert result.phase == "stopped", diagnose(result)
    held = [
        unit
        for unit in result.units.values()
        if unit.hold and unit.hold.kind == "postmerge_acceptance"
    ]
    assert held, diagnose(result)
    assert result.phase != "delivered"
    assert all(unit.state == UnitState.READY_TO_PUBLISH for unit in held)
    assert all("integration not covered" in unit.hold.reason for unit in held)
    assert not [row for row in result.events if row["kind"] == "external-merge-reconciled"]
    assert not case.service.closed_units
