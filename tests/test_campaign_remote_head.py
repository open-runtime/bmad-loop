"""Owned remote-head debt is explicit, bounded and separate from backup success."""

import copy
from dataclasses import replace
from types import SimpleNamespace

import pytest

from bmad_loop import campaign_remote_head as remote
from bmad_loop.campaign_model import (
    CampaignError,
    CampaignSnapshot,
    CampaignSpec,
    CampaignUnit,
    GitHubSpec,
    Hold,
    OutboxAction,
    UnitSpec,
    UnitState,
    WorkerClaim,
    spec_from_dict,
)
from bmad_loop.campaign_recovery import PushObservation
from bmad_loop.campaign_runtime import CampaignRuntime


@pytest.fixture
def case(tmp_path, monkeypatch):
    unit = CampaignUnit(UnitSpec("u", "s", "e", "Unit"), UnitState.REVIEWING)
    unit.claim = WorkerClaim("u", 1, 1, "worker", quiescent=True)
    unit.bindings.update(
        workspace=str(tmp_path),
        head="a" * 40,
        branch="owned",
        last_pushed_head="a" * 40,
        pr={"number": 1, "node_id": "PR1"},
    )
    snapshot = CampaignSnapshot(
        "c",
        "a/b",
        CampaignSpec("c", github=GitHubSpec(max_remote_head_refreshes=2)),
        {"u": unit},
        remote_url="remote-url",
    )
    pr = {
        "number": 1,
        "node_id": "PR1",
        "state": "open",
        "merged": False,
        "head": {"ref": "owned", "sha": "b" * 40, "repo": {"full_name": "a/b"}},
        "base": {"ref": "main", "repo": {"full_name": "a/b"}},
    }
    ancestor = {("a" * 40, "b" * 40)}
    fence = [None]
    pusher = SimpleNamespace(
        remote_head=lambda: pr["head"]["sha"],
        _ancestor=lambda older, newer: older == newer or (older, newer) in ancestor,
        fenced=lambda: fence[0],
    )
    runtime = SimpleNamespace(
        project=tmp_path,
        _pusher=lambda *args: pusher,
        gateway=lambda *args: SimpleNamespace(pull=lambda number: pr),
        _set_hold=CampaignRuntime._set_hold,
    )
    monkeypatch.setattr(
        remote.verify,
        "git_bytes",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=b"remote-url\n"),
    )
    monkeypatch.setattr(remote.verify, "rev_parse_head", lambda workspace: unit.bindings["head"])
    return SimpleNamespace(
        unit=unit,
        snapshot=snapshot,
        pr=pr,
        ancestor=ancestor,
        fence=fence,
        runtime=runtime,
        pusher=pusher,
    )


@pytest.mark.parametrize("value", [0, -1, True, "8"])
def test_remote_refresh_budget_requires_positive_integer(value):
    with pytest.raises(CampaignError):
        spec_from_dict(
            {
                "schema_version": 1,
                "name": "c",
                "planning": {"enabled": True},
                "github": {"max_remote_head_refreshes": value},
            }
        )


@pytest.mark.parametrize(
    "fault",
    ["missing", "uncertain", "wrong_action", "foreign_pr", "local_ancestry", "present_conflict"],
)
def test_legacy_adoption_anchor_requires_exact_confirmed_preparation(case, monkeypatch, fault):
    from bmad_loop import campaign_adoption as adoption

    unit = case.unit
    unit.bindings.pop("last_pushed_head")
    unit.bindings["adoption"] = {"state": "owned"}
    manifest = SimpleNamespace(head="a" * 40, branch="owned", pr_number=1, pr_node_id="PR1")
    monkeypatch.setattr(adoption, "transaction", lambda *args: (manifest, None, None))
    action = OutboxAction(
        "prepare",
        "prepare-unit",
        unit.id,
        {"adoption": True},
        1,
        state="confirmed",
        receipt={
            "head": "a" * 40,
            "branch": "owned",
            "workspace": unit.bindings["workspace"],
            "pr": {"number": 1, "node_id": "PR1"},
        },
    )
    case.snapshot.outbox.append(action)
    pushed = PushObservation("prepare-initial", "a" * 40, "confirmed", "a" * 40)
    if fault == "uncertain":
        pushed = replace(pushed, state="sent_unknown")
    elif fault == "wrong_action":
        pushed = replace(pushed, action_id="unrelated-initial")
    elif fault == "foreign_pr":
        action.receipt["pr"]["node_id"] = "OTHER"
    elif fault == "local_ancestry":
        unit.bindings["head"] = "d" * 40
    elif fault == "present_conflict":
        unit.bindings["remote_head_anchor"] = "e" * 40
    case.pusher.observations = lambda: () if fault == "missing" else (pushed,)
    with pytest.raises(CampaignError):
        remote.observe(case.runtime, case.snapshot, unit)
    assert unit.bindings.get("remote_head_anchor") == (
        "e" * 40 if fault == "present_conflict" else None
    )


