"""Production recovery proposals preserve ownership, authority and stage budgets."""

import dataclasses
import hashlib
import json
from types import SimpleNamespace

import pytest
from conftest import git

from bmad_loop import verify
from bmad_loop.campaign_decisions import ObservedFact
from bmad_loop.campaign_model import (
    CampaignError,
    CampaignSnapshot,
    CampaignSpec,
    CampaignUnit,
    DecisionSpec,
    Hold,
    ResolutionSpec,
    RoleCandidate,
    UnitSpec,
    UnitState,
    WorkerClaim,
)
from bmad_loop.campaign_recovery import Quiescence, RecoveryReceipt
from bmad_loop.campaign_resolution import CauseCode
from bmad_loop.campaign_resolution_flow import (
    apply_backoff,
    apply_context,
    apply_diagnosis,
    collect_context,
    prepare,
    proposals,
    recover_append_only,
    resolution_context,
    typed_failure,
    validate_stage_result,
)


def state(project, *, hold="verification_failed"):
    repo = project.project
    spec = CampaignSpec("resolution", resolution=ResolutionSpec(mode="bounded-auto"))
    unit = CampaignUnit(
        UnitSpec("s:project:u", "s", "epic", "Cache", scopes=("src/cache",)),
        UnitState.RUNNING,
        hold=Hold(hold, "Fix the accepted cache behavior", id="hold-1"),
        bindings={
            "head": verify.rev_parse_head(repo),
            "base": verify.rev_parse_head(repo),
            "workspace": str(repo),
        },
    )
    unit.evidence["worker_history"] = [
        {"outcome": "verification_failed", "launch_id": "failed-worker"}
    ]
    return CampaignSnapshot("campaign", "org/repo", spec, {unit.id: unit})


def candidate():
    return {
        "escalation": RoleCandidate("codex", "astra"),
        "decision_review": RoleCandidate("claude", "fable"),
        "conflict_repair": RoleCandidate("codex", "astra"),
    }


def test_context_collection_is_a_separate_effect_before_model_launch(project):
    snapshot = state(project)
    first = proposals(snapshot, project.project, candidate(), now=10)
    assert first[0].kind == "resolution-context"
    context = collect_context(project.project, snapshot, "s:project:u", "diagnosis")
    apply_context(snapshot, "s:project:u", context)
    launch = proposals(snapshot, project.project, candidate(), now=10)[0]
    assert launch.kind == "launch"
    assert launch.payload["role"] == "escalation"
    assert launch.payload["result_contract"] == "resolution-v1"
    assert launch.payload["read_only"] is True
    assert snapshot.units["s:project:u"].resolution_attempts == 0
    prepare(snapshot, "s:project:u", launch.payload)
    assert snapshot.units["s:project:u"].resolution_attempts == 1
    assert proposals(snapshot, project.project, candidate(), now=10) == ()


def test_sealed_child_recovery_does_not_admit_a_second_resolver(project):
    snapshot = state(project, hold="recovery_required")
    unit = snapshot.units["s:project:u"]
    unit.claim = WorkerClaim(unit.id, 1, 1, "sealed-child", quiescent=True)
    assert proposals(snapshot, project.project, candidate(), now=10) == ()


def test_context_effect_after_recovery_is_a_confirmed_noop(project, tmp_path):
    from bmad_loop.campaign_runtime import CampaignRuntime
    from bmad_loop.campaign_store import CampaignStore, add_action

    snapshot = state(project)
    unit = snapshot.units["s:project:u"]
    snapshot.spec = dataclasses.replace(snapshot.spec, units=(unit.spec,))
    pending = add_action(
        snapshot,
        "resolution-context",
        unit.id,
        {
            "mode": "diagnosis",
            "hold_id": unit.hold.id,
            "head": unit.bindings["head"],
        },
    )
    stale = snapshot.clone()
    unit.hold = None
    store = CampaignStore(tmp_path / "state")
    current = store.create(snapshot)
    runtime = CampaignRuntime(project.project, store)
    receipt = runtime._perform_resolution_context(stale, pending)
    assert receipt.state == "confirmed" and receipt.data == {"stale_context": True}
    runtime.apply(current, pending, receipt)
    assert current.units[unit.id].hold is None
    assert "resolution_context" not in current.units[unit.id].evidence


