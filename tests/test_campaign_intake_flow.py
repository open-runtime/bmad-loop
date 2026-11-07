"""Incoming GitHub intent stays a reviewed source proposal before execution."""

from __future__ import annotations

import copy
import dataclasses
import hashlib
import json

import pytest

from bmad_loop.campaign_intake import IntakeBatch, IntakeObservation
from bmad_loop.campaign_intake_flow import (
    accept_triage,
    activate_proposal,
    apply_batch,
    fail_triage,
)
from bmad_loop.campaign_intake_flow import next as next_triage
from bmad_loop.campaign_intake_flow import (
    packet,
    prepare_triage,
    untriaged,
)
from bmad_loop.campaign_model import (
    CampaignError,
    CampaignSnapshot,
    CampaignSpec,
    CampaignUnit,
    IntakeSpec,
    RoleCandidate,
    RoleSpec,
    SourceSelector,
    UnitSpec,
    UnitState,
)
from bmad_loop.campaign_store import CampaignStore

ROLE = RoleCandidate("codex", "gpt-6-astra", "high", "openai")


def observation(number=7, *, body="Implement incoming work", updated="2026-11-05T10:00:00Z"):
    payload = {
        "id": 1000 + number,
        "number": number,
        "body": body,
        "updated_at": updated,
        "user": {"id": 5, "login": "owner"},
    }
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return IntakeObservation(
        "a/b",
        str(payload["id"]),
        "issue",
        number,
        updated,
        digest,
        "owner",
        f"https://github.com/a/b/issues/{number}",
        payload,
    )


@pytest.fixture
def snapshot():
    unit = UnitSpec(
        "source:epic:existing",
        "source",
        "epic",
        "Existing",
        scopes=("src/existing",),
        acceptance=("Keep behavior",),
    )
    spec = CampaignSpec(
        "Campaign",
        sources=(SourceSelector("stories", path="specs", id="source"),),
        intake=IntakeSpec(
            allow_new_units=True, max_new_units=2, max_triage_attempts=2, allowed_scopes=("src",)
        ),
        roles=(("triage", RoleSpec(profile=ROLE.profile, model=ROLE.model, effort=ROLE.effort)),),
    )
    state = CampaignSnapshot(
        "intake-test", "a/b", spec, {unit.id: CampaignUnit(unit, UnitState.READY)}
    )
    state.bindings["inventory"] = {"schema_version": 1, "complete": True, "pulls": [], "issues": []}
    return state


def ingest(snapshot, value=None):
    value = value or observation()
    apply_batch(snapshot, IntakeBatch((value,), value.updated_at, True, 0), observed_at=100)
    return value.version_id


def triage_raw(
    snapshot,
    versions,
    *,
    disposition="new_story",
    unit_id="source:epic:new",
    scope="src/new",
    issue=7,
):
    result = copy.deepcopy(packet(snapshot, versions)["result_contract"])
    result["proposal"].update(
        disposition=disposition,
        unit_id=unit_id,
        rationale="Incoming acceptance is absent from the inventoried existing work",
        acceptance_criteria=["New behavior"],
        related_issue=issue,
        within_scope=True,
    )
    result["units"] = (
        [
            dataclasses.asdict(
                UnitSpec(
                    unit_id,
                    "source",
                    "epic",
                    "New work",
                    scopes=(scope,),
                    acceptance=("New behavior",),
                )
            )
        ]
        if disposition in {"new_story", "new_epic"}
        else []
    )
    return result


def prepare(snapshot, version):
    context = next_triage(snapshot)
    assert context.payload["input_versions"] == [version]
    prepare_triage(snapshot, [version], attempt_id=context.action_id, selected_role=ROLE)
    return context.action_id


def plan_unit(snapshot, proposal_id):
    value = snapshot.bindings["intake_inbox"]["proposals"][proposal_id]
    unit = UnitSpec(
        "campaign:plan:incoming", "campaign-plan", "planning", "Intake plan", kind="planning"
    )
    snapshot.units[unit.id] = CampaignUnit(
        unit,
        UnitState.MERGED_PENDING_SYNC,
        bindings={"pr": {"number": 20}},
        evidence={"intake_proposal_digest": value["digest"]},
    )
    return unit.id