@pytest.mark.parametrize(
    "field", ["node", "branch", "repository", "target", "merged", "unowned", "anchor", "remote_url"]
)
def test_observation_refuses_foreign_or_rewritten_authority(case, field):
    if field == "node":
        case.pr["node_id"] = "other"
    elif field == "branch":
        case.pr["head"]["ref"] = "other"
    elif field == "repository":
        case.pr["head"]["repo"]["full_name"] = "other/repo"
    elif field == "target":
        case.pr["base"]["ref"] = "other"
    elif field == "merged":
        case.pr["merged"] = True
    elif field == "unowned":
        case.unit.bindings.pop("pr")
    elif field == "anchor":
        case.ancestor.clear()
    else:
        case.snapshot.remote_url = "other-url"
    with pytest.raises(CampaignError):
        remote.observe(case.runtime, case.snapshot, case.unit)


def test_debt_invalidates_acceptance_but_preserves_authors_and_repeats_once(case):
    case.unit.evidence.update(
        integrated={"head": "a" * 40},
        review={"old": True},
        verification={"old": True},
        developer_families=["openai"],
        developer_attribution_incomplete=True,
    )
    value = remote.observe(case.runtime, case.snapshot, case.unit)
    remote.apply(case.runtime, case.snapshot, case.unit, value)
    assert case.unit.state == UnitState.SYNCING
    assert all(key not in case.unit.evidence for key in ("review", "verification", "integrated"))
    assert case.unit.evidence["developer_families"] == ["openai"]
    assert case.unit.evidence["developer_attribution_incomplete"]
    first = value["remote_head_sync"]["id"]
    # A later local commit is not a new remote movement or a new debt identity.
    case.unit.bindings["head"] = "c" * 40
    case.ancestor.add(("a" * 40, "c" * 40))
    again = remote.observe(case.runtime, case.snapshot, case.unit)
    remote.apply(case.runtime, case.snapshot, case.unit, again)
    assert again["remote_head_sync"]["id"] == first
    assert len(case.unit.evidence["remote_head_refreshes"]) == 1
    assert case.unit.evidence["remote_head_sync"]["provenance_before"] == "a" * 40


def test_live_worker_retains_claim_and_new_recovery_fence_is_not_deduplicated(case):
    case.unit.claim.quiescent = False
    case.unit.state = UnitState.RUNNING
    value = remote.observe(case.runtime, case.snapshot, case.unit)
    remote.apply(case.runtime, case.snapshot, case.unit, value)
    assert case.unit.state == UnitState.RUNNING and not case.unit.claim.quiescent
    case.fence[0] = "sealed-recovery"
    remote.apply(
        case.runtime,
        case.snapshot,
        case.unit,
        remote.observe(case.runtime, case.snapshot, case.unit),
    )
    assert case.unit.hold.kind == "remote_recovery_fence"
    assert case.unit.hold.evidence["fence"] == "sealed-recovery"
    assert len(case.unit.evidence["remote_head_refreshes"]) == 1


