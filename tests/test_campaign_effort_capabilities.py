"""Each thinking level selects its own immutable native qualification proof."""

from dataclasses import replace
from types import SimpleNamespace

import pytest
from test_campaign_headless import finish, fixture_capability
from test_campaign_headless import prepared as prepared

from bmad_loop.campaign_headless import capability_for, load_capabilities, read_json, write_json
from bmad_loop.campaign_jobs import prepare
from bmad_loop.campaign_model import CampaignError, ReasoningSpec, RoleSpec
from bmad_loop.campaign_supervisor import Observation


@pytest.fixture
def variants(prepared, tmp_path):
    _, _, _, high, high_cap = prepared
    medium = replace(high, effort="medium")
    medium_cap = fixture_capability(tmp_path, {**high_cap, "efforts": ["medium"]}, medium)
    route = high.profile + ":" + high.model
    routes = {
        route: {"by_effort": {"high": {"reasoning": high_cap}, "medium": {"reasoning": medium_cap}}}
    }
    path = tmp_path / "variants.json"
    write_json(path, {"schema_version": 1, "routes": routes})
    return path, routes, medium, medium_cap, high_cap


def test_manifest_preserves_independently_earned_effort_variants(variants):
    path, routes, medium, medium_cap, high_cap = variants
    loaded = load_capabilities(path)
    assert loaded == routes
    assert capability_for(loaded, medium.profile, medium.model, effort="medium") == medium_cap
    assert capability_for(loaded, medium.profile, medium.model, effort="high") == high_cap
    assert medium_cap["qualification"]["sha256"] != high_cap["qualification"]["sha256"]


@pytest.mark.parametrize("effort", [None, "low", ""])
def test_no_default_or_other_effort_is_borrowed(variants, effort):
    _, routes, candidate, _, _ = variants
    with pytest.raises(CampaignError, match="exact thinking level"):
        capability_for(routes, candidate.profile, candidate.model, effort=effort)


def test_variant_key_cannot_relabel_another_earned_effort(variants):
    path, routes, candidate, _, high = variants
    routes[candidate.profile + ":" + candidate.model]["by_effort"]["medium"] = {"reasoning": high}
    write_json(path, {"schema_version": 1, "routes": routes})
    with pytest.raises(CampaignError, match="differs from its earned thinking level"):
        load_capabilities(path)
    with pytest.raises(CampaignError, match="differs from its earned thinking level"):
        capability_for(routes, candidate.profile, candidate.model, effort="medium")


def test_effort_mapping_cannot_hide_a_flat_fallback(variants):
    path, routes, candidate, _, high = variants
    routes[candidate.profile + ":" + candidate.model]["reasoning"] = high
    write_json(path, {"schema_version": 1, "routes": routes})
    with pytest.raises(CampaignError, match="exclusive"):
        load_capabilities(path)


@pytest.mark.parametrize("nested", [False, True])
def test_original_manifest_forms_remain_usable(prepared, tmp_path, nested):
    _, _, _, candidate, cap = prepared
    value = {"reasoning": cap} if nested else cap
    routes = {candidate.profile + ":" + candidate.model: value}
    path = tmp_path / "legacy.json"
    write_json(path, {"schema_version": 1, "routes": routes})
    assert capability_for(load_capabilities(path), candidate.profile, candidate.model) == cap
    assert (
        capability_for(routes, candidate.profile, candidate.model, effort=candidate.effort) == cap
    )
    with pytest.raises(CampaignError, match="thinking level"):
        capability_for(routes, candidate.profile, candidate.model, effort="other")