def test_cursor_and_version_inbox_commit_together_and_edits_supersede(snapshot, tmp_path):
    first = ingest(snapshot)
    ingest(snapshot)
    assert untriaged(snapshot) == (first,)
    store = CampaignStore(tmp_path / "state")
    store.create(snapshot)
    resumed = store.load(snapshot.id)
    assert resumed.bindings["intake"]["seen_versions"] == [first]
    changed = observation(body="New acceptance", updated="2026-11-05T10:01:00Z")
    ingest(resumed, changed)
    assert untriaged(resumed) == (changed.version_id,)
    assert resumed.bindings["intake_inbox"]["records"][first]["state"] == "superseded"
    assert resumed.bindings["intake"]["cursor"] == changed.updated_at


def test_invalid_or_partial_batch_leaves_cursor_and_records_unchanged(snapshot):
    version = ingest(snapshot)
    before = snapshot.clone()
    with pytest.raises(CampaignError, match="incomplete"):
        apply_batch(snapshot, IntakeBatch((observation(8),), "future", False, 0), observed_at=101)
    assert snapshot == before
    forged = dataclasses.replace(observation(8), digest="forged")
    with pytest.raises(CampaignError, match="digest"):
        apply_batch(snapshot, IntakeBatch((forged,), forged.updated_at, True, 0), observed_at=101)
    assert snapshot == before
    assert untriaged(snapshot) == (version,)


def test_triage_attempts_remain_bounded_across_resumes(snapshot, tmp_path):
    version = ingest(snapshot)
    attempt1 = prepare(snapshot, version)
    assert next_triage(snapshot) is None
    fail_triage(snapshot, attempt1, "Malformed proposal")
    store = CampaignStore(tmp_path / "state")
    store.create(snapshot)
    resumed = store.load(snapshot.id)
    attempt2 = prepare(resumed, version)
    assert attempt2 != attempt1
    fail_triage(resumed, attempt2, "Malformed proposal again")
    assert untriaged(resumed) == ()
    assert resumed.bindings["intake_inbox"]["records"][version]["state"] == "held"
    assert next_triage(resumed) is None


def test_new_work_waits_for_reviewed_planning_pr_and_canonical_source_publication(snapshot):
    version = ingest(snapshot)
    attempt = prepare(snapshot, version)
    raw = triage_raw(snapshot, [version])
    before_ids = set(snapshot.units)
    proposal = accept_triage(snapshot, raw, attempt_id=attempt, observed_role=ROLE)
    assert set(snapshot.units) == before_ids
    assert next_triage(snapshot) is None
    plan_id = plan_unit(snapshot, proposal)
    pending = snapshot.bindings["intake_inbox"]["proposals"][proposal]
    sources = {
        "source:epic:new": {
            "kind": "stories",
            "path": "specs/new.md",
            "revision": "merged-main-sha",
        }
    }
    with pytest.raises(CampaignError, match="planning PR delivery"):
        activate_proposal(
            snapshot,
            proposal,
            planning_unit_id=plan_id,
            source_bindings=sources,
            reviewed_digest=pending["digest"],
        )
    assert "source:epic:new" not in snapshot.units
    snapshot.units[plan_id].state = UnitState.DELIVERED
    with pytest.raises(CampaignError, match="exact proposed graph"):
        activate_proposal(
            snapshot,
            proposal,
            planning_unit_id=plan_id,
            source_bindings=sources,
            reviewed_digest="wrong",
        )
    with pytest.raises(CampaignError, match="canonical"):
        activate_proposal(
            snapshot,
            proposal,
            planning_unit_id=plan_id,
            source_bindings={},
            reviewed_digest=pending["digest"],
        )
    added = activate_proposal(
        snapshot,
        proposal,
        planning_unit_id=plan_id,
        source_bindings=sources,
        reviewed_digest=pending["digest"],
    )
    assert added == ("source:epic:new",)
    assert snapshot.units[added[0]].state == UnitState.PENDING
    assert snapshot.graph_revision == 2
    assert (
        snapshot.bindings["source_observations"][added[0]]["tracker_id"]
        == "https://github.com/a/b/issues/7"
    )
    assert (
        activate_proposal(
            snapshot,
            proposal,
            planning_unit_id=plan_id,
            source_bindings=sources,
            reviewed_digest=pending["digest"],
        )
        == added
    )
    assert snapshot.graph_revision == 2


