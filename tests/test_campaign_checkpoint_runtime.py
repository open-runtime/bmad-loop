"""Immutable checkpoint backup uses real worker progress, Git and remote refs."""

import dataclasses
import json
from pathlib import Path

import pytest
from conftest import git
from test_campaign_runtime import production_case as production_case

from bmad_loop import campaign_checkpoint as checkpoint
from bmad_loop import verify
from bmad_loop.campaign_model import CampaignError, Hold, OutboxAction, UnitState, WorkerClaim
from bmad_loop.campaign_recovery import RecoveryHold


@pytest.fixture
def checkpoint_case(production_case):
    case = production_case
    unit = case.snapshot.units["source:project:first"]
    allocated = case.runtime._perform_prepare_unit(
        case.snapshot,
        OutboxAction(
            "checkpoint-allocation", "prepare-unit", unit.id, {}, case.snapshot.generation
        ),
    )
    assert allocated.state == "confirmed"
    unit.bindings.update(allocated.data)
    unit_id = unit.id
    for state in (
        UnitState.PLANNING,
        UnitState.DRAFT,
        UnitState.READY,
        UnitState.CLAIMED,
        UnitState.RUNNING,
    ):
        unit.state = state
        if state == UnitState.CLAIMED:
            unit.claim = WorkerClaim(
                unit.id, 1, case.snapshot.generation, "live-worker", role="execution", writes=True
            )
        case.snapshot = case.store.save(case.snapshot, case.snapshot.revision)
        unit = case.snapshot.units[unit_id]
    launch = OutboxAction(
        "live-worker",
        "launch",
        unit.id,
        {"kind": "execution"},
        case.snapshot.generation,
        state="confirmed",
    )
    directory = case.store.directory(case.snapshot.id) / "workers" / launch.id
    directory.mkdir(parents=True)
    workspace = Path(unit.bindings["workspace"])
    request = {
        "campaign_id": case.snapshot.id,
        "unit_id": unit.id,
        "attempt": 1,
        "generation": case.snapshot.generation,
        "launch_id": launch.id,
        "branch": unit.bindings["branch"],
        "workspace": str(workspace),
        "unit": dataclasses.asdict(unit.spec),
        "source": unit.bindings["effective_source"],
        "checkpoint_interval_s": 20,
        "checkpoint_base": verify.rev_parse_head(workspace),
    }
    capsule = checkpoint.seal(workspace, request, baseline=unit.bindings["head"])
    request.update(capsule)
    launch.payload.update(capsule)
    case.snapshot.outbox.append(launch)
    case.snapshot = case.store.save(case.snapshot, case.snapshot.revision)
    unit = case.snapshot.units[unit_id]
    (directory / "request.json").write_text(json.dumps(request), encoding="utf-8")
    monitor = checkpoint.ProgressMonitor(request, directory)
    pusher = case.runtime._pusher(case.snapshot, unit)
    base = request["checkpoint_base"]
    assert pusher.push("initial-backup", base).state == "confirmed"
    unit.bindings["last_pushed_head"] = base
    case.unit, case.workspace, case.monitor, case.pusher = unit, workspace, monitor, pusher
    return case


def progress(case, text, *, path="src/first.txt"):
    product = case.workspace / path
    product.parent.mkdir(parents=True, exist_ok=True)
    product.write_text(text, encoding="utf-8")
    git(case.workspace, "add", path)
    git(case.workspace, "commit", "-m", "checkpoint: " + text.strip())
    return case.monitor.observe()


def action(case, receipt):
    pending = OutboxAction(
        "backup",
        "checkpoint",
        case.unit.id,
        {"launch_id": "live-worker", "progress": receipt},
        case.snapshot.generation,
    )
    case.snapshot.outbox.append(pending)
    return pending


def save_observation(pending, result):
    pending.state = "sent_unknown" if result.state == "unknown" else result.state
    pending.receipt.update(result.data)
    pending.error = result.reason


