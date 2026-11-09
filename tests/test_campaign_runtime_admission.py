"""Batch admission reserves declared model/role capacity before actors launch."""

import dataclasses
import sys
from pathlib import Path

import pytest
from test_campaign_headless import prepared as prepared

from bmad_loop.campaign_model import CampaignSnapshot, CampaignSpec, RoleCandidate, RoleSpec
from bmad_loop.campaign_runtime import CampaignRuntime
from bmad_loop.campaign_scheduler import CapacityObservation
from bmad_loop.campaign_store import CampaignStore


def configured(project, tmp_path, *, sessions=4, binaries=None):
    astra, fable = RoleCandidate("codex", "astra"), RoleCandidate("claude", "fable")
    spec = CampaignSpec(
        "admission",
        role_pools=(("planners", (astra, fable)),),
        roles=(("planning", RoleSpec(pool="planners", max_sessions=sessions)),),
    )
    snapshot = CampaignSnapshot("campaign", "org/repo", spec)
    snapshot.bindings["capacity"] = {
        "observed_at": 100,
        "observations": [
            {
                "profile": value.profile,
                "model": value.model,
                "capacity": dataclasses.asdict(
                    CapacityObservation(True, slots=1, model_access=True)
                ),
            }
            for value in (astra, fable)
        ],
    }
    from test_campaign_route_readiness import freeze_routes

    from bmad_loop.adapters.profile import get_profile

    freeze_routes(
        snapshot,
        tmp_path / "configuration",
        {
            "campaign:"
            + value.profile: dataclasses.replace(
                get_profile(value.profile),
                binary=(binaries or {}).get(value.profile, sys.executable),
            )
            for value in (astra, fable)
        },
    )
    return (
        CampaignRuntime(project.project, CampaignStore(tmp_path / "state"), clock=lambda: 100),
        snapshot,
        astra,
        fable,
    )


def test_second_planner_uses_other_available_route(project, tmp_path):
    runtime, snapshot, astra, fable = configured(project, tmp_path)
    assert runtime._role(snapshot, "planning") == astra
    assert runtime._role(snapshot, "planning", provisional=(("planning", astra),)) == fable
    assert (
        runtime._role(snapshot, "planning", provisional=(("planning", astra), ("planning", fable)))
        is None
    )


def test_role_limit_applies_to_the_same_admission_batch(project, tmp_path):
    runtime, snapshot, astra, _ = configured(project, tmp_path, sessions=1)
    assert runtime._role(snapshot, "planning", provisional=(("planning", astra),)) is None


def test_repository_inventory_honors_frozen_recent_pull_window(project, tmp_path, monkeypatch):
    from datetime import datetime, timedelta, timezone
    from types import SimpleNamespace

    from bmad_loop.campaign_discovery import RepositoryDiscovery

    runtime, snapshot, _, _ = configured(project, tmp_path)
    snapshot.spec = dataclasses.replace(
        snapshot.spec,
        github=dataclasses.replace(
            snapshot.spec.github, recent_days=45, inventory_metadata_bytes=1234567
        ),
    )
    runtime.clock = lambda: 1700000000
    observed = []
    monkeypatch.setattr(
        RepositoryDiscovery,
        "collect",
        lambda self, **kwargs: observed.append(
            {**kwargs, "max_metadata_bytes": self.max_metadata_bytes}
        )
        or SimpleNamespace(to_dict=lambda: {"complete": True}, digest="inventory"),
    )
    assert runtime._perform_inventory(snapshot, None).state == "confirmed"
    since = datetime.fromisoformat(observed[0]["recent_since"].replace("Z", "+00:00"))
    assert since == datetime.fromtimestamp(runtime.clock(), timezone.utc) - timedelta(days=45)
    assert observed[0]["max_metadata_bytes"] == 1234567


