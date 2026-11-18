"""Shared worker construction binds frozen authority without resetting history."""

import copy
import json
from dataclasses import asdict, replace

import pytest

from bmad_loop.adapters.profile import CLIProfile, HookSpec
from bmad_loop.campaign_continuation import prepare_permit
from bmad_loop.campaign_execution import CampaignExecution
from bmad_loop.campaign_model import (
    CampaignError,
    CampaignSnapshot,
    CampaignSpec,
    CampaignUnit,
    CheckpointSpec,
    SourceSelector,
    UnitSpec,
    WorkerClaim,
    source_to_dict,
)
from bmad_loop.campaign_worker_authority import (
    ChildAllowanceExhausted,
    continuation_request,
    execution_envelope,
    source_selector,
)
from bmad_loop.model import Phase, RunState, StoryTask
from bmad_loop.plugins.grants import GrantRecord, GrantSet
from bmad_loop.policy import AdapterPolicy, LimitsPolicy, Policy, StageAdapterPolicy


@pytest.fixture
def authority_case(tmp_path):
    source = SourceSelector("tickets", id="owned-source", initiative="owned-initiative")
    foreign = SourceSelector("stories", id="foreign-source", path="foreign-plans")
    spec = UnitSpec("owned-source:owned-initiative:1.1", source.id, "epic", "Owned", selector="1.1")
    unit = CampaignUnit(
        spec,
        bindings={
            "workspace": str(tmp_path / "workspace"),
            "branch": "retained-branch",
            "pr": {"number": 41},
        },
        evidence={"dev_attempts_spent": 2, "review_cycles_spent": 1},
    )
    policy = Policy(
        limits=LimitsPolicy(max_dev_attempts=5, max_review_cycles=4),
        adapter=AdapterPolicy(
            dev=StageAdapterPolicy(name="original-dev", model="original-model"),
            review=StageAdapterPolicy(name="original-review", model="original-review-model"),
        ),
    )
    snapshot = CampaignSnapshot(
        "campaign",
        "example/repo",
        CampaignSpec(
            "Owned campaign",
            sources=(foreign, source),
            units=(spec,),
            checkpoints=CheckpointSpec(target_interval_s=37),
        ),
        {unit.id: unit},
        config_revision=7,
        common_dir=str(tmp_path / "common-git"),
        config_snapshot={"policy": policy.to_dict()},
    )
    profiles = {
        key: CLIProfile(name, "scripted-unused-cli", HookSpec("none", "", {}))
        for key, name in (
            ("dev", "original-dev"),
            ("review", "original-review"),
            ("campaign:codex", "codex"),
            ("campaign:claude", "claude"),
        )
    }
    grants = GrantSet(
        records=(
            GrantRecord(
                "owned-plugin", "plugins/owned", "a" * 64, (("hook.py", "b" * 64),), "2026-11-17"
            ),
        ),
        snapshot_root=tmp_path / "frozen-grants",
    )
    template = CampaignExecution(
        snapshot.id,
        "template",
        0,
        1,
        7,
        tmp_path / "configuration",
        policy,
        profiles,
        grants=grants,
        verify_commands=(("scripted-verifier", "accepted-check"),),
    )
    claim = WorkerClaim(
        unit.id,
        6,
        9,
        "successor-launch",
        run_id="successor-run",
        run_dir=str(tmp_path / "successor-worker"),
        workspace=unit.bindings["workspace"],
        branch=unit.bindings["branch"],
    )
    predecessor = replace(
        claim,
        attempt=5,
        generation=8,
        launch_id="predecessor-launch",
        run_id="predecessor-run",
        run_dir=str(tmp_path / "predecessor-worker"),
        quiescent=True,
    )
    unit.evidence["handoff"] = {
        "id": "owned-handoff",
        "native_role": "dev",
        "predecessor": asdict(predecessor),
        "capture": {
            "capture_digest": "captured-input",
            "current": {
                "fingerprint": {
                    "head": "c" * 40,
                    "index": "index",
                    "worktree": "dirty",
                    "untracked": "owned",
                    "source": "source",
                },
                "original_run_dir": str(tmp_path / "original-run"),
                "original_run_id": predecessor.run_id,
                "source_roots": ["private-source"],
                "original_state_path": str(tmp_path / "captured-state"),
                "original_state_sha256": "d" * 64,
                "predecessor_receipt_path": str(tmp_path / "captured-receipt"),
                "predecessor_receipt_sha256": "e" * 64,
            },
        },
    }
    payload = {
        "kind": "execution",
        "candidate": {"profile": "codex", "model": "selected-dev-model", "effort": "high"},
        "review_candidate": {
            "profile": "claude",
            "model": "selected-review-model",
            "effort": "low",
        },
    }
    return template, snapshot, unit, claim, payload