def remote_head(case):
    return git(case.remote, "rev-parse", "refs/heads/" + case.unit.bindings["branch"])


def test_unissued_checkpoint_coalesces_latest_valid_descendant_without_touching_dirty_tree(
    checkpoint_case,
):
    case = checkpoint_case
    older = progress(case, "first useful milestone\n")
    pending = action(case, older)
    latest = progress(case, "second useful milestone\n")
    product = case.workspace / "src/first.txt"
    product.write_text("next atomic edit still in progress\n", encoding="utf-8")
    index = git(case.workspace, "diff", "--cached", "--binary")
    result = case.runtime._perform_checkpoint(case.snapshot, pending)
    assert result.state == "confirmed"
    assert result.data["head"] == latest["head"]
    assert remote_head(case) == latest["head"]
    assert product.read_text(encoding="utf-8") == "next atomic edit still in progress\n"
    assert git(case.workspace, "diff", "--cached", "--binary") == index


def test_terminal_stopped_monitor_does_not_expire_original_sealed_backup(checkpoint_case):
    import time

    case = checkpoint_case
    case.monitor.clock = lambda: time.time() - 3600
    original = progress(case, "sealed before coordinator stopped\n")
    pending = action(case, original)
    newer = progress(case, "later coherent progress is now stale too\n")
    case.unit.claim.quiescent = True
    case.unit.claim.status = "finished"
    case.unit.state = UnitState.LOCALLY_COMPLETE
    assert case.monitor.thread is None
    result = case.runtime._reconcile_checkpoint(case.snapshot, pending)
    assert result.state == "confirmed" and result.data["head"] == original["head"]
    assert result.data["coalescing"] == "skipped_stale_observation"
    assert remote_head(case) == original["head"] != newer["head"]


def test_invalid_optional_coalescing_observation_is_visible_failure_not_fallback(checkpoint_case):
    case = checkpoint_case
    original = progress(case, "valid original checkpoint\n")
    pending = action(case, original)
    directory = case.store.directory(case.snapshot.id) / "workers" / case.unit.claim.launch_id
    (directory / checkpoint.NAME).write_text("{broken", encoding="utf-8")
    result = case.runtime._perform_checkpoint(case.snapshot, pending)
    assert result.state == "unknown" and result.data["backup_failures"] == 1
    assert result.data["coalescing"] == "observation_failed"
    assert not any(row.action_id == pending.id for row in case.pusher.observations())


def test_issued_checkpoint_reconciles_its_original_sha_after_lost_reply_and_later_work(
    checkpoint_case, monkeypatch
):
    case = checkpoint_case
    original = progress(case, "first published milestone\n")
    pending = action(case, original)
    selected = progress(case, "newest valid progress before any push intent\n")
    run, readback = case.pusher.git, case.pusher.remote_head
    calls, state = [], {"lost": False}

    def lose_reply(root, *args, **kwargs):
        result = run(root, *args, **kwargs)
        if args[0] == "push":
            calls.append(args)
            state["lost"] = True
            raise RecoveryHold("reply lost after remote accepted exact SHA")
        return result

    def unavailable():
        if state["lost"]:
            raise RecoveryHold("readback temporarily unavailable")
        return readback()

    monkeypatch.setattr(case.pusher, "git", lose_reply)
    monkeypatch.setattr(case.pusher, "remote_head", unavailable)
    monkeypatch.setattr(case.runtime, "_pusher", lambda *args: case.pusher)
    result = case.runtime._perform_checkpoint(case.snapshot, pending)
    assert result.state == "unknown"
    branch_intent = next(row for row in case.pusher.observations() if row.action_id == pending.id)
    assert branch_intent.head == selected["head"] and branch_intent.state == "sent_unknown"
    save_observation(pending, result)
    newer = progress(case, "later work is a different checkpoint\n")
    state["lost"] = False
    reconciled = case.runtime._reconcile_checkpoint(case.snapshot, pending)
    assert reconciled.state == "confirmed" and reconciled.data["head"] == selected["head"]
    assert remote_head(case) == selected["head"] != newer["head"]
    assert pending.payload["progress"]["head"] == original["head"] != selected["head"]
    assert len(calls) == 1


