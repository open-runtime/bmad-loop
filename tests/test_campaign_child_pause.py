"""Saved child pauses cross the normal worker boundary without prose authority."""

from __future__ import annotations

import copy
import dataclasses
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import git, install_bmad_config
from test_campaign_workers import envelope, worker_request
from test_stories_engine import SPEC_FOLDER, setup_stories

from bmad_loop import campaign_child_pause
from bmad_loop import campaign_workers as workers
from bmad_loop import policy as policy_mod
from bmad_loop import runs, runsetup
from bmad_loop.adapters import multiplexer
from bmad_loop.adapters.mock import MockAdapter
from bmad_loop.campaign_execution import CampaignExecutionError
from bmad_loop.campaign_model import (
    CampaignError,
    CampaignSnapshot,
    CampaignSpec,
    CampaignUnit,
    Hold,
    OutboxAction,
    SourceSelector,
    UnitSpec,
    UnitState,
    WorkerClaim,
)
from bmad_loop.campaign_runtime import CampaignRuntime
from bmad_loop.campaign_store import CampaignStore
from bmad_loop.engine import RunPaused
from bmad_loop.journal import load_state, save_state
from bmad_loop.model import PauseCause, PauseKind, Phase, StoryTask
from bmad_loop.stories_engine import StoriesEngine


@pytest.fixture
def paused_child(project, tmp_path, monkeypatch):
    repo = project.project
    install_bmad_config(project)
    setup_stories(project, [{"id": "first", "title": "First", "description": "Retain acceptance"}])
    (repo / "unstaged.txt").write_text("original\n")
    git(repo, "add", "--all")
    git(repo, "commit", "-qm", "test: prepare child pause source")
    (repo / "staged.txt").write_text("staged contribution\n")
    git(repo, "add", "staged.txt")
    (repo / "unstaged.txt").write_text("unfinished contribution\n")
    (repo / "untracked.txt").write_text("untracked contribution\n")
    dirty = git(repo, "status", "--porcelain")
    policy = policy_mod.loads('[gates]\nmode="none"\n[notify]\ndesktop=false\n')
    context = envelope(repo, tmp_path / "execution", policy)
    source = SourceSelector("stories", id="s", path=SPEC_FOLDER)
    spec = UnitSpec(
        context.unit_id,
        "s",
        "epic",
        "Selected",
        selector="first",
        acceptance=("Retain acceptance",),
    )
    adapter = MockAdapter([])
    monkeypatch.setattr(
        runsetup,
        "make_adapters",
        lambda *a, **kw: {role: adapter for role in ("dev", "review", "retro", "triage")},
    )
    monkeypatch.setattr(multiplexer, "configure_multiplexer", lambda *a, **kw: None)
    path = workers.prepare_launch(
        context,
        worker_request(
            repo,
            context,
            kind="execution",
            source=dataclasses.asdict(source),
            source_revision="accepted-source-1",
            unit=dataclasses.asdict(spec),
            run_id="paused-native-run",
        ),
    )
    request = json.loads(path.read_text())

    def create(
        cause,
        *,
        reason="A contextual explanation",
        exception=None,
        verify_outcome=None,
        request_recovery=False,
    ):
        def loop(engine):
            if exception:
                raise exception
            engine.state.tasks[spec.selector] = StoryTask(
                spec.selector,
                1,
                phase=Phase.DEV_VERIFY if verify_outcome is not None else Phase.DEV_RUNNING,
                attempt=2,
            )
            if verify_outcome is not None:
                engine._escalate_outcome(
                    engine.state.tasks[spec.selector], verify_outcome, role="dev"
                )
                raise AssertionError("Failed verification did not pause")
            if request_recovery:
                engine._campaign_recovery(engine.state.tasks[spec.selector], pause_cause=cause)
            raise RunPaused(reason, "escalation", spec.selector, cause=cause)

        monkeypatch.setattr(StoriesEngine, "_loop", loop)
        if exception:

            def fail_execution(*args):
                raise exception

            monkeypatch.setattr(workers, "_execution", fail_execution)
        code = workers.child_main(path)
        receipt = json.loads((context.directory / "receipt.json").read_text())
        assert code == 1
        assert adapter.sessions == []
        monkeypatch.setattr(runs, "read_named_pid_identity", lambda _: (123, 456))
        monkeypatch.setattr(runs, "probe_liveness", lambda *a: "dead")
        return SimpleNamespace(
            repo=repo,
            context=context,
            path=path,
            request=request,
            receipt=receipt,
            source=source,
            spec=spec,
            dirty=dirty,
        )

    return create