def test_already_incorporated_observed_remote_retains_provenance_debt(case):
    remote.apply(
        case.runtime,
        case.snapshot,
        case.unit,
        remote.observe(case.runtime, case.snapshot, case.unit),
    )
    original = copy.deepcopy(case.unit.evidence["remote_head_sync"])
    case.unit.bindings["head"] = "c" * 40
    case.ancestor.add(("b" * 40, "c" * 40))
    observed = remote.observe(case.runtime, case.snapshot, case.unit)
    remote.apply(case.runtime, case.snapshot, case.unit, observed)
    assert "remote_head_current" not in observed
    assert case.unit.evidence["remote_head_sync"]["id"] == original["id"]
    assert remote.request(case.unit)["remote_head_sync"]["local_head"] == "c" * 40
    assert remote.request(case.unit)["remote_head_sync"]["provenance_before"] == "a" * 40


def test_distinct_remote_churn_is_bounded_and_operator_hold_survives(case):
    previous = "a"
    for head in ("b", "c", "d"):
        case.pr["head"]["sha"] = head * 40
        case.ancestor.add(("a" * 40, head * 40))
        case.ancestor.add((previous * 40, head * 40))
        remote.apply(
            case.runtime,
            case.snapshot,
            case.unit,
            remote.observe(case.runtime, case.snapshot, case.unit),
        )
        previous = head
    assert case.unit.hold.kind == "operator"
    assert len(case.unit.evidence["remote_head_refreshes"]) == 3
    owner = Hold("operator", "owner requested pause", id="owner", actor="operator")
    case.unit.hold = owner
    case.fence[0] = "recovery"
    remote.apply(
        case.runtime,
        case.snapshot,
        case.unit,
        remote.observe(case.runtime, case.snapshot, case.unit),
    )
    assert case.unit.hold == owner


def test_remote_rewrite_cannot_discard_previously_observed_external_head(case):
    remote.apply(
        case.runtime,
        case.snapshot,
        case.unit,
        remote.observe(case.runtime, case.snapshot, case.unit),
    )
    original = copy.deepcopy(case.unit.evidence["remote_head_sync"])
    case.pr["head"]["sha"] = "c" * 40
    case.ancestor.add(("a" * 40, "c" * 40))  # sibling of the retained external b tip
    result = remote.checked(case.runtime, case.snapshot, case.unit)
    assert "previously observed" in result["remote_head_error"]
    assert case.unit.evidence["remote_head_sync"] == original


def test_issued_unknown_push_never_becomes_remote_debt(case, monkeypatch):
    def forbid(*args, **kwargs):
        pytest.fail("an unknown issued push must not authorize remote-head recovery")

    monkeypatch.setattr(remote, "observe", forbid)
    action = OutboxAction("push", "push", "u", {"head": "a" * 40}, 1)
    result = CampaignRuntime._push_observation(
        case.runtime,
        case.snapshot,
        action,
        PushObservation("push", "a" * 40, "sent_unknown", "b" * 40),
    )
    assert result.state == "unknown"
    assert "remote_head_sync" not in result.data


def test_checkpoint_reconcile_preserves_unpaid_descendant_debt(case):
    row = PushObservation("checkpoint", "a" * 40, "confirmed", "a" * 40)
    case.pusher.observations = lambda: (row,)
    case.pusher.reconcile = lambda identity: row
    from bmad_loop.campaign_supervisor import Observation

    case.runtime._push_observation = lambda *args: Observation(
        "unknown", {"head": row.head}, "older checkpoint remains uncertain"
    )
    case.runtime._checkpoint_wait = lambda snapshot, action, reason, data: Observation(
        "unknown", data, reason
    )
    case.runtime._checkpoint_done = lambda *args: pytest.fail(
        "unpaid backup debt cannot be marked done"
    )
    action = OutboxAction("checkpoint", "checkpoint", "u", {}, 1)
    observed = CampaignRuntime._reconcile_checkpoint(case.runtime, case.snapshot, action)
    assert observed.state == "unknown"
    assert "older checkpoint" in observed.reason


