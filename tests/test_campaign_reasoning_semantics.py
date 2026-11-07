"""Semantic jobs connect canonical evidence to real campaign execution paths."""

from dataclasses import replace

import pytest
from test_campaign_core import make_snapshot
from test_campaign_headless import prepared as prepared
from test_campaign_runtime_intake import observed

from bmad_loop import campaign_intake_flow as intake
from bmad_loop import campaign_reasoning_plan as plan
from bmad_loop import campaign_reasoning_results as results
from bmad_loop.campaign_intake import IntakeBatch
from bmad_loop.campaign_jobs import validate_result
from bmad_loop.campaign_model import (
    CampaignError,
    Hold,
    PlanningSpec,
    ReasoningSpec,
    RoleSpec,
    UnitState,
)
from bmad_loop.campaign_planning_flow import activate, next_stage
from bmad_loop.campaign_runtime import CampaignRuntime
from bmad_loop.campaign_store import CampaignStore, add_action


def case(tmp_path, prepared, purposes):
    _, _, _, candidate, cap = prepared
    snapshot = make_snapshot()
    snapshot.repository = "a/b"
    snapshot.spec = replace(
        snapshot.spec,
        reasoning=ReasoningSpec(enabled=True, purposes=tuple(purposes)),
        planning=PlanningSpec(enabled=True),
        roles=tuple(
            (name, RoleSpec(pool="approved"))
            for name in sorted(set(plan.ROLES.values()) | {"execution"})
        ),
        role_pools=(("approved", (candidate,)),),
    )
    snapshot.config_snapshot["reasoning_capabilities"] = {
        candidate.profile + ":" + candidate.model: cap
    }
    snapshot.bindings["inventory"] = {"complete": True, "issues": [], "pulls": []}
    store = CampaignStore(tmp_path / "campaigns")
    snapshot = store.create(snapshot)
    runtime = CampaignRuntime(tmp_path, store, clock=lambda: 100)
    return snapshot, runtime, candidate


def prepare_jobs(snapshot, runtime):
    proposal = next(
        item for item in runtime.observe_workers(snapshot) if item.kind == "semantic-plan"
    )
    action = add_action(snapshot, proposal.kind, unit=proposal.unit, payload=proposal.payload)
    observed_effect = runtime.perform(snapshot, action)
    runtime.apply(snapshot, action, observed_effect)
    action.state = "confirmed"
    return snapshot.jobs


def result_for(snapshot, purpose):
    evidence = plan.context(snapshot, purpose)
    return {
        "schema_version": 1,
        "context_digest": plan.fingerprint(snapshot, purpose),
        "citations": [evidence["units"][0]["source_id"]],
    }


def apply(snapshot, runtime, purpose, result):
    row = next(row for row in snapshot.jobs.values() if row["request"]["purpose"] == purpose)
    validate_result(result, row["request"]["output_schema"])
    row.update(result=result, validation="valid", state="validated")
    results.apply_result(runtime, snapshot, row)
    row["state"] = "applied"
    return row


def test_priority_changes_real_scheduler_order_once_and_preserves_contracts(tmp_path, prepared):
    snapshot, runtime, _ = case(tmp_path, prepared, ["priority"])
    prepare_jobs(snapshot, runtime)
    before = {key: unit.spec for key, unit in snapshot.units.items()}
    order = list(reversed(snapshot.units))
    apply(
        snapshot,
        runtime,
        "priority",
        {
            **result_for(snapshot, "priority"),
            "order": order,
            "rationale": "Unlock disjoint work first",
        },
    )
    assert snapshot.graph_revision == 2
    assert sorted(snapshot.units, key=lambda key: snapshot.units[key].spec.order) == order
    assert all(
        replace(snapshot.units[key].spec, order=before[key].order) == before[key] for key in before
    )
    assert plan.proposals(runtime, snapshot) == ()


def test_priority_rechecks_head_after_successful_json_validation(tmp_path, prepared):
    snapshot, runtime, _ = case(tmp_path, prepared, ["priority"])
    prepare_jobs(snapshot, runtime)
    result = {
        **result_for(snapshot, "priority"),
        "order": list(reversed(snapshot.units)),
        "rationale": "Priority",
    }
    next(iter(snapshot.units.values())).evidence["local"] = {"head": "new-head", "base": "main"}
    with pytest.raises(CampaignError, match="current validated result"):
        apply(snapshot, runtime, "priority", result)
    assert snapshot.graph_revision == 1


