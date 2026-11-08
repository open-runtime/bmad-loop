"""Native review pairs are an additional exact-candidate publication gate."""

import dataclasses
from pathlib import Path

import pytest
from test_campaign_takeover import configured as takeover

from bmad_loop import campaign_native_review_flow as flow
from bmad_loop import campaign_route_readiness as readiness
from bmad_loop.adapters.profile import get_profile
from bmad_loop.campaign_model import (
    CampaignError,
    OutboxAction,
    RoleCandidate,
    RoleSpec,
    UnitSpec,
    UnitState,
    WorkerClaim,
    spec_from_dict,
    spec_to_dict,
)
from bmad_loop.campaign_planning import StageCoordinates, validate_result


def configured(project, tmp_path):
    runtime, snapshot, unit = takeover(project, tmp_path)
    snapshot.generation = 1
    snapshot.spec = dataclasses.replace(
        snapshot.spec,
        roles=(("native_review_pair", RoleSpec(profile="codex", model="astra")),),
    )
    unit.state = UnitState.REVIEWING
    head = unit.bindings["head"]
    unit.bindings.update(base=head, pr={"number": 1})
    unit.evidence.update(
        integrated={"head": head, "base": head},
        verification={"outcome": "verified"},
        developer_family="openai",
        developer_families=["anthropic", "openai"],
    )
    runtime._role = lambda snapshot, role, **kwargs: RoleCandidate(
        "codex" if role == "native_review_pair" else "grok",
        "astra" if role == "native_review_pair" else "grok",
    )
    return runtime, snapshot, unit


def receipt(snapshot, unit, *, stage="native_review_pair", verdict="approved"):
    integrated = unit.evidence["integrated"]
    coordinates = StageCoordinates(
        snapshot.id,
        unit.id,
        stage,
        snapshot.graph_revision,
        snapshot.config_revision,
        integrated["base"],
        integrated["head"],
        "inventory",
        unit.bindings.get("source_revision", ""),
    )
    artifact = {
        "schema_version": 1,
        "status": "done",
        "coordinates": dataclasses.asdict(coordinates),
        "artifacts": [],
        "verdict": verdict,
        "source_artifact_digest": coordinates.source_revision,
        "findings": (
            []
            if verdict == "approved"
            else [{"id": "native-finding", "required": True, "resolved": False}]
        ),
    }
    return {
        "stage": stage,
        "launch_id": stage + "-launch",
        "outcome": "stage_complete",
        "head": integrated["head"],
        "artifact": artifact,
        "provider_family": "openai" if stage == "native_review_pair" else "xai",
        "validated": validate_result(artifact, coordinates, Path(unit.bindings["workspace"])),
        # This is the trusted observation seam. Native pointer/delegate parsing
        # is exercised through the real adapter in the stage integration test.
        "validated_native_review_pair": {
            "target": dict(integrated),
            "graph_revision": snapshot.graph_revision,
            "config_revision": snapshot.config_revision,
            "native_binding": {"headless_request": "owned-native-request"},
            "native_review_evidence": {"reviews": ["main", "custom"]},
        },
    }


def test_pair_is_opt_in_and_roundtrips_without_changing_default_roles():
    raw = {
        "schema_version": 1,
        "name": "old",
        "units": [dataclasses.asdict(UnitSpec("s:e:u", "s", "e", "Unit"))],
    }
    old = spec_from_dict(raw)
    assert "native_review_pair" not in dict(old.roles)
    assert spec_from_dict(spec_to_dict(old)) == old
    new = spec_from_dict(
        {**raw, "roles": {"native_review_pair": {"profile": "codex", "model": "astra"}}}
    )
    assert spec_from_dict(spec_to_dict(new)) == new