def test_normal_reasoning_launch_delivers_the_selected_effort_proof(variants, prepared, tmp_path):
    from test_campaign_reasoning_jobs import case
    from test_campaign_route_readiness import freeze_routes

    snapshot, runtime, supervisor, _ = case(tmp_path, prepared, count=0)
    _, routes, candidate, selected_cap, _ = variants
    snapshot.config_snapshot["reasoning_capabilities"] = routes
    snapshot.spec = replace(snapshot.spec, role_pools=(("summary-pool", (candidate,)),))
    template = runtime.template(snapshot)
    snapshot.config_revision += 1
    freeze_routes(snapshot, tmp_path / "configuration-v2", template.profiles, template.policy)
    request = replace(
        prepared[2],
        id="variant-job",
        role="summarization",
        candidates=(candidate,),
        coordinates={**prepared[2].coordinates, "config_revision": snapshot.config_revision},
    )
    prepare(snapshot, request)
    snapshot = runtime.store.save(snapshot, snapshot.revision)
    snapshot = supervisor._queue(snapshot, runtime.observe_workers(snapshot))
    action = snapshot.outbox[-1]
    assert action.payload["candidate"]["effort"] == "medium"
    observed = runtime.perform(snapshot, action)
    assert observed.data["state"] == "running"
    job_path = runtime.store.directory(snapshot.id) / "jobs" / request.id / "1" / "request.json"
    envelope = read_json(job_path)
    assert envelope["capability"] == selected_cap
    assert envelope["selection"]["effort"] == "medium"
    receipt = finish(runtime.reasoning_runner, job_path)
    runtime.apply(
        snapshot, action, Observation("confirmed", {"state": "finished", "receipt": receipt})
    )
    assert snapshot.jobs[request.id]["state"] == "validated"


def test_semantic_planning_selects_both_qualified_efforts(variants, prepared, tmp_path):
    from test_campaign_reasoning_semantics import case, prepare_jobs

    snapshot, runtime, high = case(tmp_path, prepared, ["report"])
    _, routes, medium, _, _ = variants
    snapshot.config_snapshot["reasoning_capabilities"] = routes
    snapshot.spec = replace(snapshot.spec, role_pools=(("approved", (medium, high)),))
    prepare_jobs(snapshot, runtime)
    request = next(iter(snapshot.jobs.values()))["request"]
    assert [row["effort"] for row in request["candidates"]] == ["medium", "high"]


def test_preflight_checks_the_requested_effort(variants, project, tmp_path, monkeypatch):
    from test_campaign_preflight import inspect, spec

    path, _, candidate, _, _ = variants
    configured = replace(
        spec(tmp_path),
        reasoning=ReasoningSpec(enabled=True, capabilities=str(path), purposes=("report",)),
        roles=(("reporting", RoleSpec(pool="reports")),),
        role_pools=(("reports", (candidate,)),),
    )
    result = inspect(project, tmp_path, monkeypatch, configured)
    observed = result["capabilities"]["planning"]["observations"]["reasoning"]["reporting"]
    assert observed["qualified"] == [candidate.profile + ":" + candidate.model]
    assert observed["problems"] == []


@pytest.mark.parametrize("read_only", [True, False])
def test_worker_adapter_selection_uses_exact_effort_and_permission(
    variants, tmp_path, monkeypatch, read_only
):
    from bmad_loop import campaign_workers, runsetup
    from bmad_loop.adapters.headless import HeadlessAdapter
    from bmad_loop.adapters.profile import get_profile
    from bmad_loop.policy import AdapterPolicy, Policy

    _, routes, candidate, selected, _ = variants
    policy = replace(
        Policy(),
        adapter=AdapterPolicy(
            name=candidate.profile, model=candidate.model, effort=candidate.effort
        ),
    )
    profiles = {
        role: get_profile(candidate.profile) for role in ("dev", "review", "triage", "retro")
    }
    captured = {}

    def adapters(project, run_dir, policy, **kwargs):
        captured.update(kwargs["profiles"])
        return {
            role: HeadlessAdapter(run_dir=run_dir / role, profile=profile, policy=policy)
            for role, profile in kwargs["profiles"].items()
            if profile.adapter == "bounded-headless"
        }

    monkeypatch.setattr(runsetup, "make_adapters", adapters)
    result = campaign_workers._adapters(
        {"read_only": read_only, "headless_capabilities": routes, "headless_coordinates": {}},
        SimpleNamespace(profiles=profiles),
        tmp_path,
        tmp_path / "run",
        policy,
    )
    if read_only:
        assert set(result) == set(profiles)
        assert all(adapter.capability == selected for adapter in result.values())
    else:
        assert captured == profiles
        assert result == {}
