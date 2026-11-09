"""Explicit start permission never manufactures strict publication readiness."""

import json
import time
from dataclasses import replace
from types import SimpleNamespace

import pytest
from test_campaign_cli import campaign_spec as campaign_spec
from test_campaign_cli import runtime as runtime
from test_campaign_preflight import inspect, spec
from test_campaign_publication import publishable as publishable

from bmad_loop import campaign_preflight, cli, platform_util
from bmad_loop.campaign_github import GithubError
from bmad_loop.campaign_model import (
    CampaignError,
    CampaignSnapshot,
    CampaignSpec,
    CampaignUnit,
    GitHubSpec,
    OutboxAction,
    ParadevSpec,
    PlanningSpec,
    UnitSpec,
    UnitState,
    spec_from_dict,
    spec_to_dict,
)
from bmad_loop.campaign_publication import PublicationController
from bmad_loop.campaign_runtime import CampaignRuntime
from bmad_loop.campaign_store import CampaignStore


@pytest.mark.parametrize("allowed", [False, True])
def test_only_explicit_publication_wait_can_start_with_missing_strict_rules(
    project, tmp_path, monkeypatch, publishable, allowed
):
    controller, candidate, evidence, observation = publishable
    unavailable = replace(observation, rules=replace(observation.rules, strict=False))
    monkeypatch.setattr(platform_util, "HANDLE_ANCHORED_WRITES", True)
    monkeypatch.setattr(campaign_preflight, "read_rules", lambda *args: unavailable.rules)
    selected = replace(spec(tmp_path), github=GitHubSpec(allow_publication_wait=allowed))
    result = inspect(project, tmp_path, monkeypatch, selected, gateway=controller.gateway)
    assert not result["ok"] and not result["capabilities"]["publication"]["ready"]
    assert result["can_start"] is allowed
    assert any(
        "server-enforced" in reason for reason in result["capabilities"]["publication"]["problems"]
    )
    # The real final publication gate still refuses an otherwise accepted PR.
    monkeypatch.setattr(controller, "inspect", lambda number: unavailable)
    with pytest.raises(GithubError, match="server-enforced|up-to-date"):
        controller.prepare_merge(
            candidate,
            evidence,
            action_id="strict-merge",
            generation=0,
            merge_mode=selected.github.merge_mode,
        )
    assert controller.gateway.transport.calls == []


@pytest.mark.parametrize("area", ["planning", "execution", "reflection"])
def test_publication_wait_does_not_waive_other_capability_failures(
    project, tmp_path, monkeypatch, publishable, area
):
    controller, _, _, observation = publishable
    monkeypatch.setattr(platform_util, "HANDLE_ANCHORED_WRITES", True)
    monkeypatch.setattr(
        campaign_preflight, "read_rules", lambda *args: replace(observation.rules, strict=False)
    )
    selected = replace(spec(tmp_path), github=GitHubSpec(allow_publication_wait=True))
    kwargs = {"gateway": controller.gateway}
    if area == "planning":
        selected = replace(selected, planning=PlanningSpec(enabled=True))
    elif area == "execution":
        selected = replace(selected, worktree_root="")
    else:
        selected = replace(
            selected, paradev=ParadevSpec(reflect=True, worktree_root=str(tmp_path / "worktrees"))
        )

        def unavailable():
            raise ValueError("native reflection unavailable")

        kwargs["reflection_provider"] = SimpleNamespace(preflight=unavailable)
    result = inspect(project, tmp_path, monkeypatch, selected, **kwargs)
    assert not result["ok"] and not result["can_start"]
    assert not result["capabilities"][area]["ready"]


def test_publication_wait_does_not_enable_branch_only_work_without_github(
    project, tmp_path, monkeypatch
):
    selected = replace(
        spec(tmp_path), github=GitHubSpec(enabled=False, allow_publication_wait=True)
    )
    result = inspect(project, tmp_path, monkeypatch, selected)
    assert not result["ok"] and not result["can_start"]