def parent_for(case, tmp_path, *, hold=None):
    unit = CampaignUnit(
        case.spec,
        UnitState.RUNNING,
        attempt=case.context.attempt,
        hold=hold,
        claim=WorkerClaim(
            case.spec.id,
            case.context.attempt,
            case.context.generation,
            case.request["launch_id"],
            workspace=str(case.repo),
            branch=case.request["branch"],
        ),
        bindings={
            "head": case.receipt["head"],
            "workspace": str(case.repo),
            "branch": case.request["branch"],
        },
        evidence={"developer_families": ["anthropic", "openai"]},
    )
    snapshot = CampaignSnapshot(
        case.context.campaign_id,
        "a/b",
        CampaignSpec("Pause test", sources=(case.source,), units=(case.spec,)),
        {unit.id: unit},
        config_revision=case.context.config_revision,
        generation=case.context.generation,
    )
    runtime = CampaignRuntime(case.repo, CampaignStore(tmp_path / "parent-state"))
    action = OutboxAction(
        "observe-pause",
        "observe-worker",
        unit.id,
        {"launch_id": case.request["launch_id"]},
        case.context.generation,
    )
    return runtime, snapshot, unit, action


@pytest.mark.parametrize(
    "kind,expected",
    [
        (PauseKind.SOURCE_CHECKPOINT, "checkpoint"),
        (PauseKind.AUTHENTICATION_FAILURE, "authentication_required"),
        (PauseKind.TECHNICAL_FAILURE, "technical_failure"),
        (PauseKind.QUOTA_WAIT, "quota"),
        (PauseKind.TRANSPORT_FAILURE, "transport"),
        (PauseKind.OWNER_DECISION, "owner"),
        (PauseKind.UNKNOWN, "pause_unknown"),
        (None, "pause_unknown"),
    ],
)
def test_persisted_pause_crosses_worker_observation_and_parent_classification(
    paused_child, tmp_path, kind, expected
):
    cause = PauseCause(kind, "scripted.producer", "explicit_pause") if kind is not None else None
    case = paused_child(
        cause, reason="quota authentication approval checkpoint approved: contextual prose only"
    )
    state = load_state(Path(case.receipt["run_dir"]))
    assert state.paused and state.paused_cause == cause
    assert state.tasks[case.spec.selector].attempt == 2
    observed = workers.observe(case.context.directory, expected=case.request)
    assert observed["state"] == "finished" and observed["receipt"]["outcome"] == "paused"
    sealed = observed["receipt"]["child_pause"]
    assert sealed["schema_version"] == 1 and sealed["source"] == case.request["source"]
    assert sealed["source_revision"] == "accepted-source-1"
    assert sealed["task_selector"] == case.spec.selector and sealed["task_digest"]
    assert sealed["binding"]["launch_id"] == case.request["launch_id"]
    runtime, snapshot, unit, action = parent_for(case, tmp_path)
    runtime._apply_worker(snapshot, unit, action, observed)
    assert unit.hold.kind == expected
    assert unit.hold.evidence["child_pause"] == sealed
    assert unit.evidence["child_pause"] == sealed
    assert unit.claim.quiescent and unit.claim.status == "finished"
    assert unit.evidence["dev_attempts_spent"] == 2
    assert unit.attempt == case.context.attempt
    assert unit.evidence["developer_families"] == ["anthropic", "openai"]
    assert unit.spec.acceptance == ("Retain acceptance",)
    assert git(case.repo, "status", "--porcelain") == case.dirty
    assert (case.repo / "unstaged.txt").read_text() == "unfinished contribution\n"
    assert (case.repo / "untracked.txt").read_text() == "untracked contribution\n"
    assert git(case.repo, "show", ":staged.txt") == "staged contribution"