def test_pair_runs_first_then_independent_review_without_finishing_unit(project, tmp_path):
    runtime, snapshot, unit = configured(project, tmp_path)
    proposal = next(p for p in runtime.proposals(snapshot) if p.unit == unit.id)
    assert proposal.kind == "stage-boundary-before"
    assert proposal.payload["launch"]["stage"] == "native_review_pair"
    assert not runtime._writes(proposal.payload["launch"])
    unit.claim = WorkerClaim(
        unit.id,
        1,
        snapshot.generation,
        "native_review_pair-launch",
        role="native_review_pair",
        resource="review",
        quiescent=True,
    )
    native = receipt(snapshot, unit)
    runtime._apply_stage_result(snapshot, unit, native)
    assert unit.state == UnitState.REVIEWING
    assert unit.evidence["native_review_pair"] == native
    assert flow.accepted(snapshot, unit)
    next_step = next(p for p in runtime.proposals(snapshot) if p.unit == unit.id)
    assert next_step.payload["launch"]["stage"] == "publication_review"
    independent = receipt(snapshot, unit, stage="publication_review")
    unit.claim = dataclasses.replace(
        unit.claim, launch_id=independent["launch_id"], role="publication_review"
    )
    runtime._apply_stage_result(snapshot, unit, independent)
    assert unit.state == UnitState.READY_TO_PUBLISH
    assert unit.evidence["review"] == independent
    assert unit.evidence["native_review_pair"] == native


def test_pair_findings_hold_same_pr_for_repair(project, tmp_path):
    runtime, snapshot, unit = configured(project, tmp_path)
    unit.claim = WorkerClaim(unit.id, 1, 1, "native_review_pair-launch", quiescent=True)
    binding = dict(unit.bindings)
    failed = receipt(snapshot, unit, verdict="changes_requested")
    runtime._apply_stage_result(snapshot, unit, failed)
    assert unit.hold.kind == "review_findings"
    assert unit.resume_state == UnitState.SYNCING
    assert unit.evidence["required_findings"] == failed["artifact"]["findings"]
    assert unit.bindings == binding
    assert not flow.accepted(snapshot, unit)
    assert flow.current(snapshot, unit)  # Rejected proof remains available for diagnosis.


@pytest.mark.parametrize("change", ["head", "base", "graph", "config", "source"])
def test_candidate_changes_invalidate_both_independent_and_native_reviews(
    project, tmp_path, change
):
    runtime, snapshot, unit = configured(project, tmp_path)
    unit.evidence["native_review_pair"] = receipt(snapshot, unit)
    unit.evidence["review"] = receipt(snapshot, unit, stage="publication_review")
    unit.state = UnitState.READY_TO_PUBLISH
    assert flow.accepted(snapshot, unit)
    if change in {"head", "base"}:
        unit.evidence["integrated"][change] = "b" * 40
    elif change == "graph":
        snapshot.graph_revision += 1
    elif change == "config":
        snapshot.config_revision += 1
    else:
        unit.bindings["source_revision"] = "new-source"
    assert not flow.accepted(snapshot, unit)
    assert runtime.prepare_resume(snapshot)
    assert unit.state == UnitState.SYNCING
    assert not {"native_review_pair", "review"}.intersection(unit.evidence)


def test_publication_and_draft_promotion_refuse_missing_pair(project, tmp_path):
    runtime, snapshot, unit = configured(project, tmp_path)
    unit.claim = WorkerClaim(unit.id, 1, 1, "review", quiescent=True)
    unit.evidence["review"] = receipt(snapshot, unit, stage="publication_review")
    assert not runtime._publication_evidence(snapshot, unit).review_approved
    action = OutboxAction("ready", "prepare-ready", unit.id, {}, 1)
    with pytest.raises(CampaignError, match="Required native main/custom"):
        runtime._perform_prepare_ready(snapshot, action)
    with pytest.raises(CampaignError, match="Required native main/custom"):
        runtime._apply_stage_result(snapshot, unit, unit.evidence["review"])
    unit.evidence["native_review_pair"] = receipt(snapshot, unit)
    assert runtime._publication_evidence(snapshot, unit).review_approved


@pytest.mark.parametrize("route", ["absent", "single", "generic", "pair"])
def test_native_pair_permission_never_falls_back_to_single_or_legacy(monkeypatch, route):
    candidate = RoleCandidate("codex", "astra")
    cap = {
        "protocol": "codex-native-review",
        "permissions": "read-only-tools",
        "argv": ["codex", "--review"],
    }
    if route == "pair":
        cap["argv"].append("--review-pair")
    elif route == "generic":
        cap["protocol"] = "grok-json"
        cap["argv"].append("--review-pair")  # A flag alone never earns native semantics.
    monkeypatch.setattr(readiness, "validate_capability", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        readiness,
        "capability_for",
        lambda *args, **kwargs: (
            cap
            if route != "absent"
            else (_ for _ in ()).throw(CampaignError("No qualified review"))
        ),
    )
    if route == "pair":
        assert (
            readiness.selected_capability(get_profile("codex"), candidate, {}, "native-review-pair")
            == cap
        )
    else:
        with pytest.raises(CampaignError, match="qualified"):
            readiness.selected_capability(get_profile("codex"), candidate, {}, "native-review-pair")


