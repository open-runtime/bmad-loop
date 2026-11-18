"""Never-launched retained successors preserve task ownership and spent authority."""

import copy
from dataclasses import asdict, replace
from pathlib import Path

import pytest
from test_campaign_rearm import paused_child as paused_child
from test_campaign_rearm import quota_child as quota_child
from test_campaign_rearm import ready_case

from bmad_loop.campaign_model import CampaignError, Hold, OutboxAction, UnitState


def consumed_case(quota_child, monkeypatch, tmp_path):
    from bmad_loop import campaign_rearm as rearm
    from bmad_loop.adapters.profile import CLIProfile, HookSpec
    from bmad_loop.campaign_continuation import prepare_permit
    from bmad_loop.campaign_execution import CampaignExecution
    from bmad_loop.campaign_worker_authority import continuation_request, execution_envelope
    from bmad_loop.policy import Policy, StageAdapterPolicy

    snapshot, unit, runtime = ready_case(quota_child, monkeypatch, tmp_path)
    claim = replace(
        unit.claim, launch_id="canceled-successor", run_id="canceled-run",
        run_dir=str(tmp_path / "workers" / "canceled-successor"),
        attempt=unit.attempt + 1, quiescent=False, status="prepared", writes=True,
    )
    payload = {**snapshot.outbox[0].payload, "rearm_id": unit.evidence["technical_rearm"]["id"]}
    action = OutboxAction(claim.launch_id, "launch", unit.id, payload, snapshot.generation)
    snapshot.outbox.append(action)
    unit.claim, unit.attempt = claim, claim.attempt
    rearm.adopt_launch(snapshot, unit, payload, claim)
    policy = Policy()
    policy = replace(policy, adapter=replace(policy.adapter, dev=StageAdapterPolicy(name="codex", model="gpt-scripted")))
    profile = CLIProfile("codex", "scripted-unused", HookSpec("none", "", {}))
    template = CampaignExecution(snapshot.id, unit.id, 0, 0, snapshot.config_revision, tmp_path / "frozen", policy, {"dev": profile})
    envelope = execution_envelope(template, snapshot, unit, claim, payload, Path(claim.run_dir))
    permit = prepare_permit(continuation_request(snapshot, unit, claim), envelope)
    action.payload["continuation_permit_id"] = permit["permit_id"]
    unit.evidence["continuation_permits"] = {
        permit["permit_id"]: {
            "permit": permit,
            "consumption": {
                "schema_version": 1, "permit_id": permit["permit_id"],
                "successor_launch_id": claim.launch_id, "successor_run_id": claim.run_id,
                "state": "consumed",
            },
        },
    }
    return snapshot, unit, runtime, action


@pytest.mark.parametrize("mode", ["prepared-stop", "not-launched"])
def test_cancellation_preserves_spent_permit_and_restores_only_terminal_predecessor(quota_child, monkeypatch, tmp_path, mode):
    from bmad_loop import campaign_rearm as rearm

    snapshot, unit, _, action = consumed_case(quota_child, monkeypatch, tmp_path)
    previous = copy.deepcopy(unit.evidence["technical_rearm"])
    permits = copy.deepcopy(unit.evidence["continuation_permits"])
    attempts = (unit.attempt, unit.evidence["dev_attempts_spent"], unit.evidence["review_cycles_spent"])
    if mode == "prepared-stop":
        snapshot.phase = "draining"
        result = None
    else:
        action.state = "sent_unknown"
        result = {"not_launched": True, "quota_wait": "Fresh native quota gate closed before spawn"}
    proof = rearm.cancel_unlaunched(snapshot, unit, action, mode=mode, result=result)
    assert unit.evidence["technical_rearm"]["phase"] == "canceled"
    assert asdict(unit.claim) == previous["predecessor"]
    assert asdict(unit.hold) == previous["binding"]["hold"]
    assert unit.evidence["continuation_permits"] == permits
    assert (unit.attempt, unit.evidence["dev_attempts_spent"], unit.evidence["review_cycles_spent"]) == attempts
    assert proof["permit_id"] in permits
    assert rearm.cancel_unlaunched(snapshot, unit, action, mode=mode, result=result) == proof
    if mode == "not-launched":
        action.state = "confirmed"
    snapshot.phase = "active"
    next_binding = rearm.capture_binding(snapshot, unit)
    assert next_binding["id"] != previous["binding"]["id"]
    assert next_binding["campaign_attempt"] == attempts[0]


