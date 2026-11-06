"""Fresh engines and retained recovery share one monotonic campaign budget."""

import dataclasses

from bmad_loop.campaign_capacity import from_teamrouter
from bmad_loop.campaign_model import (
    CampaignSnapshot,
    CampaignSpec,
    CampaignUnit,
    RoleCandidate,
    RoleSpec,
    UnitSpec,
    UnitState,
)
from bmad_loop.campaign_scheduler import select_role
from bmad_loop.campaign_store import CampaignStore, add_action
from bmad_loop.campaign_usage import admission_reason, apply, legacy_receipt, remaining
from bmad_loop.model import RunState, SessionRecord, StoryTask, TokenUsage


def state():
    task = StoryTask(
        "u", 1, attempt=2, review_cycle=2, dev_usage_relaunches=1, review_unlaunched_cycles=1
    )
    task.record_session(
        SessionRecord(
            "dev-1",
            "dev",
            "completed",
            session_id="native-1",
            usage=TokenUsage(input_tokens=120, output_tokens=30),
            models=("gpt-6-sol",),
        )
    )
    run = RunState("original", "/repository", "")
    run.tasks["u"] = task
    unit = CampaignUnit(UnitSpec("s:project:u", "s", "epic", "U"), UnitState.READY)
    snapshot = CampaignSnapshot(
        "campaign",
        "a/b",
        CampaignSpec("usage"),
        {unit.id: unit},
        config_snapshot={
            "policy": {
                "limits": {
                    "max_dev_attempts": 3,
                    "max_review_cycles": 2,
                    "max_tokens_per_story": 100,
                    "cache_read_weight": 0.1,
                }
            }
        },
    )
    return snapshot, unit, run, task


def record(snapshot, unit, run):
    apply(snapshot, unit, {"accounting": legacy_receipt(run)}, role="execution")


def test_original_recovery_and_retained_finalization_charge_one_monotonic_stream():
    snapshot, unit, run, task = state()
    record(snapshot, unit, run)
    assert (unit.evidence["dev_attempts_spent"], unit.evidence["review_cycles_spent"]) == (1, 1)
    assert unit.evidence["usage"]["tokens"]["input_tokens"] == 120
    replay = dataclasses.replace(run, run_id="retained", replay_of="original")
    record(snapshot, unit, replay)
    record(snapshot, unit, run)  # Delayed original recovery receipt.
    assert unit.evidence["dev_attempts_spent"] == 1
    assert unit.evidence["usage"]["tokens"]["input_tokens"] == 120
    assert len(snapshot.bindings["usage_ledger"]["streams"]) == 1
    task.attempt += 1
    task.record_session(
        SessionRecord(
            "dev-2",
            "dev",
            "completed",
            session_id="native-2",
            usage=TokenUsage(input_tokens=20),
            models=("claude-fable-5-1",),
        )
    )
    record(snapshot, unit, replay)
    assert unit.evidence["dev_attempts_spent"] == 2
    assert unit.evidence["usage"]["tokens"]["input_tokens"] == 140
    assert remaining(snapshot, unit) == (1, 1)


def test_fresh_amendment_engine_cannot_reset_attempt_token_or_advisory_warning_budget():
    snapshot, unit, run, task = state()
    record(snapshot, unit, run)
    amended = RunState("new-amendment-engine", run.project, "")
    amended.tasks["u"] = StoryTask("u", 1, attempt=2, review_cycle=1)
    amended.tasks["u"].record_session(
        SessionRecord("dev", "dev", "completed", usage=TokenUsage(input_tokens=50))
    )
    record(snapshot, unit, amended)
    assert unit.evidence["dev_attempts_spent"] == 3
    assert remaining(snapshot, unit) == (0, 0)
    assert "exhausted" in admission_reason(snapshot, unit, {"kind": "execution"})
    assert unit.evidence["usage"]["legacy_tokens"]["input_tokens"] == 170
    assert sum(event["kind"] == "story-token-budget-warning" for event in snapshot.events) == 1
    assert admission_reason(snapshot, unit, {"kind": "verify"}) == ""