def test_pair_never_waives_independent_family_selection(project, tmp_path, monkeypatch):
    from test_campaign_runtime_admission import configured as admission

    from bmad_loop.campaign_runtime import CampaignRuntime

    runtime, snapshot, astra, fable = admission(project, tmp_path)
    astra = dataclasses.replace(astra, provider_family="openai")
    fable = dataclasses.replace(fable, provider_family="anthropic")
    grok = RoleCandidate("grok", "grok", provider_family="xai")
    snapshot.spec = dataclasses.replace(
        snapshot.spec,
        roles=(("publication_review", RoleSpec(pool="reviewers")),),
        role_pools=(("reviewers", (astra, fable, grok)),),
    )
    snapshot.bindings["capacity"]["observations"].append(
        {
            "profile": "grok",
            "model": "grok",
            "capacity": {"available": True, "slots": 1, "model_access": True},
        }
    )
    monkeypatch.setattr(runtime, "_launcher_readiness", lambda *args, **kwargs: {"ready": True})
    assert (
        CampaignRuntime._role(
            runtime, snapshot, "publication_review", developer_families=("openai", "anthropic")
        )
        == grok
    )


def test_preflight_and_prepared_admission_require_the_pair_grant(project, tmp_path, monkeypatch):
    from test_campaign_preflight import inspect, spec
    from test_campaign_runtime_admission import configured as admission

    from bmad_loop.campaign_model import CampaignUnit

    selected = dataclasses.replace(
        spec(tmp_path), roles=(("native_review_pair", RoleSpec(profile="codex", model="astra")),)
    )
    result = inspect(project, tmp_path, monkeypatch, selected)
    native = result["capabilities"]["publication"]["observations"]["native_review_pair"]
    assert len(native) == 1 and not native[0]["eligible"]
    assert "qualified" in native[0]["launcher"]["reason"]
    runtime, snapshot, astra, _ = admission(project, tmp_path / "admission")
    snapshot.spec = dataclasses.replace(snapshot.spec, roles=selected.roles)
    unit = CampaignUnit(UnitSpec("s:e:u", "s", "e", "Unit"), UnitState.REVIEWING)
    snapshot.units[unit.id] = unit
    action = OutboxAction(
        "pair",
        "launch",
        unit.id,
        {
            "kind": "stage",
            "stage": "native_review_pair",
            "role": "native_review_pair",
            "candidate": dataclasses.asdict(astra),
        },
        snapshot.generation,
    )
    snapshot.outbox.append(action)
    runtime.control(snapshot, {"kind": "prepare-effect", "action": dataclasses.asdict(action)})
    assert action.state == "confirmed"
    assert "qualified" in action.receipt["admission_deferred"]
    assert unit.attempt == 0 and unit.claim is None


def test_native_pair_faults_share_monotonic_review_budget():
    from test_campaign_usage import state

    from bmad_loop.campaign_usage import admission_reason, apply

    snapshot, unit, _, _ = state()
    for role in ("native_review_pair", "publication_review"):
        sample = {
            "schema_version": 1,
            "kind": "stage",
            "stream_id": "stage:" + role,
            "tokens": {"input_tokens": 15},
            "usage_complete": True,
            "session_observed": True,
        }
        result = {"outcome": "invalid_stage_result", "accounting": sample}
        apply(snapshot, unit, result, role=role)
        apply(snapshot, unit, result, role=role)
    assert unit.review_attempts == 2
    assert unit.evidence["usage"]["tokens"]["input_tokens"] == 30
    assert "fault budget exhausted" in admission_reason(
        snapshot, unit, {"kind": "stage", "role": "native_review_pair"}
    )


