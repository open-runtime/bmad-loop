"""Reviewer quota recovery preserves all writer exclusions at every boundary."""

import dataclasses
from pathlib import Path

import pytest
from test_campaign_handoff_flow import campaign as campaign
from test_campaign_handoff_permissions import readonly, ready
from test_campaign_headless import prepared as prepared
from test_campaign_route_readiness import freeze_routes

from bmad_loop import campaign_handoff_flow as flow
from bmad_loop.adapters.profile import get_profile
from bmad_loop.campaign_model import CampaignError, OutboxAction, RoleCandidate, RoleSpec, UnitState
from bmad_loop.campaign_runtime import CampaignRuntime


def reviewers(campaign, tmp_path):
    helper, snapshot, unit, receipt, author = readonly(campaign)
    previous = snapshot.spec.candidates("publication_review")[0]
    independent = RoleCandidate("grok", "grok-test", "high", "xai")
    candidates = (previous, author, independent)
    snapshot.spec = dataclasses.replace(
        snapshot.spec,
        roles=tuple(
            (name, RoleSpec(pool="reviewers") if name == "publication_review" else role)
            for name, role in snapshot.spec.roles
        ),
        role_pools=(*snapshot.spec.role_pools, ("reviewers", candidates)),
    )
    snapshot.config_snapshot["reasoning_capabilities"]["grok:grok-test"] = {
        "reasoning": helper.fixture_cap(independent),
    }
    freeze_routes(
        snapshot,
        tmp_path / "configuration",
        {
            "campaign:" + candidate.profile: get_profile(candidate.profile)
            for candidate in candidates
        },
    )
    snapshot.bindings["capacity"] = {
        "observed_at": 100,
        "observations": [
            {
                "profile": candidate.profile,
                "model": candidate.model,
                "capacity": {
                    "available": True,
                    "slots": 8,
                    "active": 0,
                    "model_access": True,
                    "observed_at": 100,
                    "expires_at": 220,
                },
            }
            for candidate in candidates
        ],
    }
    unit.state = UnitState.REVIEWING
    runtime = CampaignRuntime(Path(unit.bindings["workspace"]), helper.store, clock=lambda: 100)
    return helper, runtime, snapshot, unit, receipt, author, independent


def route(runtime, snapshot, unit, proposal):
    action = OutboxAction("reroute", proposal.kind, unit.id, proposal.payload, snapshot.generation)
    observation = flow.perform(runtime, snapshot, action)
    assert observation.state == "confirmed", observation
    flow.apply(runtime, snapshot, action, observation)
    return observation


@pytest.mark.parametrize("mixed", [False, True])
def test_quota_fallback_skips_every_author_family_and_resumes(campaign, tmp_path, mixed):
    helper, runtime, snapshot, unit, receipt, author, independent = reviewers(campaign, tmp_path)
    unit.evidence.update(
        developer_family="" if mixed else "anthropic",
        developer_families=["openai", "anthropic"] if mixed else [],
    )
    assert flow.intercept_terminal(runtime, snapshot, unit, receipt)
    value = unit.evidence["handoff"]
    assert value["candidate"] == dataclasses.asdict(independent)
    ready(helper, snapshot, unit)
    proposal = flow.resume_proposal(snapshot, unit, runtime)
    assert proposal and proposal.payload["candidate"] == dataclasses.asdict(independent)
    assert proposal.payload["read_only"] is True
    request = dict(proposal.payload)
    flow.decorate_launch(snapshot, unit, request)
    successor = dataclasses.replace(unit.claim, launch_id="successor", writes=False)
    flow.adopt_launch(snapshot, unit, proposal.payload, successor)
    assert value["phase"] == "continuing" and value["successor"]["writes"] is False
    assert unit.bindings["pr"] == 17


def test_explicit_author_choice_reports_why_it_is_inadmissible(campaign, tmp_path):
    _, runtime, snapshot, unit, _, author, _ = reviewers(campaign, tmp_path)
    unit.evidence.update(developer_family="anthropic", developer_families=["anthropic"])
    with pytest.raises(
        CampaignError, match="Requested handoff target is inadmissible:.*author family 'anthropic'"
    ):
        flow.begin(runtime, snapshot, unit, requested=dataclasses.asdict(author), reason="operator")
    assert "handoff" not in unit.evidence and unit.hold is None