def diagnosis(packet, **changes):
    raw = {
        "schema_version": 1,
        "coordinates": packet["coordinates"],
        "cause_id": packet["cause"]["id"],
        "cause_code": packet["cause"]["code"],
        "action": "repair",
        "rationale": "Repair the accepted cache behavior",
        "evidence_ids": [packet["cause"]["id"]],
        "affected_paths": ["src/cache/db.py"],
        "preserved_refs": packet["preserved_refs"],
        "verification": packet["expected_verification"],
        "unresolved_decisions": [],
        "command": [],
        "status": "done",
    }
    return {**raw, **changes}


def test_validated_diagnosis_requests_separate_owned_same_unit_repair(project):
    snapshot = state(project)
    context = collect_context(project.project, snapshot, "s:project:u", "diagnosis")
    apply_context(snapshot, "s:project:u", context)
    launch = proposals(snapshot, project.project, candidate(), now=10)[0]
    prepare(snapshot, "s:project:u", launch.payload)
    packet = context["packet"]
    receipt = {
        "outcome": "stage_complete",
        "head": packet["coordinates"]["head"],
        "artifact": diagnosis(packet),
    }
    receipt["validated_resolution"] = validate_stage_result(
        project.project, snapshot, "s:project:u", receipt
    )
    next_step = apply_diagnosis(
        snapshot, "s:project:u", receipt, candidate=candidate()["escalation"]
    )
    assert next_step.kind == "launch"
    assert next_step.unit == "s:project:u"
    assert next_step.payload["stage"] == "technical_repair"
    assert next_step.payload["read_only"] is False
    assert snapshot.units["s:project:u"].state != UnitState.DELIVERED
    assert snapshot.units["s:project:u"].resolution_attempts == 1
    assert snapshot.units["s:project:u"].evidence["accepted_resolution"]["automatic"] is True


def test_model_capacity_wait_keeps_authorized_repair_resumable(project):
    snapshot = state(project)
    context = collect_context(project.project, snapshot, "s:project:u", "diagnosis")
    apply_context(snapshot, "s:project:u", context)
    launch = proposals(snapshot, project.project, candidate(), now=10)[0]
    prepare(snapshot, "s:project:u", launch.payload)
    packet = context["packet"]
    receipt = {
        "outcome": "stage_complete",
        "head": packet["coordinates"]["head"],
        "artifact": diagnosis(packet),
    }
    receipt["validated_resolution"] = validate_stage_result(
        project.project, snapshot, "s:project:u", receipt
    )
    assert apply_diagnosis(snapshot, "s:project:u", receipt) is None
    assert proposals(snapshot, project.project, {}, now=10) == ()
    resumed = proposals(snapshot, project.project, candidate(), now=10)
    assert resumed[0].payload["result_contract"] == "repair-v1"


def test_serialized_context_fingerprint_is_stable_across_restart(project):
    snapshot = state(project)
    original = collect_context(project.project, snapshot, "s:project:u", "diagnosis")
    roundtrip = json.loads(json.dumps(original, sort_keys=True))
    assert resolution_context(original["packet"]) == resolution_context(roundtrip["packet"])


def test_typed_transient_backoff_precedes_any_model_session(project):
    snapshot = state(project)
    unit = snapshot.units["s:project:u"]
    unit.evidence["failure"] = {
        "code": "quota",
        "producer": "adapter.quota",
        "revision": "quota-v1",
        "id": "quota-1",
    }
    first = proposals(snapshot, project.project, candidate(), now=100)
    assert first[0].kind == "resolution-backoff"
    apply_backoff(snapshot, unit.id, now=100)
    assert unit.retry_at == 105
    assert proposals(snapshot, project.project, candidate(), now=104) == ()
    retry = proposals(snapshot, project.project, candidate(), now=105)
    assert retry[0].kind == "resolution-reprobe"


def test_unknown_legacy_prose_never_becomes_automatic_transient_cause(project):
    snapshot = state(project, hold="failed")
    snapshot.units["s:project:u"].evidence["worker_history"] = [
        {"outcome": "failed", "error": "quota retry transport rate limit"}
    ]
    assert typed_failure(snapshot, "s:project:u").code == CauseCode.UNKNOWN
    assert proposals(snapshot, project.project, candidate(), now=10)[0].kind == "resolution-context"