def test_prepared_branch_intent_freezes_head_before_a_new_worker_checkpoint(
    checkpoint_case, monkeypatch
):
    from bmad_loop import campaign_recovery

    case = checkpoint_case
    original = progress(case, "prepared original milestone\n")
    pending = action(case, original)
    selected = progress(case, "coalesced before durable preparation\n")
    path = case.pusher._path(pending.id)
    save = campaign_recovery._save

    def crash_after_prepared(destination, value):
        save(destination, value)
        if destination == path and value.get("state") == "prepared":
            raise RuntimeError("coordinator crashed after exact intent, before issuing push")

    monkeypatch.setattr(campaign_recovery, "_save", crash_after_prepared)
    failed = case.runtime._perform_checkpoint(case.snapshot, pending)
    assert failed.state == "unknown"
    assert json.loads(path.read_text(encoding="utf-8"))["head"] == selected["head"]
    assert json.loads(path.read_text(encoding="utf-8"))["state"] == "prepared"
    save_observation(pending, failed)
    monkeypatch.setattr(campaign_recovery, "_save", save)
    latest = progress(case, "newer unissued work\n")
    result = case.runtime._reconcile_checkpoint(case.snapshot, pending)
    assert result.state == "confirmed" and result.data["head"] == selected["head"]
    assert remote_head(case) == selected["head"] != latest["head"]
    assert pending.payload["progress"]["head"] == original["head"]


def test_backup_failure_budget_stops_new_writes_without_replacement_or_resend(
    checkpoint_case, monkeypatch
):
    case = checkpoint_case
    original = progress(case, "locally retained milestone\n")
    pending = action(case, original)
    calls = []

    class UnavailablePusher:
        def observations(self):
            return ()

        def push(self, identifier, head):
            calls.append((identifier, head))
            raise OSError("remote unavailable before a branch intent can be created")

        def observe_existing(self, identifier, head):
            raise OSError("remote readback remains unavailable")

    monkeypatch.setattr(case.runtime, "_pusher", lambda *args: UnavailablePusher())
    for _ in range(case.snapshot.spec.checkpoints.max_backup_failures + 3):
        case.monitor.observe()  # The live worker monitor continues its read-only samples.
        result = case.runtime._reconcile_checkpoint(case.snapshot, pending)
        save_observation(pending, result)
    assert result.state == "unknown" and result.data["backup_retry_exhausted"]
    assert len(calls) == case.snapshot.spec.checkpoints.max_backup_failures
    assert {identifier for identifier, _ in calls} == {pending.id}
    assert pending.payload["progress"]["head"] == original["head"]


def test_exhausted_prepared_backup_keeps_readback_without_authorizing_a_write(
    checkpoint_case, monkeypatch
):
    case = checkpoint_case
    original = progress(case, "exact target already reached remote\n")
    pending = action(case, original)
    path = case.pusher._path(pending.id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "action_id": pending.id,
                "head": original["head"],
                "state": "prepared",
                "recovery_action": None,
            }
        ),
        encoding="utf-8",
    )
    git(
        case.workspace,
        "push",
        "origin",
        original["head"] + ":refs/heads/" + case.unit.bindings["branch"],
    )
    pending.receipt["backup_failures"] = case.snapshot.spec.checkpoints.max_backup_failures
    monkeypatch.setattr(
        case.pusher, "push", lambda *args: pytest.fail("exhausted backup issued another write")
    )
    monkeypatch.setattr(case.runtime, "_pusher", lambda *args: case.pusher)
    newer = progress(case, "different newer progress stays local\n")
    result = case.runtime._reconcile_checkpoint(case.snapshot, pending)
    assert result.state == "confirmed" and result.data["head"] == original["head"]
    assert remote_head(case) == original["head"] != newer["head"]