def test_routing_only_prefers_approved_and_currently_eligible_candidates(tmp_path, prepared):
    snapshot, runtime, candidate = case(tmp_path, prepared, ["routing"])
    prepare_jobs(snapshot, runtime)
    apply(
        snapshot,
        runtime,
        "routing",
        {
            **result_for(snapshot, "routing"),
            "recommendations": [
                {
                    "role": "execution",
                    "profile": candidate.profile,
                    "model": candidate.model,
                    "effort": candidate.effort,
                    "rationale": "Work matches route",
                }
            ],
        },
    )
    assert results.preferred_candidate(snapshot, "execution", (candidate,)) == candidate
    assert results.preferred_candidate(snapshot, "execution", ()) is None
    snapshot.config_revision += 1
    assert results.preferred_candidate(snapshot, "execution", (candidate,)) is None


def test_bulk_triage_reserves_all_exact_inputs_for_existing_triage_stage(tmp_path, prepared):
    snapshot, runtime, candidate = case(tmp_path, prepared, ["triage"])
    observations = (observed(7), observed(8))
    intake.apply_batch(
        snapshot, IntakeBatch(observations, "2026-11-05T10:00:00Z", True, 0), observed_at=100
    )
    prepare_jobs(snapshot, runtime)
    assert plan.defer_intake(snapshot, now=101)
    assert not plan.defer_intake(snapshot, now=131)
    versions = list(intake.untriaged(snapshot))
    apply(
        snapshot,
        runtime,
        "triage",
        {
            **result_for(snapshot, "triage"),
            "groups": [
                {
                    "input_versions": [version],
                    "suggestion": "Check overlap with existing story before new work",
                }
                for version in versions
            ],
        },
    )
    assert not intake.untriaged(snapshot)
    controls = snapshot.bindings["reasoning_decisions"]["triage"]["controls"]
    assert len(controls) == 2
    from bmad_loop import campaign_runtime_intake as native

    for key in controls:
        unit = snapshot.units[key]
        assert native.control(unit)["state"] == "triage_pending"
        unit.state = UnitState.READY
        stage = native.stage_proposal(snapshot, key, candidate)
        assert stage and stage.payload["result_contract"] == native.CONTRACT
        native.prepare_stage(snapshot, key, stage.payload, launch_id="launch-" + key)
        assert native.control(unit)["state"] == "triage_running"
        assert results.merge_into_stage_context(runtime, snapshot, key, "triage")[
            "semantic_proposals"
        ]


def test_bulk_triage_missing_input_cannot_partially_reserve_batch(tmp_path, prepared):
    snapshot, runtime, _ = case(tmp_path, prepared, ["triage"])
    intake.apply_batch(
        snapshot,
        IntakeBatch((observed(7), observed(8)), "2026-11-05T10:00:00Z", True, 0),
        observed_at=100,
    )
    prepare_jobs(snapshot, runtime)
    versions = intake.untriaged(snapshot)
    with pytest.raises(CampaignError, match="omits"):
        apply(
            snapshot,
            runtime,
            "triage",
            {
                **result_for(snapshot, "triage"),
                "groups": [{"input_versions": [versions[0]], "suggestion": "Review"}],
            },
        )
    assert intake.untriaged(snapshot) == versions
    assert len(snapshot.units) == 4


@pytest.mark.parametrize("purpose", ["graph_review", "semantic_sync"])
def test_graph_advice_creates_real_planning_control_without_activating_change(
    tmp_path, prepared, purpose
):
    snapshot, runtime, _ = case(tmp_path, prepared, [purpose])
    prepare_jobs(snapshot, runtime)
    before = {key: unit.spec for key, unit in snapshot.units.items()}
    apply(
        snapshot,
        runtime,
        purpose,
        {
            **result_for(snapshot, purpose),
            "disposition": "replan",
            "targets": [next(iter(snapshot.units))],
            "proposal": "Review potential ownership split; preserve exact acceptance",
        },
    )
    control = snapshot.bindings["reasoning_decisions"][purpose]["planning_control"]
    assert next_stage(snapshot, control).stage == "requirements"
    assert results.merge_into_stage_context(runtime, snapshot, control, "requirements")[
        "semantic_proposals"
    ][0]["proposal"].startswith("Review")
    assert all(snapshot.units[key].spec == value for key, value in before.items())
    with pytest.raises(CampaignError, match="delivery"):
        activate(snapshot, control, canonical_revision="main", source_bindings={})