def child(case, *, payload=None):
    template, snapshot, unit, claim, original_payload = case
    return execution_envelope(
        template,
        snapshot,
        unit,
        claim,
        original_payload if payload is None else payload,
        template.directory.parent / "successor-worker",
    )


def test_execution_envelope_preserves_frozen_commands_grants_and_lifetime_history(authority_case):
    template, snapshot, unit, claim, payload = authority_case
    before_template = copy.deepcopy(template)
    before_unit = copy.deepcopy(unit)
    payload.update(commands=[["unapproved-command"]], grants={"unapproved": True})
    envelope = child(authority_case)
    assert envelope.verify_commands == template.verify_commands
    assert envelope.grants == template.grants
    assert envelope.config_revision == snapshot.config_revision == 7
    assert envelope.campaign_id == snapshot.id
    assert (envelope.unit_id, envelope.attempt, envelope.generation) == (unit.id, 6, 9)
    assert envelope.directory == template.directory.parent / "successor-worker"
    assert envelope.checkpoint_interval_s == 37.0
    assert envelope.policy.limits.max_dev_attempts == 3
    assert envelope.policy.limits.max_review_cycles == 3
    assert envelope.policy.adapter.dev.name == envelope.profiles["dev"].name == "codex"
    assert envelope.policy.adapter.dev.model == "selected-dev-model"
    assert envelope.policy.adapter.dev.effort == "high"
    assert envelope.policy.adapter.review.name == envelope.profiles["review"].name == "claude"
    assert envelope.policy.adapter.review.model == "selected-review-model"
    assert envelope.policy.adapter.review.effort == "low"
    assert template == before_template and unit == before_unit
    assert claim.launch_id == "successor-launch"


@pytest.mark.parametrize("role", ["dev", "review"])
@pytest.mark.parametrize("damage", ["missing", "wrong_name"])
def test_selected_route_requires_its_actual_frozen_profile(authority_case, role, damage):
    template, _, unit, _, _ = authority_case
    assert child(authority_case).profiles[role].name == ("codex" if role == "dev" else "claude")
    profile_key = "campaign:" + ("codex" if role == "dev" else "claude")
    if damage == "missing":
        template.profiles.pop(profile_key)
    else:
        template.profiles[profile_key] = replace(
            template.profiles[profile_key], name="unrelated-profile"
        )
    before = copy.deepcopy(unit)
    with pytest.raises(CampaignError, match="frozen profiles"):
        child(authority_case)
    assert unit == before


def test_original_developer_profile_is_a_valid_frozen_fallback(authority_case):
    _, _, _, _, payload = authority_case
    payload["candidate"] = {"profile": "original-dev", "model": "frozen-original-model"}
    envelope = child(authority_case)
    assert envelope.profiles["dev"].name == "original-dev"
    assert envelope.policy.adapter.dev.name == "original-dev"


@pytest.mark.parametrize("commands", [None, ()])
def test_unknown_and_explicitly_empty_verification_authority_remain_distinct(
    authority_case, commands
):
    template, snapshot, unit, claim, payload = authority_case
    template = replace(template, verify_commands=commands)
    payload["commands"] = [["unapproved-command"]]
    envelope = child((template, snapshot, unit, claim, payload))
    assert envelope.verify_commands == commands
    assert (envelope.verify_commands is None) == (commands is None)


@pytest.mark.parametrize("role", ["dev", "review"])
@pytest.mark.parametrize("overspent", [0, 1])
def test_exhausted_allowance_is_refused_without_resetting_history(authority_case, role, overspent):
    template, _, unit, _, payload = authority_case
    if role == "review":
        unit.evidence["handoff"]["native_role"] = "review"
        payload["handoff_id"] = unit.evidence["handoff"]["id"]
    assert child(authority_case).policy.limits.max_review_cycles == 3
    key = "dev_attempts_spent" if role == "dev" else "review_cycles_spent"
    ceiling = (
        template.policy.limits.max_dev_attempts
        if role == "dev"
        else template.policy.limits.max_review_cycles
    )
    unit.evidence[key] = ceiling + overspent
    before = copy.deepcopy(unit)
    with pytest.raises(ChildAllowanceExhausted, match="lifetime child attempt budget"):
        child(authority_case)
    assert unit == before
    assert unit.evidence[key] == ceiling + overspent