def test_missing_push_journal_reenters_only_original_immutable_action(case):
    from bmad_loop.campaign_supervisor import Observation

    case.pusher.observations = lambda: ()
    calls = []
    case.runtime._perform_push = lambda snapshot, action: calls.append(action) or Observation(
        "unknown", reason="still observing"
    )
    action = OutboxAction("original", "push", "u", {"head": "d" * 40}, 1, state="sent_unknown")
    observed = CampaignRuntime.reconcile_action(case.runtime, case.snapshot, action)
    assert calls == [action] and calls[0].payload["head"] == "d" * 40
    assert observed.state == "unknown"


@pytest.mark.parametrize("fault", [None, "id", "head", "branch", "fence"])
def test_adopted_owned_pr_retains_original_fence_and_exact_remote_debt(case, monkeypatch, fault):
    from bmad_loop import campaign_adoption as adoption

    observed = remote.observe(case.runtime, case.snapshot, case.unit)
    debt = observed["remote_head_sync"]
    case.unit.evidence["remote_head_sync"] = copy.deepcopy(debt)
    case.unit.bindings["adoption"] = {"state": "owned"}
    manifest = SimpleNamespace(
        pr_number=1, pr_node_id="PR1", branch="owned", target="main", head="a" * 40
    )
    monkeypatch.setattr(adoption, "transaction", lambda *args: (manifest, "provider", None))
    checks = []

    def qualify(*args, **kwargs):
        checks.append(kwargs)
        if fault == "fence":
            raise adoption.AdoptionError("original owner fence is unavailable")

    monkeypatch.setattr(adoption, "qualify", qualify)
    monkeypatch.setattr(adoption, "_remote_head", lambda *args: "b" * 40)
    monkeypatch.setattr(
        adoption.verify,
        "is_ancestor",
        lambda workspace, older, newer: older == newer or (older, newer) in case.ancestor,
    )
    case.runtime.adoption_resolver = object()
    request = copy.deepcopy(debt)
    if fault in {"id", "head", "branch"}:
        request[fault] = "changed"
    if fault:
        with pytest.raises(adoption.AdoptionError):
            adoption.guard_owned(
                case.runtime, case.snapshot, case.unit.id, remote_head_sync=request
            )
    else:
        adoption.guard_owned(case.runtime, case.snapshot, case.unit.id, remote_head_sync=request)
        with pytest.raises(adoption.AdoptionError):
            adoption.guard_owned(case.runtime, case.snapshot, case.unit.id)
    assert checks and all(row["allow_owned"] for row in checks)


@pytest.mark.parametrize("state", [UnitState.READY, UnitState.RUNNING])
def test_unfinished_stage_sync_requires_push_and_never_grants_completion(case, state):
    case.unit.state = state
    remote.apply(
        case.runtime,
        case.snapshot,
        case.unit,
        remote.observe(case.runtime, case.snapshot, case.unit),
    )
    case.snapshot.graph_revision += 1
    request = remote.request(case.unit, snapshot=case.snapshot)
    assert request["remote_resume_state"] == state.value
    assert request["remote_head_sync"]["graph_revision"] == case.snapshot.graph_revision
    case.unit.evidence["remote_head_continuation"] = {"state": state.value}
    case.unit.state = UnitState.RUNNING  # normal integrate launch from READY
    case.unit.evidence["integrated"] = {"head": "c" * 40}
    remote.resume_unfinished(case.snapshot, case.unit)
    assert "remote_head_continuation" in case.unit.evidence
    case.unit.evidence.pop("remote_head_sync")  # qualified mechanical integration accepted
    remote.resume_unfinished(case.snapshot, case.unit)
    assert "remote_head_continuation" in case.unit.evidence
    case.unit.evidence["pushed"] = "c" * 40
    remote.resume_unfinished(case.snapshot, case.unit)
    assert case.unit.state == state
    assert all(
        name not in case.unit.evidence
        for name in ("integrated", "pushed", "remote_head_continuation")
    )
    assert case.unit.legacy_outcome != "locally_complete"