def test_approved_re_reviews_after_base_changes_do_not_spend_fault_budget():
    snapshot, unit, _, _ = state()
    for index in range(6):
        sample = {
            "schema_version": 1,
            "kind": "stage",
            "stream_id": f"stage:review-{index}",
            "tokens": {"input_tokens": 10},
            "usage_complete": True,
            "session_observed": True,
        }
        receipt = {
            "accounting": sample,
            "outcome": "stage_complete",
            "validated": {"verdict": "approved"},
        }
        apply(snapshot, unit, receipt, role="publication_review")
    assert unit.review_attempts == 0
    assert admission_reason(snapshot, unit, {"kind": "stage", "role": "publication_review"}) == ""
    for index in range(2):
        sample = {
            "schema_version": 1,
            "kind": "stage",
            "stream_id": f"stage:fault-{index}",
            "tokens": {},
            "usage_complete": True,
            "session_observed": True,
        }
        receipt = {"accounting": sample, "outcome": "invalid_stage_result"}
        apply(snapshot, unit, receipt, role="publication_review")
        apply(snapshot, unit, receipt, role="publication_review")
    assert unit.review_attempts == 2
    assert "fault budget exhausted" in admission_reason(
        snapshot, unit, {"kind": "stage", "role": "publication_review"}
    )


def test_missing_actual_counters_are_visible_and_stop_fresh_engine_admission():
    snapshot, unit, _, _ = state()
    apply(
        snapshot,
        unit,
        {
            "accounting": {
                "schema_version": 1,
                "kind": "unknown",
                "stream_id": "worker:missing",
                "legacy_counters_unknown": True,
                "usage_complete": False,
                "unavailable": "native state unreadable",
            }
        },
        role="execution",
    )
    assert not unit.evidence["usage"]["complete"]
    assert "unavailable" in admission_reason(snapshot, unit, {"kind": "execution"})


def test_router_eligible_account_count_is_not_a_concurrency_ceiling():
    candidate = RoleCandidate("codex", "astra")
    route = from_teamrouter(
        {
            "availability": {
                "astra": {
                    "observedAt": 100000,
                    "accounts": [{"eligible": True, "quotaKnown": True}],
                }
            }
        },
        candidate,
        now=100,
    )
    assert route.available and route.model_access and not route.concurrency_known
    route = dataclasses.replace(route, active=12)
    spec = CampaignSpec(
        "parallel", roles=(("planning", RoleSpec(profile="codex", model="astra", max_sessions=24)),)
    )
    assert select_role(spec, "planning", {("codex", "astra"): route})[0] == candidate
    fixed = dataclasses.replace(route, concurrency_known=True, slots=2)
    assert select_role(spec, "planning", {("codex", "astra"): fixed})[0] is None


def test_stale_or_unknown_quota_still_refuses_without_inventing_capacity():
    candidate = RoleCandidate("codex", "astra")
    raw = {
        "availability": {
            "astra": {"observedAt": 1000, "accounts": [{"eligible": True, "quotaKnown": True}]}
        }
    }
    route = from_teamrouter(raw, candidate, now=1000)
    assert not route.available and not route.known
    raw["availability"]["astra"]["observedAt"] = 1000000
    raw["availability"]["astra"]["accounts"][0]["quotaKnown"] = False
    assert not from_teamrouter(raw, candidate, now=1000).available


def test_budget_exhaustion_is_held_before_any_launch_claim_or_capacity_is_reserved(
    project, tmp_path
):
    from bmad_loop.campaign_runtime import CampaignRuntime

    snapshot, unit, run, task = state()
    snapshot.spec = dataclasses.replace(snapshot.spec, units=(unit.spec,))
    unit.bindings.update(workspace=str(project.project), branch="campaign-unit")
    task.attempt = 4  # One usage relaunch is uncharged; three real attempts spent.
    record(snapshot, unit, run)
    store = CampaignStore(tmp_path / "state")
    store.create(snapshot)
    runtime = CampaignRuntime(project.project, store)
    action = add_action(snapshot, "launch", unit.id, {"kind": "execution"})
    runtime.control(snapshot, {"kind": "prepare-effect", "action": dataclasses.asdict(action)})
    assert action.state == "rejected"
    assert unit.claim is None and unit.attempt == 0 and unit.hold.kind == "lifetime_budget"
    assert unit.state == UnitState.READY