def test_exact_review_continuation_can_use_remaining_review_after_dev_exhaustion(authority_case):
    template, _, unit, _, payload = authority_case
    unit.evidence["dev_attempts_spent"] = template.policy.limits.max_dev_attempts
    unit.evidence["handoff"]["native_role"] = "review"
    payload["handoff_id"] = unit.evidence["handoff"]["id"]
    envelope = child(authority_case)
    assert envelope.policy.limits.max_dev_attempts == 0
    assert envelope.policy.limits.max_review_cycles == 3
    assert unit.evidence["dev_attempts_spent"] == 5
    payload["handoff_id"] = "another-handoff"
    with pytest.raises(ChildAllowanceExhausted):
        child(authority_case)


def test_source_selector_never_falls_back_to_another_configured_source(authority_case):
    _, snapshot, unit, _, _ = authority_case
    assert snapshot.spec.sources[0].id != unit.spec.source
    assert source_selector(snapshot, unit) == snapshot.spec.sources[1]
    unit.spec = replace(unit.spec, source="unconfigured-foreign-source")
    with pytest.raises(CampaignError, match="no configured BMAD source"):
        source_selector(snapshot, unit)
    with pytest.raises(CampaignError, match="no configured BMAD source"):
        continuation_request(snapshot, unit, authority_case[3])


@pytest.mark.parametrize("kind", ["planning", "tracking", "landing_repair"])
def test_controller_units_have_explicit_source_without_adopting_foreign_work(authority_case, kind):
    _, snapshot, unit, _, _ = authority_case
    unit.spec = replace(unit.spec, kind=kind, source="controller-source")
    assert source_selector(snapshot, unit) == SourceSelector("explicit", id="controller-source")


@pytest.fixture
def permit_case(authority_case, monkeypatch):
    template, snapshot, unit, claim, payload = authority_case
    original = RunState(
        run_id="predecessor-run",
        project=unit.bindings["workspace"],
        started_at="now",
        source="tickets",
        tasks={
            unit.spec.selector: StoryTask(unit.spec.selector, 0, phase=Phase.DEV_VERIFY, attempt=2)
        },
    )
    monkeypatch.setattr(
        "bmad_loop.campaign_continuation._archive", lambda handoff, key: original.to_dict()
    )
    return authority_case


def test_shared_request_and_envelope_bind_one_captured_source_and_successor(permit_case):
    template, snapshot, unit, claim, _ = permit_case
    unit.bindings["effective_source"] = source_to_dict(
        replace(snapshot.spec.sources[1], path="private-owned-source")
    )
    before = copy.deepcopy(snapshot)
    request = continuation_request(snapshot, unit, claim)
    second = continuation_request(snapshot, unit, claim)
    envelope = child(permit_case)
    permit = prepare_permit(request, envelope)
    assert permit == prepare_permit(second, envelope)
    binding = permit["binding"]
    assert request["source"] == unit.bindings["effective_source"]
    assert binding["source"] == json.loads(json.dumps(unit.bindings["effective_source"]))
    assert binding["source_roots"] == ["private-source"]
    assert binding["capture_digest"] == "captured-input"
    assert binding["original_state_sha256"] == "d" * 64
    assert binding["predecessor_receipt_sha256"] == "e" * 64
    assert binding["predecessor"]["launch_id"] == "predecessor-launch"
    assert binding["successor"] == {
        "launch_id": claim.launch_id,
        "run_id": claim.run_id,
        "directory": str(envelope.directory.resolve()),
        "attempt": claim.attempt,
        "generation": claim.generation,
    }
    assert binding["remaining_dev_attempts"] == 3
    assert binding["remaining_review_cycles"] == 3
    assert snapshot == before
    assert template.unit_id == "template"


@pytest.mark.parametrize(
    "change", ["launch", "run", "pr", "source", "capture", "fingerprint", "archive"]
)
def test_changed_successor_or_captured_source_changes_continuation_identity(permit_case, change):
    template, snapshot, unit, claim, payload = permit_case
    original = prepare_permit(continuation_request(snapshot, unit, claim), child(permit_case))
    current = unit.evidence["handoff"]["capture"]["current"]
    if change == "launch":
        claim = replace(claim, launch_id="another-successor")
    elif change == "run":
        claim = replace(claim, run_id="another-successor-run")
    elif change == "pr":
        unit.bindings["pr"]["number"] = 42
    elif change == "source":
        unit.bindings["effective_source"] = source_to_dict(
            replace(snapshot.spec.sources[1], path="changed-source")
        )
    elif change == "capture":
        unit.evidence["handoff"]["capture"]["capture_digest"] = "another-capture"
    elif change == "fingerprint":
        current["fingerprint"]["source"] = "another-source-fingerprint"
    else:
        current["original_state_sha256"] = "f" * 64
    changed_case = (template, snapshot, unit, claim, payload)
    changed = prepare_permit(continuation_request(snapshot, unit, claim), child(changed_case))
    assert changed["permit_id"] != original["permit_id"]