@pytest.mark.parametrize("qualified_pair", [True, False])
def test_native_pair_handoff_requires_readonly_pair_not_writer_grant(
    project, tmp_path, monkeypatch, qualified_pair
):
    from bmad_loop import campaign_handoff_flow as handoff

    runtime, snapshot, unit = configured(project, tmp_path)
    first, second = RoleCandidate("codex", "astra"), RoleCandidate("codex", "sol")
    snapshot.spec = dataclasses.replace(
        snapshot.spec,
        roles=(("native_review_pair", RoleSpec(pool="pairs")),),
        role_pools=(("pairs", (first, second)),),
    )
    unit.claim = WorkerClaim(
        unit.id,
        1,
        1,
        "original",
        role="native_review_pair",
        resource="review",
        writes=False,
        quiescent=True,
    )
    snapshot.outbox.append(
        OutboxAction(
            "original",
            "launch",
            unit.id,
            {
                "kind": "stage",
                "role": "native_review_pair",
                "stage": "native_review_pair",
                "candidate": dataclasses.asdict(first),
            },
            1,
            state="confirmed",
        )
    )
    modes = []

    def capability(current, candidate, mode="workspace-tools"):
        modes.append(mode)
        assert mode == "review", "a native pair must not require or borrow writer authority"
        return {
            "permissions": "read-only-tools",
            "protocol": "codex-native-review",
            "argv": ["--review-pair"] if qualified_pair else [],
        }

    monkeypatch.setattr(handoff, "_cap", capability)
    handoff.begin(runtime, snapshot, unit, reason="quota")
    assert modes == ["review"]
    value = unit.evidence["handoff"]
    if qualified_pair:
        assert value["candidate"] == dataclasses.asdict(second)
        assert value["original_payload"]["stage"] == "native_review_pair"
    else:
        assert value["candidate"] is None
        assert unit.hold.kind == "handoff"


@pytest.mark.parametrize(
    "state",
    [
        UnitState.MERGE_REQUESTED,
        UnitState.MERGED_PENDING_SYNC,
        UnitState.DELIVERED,
        UnitState.CANCELED,
    ],
)
@pytest.mark.parametrize("revision", ["graph_revision", "config_revision"])
def test_issued_and_delivered_candidates_retain_historical_pair_lineage(
    project, tmp_path, state, revision
):
    runtime, snapshot, unit = configured(project, tmp_path)
    pair = receipt(snapshot, unit)
    independent = receipt(snapshot, unit, stage="publication_review")
    unit.evidence.update(native_review_pair=pair, review=independent)
    unit.state = state
    setattr(snapshot, revision, getattr(snapshot, revision) + 1)
    runtime.prepare_resume(snapshot)
    assert unit.state == state
    assert unit.evidence["native_review_pair"] == pair
    assert unit.evidence["review"] == independent
    # Issued receipt remains reconcilable; it does not authorize a fresh merge.
    assert not runtime._publication_evidence(snapshot, unit).review_approved


@pytest.mark.parametrize("state", ["prepared", "sent_unknown"])
def test_lost_merge_ack_preserves_submitted_pair_until_reconciliation(project, tmp_path, state):
    runtime, snapshot, unit = configured(project, tmp_path)
    pair = receipt(snapshot, unit)
    independent = receipt(snapshot, unit, stage="publication_review")
    unit.evidence.update(native_review_pair=pair, review=independent)
    unit.state = UnitState.READY_TO_PUBLISH
    action = OutboxAction(
        "merge", "github", unit.id, {"purpose": "merge"}, snapshot.generation, state=state
    )
    snapshot.outbox.append(action)
    snapshot.graph_revision += 1
    runtime.prepare_resume(snapshot)
    assert unit.state == UnitState.READY_TO_PUBLISH
    assert unit.evidence["native_review_pair"] == pair
    assert unit.evidence["review"] == independent
    assert not runtime._publication_evidence(snapshot, unit).review_approved
    # Once the uncertain intent is definitively rejected, normal fresh review
    # can proceed. Never discard evidence while that write may have succeeded.
    action.state = "rejected"
    runtime.prepare_resume(snapshot)
    assert unit.state == UnitState.SYNCING
    assert not {"native_review_pair", "review"}.intersection(unit.evidence)