@pytest.mark.parametrize("producer", ["verification", "environment", "ambiguous_timeout"])
def test_child_failure_producer_limits_environment_host_repair_authority(
    paused_child, tmp_path, producer
):
    from test_campaign_resolution import artifact, context

    from bmad_loop.campaign_model import EvidenceCoordinates, ResolutionSpec
    from bmad_loop.campaign_resolution import CauseCode, validate_proposal
    from bmad_loop.campaign_resolution_flow import typed_failure
    from bmad_loop.model import VerifyOutcome

    if producer == "ambiguous_timeout":
        expected_pause = PauseCause(PauseKind.TECHNICAL_FAILURE, "engine.session", "timeout")
        expected_code = CauseCode.UNKNOWN
        case = paused_child(expected_pause, reason="Environment timeout; host repair is approved")
    else:
        environment = producer == "environment"
        outcome = VerifyOutcome(
            ok=False,
            reason="Environment timeout; host repair is approved",
            env_fault=environment,
        )
        case = paused_child(None, verify_outcome=outcome)
        expected_pause = PauseCause(
            PauseKind.TECHNICAL_FAILURE,
            "engine.environment" if environment else "engine.verification",
            "verify:dev" if environment else "verification_failed",
        )
        expected_code = CauseCode.ENVIRONMENT if environment else CauseCode.VERIFICATION
    state = load_state(Path(case.receipt["run_dir"]))
    assert state.paused_cause == expected_pause
    if producer != "ambiguous_timeout":
        assert state.tasks[case.spec.selector].phase == Phase.ESCALATED
        assert state.tasks[case.spec.selector].env_fault_site == (
            "verify:dev" if producer == "environment" else None
        )
    observed = workers.observe(case.context.directory, expected=case.request)
    assert observed["state"] == "finished"
    assert observed["receipt"]["child_pause"]["cause"] == expected_pause.to_dict()
    runtime, snapshot, unit, action = parent_for(case, tmp_path)
    runtime._apply_worker(snapshot, unit, action, observed)
    assert unit.hold.kind == "technical_failure"
    assert unit.hold.evidence["child_pause"]["binding"]["launch_id"] == unit.hold.id
    failure = typed_failure(snapshot, unit.id)
    approved_command = ("project-tool", "repair-local")
    ctx = context(
        cause=failure,
        coordinates=EvidenceCoordinates(
            snapshot.repository,
            unit.id,
            unit.bindings["head"],
            "",
            snapshot.graph_revision,
            snapshot.config_revision,
            case.request["source_revision"],
        ),
        repair_commands=(approved_command,),
        quiescent=unit.claim.quiescent,
        candidate_clean=False,
    )
    policy = ResolutionSpec(mode="bounded-auto")
    # Establish a fully valid ordinary proposal before changing only the
    # operation to an explicitly configured host/environment repair command.
    local = validate_proposal(artifact(ctx), ctx, policy)
    raw = artifact(ctx, command=list(approved_command), affected_paths=[])
    assert tuple(raw["command"]) in ctx.repair_commands
    if producer == "environment":
        proposal = validate_proposal(raw, ctx, policy)
        assert proposal.automatic and proposal.command == approved_command
    else:
        with pytest.raises(
            CampaignError, match="host repair commands only apply to explicit environment repair"
        ):
            validate_proposal(raw, ctx, policy)
    assert failure.code == expected_code
    assert local.automatic is (expected_code != CauseCode.UNKNOWN)
    assert unit.hold.kind == "technical_failure"


@pytest.mark.parametrize(
    "mutation",
    [
        "head",
        "task",
        "task_selector",
        "config",
        "claim",
        "source",
        "source_revision",
        "producer",
        "receipt_cause",
        "run_directory",
    ],
)
def test_observer_rejects_changed_pause_authority_after_a_valid_receipt(
    paused_child, tmp_path, mutation
):
    case = paused_child(PauseCause(PauseKind.TECHNICAL_FAILURE, "scripted.producer", "technical"))
    assert workers.observe(case.context.directory, expected=case.request)["state"] == "finished"
    receipt = copy.deepcopy(case.receipt)
    request = copy.deepcopy(case.request)
    state = load_state(Path(receipt["run_dir"]))
    if mutation == "head":
        receipt["head"] = "0" * 40
    elif mutation == "claim":
        receipt["launch_id"] = "different-claim"
    elif mutation == "task":
        state.tasks[case.spec.selector].attempt += 1
        save_state(Path(receipt["run_dir"]), state)
    elif mutation == "producer":
        state.paused_cause = dataclasses.replace(state.paused_cause, producer="different.producer")
        save_state(Path(receipt["run_dir"]), state)
    elif mutation == "task_selector":
        request["unit"]["selector"] = "different-task"
    elif mutation == "source":
        request["source"]["path"] = "other-source"
    elif mutation == "source_revision":
        request["source_revision"] = "changed-source"
    elif mutation == "config":
        execution = Path(request["execution"])
        raw = json.loads(execution.read_text())
        raw["config_revision"] += 1
        execution.write_text(json.dumps(raw))
    elif mutation == "receipt_cause":
        receipt["child_pause"]["cause"]["kind"] = "owner_decision"
    elif mutation == "run_directory":
        other = tmp_path / "foreign-run"
        save_state(other, dataclasses.replace(state, run_dir_identity=None))
        receipt["run_dir"] = str(other)
    case.path.write_text(json.dumps(request))
    (case.context.directory / "receipt.json").write_text(json.dumps(receipt))
    with pytest.raises(
        (CampaignError, CampaignExecutionError), match="changed|differs|binding|mismatch|snapshot"
    ):
        workers.observe(case.context.directory, expected=case.request)