def test_reports_require_exact_completion_state_and_expose_advisory_text(tmp_path, prepared):
    snapshot, runtime, _ = case(tmp_path, prepared, ["report"])
    prepare_jobs(snapshot, runtime)
    fact = plan.context(snapshot, "report")["units"][0]
    claim = {
        "unit": fact["id"],
        **{name: fact[name] for name in ("state", "head", "base", "source_id")},
    }
    payload = {
        **result_for(snapshot, "report"),
        "summary": "Work remains unfinished",
        "claims": [{**claim, "state": "delivered"}],
    }
    with pytest.raises(CampaignError, match="completion/state"):
        apply(snapshot, runtime, "report", payload)
    payload["claims"] = [claim]
    apply(snapshot, runtime, "report", payload)
    assert (
        snapshot.bindings["reasoning_decisions"]["report"]["narrative_authority"] == "context-only"
    )


def test_unqualified_configuration_is_visible_and_does_not_repeat_each_tick(tmp_path, prepared):
    snapshot, runtime, _ = case(tmp_path, prepared, ["report"])
    snapshot.config_snapshot["reasoning_capabilities"] = {}
    prepare_jobs(snapshot, runtime)
    assert not snapshot.jobs
    assert "qualified" in snapshot.bindings["semantic_jobs"]["errors"]["report"]
    assert plan.proposals(runtime, snapshot) == ()


def test_inventory_poll_clocks_do_not_trigger_duplicate_reasoning(tmp_path, prepared):
    snapshot, runtime, _ = case(tmp_path, prepared, ["semantic_sync"])
    snapshot.bindings["inventory"]["observed_at"] = 100
    snapshot.bindings["inventory"]["pulls"] = [{"id": 1, "head": "head", "observed_at": 100}]
    prepare_jobs(snapshot, runtime)
    snapshot.bindings["inventory"]["observed_at"] = 200
    snapshot.bindings["inventory"]["pulls"][0]["observed_at"] = 200
    assert plan.proposals(runtime, snapshot) == ()
    snapshot.bindings["inventory"]["pulls"][0]["head"] = "new-head"
    assert plan.proposals(runtime, snapshot)[0].kind == "semantic-plan"


def test_graph_revision_change_replans_jobs_even_with_same_visible_units(tmp_path, prepared):
    snapshot, runtime, _ = case(tmp_path, prepared, ["report"])
    prepare_jobs(snapshot, runtime)
    snapshot.graph_revision += 1
    assert plan.proposals(runtime, snapshot)[0].kind == "semantic-plan"


def test_normal_driver_persists_generated_job_and_applies_validated_report(tmp_path, prepared):
    snapshot, runtime, _ = case(tmp_path, prepared, ["report"])
    prepare_jobs(snapshot, runtime)
    snapshot = runtime.store.save(snapshot, snapshot.revision)
    loaded = runtime.store.load(snapshot.id)
    row = next(iter(loaded.jobs.values()))
    result = {
        **result_for(loaded, "report"),
        "summary": "Current campaign work remains open",
        "claims": [],
    }
    validate_result(result, row["request"]["output_schema"])
    row.update(validation="valid", state="validated", result=result)
    proposal = next(
        item for item in runtime.observe_workers(loaded) if item.kind == "reasoning-apply"
    )
    action = add_action(loaded, proposal.kind, unit=proposal.unit, payload=proposal.payload)
    observation = runtime.perform(loaded, action)
    assert observation.state == "confirmed"
    runtime.apply(loaded, action, observation)
    action.state = "confirmed"
    runtime.store.save(loaded, loaded.revision)
    final = runtime.store.load(loaded.id)
    assert final.bindings["reasoning_decisions"]["report"]["summary"] == result["summary"]
    assert runtime.store.jobs(final.id, job_id=row["id"])[0]["state"] == "applied"


def test_initial_bulk_analysis_reserves_creation_but_plan_timeout_releases_it(tmp_path, prepared):
    snapshot, runtime, _ = case(tmp_path, prepared, ["triage"])
    intake.apply_batch(
        snapshot, IntakeBatch((observed(7),), "2026-11-05T10:00:00Z", True, 0), observed_at=100
    )
    assert plan.defer_intake(snapshot, now=100)
    proposal = next(
        item for item in runtime.observe_workers(snapshot) if item.kind == "semantic-plan"
    )
    action = add_action(snapshot, proposal.kind, "", payload=proposal.payload)
    assert plan.defer_intake(snapshot, now=110)
    assert not plan.defer_intake(snapshot, now=131)
    action.state = "confirmed"
    snapshot.config_snapshot["reasoning_capabilities"] = {}
    assert not plan.defer_intake(snapshot, now=132)