def test_saved_author_route_recovers_through_actual_capacity_admission(campaign, tmp_path):
    helper, runtime, snapshot, unit, receipt, author, independent = reviewers(campaign, tmp_path)
    flow.intercept_terminal(helper, snapshot, unit, receipt)
    value = unit.evidence["handoff"]
    assert value["candidate"] == dataclasses.asdict(author)
    ready(helper, snapshot, unit)
    unit.evidence.update(developer_family="anthropic", developer_families=["anthropic"])
    assert flow._available_alternative(runtime, snapshot, value) == independent
    proposed = flow.proposals(runtime, snapshot)
    assert len(proposed) == 1 and proposed[0].kind == "handoff-route"
    assert flow.resume_proposal(snapshot, unit, runtime) is None
    route(runtime, snapshot, unit, proposed[0])
    proposal = flow.resume_proposal(snapshot, unit, runtime)
    assert proposal and proposal.payload["candidate"] == dataclasses.asdict(independent)
    assert unit.hold is None


def test_author_route_waits_without_attempts_until_independent_capacity_recovers(
    campaign, tmp_path
):
    helper, runtime, snapshot, unit, receipt, author, independent = reviewers(campaign, tmp_path)
    flow.intercept_terminal(helper, snapshot, unit, receipt)
    ready(helper, snapshot, unit)
    value = unit.evidence["handoff"]
    unit.evidence.update(developer_family="anthropic", developer_families=["anthropic"])
    capacity = next(
        row["capacity"]
        for row in snapshot.bindings["capacity"]["observations"]
        if row["profile"] == independent.profile
    )
    capacity["active"] = capacity["slots"]
    claim = dataclasses.asdict(unit.claim)
    assert flow._available_alternative(runtime, snapshot, value) is None
    proposed = flow.proposals(runtime, snapshot)
    observation = route(runtime, snapshot, unit, proposed[0])
    assert "author family" in observation.data["wait_reason"]
    assert unit.hold and "author family" in unit.hold.reason
    assert not flow.resume_proposal(snapshot, unit, runtime)
    assert not flow.proposals(runtime, snapshot)
    assert dataclasses.asdict(unit.claim) == claim
    capacity["active"] = 0
    proposal = flow.proposals(runtime, snapshot)[0]
    assert proposal.payload["candidate"] == dataclasses.asdict(independent)
    route(runtime, snapshot, unit, proposal)
    assert "route_wait" not in value and unit.hold is None
    assert flow.resume_proposal(snapshot, unit, runtime).payload["candidate"] == dataclasses.asdict(
        independent
    )
    assert dataclasses.asdict(unit.claim) == claim


@pytest.mark.parametrize("boundary", ["resume", "decorate", "adopt"])
def test_final_successor_rechecks_current_writer_provenance(campaign, tmp_path, boundary):
    helper, _, snapshot, unit, receipt, _, _ = reviewers(campaign, tmp_path)
    flow.intercept_terminal(helper, snapshot, unit, receipt)
    ready(helper, snapshot, unit)
    proposal = flow.resume_proposal(snapshot, unit)
    assert proposal
    unit.evidence.update(developer_family="anthropic", developer_families=["anthropic"])
    if boundary == "resume":
        assert flow.resume_proposal(snapshot, unit) is None
    else:
        with pytest.raises(CampaignError, match="author family"):
            if boundary == "decorate":
                flow.decorate_launch(snapshot, unit, dict(proposal.payload))
            else:
                successor = dataclasses.replace(unit.claim, launch_id="successor", writes=False)
                flow.adopt_launch(snapshot, unit, proposal.payload, successor)