def test_owner_decision_uses_evidence_review_before_escalation(project):
    snapshot = state(project, hold="product_choice")
    path = project.project / "owner-notes.md"
    path.write_text("Use SQLite for the cache.")
    git(project.project, "add", path.name)
    git(project.project, "commit", "-qm", "adopt owner decision")
    unit = snapshot.units["s:project:u"]
    unit.bindings["head"] = verify.rev_parse_head(project.project)
    snapshot.spec = dataclasses.replace(
        snapshot.spec, decisions=DecisionSpec(sources=("owner-notes.md",))
    )
    context = collect_context(project.project, snapshot, unit.id, "decision")
    apply_context(snapshot, unit.id, context)
    launch = proposals(snapshot, project.project, candidate(), now=10)[0]
    assert launch.payload["role"] == "decision_review"
    assert launch.payload["candidate"]["model"] == "fable"
    prepare(snapshot, unit.id, launch.payload)
    packet = context["packet"]
    source = packet["sources"][0]
    artifact = {
        "schema_version": 1,
        "coordinates": packet["coordinates"],
        "hold_id": unit.hold.id,
        "disposition": "prior_decision_applies",
        "action": "choose",
        "answer": "SQLite",
        "support": [
            {
                "source_id": source["id"],
                "revision": source["revision"],
                "quote": "Use SQLite for the cache.",
            }
        ],
    }
    receipt = {
        "outcome": "stage_complete",
        "head": packet["coordinates"]["head"],
        "artifact": artifact,
    }
    receipt["validated_resolution"] = validate_stage_result(
        project.project, snapshot, unit.id, receipt
    )
    assert apply_diagnosis(snapshot, unit.id, receipt) is None
    assert unit.hold is None
    assert snapshot.bindings["decision_receipts"]
    assert unit.resolution_attempts == 0


@pytest.mark.parametrize(
    "binding", ["pinned", "unpinned", "modified", "planning", "owner_stop", "design_choice"]
)
def test_runtime_delegation_requires_frozen_committed_policy_and_execution_choice(
    project, binding, tmp_path, monkeypatch
):
    from bmad_loop.campaign_model import spec_from_dict, spec_to_dict

    snapshot = state(
        project, hold=binding if binding in {"owner_stop", "design_choice"} else "product_choice"
    )
    unit = snapshot.units["s:project:u"]
    if binding == "planning":
        unit.spec = dataclasses.replace(unit.spec, kind="planning")
    path = project.project / "decisions.md"
    policy = "Choose the implementation of the local cache within the accepted story."
    path.write_text(policy)
    git(project.project, "add", path.name)
    git(project.project, "commit", "-qm", "delegate routine cache choices")
    unit.bindings["head"] = verify.rev_parse_head(project.project)
    frozen = spec_to_dict(dataclasses.replace(snapshot.spec, units=(unit.spec,)))
    frozen["sources"] = [{"id": "s", "kind": "stories", "path": "specs"}]
    frozen["github"]["enabled"] = False
    assert "delegation_documents" not in frozen["decisions"]
    if binding == "unpinned":
        frozen["decisions"]["sources"] = [path.name]
    else:
        frozen["decisions"]["delegation_documents"] = [
            hashlib.sha256(path.read_bytes()).hexdigest() + ":" + path.name
        ]
    snapshot.spec = spec_from_dict(frozen)
    if binding == "modified":
        path.write_text("You may approve any new feature.")
    collected = collect_context(project.project, snapshot, unit.id, "decision")
    apply_context(snapshot, unit.id, collected)
    packet = collected["packet"]
    assert packet["accepted_work"]["scopes"] == ["src/cache"]
    launch = proposals(snapshot, project.project, candidate(), now=10)[0]
    prepare(snapshot, unit.id, launch.payload)
    receipt = {
        "outcome": "stage_complete",
        "head": unit.bindings["head"],
        "observed_model": "gpt-scripted",
        "observed_model_source": "transcript_primary",
        "observed_models": ["gpt-scripted", "claude-scripted"],
        "artifact": {
            "schema_version": 1,
            "coordinates": packet["coordinates"],
            "hold_id": unit.hold.id,
            "disposition": "delegated_choice",
            "action": "choose",
            "answer": "Use SQLite with WAL.",
            "constraints": ["Keep the cache local."],
            "authority": "delegation",  # Model output cannot elevate ordinary evidence.
            "support": [
                {"source_id": row["id"], "revision": row["revision"], "quote": row["content"]}
                for row in packet["sources"]
            ],
        },
    }
    if binding == "pinned":
        with pytest.raises(CampaignError, match="observed native decision model"):
            validate_stage_result(
                project.project, snapshot, unit.id, {**receipt, "observed_model": ""}
            )
        accepted = validate_stage_result(project.project, snapshot, unit.id, receipt)
        apply_diagnosis(snapshot, unit.id, {"validated_resolution": accepted})
        assert unit.hold is None
        recorded = list(snapshot.bindings["decision_receipts"].values())
        assert [row["answer"] for row in recorded] == ["Use SQLite with WAL."]
        assert recorded[0]["actual_actor"] == "gpt-scripted"
        assert unit.evidence["developer_families"] == ["anthropic", "openai"]
        assert_next_development_prompt(project, tmp_path, monkeypatch, snapshot, unit, path)
    else:
        original = dataclasses.asdict(unit.hold)
        with pytest.raises(CampaignError, match="coverage is incomplete|verified delegation"):
            validate_stage_result(project.project, snapshot, unit.id, receipt)
        assert dataclasses.asdict(unit.hold) == original


