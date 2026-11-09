"""Oversized semantic inputs use a durable bounded DAG before one decision."""

import copy
import json
from dataclasses import replace
from pathlib import Path

import pytest
from test_campaign_headless import fixture_capability
from test_campaign_headless import prepared as prepared
from test_campaign_reasoning_semantics import case, prepare_jobs, result_for

from bmad_loop import campaign_reasoning_plan as plan
from bmad_loop import campaign_reasoning_results as results
from bmad_loop.campaign_jobs import validate_result
from bmad_loop.campaign_model import CampaignError
from bmad_loop.campaign_store import add_action


def large_case(tmp_path, prepared, purpose="priority", input_bound=None):
    snapshot, runtime, _ = case(tmp_path, prepared, [purpose])
    if input_bound:
        candidate, capability = prepared[3:]
        snapshot.config_snapshot["reasoning_capabilities"][
            candidate.profile + ":" + candidate.model
        ] = fixture_capability(tmp_path, {**capability, "input_tokens": input_bound}, candidate)
        snapshot.config_revision += 1
    keys = list(snapshot.units)
    for unit in snapshot.units.values():
        unit.spec = replace(unit.spec, title=unit.spec.title + " Owner obligation." * 6000)
    # This dependency crosses map boundaries and must reach the final reducer
    # even if intermediate model prose omits it.
    snapshot.units[keys[-1]].spec = replace(
        snapshot.units[keys[-1]].spec, after=(keys[0],), scopes=snapshot.units[keys[0]].spec.scopes
    )
    snapshot.graph_revision += 1
    snapshot = runtime.store.save(snapshot, snapshot.revision)
    prepare_jobs(snapshot, runtime)
    assert len(snapshot.jobs) > 1
    pipeline = next(iter(snapshot.bindings["semantic_jobs"]["pipelines"].values()))
    return snapshot, runtime, pipeline


def packet(row):
    return json.loads(Path(row["request"]["inputs"][0]["path"]).read_text())


def summary(row):
    source = packet(row)
    return {
        "schema_version": 1,
        "context_digest": source["context_digest"],
        "coverage": source["coverage"],
        "citations": list(
            dict.fromkeys(value for record in source["records"] for value in record["source_ids"])
        ),
        "summary": "Owner obligations remain open. Reconcile dependency and ownership constraints before changing priority.",
    }


def apply_job(runtime, snapshot, identity, value):
    row = snapshot.jobs[identity]
    validate_result(value, row["request"]["output_schema"])
    row.update(result=value, validation="valid", state="validated")
    action = add_action(snapshot, "reasoning-apply", "", payload={"job_id": identity})
    observation = runtime.perform(snapshot, action)
    runtime.apply(snapshot, action, observation)
    action.state = "confirmed"
    assert row["state"] == "applied", row["reason"]
    saved = runtime.store.save(snapshot, snapshot.revision)
    return runtime.store.load(saved.id)


def through_final(snapshot, runtime, pipeline, *, dense_summaries=False):
    identity = pipeline["id"]
    original = {key: unit.spec for key, unit in snapshot.units.items()}
    for _ in range(10):
        pipeline = snapshot.bindings["semantic_jobs"]["pipelines"][identity]
        if pipeline["state"] == "final":
            return snapshot, snapshot.jobs[pipeline["frontier"][0]]
        for job in pipeline["frontier"]:
            row = snapshot.jobs[job]
            bound = len(Path(row["request"]["inputs"][0]["path"]).read_bytes())
            bound += (
                len(row["request"]["prompt"].encode())
                + len(json.dumps(row["request"]["output_schema"], sort_keys=True).encode())
                + 2048
            )
            assert bound <= row["request"]["limits"]["input_tokens"]
            value = summary(row)
            if dense_summaries:
                value["summary"] = (
                    "x" * row["request"]["output_schema"]["properties"]["summary"]["maxLength"]
                )
            snapshot = apply_job(runtime, snapshot, job, value)
            assert snapshot.graph_revision == 2
            assert {key: unit.spec for key, unit in snapshot.units.items()} == original
        # All applied map rows are archived; reducer creation must survive a
        # real store reload rather than depend on manually retained hot rows.
        assert not snapshot.jobs
        prepare_jobs(snapshot, runtime)
    pytest.fail("partition DAG did not converge")


def test_oversized_priority_restarts_between_maps_and_applies_one_complete_decision(
    tmp_path, prepared
):
    snapshot, runtime, pipeline = large_case(tmp_path, prepared)
    snapshot, final = through_final(snapshot, runtime, pipeline)
    surface = packet(final)["global_constraints"]["execution_constraints"]
    last = list(snapshot.units)[-1]
    assert next(row for row in surface if row["id"] == last)["after"] == [list(snapshot.units)[0]]
    order = list(reversed(snapshot.units))
    result = {
        **result_for(snapshot, "priority"),
        "coverage": packet(final)["coverage"],
        "order": order,
        "rationale": "Prefer independent eligible work; retain every dependency",
    }
    snapshot = apply_job(runtime, snapshot, final["id"], result)
    assert snapshot.graph_revision == 3
    assert sorted(snapshot.units, key=lambda key: snapshot.units[key].spec.order) == order
    assert snapshot.units[last].spec.after == (list(snapshot.units)[0],)
    assert plan.proposals(runtime, snapshot) == ()
    assert snapshot.bindings["semantic_jobs"]["pipelines"][pipeline["id"]]["state"] == "applied"