def test_explicit_saved_choice_waits_visibly_without_automatic_substitution(campaign, tmp_path):
    helper, runtime, snapshot, unit, receipt, author, independent = reviewers(campaign, tmp_path)
    flow.begin(
        helper,
        snapshot,
        unit,
        requested=dataclasses.asdict(author),
        reason="operator",
        receipt=receipt,
    )
    ready(helper, snapshot, unit)
    unit.evidence.update(developer_family="anthropic", developer_families=["anthropic"])
    value = unit.evidence["handoff"]
    assert flow._available_alternative(runtime, snapshot, value) is None
    proposal = flow.proposals(runtime, snapshot)[0]
    assert proposal.payload["candidate"] == dataclasses.asdict(author)
    route(runtime, snapshot, unit, proposal)
    assert unit.hold and "Requested handoff target is inadmissible" in unit.hold.reason
    assert flow.resume_proposal(snapshot, unit, runtime) is None


def test_disabled_independence_policy_preserves_explicit_choice(campaign, tmp_path):
    _, runtime, snapshot, unit, _, author, _ = reviewers(campaign, tmp_path)
    snapshot.spec = dataclasses.replace(
        snapshot.spec,
        github=dataclasses.replace(snapshot.spec.github, require_independent_review=False),
    )
    unit.evidence.update(developer_family="anthropic", developer_families=["anthropic"])
    flow.begin(runtime, snapshot, unit, requested=dataclasses.asdict(author), reason="operator")
    assert unit.evidence["handoff"]["candidate"] == dataclasses.asdict(author)


def test_family_is_derived_when_candidate_has_no_explicit_hint(campaign, tmp_path):
    _, _, snapshot, unit, _, author, _ = reviewers(campaign, tmp_path)
    inferred = dataclasses.replace(author, provider_family="")
    snapshot.spec = dataclasses.replace(
        snapshot.spec,
        role_pools=tuple(
            (
                name,
                tuple(inferred if candidate == author else candidate for candidate in candidates),
            )
            for name, candidates in snapshot.spec.role_pools
        ),
    )
    unit.evidence["developer_families"] = ["anthropic"]
    with pytest.raises(CampaignError, match="author family"):
        flow._target_cap(snapshot, inferred, "publication_review", unit=unit, permissions="review")


def test_exhausted_claude_reviewer_skips_codex_author_for_grok(campaign, tmp_path):
    helper, runtime, snapshot, unit, receipt, claude, grok = reviewers(campaign, tmp_path)
    codex = snapshot.spec.candidates("publication_review")[0]
    snapshot.spec = dataclasses.replace(
        snapshot.spec,
        role_pools=tuple(
            (name, (claude, codex, grok) if name == "reviewers" else candidates)
            for name, candidates in snapshot.spec.role_pools
        ),
    )
    snapshot.outbox[0].payload["candidate"] = dataclasses.asdict(claude)
    unit.evidence.update(developer_family="openai", developer_families=["openai"])
    assert flow.intercept_terminal(runtime, snapshot, unit, receipt)
    assert unit.evidence["handoff"]["candidate"] == dataclasses.asdict(grok)
    ready(helper, snapshot, unit)
    assert flow.resume_proposal(snapshot, unit, runtime).payload["candidate"] == dataclasses.asdict(
        grok
    )


def test_refresh_passes_current_writers_into_shared_scheduler(campaign, tmp_path, monkeypatch):
    helper, runtime, snapshot, unit, receipt, author, independent = reviewers(campaign, tmp_path)
    flow.intercept_terminal(helper, snapshot, unit, receipt)
    ready(helper, snapshot, unit)
    unit.evidence.update(developer_family="openai", developer_families=["openai"])
    capacity = next(
        row["capacity"]
        for row in snapshot.bindings["capacity"]["observations"]
        if row["profile"] == author.profile
    )
    capacity["active"] = capacity["slots"]
    observed = []
    actual = runtime._role

    def record(snapshot, role, **kwargs):
        observed.append(kwargs)
        return actual(snapshot, role, **kwargs)

    monkeypatch.setattr(runtime, "_role", record)
    assert flow._available_alternative(runtime, snapshot, unit.evidence["handoff"]) == independent
    assert len(observed) == 2
    assert all(
        row["developer_family"] == "openai"
        and row["developer_families"] == ("openai",)
        and row["permissions"] == "review"
        for row in observed
    )