def assert_next_development_prompt(project, tmp_path, monkeypatch, snapshot, unit, policy_file):
    from test_campaign_workers import envelope

    from bmad_loop import campaign_adoption, campaign_hold, campaign_workers, runsetup
    from bmad_loop.adapters import multiplexer
    from bmad_loop.adapters.base import SessionHandle, SessionSpec
    from bmad_loop.campaign_model import OutboxAction
    from bmad_loop.campaign_runtime import CampaignRuntime
    from bmad_loop.campaign_store import CampaignStore

    repo = project.project
    branch = git(repo, "branch", "--show-current").strip()
    unit.bindings["branch"] = branch
    unit.claim = WorkerClaim(
        unit.id,
        unit.attempt,
        snapshot.generation,
        "dev-launch",
        run_id="dev-run",
        workspace=str(repo),
        branch=branch,
    )
    snapshot.common_dir = str(campaign_workers.common_directory(repo))
    store = CampaignStore(tmp_path / "state")
    runtime = CampaignRuntime(repo, store)
    context = envelope(repo, store.directory(snapshot.id) / "workers" / "dev-launch")
    campaign_hold.initialize(store.directory(snapshot.id), snapshot)
    monkeypatch.setattr(runtime, "_launch_envelope", lambda *args: context)
    monkeypatch.setattr(campaign_adoption, "guard_owned", lambda *args, **kwargs: None)
    monkeypatch.setattr(multiplexer, "configure_multiplexer", lambda *args, **kwargs: None)
    action = OutboxAction(
        "dev-launch",
        "launch",
        unit.id,
        {
            "kind": "execution",
            "checkpoint_base": unit.bindings["head"],
            "checkpoint_paths": list(unit.spec.scopes),
        },
        snapshot.generation,
    )
    prepared = runtime._prepare_launch(snapshot, action)
    assert isinstance(prepared, tuple), prepared
    _, request, _ = prepared
    prompts = []

    def capture(spec):
        prompts.append(spec.prompt)
        return SessionHandle(spec.task_id, "scripted")

    adapter = SimpleNamespace(start_session=capture)
    monkeypatch.setattr(
        runsetup, "make_adapters", lambda *args, **kwargs: {"dev": adapter, "review": adapter}
    )
    adapters = campaign_workers._adapters(request, context, repo, tmp_path / "run", context.policy)
    adapters["dev"].start_session(SessionSpec("impl", "dev", "Implement", repo))
    adapters["review"].start_session(SessionSpec("review", "review", "Review", repo))
    assert all(
        text in prompts[0]
        for text in (
            "Use SQLite with WAL.",
            "Keep the cache local.",
            "gpt-scripted",
            '"action": "choose"',
        )
    )
    assert policy_file.read_text() not in prompts[0] and "Use SQLite with WAL." not in prompts[1]
    policy_file.write_text("Hold implementation for a revised policy.")
    refused = runtime._prepare_launch(snapshot, action)
    assert refused.data["not_launched"]
    runtime.apply(snapshot, action, refused)
    assert unit.hold.kind == "product_choice" and unit.hold.id == "hold-1"
    assert not unit.evidence["pending_delegated_choices"] and len(prompts) == 2