@pytest.mark.parametrize("defect", ["missing", "duplicate", "invented", "citation"])
def test_partition_rejects_incomplete_or_invented_evidence_before_authority(
    tmp_path, prepared, defect
):
    snapshot, runtime, pipeline = large_case(tmp_path, prepared)
    row = next(iter(snapshot.jobs.values()))
    value = summary(row)
    healthy = copy.deepcopy(row)
    healthy.update(result=value, validation="valid")
    results.apply_result(runtime, snapshot.clone(), healthy)
    if defect == "missing":
        value = {**value, "coverage": value["coverage"][1:]}
    elif defect == "duplicate":
        value = {**value, "coverage": value["coverage"] * 2}
    elif defect == "invented":
        value = {**value, "coverage": ["invented-record"]}
    else:
        value = {**value, "citations": ["invented-source"]}
    row.update(result=value, validation="valid")
    with pytest.raises(CampaignError, match="coverage|citation"):
        results.apply_result(runtime, snapshot, row)
    assert snapshot.graph_revision == 2
    assert "reasoning_decisions" not in snapshot.bindings


def test_final_reducer_requires_entire_original_partition_set(tmp_path, prepared):
    snapshot, runtime, pipeline = large_case(tmp_path, prepared)
    snapshot, row = through_final(snapshot, runtime, pipeline)
    row.update(
        result={
            **result_for(snapshot, "priority"),
            "coverage": packet(row)["coverage"],
            "order": list(reversed(snapshot.units)),
            "rationale": "Whole graph",
        },
        validation="valid",
    )
    healthy = snapshot.clone()
    results.apply_result(runtime, healthy, healthy.jobs[row["id"]])
    snapshot.bindings["semantic_jobs"]["pipelines"][pipeline["id"]]["leaves"].append("missing-map")
    with pytest.raises(CampaignError, match="complete original partition coverage"):
        results.apply_result(runtime, snapshot, row)
    assert snapshot.graph_revision == 2


def test_irreducible_global_graph_is_a_visible_wait_not_partial_priority(tmp_path, prepared):
    snapshot, runtime, _ = case(tmp_path, prepared, ["priority"])
    for unit in snapshot.units.values():
        unit.spec = replace(unit.spec, scopes=tuple(f"src/component-{i}" for i in range(10000)))
    prepare_jobs(snapshot, runtime)
    assert not snapshot.jobs
    assert (
        "Global semantic decision/dependency footprint"
        in snapshot.bindings["semantic_jobs"]["errors"]["priority"]
    )
    assert snapshot.graph_revision == 1
    assert plan.proposals(runtime, snapshot) == ()


def test_summary_frontier_reduces_in_bounded_levels_with_archived_dependencies(tmp_path, prepared):
    snapshot, runtime, pipeline = large_case(tmp_path, prepared, input_bound=16000)
    snapshot, final = through_final(snapshot, runtime, pipeline, dense_summaries=True)
    assert final["request"]["coordinates"]["semantic_partition"]["level"] >= 2
    assert packet(final)["global_constraints"]["execution_constraints"]
    assert snapshot.graph_revision == 2


def test_oversized_graph_review_creates_one_reviewed_planning_proposal(tmp_path, prepared):
    snapshot, runtime, pipeline = large_case(tmp_path, prepared, purpose="graph_review")
    original = {key: unit.spec for key, unit in snapshot.units.items()}
    snapshot, final = through_final(snapshot, runtime, pipeline)
    value = {
        **result_for(snapshot, "graph_review"),
        "coverage": packet(final)["coverage"],
        "disposition": "replan",
        "targets": [next(iter(snapshot.units))],
        "proposal": "Review shared ownership and retain the cross-partition dependency.",
    }
    snapshot = apply_job(runtime, snapshot, final["id"], value)
    control = snapshot.bindings["reasoning_decisions"]["graph_review"]["planning_control"]
    assert snapshot.units[control].spec.kind == "planning"
    assert len(snapshot.units) == len(original) + 1
    assert all(snapshot.units[key].spec == spec for key, spec in original.items())


def test_exhausted_map_stops_reduction_and_reports_missing_coverage(tmp_path, prepared):
    snapshot, runtime, pipeline = large_case(tmp_path, prepared)
    row = snapshot.jobs[pipeline["frontier"][0]]
    row.update(state="failed", failure_attempts=snapshot.spec.reasoning.max_attempts)
    prepare_jobs(snapshot, runtime)
    current = snapshot.bindings["semantic_jobs"]["pipelines"][pipeline["id"]]
    assert current["state"] == "blocked"
    assert "incomplete coverage" in snapshot.bindings["semantic_jobs"]["errors"]["priority"]
    assert snapshot.graph_revision == 2 and "reasoning_decisions" not in snapshot.bindings
    assert plan.proposals(runtime, snapshot) == ()