def test_cancel_preserves_new_owner_and_its_original_quota_obligation(quota_child, monkeypatch, tmp_path):
    from bmad_loop import campaign_rearm as rearm

    snapshot, unit, _, action = consumed_case(quota_child, monkeypatch, tmp_path)
    original = unit.evidence["technical_rearm"]["binding"]["hold"]
    unit.evidence["operator_prior_hold"] = asdict(unit.hold)
    owner = Hold("operator", "Owner asked to hold", id="new-owner-hold")
    unit.hold = owner
    snapshot.phase = "draining"
    rearm.cancel_unlaunched(snapshot, unit, action, mode="prepared-stop")
    assert unit.hold == owner
    assert unit.evidence["operator_prior_hold"] == original
    assert unit.evidence["unresolved_approval_requirements"] == ["fresh-human-signoff"]


@pytest.mark.parametrize("change", ["sent_unknown", "launched", "no_receipt", "foreign_claim", "missing_permit", "other_permit"])
def test_cancel_requires_definitive_never_launched_exact_successor(quota_child, monkeypatch, tmp_path, change):
    from bmad_loop import campaign_rearm as rearm

    snapshot, unit, _, action = consumed_case(quota_child, monkeypatch, tmp_path)
    snapshot.phase = "draining"
    baseline = snapshot.clone()
    rearm.cancel_unlaunched(baseline, baseline.units[unit.id], baseline.outbox[-1], mode="prepared-stop")
    assert baseline.units[unit.id].evidence["technical_rearm"]["phase"] == "canceled"
    mode, result = "prepared-stop", None
    if change == "sent_unknown":
        action.state = "sent_unknown"
    elif change in {"launched", "no_receipt"}:
        action.state = "sent_unknown"
        mode = "not-launched"
        result = {"not_launched": True, "launched": True} if change == "launched" else {}
    elif change == "foreign_claim":
        unit.claim.launch_id = "foreign-launch"
    elif change == "missing_permit":
        unit.evidence["continuation_permits"] = {}
    elif change == "other_permit":
        action.payload["continuation_permit_id"] = "unbound-permit"
    with pytest.raises(CampaignError, match="never-launched"):
        rearm.cancel_unlaunched(snapshot, unit, action, mode=mode, result=result)
    assert unit.evidence["technical_rearm"]["phase"] == "continuing"


def test_first_never_launched_quota_stays_fresh_admission_not_retained_work(paused_child):
    from bmad_loop.campaign_rearm import capture_binding
    from bmad_loop.campaign_runtime import CampaignRuntime
    from bmad_loop.campaign_supervisor import Observation

    snapshot, unit = paused_child
    unit.state = UnitState.CLAIMED
    unit.hold = None
    unit.evidence = {}
    unit.claim.quiescent = False
    action = snapshot.outbox[0]
    runtime = CampaignRuntime.__new__(CampaignRuntime)
    runtime._apply_effect(snapshot, action, Observation("confirmed", {"not_launched": True, "quota_wait": "Admission closed before first spawn"}))
    assert unit.state == UnitState.READY and unit.hold is None
    assert unit.claim.status == "not_launched" and unit.claim.quiescent
    assert "worker_history" not in unit.evidence and "technical_rearm" not in unit.evidence
    with pytest.raises(CampaignError, match="quiescent development pause"):
        capture_binding(snapshot, unit)


@pytest.mark.parametrize("wrong_run", [False, True])
def test_bound_bootstrap_failure_is_terminal_without_inventing_native_run_identity(quota_child, monkeypatch, tmp_path, wrong_run):
    from bmad_loop import campaign_rearm as rearm

    snapshot, unit, _, _ = consumed_case(quota_child, monkeypatch, tmp_path)
    unit.claim.quiescent = True
    receipt = {"campaign_id": snapshot.id, "unit_id": unit.id, "launch_id": unit.claim.launch_id, "attempt": unit.claim.attempt, "generation": unit.claim.generation, "outcome": "failed", "error": "Controller rejected changed startup authority"}
    if wrong_run:
        receipt["run_id"] = "foreign-native-run"
        with pytest.raises(CampaignError, match="does not own"):
            rearm.finish(snapshot, unit, receipt)
        assert unit.evidence["technical_rearm"]["phase"] == "continuing"
    else:
        rearm.finish(snapshot, unit, receipt)
        assert unit.evidence["technical_rearm"]["phase"] == "finished"
        assert unit.evidence["technical_rearm"]["terminal_outcome"] == "failed"
        assert unit.evidence["continuation_permits"]