def test_reviewed_owner_options_reach_inbox_and_keep_the_hold(project, capsys):
    from bmad_loop.campaign_cli import _emit, inbox_document

    snapshot = state(project, hold="product_choice")
    unit = snapshot.units["s:project:u"]
    before = next(row for row in inbox_document(snapshot, now=5)["items"] if row["kind"] == "hold")
    assert before["operator_action_required"] is False
    assert "configured decision reviewer" in before["next_automatic_action"]
    original = dataclasses.asdict(unit.hold)
    context = collect_context(project.project, snapshot, unit.id, "decision")
    apply_context(snapshot, unit.id, context)
    launch = proposals(snapshot, project.project, candidate(), now=10)[0]
    assert "preference order" in launch.payload["custom_prompt"]
    prepare(snapshot, unit.id, launch.payload)
    options = [
        {
            "answer": "Keep the hold",
            "reason": "The existing evidence does not authorize this choice.",
            "source_ids": ["hold:hold-1"],
        },
        {
            "answer": "Request a scoped release",
            "reason": "The owner can release this specific hold.",
            "source_ids": ["hold:hold-1"],
        },
    ]
    receipt = {
        "outcome": "stage_complete",
        "head": unit.bindings["head"],
        "artifact": {
            "schema_version": 1,
            "coordinates": context["packet"]["coordinates"],
            "hold_id": unit.hold.id,
            "disposition": "needs_new_owner_decision",
            "action": "",
            "answer": options[0]["answer"],
            "options": options,
        },
    }
    accepted = validate_stage_result(project.project, snapshot, unit.id, receipt)
    apply_diagnosis(snapshot, unit.id, {"validated_resolution": json.loads(json.dumps(accepted))})
    assert dataclasses.asdict(unit.hold) == original
    assert accepted["accepted"]["actual_actor"] == ""
    inbox = inbox_document(snapshot, now=20)
    item = next(row for row in inbox["items"] if row["kind"] == "hold")
    assert item["operator_action_required"]
    assert item["owner_advice"] == {
        "decision_id": accepted["accepted"]["id"],
        "private": False,
        "options": options,
    }
    _emit(SimpleNamespace(json=False), {"ok": True, "inbox": inbox})
    out = capsys.readouterr().out
    assert "Recommended: Keep the hold" in out and "Alternative: Request a scoped release" in out
    assert (
        "The existing evidence does not authorize this choice." in out
        and "Evidence: hold:hold-1" in out
    )
    unit.evidence["decision_input_revision"] = "new owner message"
    item = next(row for row in inbox_document(snapshot, now=21)["items"] if row["kind"] == "hold")
    assert "owner_advice" not in item


def test_changed_candidate_or_authoritative_document_cannot_clear_hold(project):
    snapshot = state(project, hold="product_choice")
    context = collect_context(project.project, snapshot, "s:project:u", "decision")
    apply_context(snapshot, "s:project:u", context)
    launch = proposals(snapshot, project.project, candidate(), now=10)[0]
    prepare(snapshot, "s:project:u", launch.payload)
    snapshot.units["s:project:u"].bindings["head"] = "changed-head"
    with pytest.raises(CampaignError, match="no longer matches"):
        validate_stage_result(
            project.project,
            snapshot,
            "s:project:u",
            {"outcome": "stage_complete", "head": context["packet"]["coordinates"]["head"]},
        )


