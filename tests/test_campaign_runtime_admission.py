"""Batch admission reserves declared model/role capacity before actors launch."""

import dataclasses

from bmad_loop.campaign_model import CampaignSnapshot, CampaignSpec, RoleCandidate, RoleSpec
from bmad_loop.campaign_runtime import CampaignRuntime
from bmad_loop.campaign_scheduler import CapacityObservation
from bmad_loop.campaign_store import CampaignStore


def configured(project, tmp_path, *, sessions=4):
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
    return (
        CampaignRuntime(project.project, CampaignStore(tmp_path / "state")),
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