def test_queued_candidate_keeps_acceptance_until_confirmed_removal(case):
    case.snapshot.spec = replace(
        case.snapshot.spec, github=replace(case.snapshot.spec.github, merge_mode="queue")
    )
    case.unit.state = UnitState.MERGE_REQUESTED
    case.unit.evidence["integrated"] = {"head": "a" * 40}
    case.unit.evidence["review"] = {"accepted": True}
    value = remote.observe(case.runtime, case.snapshot, case.unit)
    remote.apply(case.runtime, case.snapshot, case.unit, value)
    assert case.unit.state == UnitState.MERGE_REQUESTED
    assert case.unit.evidence["integrated"] == {"head": "a" * 40}
    assert case.unit.evidence["review"] == {"accepted": True}
    assert "remote_head_sync" not in case.unit.evidence
    assert case.unit.evidence["queue"]["remote_head_pending"] == value


@pytest.mark.parametrize("push_state", ["remote_changed", "sent_unknown"])
@pytest.mark.parametrize("later_hold", [None, "operator", "remote_recovery_fence"])
def test_verified_repair_remote_move_preserves_evidence_without_success_credit(
    project, push_state, later_hold
):
    from test_campaign_resolution_delivery import accepted, repair

    from bmad_loop.campaign_resolution_delivery import finish, next_step, validate_repair
    from bmad_loop.campaign_supervisor import Observation

    snapshot, unit = accepted(project)
    receipt = repair(project, unit)
    unit.bindings["head"] = receipt["head"]
    unit.evidence["repair_candidate"] = validate_repair(snapshot, unit, receipt)
    unit.claim = WorkerClaim(unit.id, 2, snapshot.generation, "verify", quiescent=True)
    accepted_before = copy.deepcopy(unit.evidence["accepted_resolution"])
    verification = {
        "outcome": "verified",
        "resolution_id": accepted_before["id"],
        "head": receipt["head"],
        "commands": [],
    }
    unit.evidence["resolution_verification"] = verification
    unit.evidence.update(integrated={"head": receipt["head"]}, review={"old": True})
    debt = {
        "id": "remote-debt",
        "head": "f" * 40,
        "local_head": receipt["head"],
        "provenance_before": accepted_before["coordinates"]["head"],
    }
    observed = {
        "head": receipt["head"],
        "backup_pending": True,
        "remote_head_sync": debt,
        "remote_recovery_fence": (
            "preserved-fence" if later_hold == "remote_recovery_fence" else None
        ),
    }
    calls = []
    runtime = SimpleNamespace(
        _pusher=lambda *args: SimpleNamespace(
            push=lambda action, head: PushObservation(action, head, push_state, "f" * 40)
        ),
        _push_observation=lambda *args: calls.append(args) or Observation("confirmed", observed),
        _set_hold=CampaignRuntime._set_hold,
    )
    following = next_step(snapshot, unit)
    action = SimpleNamespace(id="repair-push", payload=following.payload)
    result = finish(runtime, snapshot, unit, action)
    if push_state == "sent_unknown":
        assert result.state == "unknown" and not calls
        assert unit.evidence["accepted_resolution"] == accepted_before
        assert "resolution_remote_history" not in unit.evidence
        return
    assert result.state == "confirmed" and calls and "result" not in result.data
    owner = Hold("operator", "pause", id="owner", actor="operator")
    if later_hold == "operator":
        unit.hold = owner
    remote.interrupt_resolution(runtime, snapshot, unit, action, result.data)
    retained = unit.evidence["resolution_remote_history"][-1]
    assert retained["accepted_resolution"] == accepted_before
    assert retained["resolution_verification"] == verification
    assert retained["status"] == "interrupted-by-external-head"
    assert unit.evidence["external_backup_debt"] == {action.id: receipt["head"]}
    assert unit.evidence["remote_head_sync"] == debt
    assert unit.state == UnitState.SYNCING
    assert not any(
        name in unit.evidence
        for name in ("accepted_resolution", "verification", "review", "integrated")
    )
    assert "result" not in unit.evidence["resolution_history"][-1]
    if later_hold == "operator":
        assert unit.hold == owner
    elif later_hold == "remote_recovery_fence":
        assert unit.hold.kind == "remote_recovery_fence"
    else:
        assert unit.hold is None