def test_operator_report_marks_changed_canonical_facts_stale(tmp_path, prepared):
    from bmad_loop.campaign_cli import status_document

    snapshot, runtime, _ = case(tmp_path, prepared, ["report"])
    prepare_jobs(snapshot, runtime)
    apply(
        snapshot,
        runtime,
        "report",
        {**result_for(snapshot, "report"), "summary": "Open work", "claims": []},
    )
    assert status_document(snapshot)["reasoning"]["report"]["current"] is True
    next(iter(snapshot.units.values())).state = UnitState.DELIVERED
    assert status_document(snapshot)["reasoning"]["report"]["current"] is False


def test_recurring_context_creates_new_job_without_reactivating_cold_history(tmp_path, prepared):
    snapshot, runtime, _ = case(tmp_path, prepared, ["report"])
    prior = []
    for index, hold in enumerate((None, Hold("operator", "Pause"), None)):
        runtime.clock = lambda: 100 + index
        for unit in snapshot.units.values():
            unit.hold = hold
        prepare_jobs(snapshot, runtime)
        row = apply(
            snapshot,
            runtime,
            "report",
            {**result_for(snapshot, "report"), "summary": "Open work", "claims": []},
        )
        prior.append((row["id"], row["request_digest"]))
        snapshot = runtime.store.save(snapshot, snapshot.revision)
        assert snapshot.jobs == {}
        assert plan.proposals(runtime, snapshot) == ()
    assert len({identity for identity, _ in prior}) == 3
    for identity, request_digest in prior:
        saved = runtime.store.jobs(snapshot.id, job_id=identity)[0]
        assert saved["state"] == "applied"
        assert saved["request_digest"] == request_digest


def test_exhausted_obsolete_reports_archive_before_retry_and_latest_report_applies(
    tmp_path, prepared
):
    from bmad_loop.campaign_supervisor import CampaignSupervisor

    snapshot, runtime, _ = case(tmp_path, prepared, ["report"])
    supervisor = CampaignSupervisor(runtime.store, runtime, clock=runtime.clock)
    old_ids = []
    for index in range(3):
        prepare_jobs(snapshot, runtime)
        row = next(iter(snapshot.jobs.values()))
        old_ids.append(row["id"])
        row.update(
            state="failed",
            failure_attempts=snapshot.spec.reasoning.max_attempts,
            retry_at=runtime.clock() + 1000,
        )
        # Current exhausted work remains visible; changing canonical evidence
        # makes the old advice obsolete even without a graph/config revision.
        assert not [p for p in runtime.observe_workers(snapshot) if p.kind == "reasoning-stale"]
        next(iter(snapshot.units.values())).hold = Hold("operator", f"owner direction {index}")
        proposals = [p for p in runtime.observe_workers(snapshot) if p.kind == "reasoning-stale"]
        assert len(proposals) == 1
        snapshot = supervisor._queue(snapshot, proposals)
        action = next(
            a for a in snapshot.outbox if a.kind == "reasoning-stale" and a.state == "prepared"
        )
        runtime.apply(snapshot, action, runtime.perform(snapshot, action))
        action.state = "confirmed"
        snapshot = runtime.store.save(snapshot, snapshot.revision)
        assert snapshot.jobs == {}
        assert runtime.store.jobs(snapshot.id, job_id=old_ids[-1])[0]["state"] == "stale"
    prepare_jobs(snapshot, runtime)
    latest = apply(
        snapshot,
        runtime,
        "report",
        {**result_for(snapshot, "report"), "summary": "Newest owner direction", "claims": []},
    )
    snapshot = runtime.store.save(snapshot, snapshot.revision)
    assert snapshot.jobs == {}
    assert runtime.store.jobs(snapshot.id, job_id=latest["id"])[0]["state"] == "applied"
    assert snapshot.bindings["reasoning_decisions"]["report"]["summary"] == "Newest owner direction"


def test_replayed_semantic_trigger_preserves_request_despite_later_clock(tmp_path, prepared):
    snapshot, runtime, _ = case(tmp_path, prepared, ["report"])
    proposal = plan.proposals(runtime, snapshot)[0]
    action = add_action(snapshot, proposal.kind, proposal.unit, payload=proposal.payload)
    first = plan.perform(runtime, snapshot, action)
    runtime.clock = lambda: 200
    second = plan.perform(runtime, snapshot, action)
    assert first.data["requests"] == second.data["requests"]
    plan.apply(runtime, snapshot, action, first)
    plan.apply(runtime, snapshot, action, second)
    assert len(snapshot.jobs) == 1