@pytest.mark.parametrize("saved_target", ["inadmissible", "missing"])
def test_live_predecessor_drains_before_replacement_admission_and_then_recovers(
    campaign, tmp_path, saved_target
):
    import json

    from bmad_loop import runs, verify
    from bmad_loop.campaign_supervisor import Observation

    helper, runtime, snapshot, unit, receipt, author, independent = reviewers(campaign, tmp_path)
    unit.claim.quiescent = False
    flow.begin(helper, snapshot, unit, reason="operator continuation")
    value = unit.evidence["handoff"]
    assert value["candidate"] == dataclasses.asdict(author)
    unit.evidence.update(developer_family="anthropic", developer_families=["anthropic"])
    if saved_target == "missing":
        value["candidate"] = None
    assert snapshot.spec.role("publication_review").max_sessions == 1
    assert flow._available_alternative(runtime, snapshot, value) is None
    before = unit.attempt, unit.failure_attempts, unit.review_attempts
    (proposal,) = flow.proposals(runtime, snapshot)
    assert proposal.kind == "terminate-worker"
    directory = runtime.store.directory(snapshot.id) / "workers" / unit.claim.launch_id
    directory.mkdir(parents=True)
    (directory / "startup.json").write_text(
        json.dumps(
            {
                "launch_id": unit.claim.launch_id,
                "generation": unit.claim.generation,
            }
        ),
        encoding="utf-8",
    )
    (directory / "request.json").write_text(json.dumps({"kind": "stage"}), encoding="utf-8")
    action = OutboxAction(
        "stop-predecessor", proposal.kind, unit.id, proposal.payload, snapshot.generation
    )
    observation = runtime.perform(snapshot, action)
    assert observation.state == "confirmed", observation
    runtime.apply(snapshot, action, observation)
    assert (
        json.loads((directory / runs.STOP_REQUEST_FILE).read_text(encoding="utf-8"))["mode"]
        == "hard"
    )
    assert unit.bindings["stop_requested_for"] == unit.claim.launch_id
    assert not unit.claim.quiescent
    assert not flow.proposals(runtime, snapshot)  # Stop is not itself quiescence.
    observe = OutboxAction(
        "predecessor-stopped",
        "observe-worker",
        unit.id,
        {"launch_id": unit.claim.launch_id},
        snapshot.generation,
    )
    # Trusted worker-observer seam: independent process/tree quiescence is proven.
    terminal = {
        **receipt,
        "state": "interrupted",
        "quiescence_confirmed": True,
        "head": verify.rev_parse_head(Path(unit.bindings["workspace"])),
    }
    runtime.apply(snapshot, observe, Observation("confirmed", terminal))
    assert unit.claim.quiescent and unit.claim.status == "interrupted"
    (proposal,) = flow.proposals(runtime, snapshot)
    assert proposal.kind == "handoff-route"
    assert proposal.payload["candidate"] == dataclasses.asdict(independent)
    route(runtime, snapshot, unit, proposal)
    ready(helper, snapshot, unit)
    successor = flow.resume_proposal(snapshot, unit, runtime)
    assert successor and successor.payload["candidate"] == dataclasses.asdict(independent)
    flow.decorate_launch(snapshot, unit, dict(successor.payload))
    claim = dataclasses.replace(unit.claim, launch_id="successor", writes=False, quiescent=False)
    flow.adopt_launch(snapshot, unit, successor.payload, claim)
    assert value["phase"] == "continuing" and value["successor"]["launch_id"] == "successor"
    assert (unit.attempt, unit.failure_attempts, unit.review_attempts) == before


def test_priority_drain_retains_foreign_hold_and_exact_predecessor_fence(campaign, tmp_path):
    from bmad_loop.campaign_model import Hold

    helper, runtime, snapshot, unit, _, _, _ = reviewers(campaign, tmp_path)
    unit.claim.quiescent = False
    flow.begin(helper, snapshot, unit, reason="operator continuation")
    owned_hold = unit.hold
    unit.hold = Hold("operator", "Do not change this unit", id="owner")
    assert not flow.proposals(runtime, snapshot)
    unit.hold = owned_hold
    unit.claim.launch_id = "another-worker"
    with pytest.raises(CampaignError, match="Another writer replaced"):
        flow.proposals(runtime, snapshot)