def test_repository_inventory_selects_explicit_and_native_issue_bindings(
    project, tmp_path, monkeypatch
):
    from types import SimpleNamespace

    from bmad_loop.campaign_discovery import RepositoryDiscovery
    from bmad_loop.campaign_model import CampaignUnit, UnitSpec

    runtime, snapshot, _, _ = configured(project, tmp_path)
    snapshot.spec = dataclasses.replace(
        snapshot.spec, intake=dataclasses.replace(snapshot.spec.intake, issue_numbers=(13, 11))
    )
    first = CampaignUnit(UnitSpec("source:epic:first", "source", "epic", "First"))
    first.bindings.update(issue={"number": 11}, epic_issue="17")
    second = CampaignUnit(UnitSpec("source:epic:second", "source", "epic", "Second"))
    second.bindings["pr"] = {"number": 31}
    third = CampaignUnit(UnitSpec("source:epic:third", "source", "epic", "Third"))
    third.bindings["tracker_id"] = "#19"
    fourth = CampaignUnit(UnitSpec("source:epic:fourth", "source", "epic", "Fourth"))
    snapshot.units = {unit.id: unit for unit in (first, second, third, fourth)}
    snapshot.bindings["source_observations"] = {
        first.id: {"tracker_id": "https://github.com/foreign/repo/issues/999"},
        second.id: {"tracker_id": "https://github.com/org/repo/issues/23"},
        third.id: {"tracker_id": "https://github.com/foreign/repo/issues/999"},
        fourth.id: {"tracker_id": "https://github.com/foreign/repo/issues/999"},
    }
    snapshot.bindings["github_hierarchy"] = {
        "story:" + fourth.id: {"kind": "issue", "number": 29},
        "milestone": {"kind": "milestone", "number": 97},
    }
    observed = []
    monkeypatch.setattr(
        RepositoryDiscovery,
        "collect",
        lambda self, **kwargs: observed.append(kwargs)
        or SimpleNamespace(to_dict=lambda: {"complete": True}, digest="inventory"),
    )
    assert runtime._perform_inventory(snapshot, None).state == "confirmed"
    assert observed[0]["selected_issues"] == (11, 13, 17, 19, 23, 29)
    assert observed[0]["mapped_prs"] == (31,)


@pytest.mark.parametrize("binding", [{"number": True}, {"number": 0}, {"number": "bad"}])
def test_repository_inventory_refuses_malformed_native_issue_binding(
    project, tmp_path, monkeypatch, binding
):
    from types import SimpleNamespace

    from bmad_loop.campaign_discovery import RepositoryDiscovery
    from bmad_loop.campaign_model import CampaignError, CampaignUnit, UnitSpec

    monkeypatch.setattr(
        RepositoryDiscovery,
        "collect",
        lambda self, **kwargs: SimpleNamespace(to_dict=lambda: {"complete": True}, digest="test"),
    )
    runtime, snapshot, _, _ = configured(project, tmp_path)
    unit = CampaignUnit(UnitSpec("source:epic:first", "source", "epic", "First"))
    unit.bindings["issue"] = binding
    snapshot.units[unit.id] = unit
    with pytest.raises(CampaignError, match="positive native number"):
        runtime._perform_inventory(snapshot, None)


def test_repository_inventory_refuses_foreign_imported_tracker(project, tmp_path, monkeypatch):
    from types import SimpleNamespace

    from bmad_loop.campaign_discovery import RepositoryDiscovery
    from bmad_loop.campaign_github import GithubError
    from bmad_loop.campaign_model import CampaignUnit, UnitSpec

    monkeypatch.setattr(
        RepositoryDiscovery,
        "collect",
        lambda self, **kwargs: SimpleNamespace(to_dict=lambda: {"complete": True}, digest="test"),
    )
    runtime, snapshot, _, _ = configured(project, tmp_path)
    unit = CampaignUnit(UnitSpec("source:epic:first", "source", "epic", "First"))
    snapshot.units[unit.id] = unit
    snapshot.bindings["source_observations"] = {
        unit.id: {"tracker_id": "https://github.com/foreign/repo/issues/999"}
    }
    with pytest.raises(GithubError, match="does not identify an issue in this repository"):
        runtime._perform_inventory(snapshot, None)


def test_never_submitted_merge_wait_reenters_fresh_gate(project, tmp_path, monkeypatch):
    from bmad_loop.campaign_github import MutationIntent
    from bmad_loop.campaign_model import OutboxAction
    from bmad_loop.campaign_supervisor import Observation

    runtime, snapshot, _, _ = configured(project, tmp_path)
    intent = MutationIntent(
        "merge-action", "merge", "PUT", "repos/org/repo/pulls/1/merge-async", {"sha": "head"}
    )
    action = OutboxAction(
        "merge-action",
        "github",
        "unit",
        {"intent": intent.to_dict(), "purpose": "merge"},
        generation=snapshot.generation,
        state="sent_unknown",
    )
    observed = []
    monkeypatch.setattr(runtime, "gateway", lambda _: None)
    monkeypatch.setattr(
        runtime,
        "_perform_github",
        lambda current, pending: observed.append(pending.id)
        or Observation("waiting", reason="CI pending"),
    )
    result = runtime.reconcile_action(snapshot, action)
    assert result.state == "waiting" and observed == ["merge-action"]