def test_exhausted_unissued_backup_observes_operator_push_and_records_its_history(
    checkpoint_case, monkeypatch
):
    case = checkpoint_case
    original = progress(case, "operator backed this sealed checkpoint\n")
    pending = action(case, original)
    pending.receipt["backup_failures"] = case.snapshot.spec.checkpoints.max_backup_failures
    git(
        case.workspace,
        "push",
        "origin",
        original["head"] + ":refs/heads/" + case.unit.bindings["branch"],
    )
    monkeypatch.setattr(case.pusher, "push", lambda *args: pytest.fail("readback issued a write"))
    monkeypatch.setattr(case.runtime, "_pusher", lambda *args: case.pusher)
    newer = progress(case, "newer unpublished work\n")
    result = case.runtime._reconcile_checkpoint(case.snapshot, pending)
    assert result.state == "confirmed" and result.data["head"] == original["head"]
    assert remote_head(case) == original["head"] != newer["head"]
    durable = next(row for row in case.pusher.observations() if row.action_id == pending.id)
    assert durable.state == "confirmed" and durable.head == original["head"]


@pytest.mark.parametrize("branch_state", [None, "prepared", "sent_unknown"])
def test_explicit_resume_rearms_only_never_issued_epoch_and_preserves_history(
    checkpoint_case, monkeypatch, branch_state
):
    case = checkpoint_case
    original = progress(case, "retained during an exhausted outage\n")
    pending = action(case, original)
    pending.state = "sent_unknown"
    cap = case.snapshot.spec.checkpoints.max_backup_failures
    pending.receipt.update(
        backup_failures=cap, backup_total_failures=cap, backup_retry_exhausted=True
    )
    if branch_state is not None:
        path = case.pusher._path(pending.id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "action_id": pending.id,
                    "head": original["head"],
                    "state": branch_state,
                    "recovery_action": None,
                }
            ),
            encoding="utf-8",
        )
    case.runtime.authorize_backup_resume(case.snapshot)
    case.runtime.prepare_reconciliation(case.snapshot, pending)
    case.snapshot = case.store.save(case.snapshot, case.snapshot.revision)
    pending = next(row for row in case.snapshot.outbox if row.id == pending.id)
    if branch_state == "sent_unknown":
        assert pending.receipt["backup_failures"] == cap
        assert "backup_retry_history" not in pending.receipt
        monkeypatch.setattr(
            case.pusher,
            "push",
            lambda *args: pytest.fail("resume retried an issued uncertain push"),
        )
        monkeypatch.setattr(case.runtime, "_pusher", lambda *args: case.pusher)
        result = case.runtime._reconcile_checkpoint(case.snapshot, pending)
        assert result.state == "unknown"
    else:
        assert pending.receipt["backup_failures"] == 0
        assert pending.receipt["backup_retry_history"][0]["failures"] == cap
        result = case.runtime._reconcile_checkpoint(case.snapshot, pending)
        assert result.state == "confirmed" and result.data["head"] == original["head"]
        assert result.data["backup_retry_history"][0]["failures"] == cap
        assert result.data["backup_total_failures"] == cap