@pytest.mark.parametrize("kind", ["operator", "owner", "checkpoint", "product_choice"])
def test_parent_retains_original_protected_hold_and_defers_new_failure(
    paused_child, tmp_path, kind
):
    case = paused_child(PauseCause(PauseKind.TECHNICAL_FAILURE, "scripted.producer", "technical"))
    observed = workers.observe(case.context.directory, expected=case.request)
    original = Hold(
        kind,
        "Original owner condition",
        id="owner-hold",
        actor="owner",
        evidence={"approval_required": True},
    )
    before = dataclasses.asdict(original)
    runtime, snapshot, unit, action = parent_for(case, tmp_path, hold=original)
    runtime._apply_worker(snapshot, unit, action, observed)
    assert dataclasses.asdict(unit.hold) == before
    assert unit.evidence["pending_failure_hold"]["kind"] == "technical_failure"
    assert (
        unit.evidence["pending_failure_hold"]["evidence"]["child_pause"]
        == case.receipt["child_pause"]
    )
    assert unit.attempt == case.context.attempt and unit.evidence["dev_attempts_spent"] == 2


def test_historical_receipt_without_typed_pause_stays_unknown(paused_child, tmp_path):
    case = paused_child(None, reason="campaign operator hold; checkpoint approved; retry safely")
    case.receipt.pop("child_pause")
    (case.context.directory / "receipt.json").write_text(json.dumps(case.receipt))
    observed = workers.observe(case.context.directory, expected=case.request)
    assert observed["state"] == "finished"
    runtime, snapshot, unit, action = parent_for(case, tmp_path)
    runtime._apply_worker(snapshot, unit, action, observed)
    assert unit.hold.kind == "pause_unknown"
    assert campaign_child_pause.cause(observed["receipt"]) is None


def test_durable_engine_recovery_request_retains_its_recovery_hold(paused_child, tmp_path):
    case = paused_child(None, request_recovery=True)
    persisted = json.loads((case.context.directory / "recovery-request.json").read_text())
    observed = workers.observe(case.context.directory, expected=case.request)
    assert persisted["task"]["story_key"] == case.spec.selector
    assert observed["receipt"]["outcome"] == "recovery_required"
    runtime, snapshot, unit, action = parent_for(case, tmp_path)
    runtime._apply_worker(snapshot, unit, action, observed)
    assert unit.hold.kind == "recovery_required"
    assert unit.claim.quiescent and unit.claim.launch_id == case.request["launch_id"]


@pytest.mark.parametrize("legacy", [False, True])
def test_protected_successor_pause_retires_handoff_and_keeps_owner_hold(
    paused_child, tmp_path, legacy
):
    case = paused_child(PauseCause(PauseKind.OWNER_DECISION, "engine.session", "human_prompt"))
    observed = workers.observe(case.context.directory, expected=case.request)
    if legacy:
        observed["receipt"].pop("child_pause")
    runtime, snapshot, unit, action = parent_for(case, tmp_path)
    handoff = unit.evidence["handoff"] = {
        "id": "h",
        "phase": "continuing",
        "predecessor": {"launch_id": "old"},
        "successor": {"launch_id": unit.claim.launch_id},
    }
    runtime._apply_worker(snapshot, unit, action, observed)
    assert handoff["phase"] == "stopped"
    assert handoff["successor_receipt"] == observed["receipt"]
    assert unit.hold.kind == ("pause_unknown" if legacy else "owner") and unit.claim.quiescent


def test_active_handoff_cannot_intercept_untyped_paused_predecessor(paused_child, tmp_path):
    case = paused_child(None)
    observed = workers.observe(case.context.directory, expected=case.request)
    observed["receipt"].pop("child_pause")
    runtime, snapshot, unit, action = parent_for(case, tmp_path)
    handoff = unit.evidence["handoff"] = {
        "id": "h",
        "phase": "drain",
        "predecessor": {"launch_id": unit.claim.launch_id},
    }
    runtime._apply_worker(snapshot, unit, action, observed)
    assert handoff["phase"] == "stopped" and handoff["receipt"] == observed["receipt"]
    assert unit.hold.kind == "pause_unknown" and unit.claim.quiescent


def test_typed_receipt_cannot_finish_a_live_child(paused_child, monkeypatch):
    case = paused_child(PauseCause(PauseKind.SOURCE_CHECKPOINT, "scripted.producer", "approval"))
    assert workers.observe(case.context.directory, expected=case.request)["state"] == "finished"
    monkeypatch.setattr(runs, "probe_liveness", lambda *args: "alive")
    assert workers.observe(case.context.directory, expected=case.request)["state"] == "running"


def test_child_exception_prose_cannot_manufacture_operator_hold(paused_child):
    case = paused_child(
        None, exception=CampaignError("campaign operator hold from untrusted exception text")
    )
    assert case.receipt["outcome"] == "failed"