@pytest.mark.parametrize(
    "change,expected",
    [
        ("scope", "frozen intake authority"),
        ("source", "frozen intake authority"),
        ("disabled", "scope"),
        ("budget", "limit exhausted"),
        ("model", "attribution"),
        ("unknownissue", "absent from its observed evidence"),
    ],
)
def test_model_cannot_invent_scope_budget_attribution_or_tracking_authority(
    snapshot, change, expected
):
    version = ingest(snapshot)
    attempt = prepare(snapshot, version)
    raw = triage_raw(snapshot, [version])
    observed = ROLE
    if change == "scope":
        raw["units"][0]["scopes"] = ["private/credentials"]
    elif change == "source":
        raw["units"][0]["source"] = "foreign-source"
    elif change == "disabled":
        snapshot.spec = dataclasses.replace(
            snapshot.spec, intake=dataclasses.replace(snapshot.spec.intake, allow_new_units=False)
        )
    elif change == "budget":
        snapshot.spec = dataclasses.replace(
            snapshot.spec, intake=dataclasses.replace(snapshot.spec.intake, max_new_units=1)
        )
        raw["units"].append(
            dataclasses.asdict(
                UnitSpec(
                    "source:epic:another",
                    "source",
                    "epic",
                    "Another",
                    scopes=("src/another",),
                    acceptance=("Another behavior",),
                )
            )
        )
    elif change == "model":
        observed = dataclasses.replace(ROLE, model="unlisted")
    elif change == "unknownissue":
        raw["proposal"]["related_issue"] = 99
    with pytest.raises(CampaignError, match=expected):
        accept_triage(snapshot, raw, attempt_id=attempt, observed_role=observed)
    assert "source:epic:new" not in snapshot.units
    assert not snapshot.bindings["intake_inbox"]["proposals"]


def test_changed_issue_during_planning_invalidates_pending_graph(snapshot):
    version = ingest(snapshot)
    attempt = prepare(snapshot, version)
    proposal = accept_triage(
        snapshot, triage_raw(snapshot, [version]), attempt_id=attempt, observed_role=ROLE
    )
    plan_id = plan_unit(snapshot, proposal)
    snapshot.bindings["intake_inbox"]["proposals"][proposal]["planning_unit_id"] = plan_id
    ingest(snapshot, observation(body="Acceptance changed again", updated="2026-11-05T10:01:00Z"))
    pending = snapshot.bindings["intake_inbox"]["proposals"][proposal]
    assert pending["state"] == "stale_inputs"
    assert snapshot.units[plan_id].hold.kind == "intake_plan_changed"
    snapshot.units[plan_id].state = UnitState.DELIVERED
    with pytest.raises(CampaignError, match="stale"):
        activate_proposal(
            snapshot,
            proposal,
            planning_unit_id=plan_id,
            source_bindings={},
            reviewed_digest=pending["digest"],
        )


def test_required_review_preserves_same_pr_and_live_acceptance(snapshot):
    version = ingest(snapshot)
    existing = snapshot.units["source:epic:existing"]
    existing.state = UnitState.RUNNING
    existing.bindings["pr"] = {"number": 11}
    attempt = prepare(snapshot, version)
    raw = triage_raw(snapshot, [version], disposition="repair_current_pr", unit_id=existing.id)
    raw["proposal"]["related_pr"] = 11
    original = existing.spec
    proposal_id = accept_triage(
        snapshot,
        raw,
        attempt_id=attempt,
        observed_role=ROLE,
        required_review=True,
        current_pr=11,
    )
    assert snapshot.bindings["intake_inbox"]["proposals"][proposal_id]["amendment"] is True
    assert existing.spec == original
    assert existing.bindings["pr"]["number"] == 11
    assert existing.hold.kind == "intake_plan_change"
    assert existing.bindings["intake_amendments"][0]["state"] == "requires_reviewed_plan"


def test_stale_graph_and_unknown_model_pool_refuse_before_source_mutation(snapshot):
    version = ingest(snapshot)
    with pytest.raises(CampaignError, match="authorized role pool"):
        prepare_triage(
            snapshot,
            [version],
            attempt_id="bad",
            selected_role=dataclasses.replace(ROLE, model="unlisted"),
        )
    attempt = prepare(snapshot, version)
    raw = triage_raw(snapshot, [version])
    snapshot.graph_revision += 1
    with pytest.raises(CampaignError, match="changed during"):
        accept_triage(snapshot, raw, attempt_id=attempt, observed_role=ROLE)
    assert set(snapshot.units) == {"source:epic:existing"}