def test_resume_epoch_cannot_renew_after_repeated_preintent_outage(checkpoint_case, monkeypatch):
    case = checkpoint_case
    pending = action(case, progress(case, "retained through repeated pre-push outage\n"))
    pending.state = "sent_unknown"
    cap = case.snapshot.spec.checkpoints.max_backup_failures
    pending.receipt["backup_failures"] = cap
    case.runtime.authorize_backup_resume(case.snapshot)
    case.runtime.prepare_reconciliation(case.snapshot, pending)
    original_readback = case.pusher.remote_head

    def unavailable():
        raise RecoveryHold("remote unavailable before any branch intent")

    monkeypatch.setattr(case.pusher, "remote_head", unavailable)
    monkeypatch.setattr(case.runtime, "_pusher", lambda *args: case.pusher)
    for _ in range(cap):
        result = case.runtime._reconcile_checkpoint(case.snapshot, pending)
        save_observation(pending, result)
    assert pending.receipt["backup_failures"] == cap
    assert not any(row.action_id == pending.id for row in case.pusher.observations())
    case.runtime.prepare_reconciliation(case.snapshot, pending)
    assert pending.receipt["backup_failures"] == cap
    assert len(pending.receipt["backup_retry_history"]) == 1
    # A subsequent explicit resume grants another finite epoch, retaining both histories.
    case.runtime.authorize_backup_resume(case.snapshot)
    case.runtime.prepare_reconciliation(case.snapshot, pending)
    assert pending.receipt["backup_failures"] == 0
    assert len(pending.receipt["backup_retry_history"]) == 2
    monkeypatch.setattr(case.pusher, "remote_head", original_readback)
    assert case.runtime._reconcile_checkpoint(case.snapshot, pending).state == "confirmed"


def test_technical_failure_backup_resume_preserves_hold_and_recovery_fence(checkpoint_case):
    case = checkpoint_case
    original = progress(case, "sealed before technical failure\n")
    pending = action(case, original)
    pending.state = "sent_unknown"
    pending.receipt["backup_failures"] = case.snapshot.spec.checkpoints.max_backup_failures
    case.unit.hold = Hold("technical_failure", "child requires diagnosis", id="technical-hold")
    (case.pusher.scope / "fence.json").write_text(
        json.dumps({"schema_version": 1, "active": True, "action_id": "append-only-recovery"}),
        encoding="utf-8",
    )
    case.runtime.authorize_backup_resume(case.snapshot)
    case.runtime.prepare_reconciliation(case.snapshot, pending)
    assert pending.receipt["backup_failures"] == 0
    fenced = case.runtime._reconcile_checkpoint(case.snapshot, pending)
    assert fenced.state == "unknown" and "recovery fence" in fenced.reason
    assert remote_head(case) != original["head"]
    (case.pusher.scope / "fence.json").write_text(
        json.dumps({"schema_version": 1, "active": False, "action_id": "append-only-recovery"}),
        encoding="utf-8",
    )
    save_observation(pending, fenced)
    backed = case.runtime._reconcile_checkpoint(case.snapshot, pending)
    assert backed.state == "confirmed" and remote_head(case) == original["head"]
    assert case.unit.hold.kind == "technical_failure" and case.unit.hold.id == "technical-hold"


@pytest.mark.parametrize("gate", ["operator", "required_review", "channel"])
def test_explicit_resume_preserves_operator_and_approval_holds(checkpoint_case, gate):
    from bmad_loop import campaign_hold

    case = checkpoint_case
    pending = action(case, progress(case, "retained behind explicit gate\n"))
    pending.state = "sent_unknown"
    cap = case.snapshot.spec.checkpoints.max_backup_failures
    pending.receipt["backup_failures"] = cap
    if gate == "channel":
        campaign_hold.write(
            campaign_hold.channel(case.store.directory(case.snapshot.id), case.unit.id),
            campaign=case.snapshot.id,
            unit=case.unit.id,
            hold="actual-operator-channel",
        )
    else:
        case.unit.hold = Hold(gate, "explicit gate remains active", id="preserved-gate")
    case.runtime.authorize_backup_resume(case.snapshot)
    case.runtime.prepare_reconciliation(case.snapshot, pending)
    assert pending.receipt["backup_failures"] == cap
    assert "backup_retry_history" not in pending.receipt