def test_actual_stage_token_receipt_survives_invalid_completion_and_is_monotonic(tmp_path):
    from bmad_loop.adapters.base import SessionResult
    from bmad_loop.campaign_usage import save_stage_usage, worker_accounting

    snapshot, unit, _, _ = state()
    directory = tmp_path / "worker"
    directory.mkdir()
    request = {
        "kind": "stage",
        "launch_id": "review",
        "role": "publication_review",
        "execution": str(directory / "execution.json"),
    }
    save_stage_usage(
        request,
        directory,
        TokenUsage(input_tokens=123, models=("gpt-6-astra",)),
        SessionResult(status="completed", session_id="native"),
    )
    sample = worker_accounting(request, {"outcome": "failed", "error": "Invalid model artifact"})
    assert sample["tokens"]["input_tokens"] == 123 and sample["models"] == ["gpt-6-astra"]
    apply(snapshot, unit, {"accounting": sample, "outcome": "failed"}, role="publication_review")
    apply(snapshot, unit, {"accounting": sample, "outcome": "failed"}, role="publication_review")
    assert unit.review_attempts == 1
    assert unit.evidence["usage"]["tokens"]["input_tokens"] == 123


def test_native_legacy_usage_receipt_uses_original_replay_lineage(project, tmp_path):
    from bmad_loop import runs
    from bmad_loop.campaign_usage import worker_accounting
    from bmad_loop.runs import save_state

    snapshot, unit, native, _ = state()
    native.project = str(project.project)
    original = runs.run_dir_for(project.project, native.run_id)
    original.mkdir(parents=True)
    save_state(original, native)
    retained = dataclasses.replace(native, run_id="retained", replay_of=native.run_id)
    directory = runs.run_dir_for(project.project, retained.run_id)
    directory.mkdir(parents=True)
    save_state(directory, retained)
    request = {
        "kind": "recovery",
        "launch_id": "finish",
        "workspace": str(project.project),
        "run_id": "outer-recovery",
    }
    sample = worker_accounting(
        request, {"outcome": "locally_complete", "run_id": "retained", "run_dir": str(directory)}
    )
    assert sample["stream_id"] == "legacy:original"
    apply(snapshot, unit, {"accounting": sample}, role="recovery")
    assert unit.evidence["dev_attempts_spent"] == 1


def test_interrupted_owned_worker_keeps_native_attempts_before_a_fresh_engine(
    project, tmp_path, monkeypatch
):
    import types

    from bmad_loop import campaign_runtime, runs, verify
    from bmad_loop.campaign_model import WorkerClaim
    from bmad_loop.runs import save_state

    snapshot, unit, native, _ = state()
    native.project = str(project.project)
    directory = runs.run_dir_for(project.project, native.run_id)
    directory.mkdir(parents=True)
    save_state(directory, native)
    store = CampaignStore(tmp_path / "state")
    runtime = campaign_runtime.CampaignRuntime(project.project, store)
    unit.state = UnitState.RUNNING
    unit.bindings.update(
        workspace=str(project.project), head=verify.rev_parse_head(project.project)
    )
    unit.claim = WorkerClaim(unit.id, 1, 0, "launch", run_id=native.run_id, role="execution")
    worker = store.directory(snapshot.id) / "workers" / "launch"
    worker.mkdir(parents=True)
    request = {
        "kind": "execution",
        "launch_id": "launch",
        "workspace": str(project.project),
        "run_id": native.run_id,
    }
    import json

    (worker / "request.json").write_text(json.dumps(request), encoding="utf-8")
    monkeypatch.setattr(campaign_runtime, "observe", lambda *a, **k: {"state": "interrupted"})
    monkeypatch.setattr(
        runtime,
        "template",
        lambda _: types.SimpleNamespace(
            policy=types.SimpleNamespace(mux=types.SimpleNamespace(backend=""))
        ),
    )
    from bmad_loop.adapters import multiplexer

    monkeypatch.setattr(multiplexer, "configure_multiplexer", lambda *a, **k: None)
    monkeypatch.setattr(
        multiplexer, "get_multiplexer", lambda: types.SimpleNamespace(list_window_ids=lambda _: [])
    )
    action = add_action(snapshot, "observe-worker", unit.id, {"launch_id": "launch"})
    observed = runtime._perform_observe_worker(snapshot, action)
    assert observed.state == "confirmed" and observed.data["quiescence_confirmed"]
    runtime._apply_worker(snapshot, unit, action, observed.data)
    assert unit.claim.quiescent and unit.evidence["dev_attempts_spent"] == 1
    assert unit.evidence["usage"]["tokens"]["input_tokens"] == 120
    assert unit.hold.kind == "technical_failure"