def test_recovery_wrapper_keeps_fence_through_confirmed_publication(project):
    snapshot = state(project)
    packet = collect_context(project.project, snapshot, "s:project:u", "diagnosis")["packet"]
    ctx = resolution_context(packet)
    calls = []
    head = "a" * 40
    corrected = "b" * 40
    ctx = dataclasses.replace(ctx, coordinates=dataclasses.replace(ctx.coordinates, head=head))

    class Recovery:
        def begin(self, action, **kwargs):
            calls.append(("begin", action, kwargs))

        def recover(self, action, **kwargs):
            calls.append(("recover", action, kwargs))
            return RecoveryReceipt(action, head, corrected, "tree", "ref", None, (), "committed")

        def finish(self, action, **kwargs):
            calls.append(("finish", action, kwargs))
            return RecoveryReceipt(action, head, corrected, "tree", "ref", None, (), "complete")

    result = recover_append_only(
        Recovery(),
        "recovery-1",
        context=ctx,
        baseline="c" * 40,
        owned_paths=("src/cache/db.py",),
        quiescence=Quiescence(head, 2, True, True, "observed-quiescence"),
    )
    assert [row[0] for row in calls] == ["begin", "recover", "finish"]
    assert calls[-1][2]["quiescence"].head == corrected
    assert result["state"] == "complete"
    assert result["head"] == corrected


def test_approval_revocation_after_context_collection_invalidates_satisfaction(project):
    snapshot = state(project, hold="github_review")
    unit = snapshot.units["s:project:u"]
    fact = ObservedFact(
        "review-1",
        "github_approval",
        unit.hold.id,
        unit.id,
        unit.bindings["head"],
        True,
        "eligible-reviewer",
        "review-v1",
        verified=True,
    )
    result = collect_context(project.project, snapshot, unit.id, "decision", facts=(fact,))
    apply_context(snapshot, unit.id, result)
    launch = proposals(snapshot, project.project, candidate(), now=10)[0]
    prepare(snapshot, unit.id, launch.payload)
    packet = result["packet"]
    raw = {
        "schema_version": 1,
        "coordinates": packet["coordinates"],
        "hold_id": unit.hold.id,
        "disposition": "already_satisfied",
        "action": "continue",
        "answer": "Current review approved",
        "fact_ids": ["review-1"],
    }
    receipt = {"outcome": "stage_complete", "head": unit.bindings["head"], "artifact": raw}
    with pytest.raises(CampaignError, match="independently refreshed"):
        validate_stage_result(project.project, snapshot, unit.id, receipt)
    with pytest.raises(CampaignError, match="facts changed"):
        validate_stage_result(
            project.project,
            snapshot,
            unit.id,
            receipt,
            facts=(dataclasses.replace(fact, satisfied=False),),
        )
    receipt["validated_resolution"] = validate_stage_result(
        project.project, snapshot, unit.id, receipt, facts=(fact,)
    )
    apply_diagnosis(snapshot, unit.id, receipt)
    assert unit.hold is None


def test_authoritative_document_edit_after_context_cannot_apply_old_decision(project):
    snapshot = state(project, hold="product_choice")
    path = project.project / "owner-notes.md"
    path.write_text("Use SQLite for the cache.")
    git(project.project, "add", path.name)
    git(project.project, "commit", "-qm", "adopt owner decision")
    unit = snapshot.units["s:project:u"]
    unit.bindings["head"] = verify.rev_parse_head(project.project)
    snapshot.spec = dataclasses.replace(
        snapshot.spec, decisions=DecisionSpec(sources=("owner-notes.md",))
    )
    result = collect_context(project.project, snapshot, unit.id, "decision")
    apply_context(snapshot, unit.id, result)
    launch = proposals(snapshot, project.project, candidate(), now=10)[0]
    prepare(snapshot, unit.id, launch.payload)
    packet = result["packet"]
    source = packet["sources"][0]
    artifact = {
        "schema_version": 1,
        "coordinates": packet["coordinates"],
        "hold_id": unit.hold.id,
        "disposition": "prior_decision_applies",
        "action": "choose",
        "answer": "SQLite",
        "support": [
            {
                "source_id": source["id"],
                "revision": source["revision"],
                "quote": "Use SQLite for the cache.",
            }
        ],
    }
    path.write_text("Wait for a new owner decision before choosing storage.")
    with pytest.raises(CampaignError, match="changed or was revoked"):
        validate_stage_result(
            project.project,
            snapshot,
            unit.id,
            {"outcome": "stage_complete", "head": unit.bindings["head"], "artifact": artifact},
        )
    assert unit.hold is not None