def test_operator_channel_after_rearm_prevents_prepared_backup_write(checkpoint_case):
    from bmad_loop import campaign_hold

    case = checkpoint_case
    original = progress(case, "retained before the hold arrived\n")
    pending = action(case, original)
    pending.state = "sent_unknown"
    pending.receipt["backup_failures"] = case.snapshot.spec.checkpoints.max_backup_failures
    case.runtime.authorize_backup_resume(case.snapshot)
    case.runtime.prepare_reconciliation(case.snapshot, pending)
    assert pending.receipt["backup_failures"] == 0
    campaign_hold.write(
        campaign_hold.channel(case.store.directory(case.snapshot.id), case.unit.id),
        campaign=case.snapshot.id,
        unit=case.unit.id,
        hold="takeover-after-rearm",
    )
    result = case.runtime._reconcile_checkpoint(case.snapshot, pending)
    assert result.state == "unknown" and "operator hold" in result.reason
    assert remote_head(case) != original["head"]
    assert not any(row.action_id == pending.id for row in case.pusher.observations())


def test_unowned_committed_checkpoint_is_refused_before_remote_backup(checkpoint_case):
    case = checkpoint_case
    baseline = remote_head(case)
    outside = progress(case, "unrelated product must stay local\n", path="outside-owned-scope.txt")
    pending = action(case, outside)
    with pytest.raises(CampaignError, match="outside the owned scope"):
        case.runtime._perform_checkpoint(case.snapshot, pending)
    assert remote_head(case) == baseline
    assert not any(row.action_id == pending.id for row in case.pusher.observations())


def test_pending_backup_suppresses_replacement_while_progress_remains_visible(checkpoint_case):
    case = checkpoint_case
    original = progress(case, "pending checkpoint\n")
    pending = action(case, original)
    pending.state = "sent_unknown"
    newest = progress(case, "newest progress\n")
    case.unit.evidence["checkpoint_progress"] = newest
    case.snapshot.spec = dataclasses.replace(
        case.snapshot.spec,
        checkpoints=dataclasses.replace(case.snapshot.spec.checkpoints, target_interval_s=1),
    )
    case.unit.bindings["last_checkpoint_at"] = case.runtime.clock() - 2
    assert not any(row.kind == "checkpoint" for row in case.runtime.observe_workers(case.snapshot))
    observed = case.runtime._perform_checkpoint_progress(
        case.snapshot,
        OutboxAction("progress", "checkpoint-progress", case.unit.id, {}, case.snapshot.generation),
    )
    assert observed.data["head"] == newest["head"] and observed.data["unbacked_committed"]
    assert observed.data["backup_debt"][0]["action_id"] == pending.id
    pending.state = "confirmed"
    eligible = [
        row for row in case.runtime.observe_workers(case.snapshot) if row.kind == "checkpoint"
    ]
    assert len(eligible) == 1 and eligible[0].payload["progress"]["head"] == newest["head"]


@pytest.mark.parametrize("stage", ["closure", "planning_review"])
def test_writable_stage_uses_stage_contract_not_review_named_role(checkpoint_case, stage):
    case = checkpoint_case
    case.unit.claim.role = "planning_review"
    launch = next(row for row in case.snapshot.outbox if row.id == case.unit.claim.launch_id)
    launch.payload.update(kind="stage", stage=stage)
    case.unit.evidence["checkpoint_progress"] = progress(case, "coherent closure milestone\n")
    case.snapshot.spec = dataclasses.replace(
        case.snapshot.spec,
        checkpoints=dataclasses.replace(case.snapshot.spec.checkpoints, target_interval_s=1),
    )
    case.unit.bindings["last_checkpoint_at"] = case.runtime.clock() - 2
    emitted = {row.kind for row in case.runtime.observe_workers(case.snapshot)}
    if stage == "closure":
        assert {"checkpoint", "checkpoint-progress"} <= emitted
    else:
        assert not {"checkpoint", "checkpoint-progress"}.intersection(emitted)