def test_native_quota_invalidates_fresh_positive_until_newer_observation(project, tmp_path):
    from bmad_loop.campaign_capacity import record_unavailable

    runtime, snapshot, astra, fable = configured(project, tmp_path)
    for row in snapshot.bindings["capacity"]["observations"]:
        row["capacity"].update(observed_at=99, expires_at=220)
    assert runtime._role(snapshot, "planning") == astra
    record_unavailable(snapshot, astra.profile, astra.model, now=100, reason="native quota")
    assert runtime._role(snapshot, "planning") == fable
    snapshot.bindings["capacity"]["observed_at"] = 101
    assert runtime._role(snapshot, "planning") == fable
    runtime.clock = lambda: 102
    snapshot.bindings["capacity"]["observations"][0]["capacity"]["observed_at"] = 101
    assert runtime._role(snapshot, "planning") == astra


def test_capacity_refresh_cannot_select_missing_frozen_launcher_and_recovers(project, tmp_path):
    missing = tmp_path / "native-codex"
    runtime, snapshot, astra, fable = configured(
        project, tmp_path, binaries={"codex": str(missing)}
    )
    runtime.capacity_observer = lambda *args: CapacityObservation(True, slots=8, model_access=True)
    snapshot.bindings["capacity"] = runtime._perform_capacity(snapshot, None).data
    assert runtime._role(snapshot, "planning") == fable
    row = next(
        row for row in snapshot.bindings["capacity"]["observations"] if row["profile"] == "codex"
    )
    assert row["capacity"]["available"]
    assert not row["launchers"][0]["ready"]
    missing.write_text("#!/bin/sh\nexit 0\n")
    missing.chmod(0o755)
    assert runtime._role(snapshot, "planning") == astra
    missing.unlink()
    assert runtime._role(snapshot, "planning") == fable


def test_unavailable_launcher_defers_prepared_effect_without_charging_attempt(
    prepared, project, tmp_path
):
    from bmad_loop.campaign_jobs import JobLimits, JobRequest, prepare
    from bmad_loop.campaign_model import OutboxAction

    _, _, _, candidate, cap = prepared
    runtime, snapshot, _, _ = configured(project, tmp_path)
    snapshot.spec = dataclasses.replace(
        snapshot.spec, reasoning=dataclasses.replace(snapshot.spec.reasoning, enabled=True)
    )
    snapshot.config_snapshot["reasoning_capabilities"] = {
        candidate.profile + ":" + candidate.model: {"reasoning": cap}
    }
    assert runtime._launcher_readiness(snapshot, candidate, "no-tools", require_headless=True)[
        "ready"
    ]
    launcher_script = Path(cap["dependencies"][0]["path"])
    original_script = launcher_script.read_bytes()
    launcher_script.unlink()
    request = JobRequest(
        "job",
        "triage",
        "planning",
        {"graph_revision": 1, "config_revision": 1, "head": "head", "base": "base"},
        (),
        "Prompt",
        {"type": "object"},
        JobLimits(100, 100, 1, 30),
        candidates=(candidate,),
    )
    prepare(snapshot, request)
    action = OutboxAction(
        "launch",
        "reasoning-launch",
        "",
        {"job_id": "job", "candidate": dataclasses.asdict(candidate)},
        generation=snapshot.generation,
    )
    snapshot.outbox.append(action)
    runtime.control(snapshot, {"kind": "prepare-effect", "action": dataclasses.asdict(action)})
    assert action.state == "confirmed" and action.receipt["admission_deferred"]
    assert snapshot.jobs["job"]["attempt"] == 0
    assert snapshot.jobs["job"]["claim"] is None
    launcher_script.write_bytes(original_script)
    resumed = dataclasses.replace(action, id="retry-launch", state="prepared", receipt={})
    snapshot.outbox.append(resumed)
    runtime.control(snapshot, {"kind": "prepare-effect", "action": dataclasses.asdict(resumed)})
    assert snapshot.jobs["job"]["attempt"] == 1
    assert snapshot.jobs["job"]["claim"]["launch_id"] == resumed.id