def test_diagnosis_packet_redacts_credentials_but_preserves_evidence_coordinates(project):
    snapshot = state(project, hold="failed")
    secret = "ghp_scripted_test_credential_never_real"
    unit = snapshot.units["s:project:u"]
    unit.evidence["worker_history"] = [
        {
            "outcome": "failed",
            "error": f"request refused with {secret}",
            "authorization": "Bearer test-credential",
        }
    ]
    packet = collect_context(project.project, snapshot, unit.id, "diagnosis")["packet"]
    rendered = json.dumps(packet)
    assert secret not in rendered
    assert "Bearer test-credential" not in rendered
    assert unit.bindings["head"] in rendered
    assert "<redacted:credential>" in rendered


def test_accepted_technical_diagnosis_can_finish_without_waiving_original_hold(project):
    from bmad_loop.campaign_resolution import ResolutionResult
    from bmad_loop.campaign_resolution_flow import finish_repair

    snapshot = state(project)
    unit = snapshot.units["s:project:u"]
    unit.state = UnitState.REVIEWING
    unit.resume_state = UnitState.SYNCING
    context = collect_context(project.project, snapshot, unit.id, "diagnosis")
    apply_context(snapshot, unit.id, context)
    launch = proposals(snapshot, project.project, candidate(), now=10)[0]
    prepare(snapshot, unit.id, launch.payload)
    packet = context["packet"]
    receipt = {
        "outcome": "stage_complete",
        "head": packet["coordinates"]["head"],
        "artifact": diagnosis(packet),
    }
    receipt["validated_resolution"] = validate_stage_result(
        project.project, snapshot, unit.id, receipt
    )
    next_step = apply_diagnosis(snapshot, unit.id, receipt, candidate=candidate()["escalation"])
    assert unit.hold.kind == "resolution"
    assert unit.hold.evidence["original_hold"]["kind"] == "verification_failed"
    accepted = unit.evidence["accepted_resolution"]
    from bmad_loop.campaign_model import EvidenceCoordinates

    result = ResolutionResult(
        accepted["id"],
        EvidenceCoordinates(**accepted["coordinates"]),
        unit.bindings["head"],
        unit.bindings["head"],
        True,
        True,
        "source",
        recovery_push_confirmed=True,
    )
    finish_repair(snapshot, unit.id, result)
    assert unit.state == UnitState.SYNCING
    assert unit.hold is None
    assert next_step.unit == unit.id


def test_owner_holds_cannot_enter_technical_diagnosis_to_bypass_decision_review(project):
    snapshot = state(project, hold="owner_stop")
    with pytest.raises(CampaignError, match="decision evidence review first"):
        collect_context(project.project, snapshot, "s:project:u", "diagnosis")


@pytest.mark.parametrize("action", ["repair", "reverify", "retry", "reprobe"])
def test_diagnosis_cannot_schedule_model_proposed_controller_commands(project, action):
    snapshot = state(project)
    snapshot.spec = dataclasses.replace(snapshot.spec, verify_commands=(("approved-test",),))
    unit = snapshot.units["s:project:u"]
    context = collect_context(project.project, snapshot, unit.id, "diagnosis")
    apply_context(snapshot, unit.id, context)
    launch = proposals(snapshot, project.project, candidate(), now=10)[0]
    prepare(snapshot, unit.id, launch.payload)
    original = snapshot.clone()
    packet = context["packet"]
    receipt = {
        "outcome": "stage_complete",
        "head": packet["coordinates"]["head"],
        "artifact": diagnosis(
            packet, action=action, verification=[["approved-test"], ["controller-secret-reader"]]
        ),
    }
    with pytest.raises(CampaignError, match="frozen verification"):
        validate_stage_result(project.project, snapshot, unit.id, receipt)
    assert snapshot == original
    assert unit.hold.id == "hold-1"


