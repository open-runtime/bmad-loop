"""Admission uses the frozen command, not provider quota as a launch proof."""

import dataclasses
import json
import sys

import pytest
from test_campaign_headless import prepared as prepared
from test_campaign_runtime import production_case as production_case

from bmad_loop.adapters.profile import get_profile
from bmad_loop.campaign_execution import CampaignExecution
from bmad_loop.campaign_model import RoleCandidate
from bmad_loop.campaign_route_readiness import inspect, selected_capability
from bmad_loop.policy import Policy


def freeze_routes(snapshot, directory, profiles, policy=None):
    envelope = CampaignExecution(
        snapshot.id,
        "configuration",
        0,
        0,
        snapshot.config_revision,
        directory,
        policy or Policy(),
        profiles,
    )
    path, digest = envelope.save()
    snapshot.bindings["execution_template"] = {"path": str(path), "digest": digest}
    return envelope


def test_missing_launcher_is_not_made_available_by_a_profile_name(tmp_path):
    profile = dataclasses.replace(get_profile("codex"), binary=str(tmp_path / "absent"))
    candidate = RoleCandidate("codex", "astra")
    assert not inspect(profile, candidate, {}, "workspace-tools")["ready"]
    profile = dataclasses.replace(profile, binary=sys.executable)
    assert inspect(profile, candidate, {}, "workspace-tools")["ready"]


def test_bounded_headless_requires_its_permission_even_without_handoff():
    from bmad_loop.campaign_model import CampaignError

    profile = get_profile("grok")
    candidate = RoleCandidate("grok", "grok-test")
    assert not inspect(profile, candidate, {}, "workspace-tools")["ready"]
    with pytest.raises(CampaignError, match="workspace-tools"):
        selected_capability(profile, candidate, {}, "workspace-tools")


def test_legacy_transport_does_not_borrow_opposite_permission():
    profile = dataclasses.replace(get_profile("codex"), binary=sys.executable)
    candidate = RoleCandidate("codex", "astra")
    routes = {"codex:astra": {"reasoning": {"permissions": "no-tools"}}}
    assert selected_capability(profile, candidate, routes, "workspace-tools") is None
    assert inspect(profile, candidate, routes, "workspace-tools")["ready"]


def test_inherited_execution_preserves_worker_parallelism():
    from bmad_loop.campaign_model import CampaignSpec, RoleSpec
    from bmad_loop.campaign_preflight import resolve_roles

    spec = CampaignSpec("implicit", max_workers=8)
    resolved = resolve_roles(spec, Policy())
    assert resolved.role("execution").max_sessions == 8
    assert resolved.role("execution").profile == Policy().adapter.resolved("dev").name
    explicit = dataclasses.replace(
        spec, roles=(("execution", RoleSpec(profile="codex", max_sessions=3)),)
    )
    assert resolve_roles(explicit, Policy()).role("execution").max_sessions == 3