def test_waiting_campaign_retains_ready_pr_until_real_strict_gate_passes(
    project, tmp_path, monkeypatch, publishable
):
    from bmad_loop import campaign_ownership, campaign_remote_feedback

    controller, candidate, evidence, observation = publishable
    unavailable = replace(
        observation, observed_at=time.time(), rules=replace(observation.rules, strict=False)
    )
    unit = CampaignUnit(
        UnitSpec("unit", "s", "e", "Unit"),
        state=UnitState.READY_TO_PUBLISH,
        bindings={"workspace": str(project.project), "pr": {"number": 1}},
    )
    selected = CampaignSpec(
        "waiting", units=(unit.spec,), github=GitHubSpec(allow_publication_wait=True)
    )
    state = CampaignSnapshot("waiting", "a/b", selected, {unit.id: unit})
    driver = CampaignRuntime(
        project.project, CampaignStore(tmp_path / "authority"), gateway=controller.gateway
    )
    unit.bindings["source_revision"] = candidate.source_revision
    unit.evidence["integrated"] = {"head": candidate.head, "base": candidate.base}
    unit.evidence["review"] = {
        "head": candidate.head,
        "validated": {
            "verdict": "approved",
            "coordinates": {
                "head": candidate.head,
                "base": candidate.base,
                "source_revision": unit.bindings["source_revision"],
                "graph_revision": state.graph_revision,
                "config_revision": state.config_revision,
            },
        },
    }
    candidate = driver._candidate(state, unit)
    evidence = replace(evidence, candidate=candidate)
    monkeypatch.setattr(driver, "_publication_evidence", lambda *args: evidence)
    monkeypatch.setattr(campaign_ownership, "canonical_unchanged", lambda *args: None)
    monkeypatch.setattr(campaign_remote_feedback, "collect", lambda *args, **kwargs: None)
    monkeypatch.setattr(PublicationController, "inspect", lambda *args: unavailable)
    result = driver._perform_prepare_merge(
        state, OutboxAction("merge", "prepare-merge", unit.id, {}, 0)
    )
    assert result.state == "waiting" and not result.data["gate"]["ready"]
    assert "up-to-date" in result.reason
    assert unit.state == UnitState.READY_TO_PUBLISH and unit.bindings["pr"]["number"] == 1
    assert "intent" not in result.data and "merge" not in unit.evidence
    assert controller.gateway.transport.calls == []

    unavailable = replace(observation, observed_at=time.time())
    ready = driver._perform_prepare_merge(
        state, OutboxAction("ready", "prepare-merge", unit.id, {}, 0)
    )
    assert ready.state == "confirmed" and ready.data["purpose"] == "merge"
    assert ready.data["intent"]["payload"]["sha"] == candidate.head
    assert controller.gateway.transport.calls == []


@pytest.mark.parametrize("allowed", [False, True])
@pytest.mark.parametrize("start_verdict", [False, True])
def test_cli_run_uses_explicit_opt_in_and_separate_start_verdict(
    campaign_spec, runtime, monkeypatch, capsys, allowed, start_verdict
):
    _, _, calls = runtime
    if allowed:
        with campaign_spec.open("a") as stream:
            stream.write("\n[github]\nallow_publication_wait = true\n")
    monkeypatch.setattr(
        campaign_preflight,
        "inspect",
        lambda *args: {
            "schema_version": 1,
            "ok": False,
            "can_start": start_verdict,
            "capabilities": {
                "publication": {"ready": False, "problems": ["strict rules unavailable"]}
            },
        },
    )
    started = allowed and start_verdict
    assert cli.main(["campaign", "run", str(campaign_spec), "--json"]) == (0 if started else 1)
    document = json.loads(capsys.readouterr().out)
    creations = [call for call in calls if call[0] == "create"]
    assert bool(creations) is started
    if started:
        selected = creations[0][2]
        assert spec_from_dict(spec_to_dict(selected)).github.allow_publication_wait
    else:
        assert not document["ok"]


@pytest.mark.parametrize("value", [0, 1, "true", None])
def test_publication_wait_requires_an_actual_boolean(value):
    with pytest.raises(CampaignError, match="allow_publication_wait.*boolean"):
        spec_from_dict(
            {
                "schema_version": 1,
                "name": "waiting",
                "sources": [{"kind": "stories"}],
                "github": {"allow_publication_wait": value},
            }
        )