@pytest.mark.parametrize("hold", ["verification_failed", "operator", "required_approval"])
@pytest.mark.parametrize("contract", ["decision-v1", "resolution-v1"])
@pytest.mark.parametrize("outcome", ["invalid_stage_result", "failed"])
@pytest.mark.parametrize("interruption", [None, "quota", "transport"])
@pytest.mark.parametrize("worker_state", ["finished", "interrupted"])
def test_terminal_failed_diagnosis_preserves_original_hold_and_dirty_work(
    project, tmp_path, hold, contract, outcome, interruption, worker_state
):
    from bmad_loop.campaign_runtime import CampaignRuntime
    from bmad_loop.campaign_store import CampaignStore, add_action
    from bmad_loop.campaign_workers import common_directory

    snapshot = state(project, hold=hold)
    unit = snapshot.units["s:project:u"]
    snapshot.spec = dataclasses.replace(snapshot.spec, units=(unit.spec,))
    snapshot.spec = dataclasses.replace(
        snapshot.spec, reasoning=dataclasses.replace(snapshot.spec.reasoning, enabled=True)
    )
    unit.evidence["contributing_models"] = [{"family": "claude", "model": "writer"}]
    unit.evidence["uncertain_external_action"] = {"id": "retained-write", "state": "unknown"}
    unit.evidence["required_findings"] = [{"id": "unfinished-acceptance"}]
    unit.resolution_attempts = 2
    unit.attempt = 3
    unit.claim = WorkerClaim(
        unit.id, unit.attempt, snapshot.generation, "diagnosis", role="escalation"
    )
    store = CampaignStore(tmp_path / "store")
    snapshot = store.create(snapshot)
    unit = snapshot.units[unit.id]
    request = store.directory(snapshot.id) / "workers" / "diagnosis" / "request.json"
    request.parent.mkdir(parents=True)
    stage_request = {
        "result_contract": contract,
        "kind": "stage",
        "workspace": str(project.project),
        "common_dir": str(common_directory(project.project)),
        "branch": git(project.project, "branch", "--show-current").strip(),
    }
    request.write_text(json.dumps(stage_request))
    tracked = project.project / "retained.txt"
    tracked.write_text("staged\n")
    git(project.project, "add", tracked.name)
    tracked.write_text("unstaged\n")
    (project.project / "untracked.txt").write_text("untracked\n")
    before = {
        "head": verify.rev_parse_head(project.project),
        "index": git(project.project, "diff", "--cached"),
        "worktree": git(project.project, "diff"),
        "untracked": (project.project / "untracked.txt").read_bytes(),
        "hold": dataclasses.asdict(unit.hold),
        "attempts": (unit.attempt, unit.resolution_attempts),
    }
    runtime = CampaignRuntime(project.project, store)
    receipt = {
        "outcome": outcome,
        "launch_id": "diagnosis",
        "head": before["head"],
        "stage": "decision_review" if contract == "decision-v1" else "escalation",
        "error": "Scripted rejected diagnosis",
    }
    if interruption:
        receipt["interruption"] = {"kind": interruption}
    add_action(
        snapshot,
        "launch",
        unit.id,
        {"candidate": {"profile": "claude", "model": "scripted-model"}},
        action_id="diagnosis",
    )
    data = {"state": "finished", "receipt": receipt}
    expected = receipt
    if worker_state == "interrupted":
        data = {
            "state": "interrupted",
            "head": before["head"],
            "quiescence_confirmed": True,
            "interruption": receipt.get("interruption"),
        }
    assert runtime._observe_git_activity(snapshot, stage_request, data) is None
    if worker_state == "interrupted":
        expected = {**data, "outcome": "interrupted"}
    runtime._apply_worker(
        snapshot,
        unit,
        SimpleNamespace(payload={"launch_id": "diagnosis"}),
        data,
    )
    assert dataclasses.asdict(unit.hold) == before["hold"]
    assert (unit.attempt, unit.resolution_attempts) == before["attempts"]
    assert unit.claim.quiescent
    assert "handoff" not in unit.evidence
    assert unit.evidence["resolution_failures"][-1]["receipt"] == expected
    if interruption == "quota":
        assert "diagnosis" in unit.evidence["quota_fault_observations"]
    assert unit.evidence["required_findings"] == [{"id": "unfinished-acceptance"}]
    assert unit.evidence["uncertain_external_action"]["state"] == "unknown"
    assert unit.evidence["contributing_models"] == [{"family": "claude", "model": "writer"}]
    assert verify.rev_parse_head(project.project) == before["head"]
    assert git(project.project, "diff", "--cached") == before["index"]
    assert git(project.project, "diff") == before["worktree"]
    assert (project.project / "untracked.txt").read_bytes() == before["untracked"]
    saved = store.save(snapshot, snapshot.revision)
    assert dataclasses.asdict(store.load(saved.id).units[unit.id].hold) == before["hold"]