def test_standalone_writer_manifest_is_frozen_and_worker_receives_it(
    production_case, prepared, tmp_path, monkeypatch
):
    from test_campaign_execution_qualification import fixture_writer_capability

    from bmad_loop import campaign_runtime, campaign_workers, runsetup
    from bmad_loop.adapters.headless import HeadlessAdapter
    from bmad_loop.campaign_model import ReasoningSpec, RoleSpec

    case = production_case
    _, _, _, candidate, basecap = prepared
    cap = fixture_writer_capability(tmp_path, basecap, candidate)
    routes = {candidate.profile + ":" + candidate.model: {"execution": cap}}
    manifest = tmp_path / "writer-routes.json"
    manifest.write_text(json.dumps({"schema_version": 1, "routes": routes}))
    roles = dict(case.snapshot.spec.roles)
    roles["execution"] = RoleSpec(
        profile=candidate.profile, model=candidate.model, effort=candidate.effort
    )
    spec = dataclasses.replace(
        case.snapshot.spec,
        units=(),
        roles=tuple(roles.items()),
        reasoning=ReasoningSpec(enabled=False, capabilities=str(manifest)),
    )
    _, snapshot = campaign_runtime.create(case.repo, spec)
    restored = case.store.load(snapshot.id)
    assert restored.config_snapshot["reasoning_capabilities"] == routes
    manifest.unlink()  # Runtime consumes the frozen route, not this source file.
    runtime = campaign_runtime.CampaignRuntime(case.repo, case.store)
    assert runtime._launcher_readiness(restored, candidate, "workspace-tools")["ready"]
    assert not runtime._launcher_readiness(restored, candidate, "no-tools", require_headless=True)[
        "ready"
    ]
    envelope = runtime.template(restored)
    pol = dataclasses.replace(
        envelope.policy,
        adapter=dataclasses.replace(
            envelope.policy.adapter,
            dev=dataclasses.replace(
                envelope.policy.adapter.dev,
                name=candidate.profile,
                model=candidate.model,
                effort=candidate.effort,
            ),
        ),
    )
    captured = {}

    def adapters(*args, profiles, **kwargs):
        captured.update(profiles)
        return {}

    monkeypatch.setattr(runsetup, "make_adapters", adapters)
    campaign_workers._adapters(
        {
            "kind": "execution",
            "headless_capabilities": restored.config_snapshot["reasoning_capabilities"],
        },
        envelope,
        case.repo,
        tmp_path / "worker",
        pol,
    )
    assert captured["dev"].adapter == HeadlessAdapter.name


def test_older_campaign_execution_uses_frozen_dev_without_live_policy(project, tmp_path):
    from bmad_loop.campaign_model import CampaignSnapshot, CampaignSpec
    from bmad_loop.campaign_runtime import CampaignRuntime
    from bmad_loop.campaign_scheduler import CapacityObservation
    from bmad_loop.campaign_store import CampaignStore

    spec = CampaignSpec("older", max_workers=6)
    snapshot = CampaignSnapshot("old", "owner/repo", spec)
    policy = Policy()
    candidate = policy.adapter.resolved("dev")
    profile = dataclasses.replace(get_profile(candidate.name), binary=sys.executable)
    freeze_routes(snapshot, tmp_path / "configuration", {"dev": profile}, policy)
    snapshot.bindings["capacity"] = {
        "observed_at": 100,
        "observations": [
            {
                "profile": candidate.name,
                "model": candidate.model,
                "capacity": dataclasses.asdict(
                    CapacityObservation(True, slots=6, model_access=True)
                ),
            }
        ],
    }
    runtime = CampaignRuntime(project.project, CampaignStore(tmp_path / "state"), clock=lambda: 100)
    selected = runtime._role(snapshot, "execution")
    assert selected.profile == candidate.name and selected.model == candidate.model
    assert "execution" not in dict(snapshot.spec.roles)
    assert runtime._role(snapshot, "execution", provisional=(("execution", selected),)) == selected


def test_runtime_permission_and_effort_readiness_do_not_poison_provider_capacity(
    prepared, project, tmp_path
):
    from test_campaign_runtime_admission import configured

    from bmad_loop.campaign_model import RoleSpec

    _, _, _, high, cap = prepared
    low = dataclasses.replace(high, effort="low")
    runtime, snapshot, _, _ = configured(project, tmp_path)
    snapshot.spec = dataclasses.replace(
        snapshot.spec,
        roles=(("planning", RoleSpec(pool="planners", max_sessions=4)),),
        role_pools=(("planners", (low, high)),),
    )
    snapshot.config_snapshot["reasoning_capabilities"] = {
        high.profile + ":" + high.model: {"by_effort": {"high": {"reasoning": cap}}}
    }
    snapshot.bindings["capacity"]["observations"] = [
        {
            "profile": high.profile,
            "model": high.model,
            "capacity": {"available": True, "slots": 4, "model_access": True},
        }
    ]
    assert (
        runtime._role(snapshot, "planning", permissions="no-tools", require_headless=True) == high
    )
    assert (
        runtime._role(snapshot, "planning", permissions="workspace-tools", require_headless=True)
        is None
    )
