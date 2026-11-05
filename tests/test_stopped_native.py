"""An explicitly stopped native resumes without discarding its unfinished work."""

import copy
import dataclasses
import json
import time
from types import SimpleNamespace

import pytest
from conftest import (
    _OK,
    EntrySpec,
    EpicSpec,
    git,
    install_bmad_config,
    write_plan,
    write_spec,
    write_sprint,
    write_ticket_tree,
)
from test_engine import _dev_verify_crash_state, make_engine

from bmad_loop import bmadconfig, cli, policy, runs, ticketstore, verify
from bmad_loop.adapters.base import SessionResult
from bmad_loop.adapters.profile import get_profile
from bmad_loop.journal import load_state
from bmad_loop.model import Phase, RunState, StoryTask, TicketBinding
from bmad_loop.native_binding import capture_binding
from bmad_loop.policy import RecoveryPolicy, VerifyPolicy

NATIVE = "01a1aafb-e50b-7020-a9f4-ba036230a4fe"


def test_stopped_snapshot_refuses_changed_live_ticket_store(project_v7):
    """A tracked store becoming shared cannot reuse stopped-native authority."""
    from bmad_loop import stopped_native as recovery

    root = project_v7.project
    epic = "epic-native-store"
    write_ticket_tree(
        project_v7,
        [EpicSpec(205, epic, "Native store", (EntrySpec(9, "Keep work", refined=True),))],
    )
    plan = write_plan(project_v7, epic, "story-keep-work-plan", 9, "ready-for-dev")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "tracked ticket store")
    (root / policy.POLICY_FILE).parent.mkdir(parents=True, exist_ok=True)
    (root / policy.POLICY_FILE).write_text("")
    paths = bmadconfig.load_paths(root, want_tickets=True)
    topology = ticketstore.assess_store(paths, "in-place")
    assert isinstance(topology, ticketstore.StoreTopology)
    task = StoryTask(
        "205.9",
        0,
        spec_file=str(plan),
        ticket=TicketBinding.from_dict(
            {
                "ref": "205.9",
                "id": "9",
                "epic_id": "205",
                "epic_slug": epic,
                "risk": "medium",
                "plan": str(plan),
            }
        ),
    )
    state = RunState(
        "native-store",
        str(root),
        "now",
        source="tickets",
        initiative=project_v7.initiative,
        initiative_folder=str(project_v7.initiative_folder),
        store_topology=topology.to_dict(),
    )
    observed = recovery.snapshot(state, task, root)
    store_rel = project_v7.output_folder.relative_to(root).as_posix()
    git(root, "rm", "-r", "--cached", store_rel)
    with (root / ".gitignore").open("a") as stream:
        stream.write(f"\n/{store_rel}/\n")
    current = ticketstore.assess_store(bmadconfig.load_paths(root, want_tickets=True), "in-place")
    assert isinstance(current, ticketstore.StoreTopology) and current != topology
    before = plan.read_bytes()
    with pytest.raises(ValueError, match="ticket store changed"):
        recovery.snapshot(state, task, root)
    assert plan.read_bytes() == before
    assert observed["head"] == verify.rev_parse_head(root)


@pytest.fixture
def stopped_native(project, monkeypatch, tmp_path):
    install_bmad_config(project)
    write_sprint(project, {"1-1-a": "ready-for-dev"})
    engine, adapter = make_engine(project, [])
    baseline, plan = _dev_verify_crash_state(project, engine, "in-progress")
    task = engine.state.tasks["1-1-a"]
    task.spec_file = str(plan)
    task.phase = Phase.DEV_RUNNING
    task.sessions.clear()
    engine.state.stopped = True
    engine.policy = dataclasses.replace(
        engine.policy,
        recovery=RecoveryPolicy(max_session_resumes=2),
        verify=VerifyPolicy(commands=(_OK,)),
    )
    adapter.profile = dataclasses.replace(
        get_profile(engine.policy.adapter.resolved("dev").name),
        resume_flag="--resume",
    )
    task_id = "1-1-a-dev-1"
    transcript = tmp_path / "native.jsonl"
    transcript.write_text(json.dumps({"type": "user", "sessionId": NATIVE}) + "\n")
    events = runs.events_dir_for(project.project, engine.state.run_id)
    events.mkdir(parents=True, exist_ok=True)
    (events / (task_id + "-SessionStart.json")).write_text(
        json.dumps(
            {
                "event": "SessionStart",
                "ts": 1,
                "task_id": task_id,
                "session_id": NATIVE,
                "transcript_path": str(transcript),
                "source": "startup",
            }
        )
    )
    original = engine.run_dir / "tasks" / task_id
    original.mkdir(parents=True, exist_ok=True)
    (original / "prompt.txt").write_text("Original prompt must remain intact.\n")
    engine.journal.set_active_log(task_id)
    engine.journal.append(
        "session-start",
        task_id=task_id,
        role="dev",
        adapter=engine.policy.adapter.resolved("dev").name,
    )
    binding = capture_binding(
        adapter.profile,
        adapter.profile.binary,
        NATIVE,
        str(transcript),
        extra_args=engine.policy.adapter.resolved("dev").extra_args,
    )
    engine.journal.append(
        "session-bound", task_id=task_id, session_id=NATIVE, native_binding=binding
    )
    engine.journal.append("session-end", task_id=task_id, status="aborted", error="RunStopped")
    engine.journal.append("run-stop")
    (original / "session-lifecycle.jsonl").write_text(
        json.dumps(
            {
                "event": "kill-outcome",
                "reaped": True,
                "unreaped": [],
                "ts": time.time_ns(),
            }
        )
        + "\n"
    )
    (project.project / "unfinished.txt").write_text("Keep the unfinished source.\n")
    engine._save()
    monkeypatch.setattr(runs, "engine_liveness", lambda _: "dead")
    from bmad_loop import stopped_native as recovery

    monkeypatch.setattr(recovery, "mux_quiescent", lambda _: None)
    monkeypatch.setattr(
        recovery,
        "load_profiles",
        lambda _: {engine.policy.adapter.resolved("dev").name: adapter.profile},
    )
    return SimpleNamespace(
        engine=engine,
        adapter=adapter,
        task=task,
        baseline=baseline,
        plan=plan,
        project=project,
        original=original,
        transcript=transcript,
    )


def test_public_explicit_resume_keeps_native_attempt_and_original_artifacts(
    stopped_native, monkeypatch
):
    case = stopped_native
    engine, task = case.engine, case.task
    before = {p.name: p.read_bytes() for p in case.original.iterdir()}
    head = verify.rev_parse_head(case.project.project)

    def complete(spec):
        assert spec.resume_session_id == NATIVE
        assert spec.resume_usage_accounted is False
        assert spec.resume_transcript_path == str(case.transcript)
        assert spec.task_id != "1-1-a-dev-1"
        assert (
            case.project.project / "unfinished.txt"
        ).read_text() == "Keep the unfinished source.\n"
        write_spec(case.plan, "done", case.baseline)
        return SessionResult(
            status="completed",
            session_id=NATIVE,
            resumed_from=NATIVE,
            result_json={
                "workflow": "auto-dev",
                "story_key": task.story_key,
                "spec_file": str(case.plan),
                "baseline_commit": case.baseline,
                "escalations": [],
                "followup_review_recommended": False,
            },
        )

    case.adapter.script.append(complete)
    # Keep real dispatch/result recording and deterministic dev verification;
    # stop at the normal review seam, without another session or a commit.
    reviewed = []
    monkeypatch.setattr(engine, "_review_and_commit", lambda t, **kw: reviewed.append(t.story_key))

    def resume(project, run_dir, **kwargs):
        from bmad_loop import stopped_native as recovery

        task.stopped_native = recovery.prepare(
            engine.state, run_dir, project, kwargs["continue_native"], engine.policy
        )
        engine.state.stopped = False
        engine._save()
        engine._finish_inflight()
        return 0

    monkeypatch.setattr(cli, "_resume_paused_run", resume)
    assert (
        cli.main(
            [
                "resume",
                engine.state.run_id,
                "--project",
                str(case.project.project),
                "--continue-native",
                NATIVE,
            ]
        )
        == 0
    )
    assert reviewed == [task.story_key]
    assert task.attempt == 1 and task.generation == 0 and task.baseline_commit == case.baseline
    assert task.session_resumes == 1
    assert len(task.sessions) == 1 and task.sessions[0].session_id == NATIVE
    assert task.sessions[0].task_id == "1-1-a-dev-1"
    assert task.sessions[0].artifact_task_id == case.adapter.sessions[0].task_id
    assert {p.name: p.read_bytes() for p in case.original.iterdir()} == before
    assert verify.rev_parse_head(case.project.project) == head
    assert not task.stopped_native


def _prepare(case):
    from bmad_loop import stopped_native as recovery

    return recovery.prepare(
        case.engine.state, case.engine.run_dir, case.project.project, NATIVE, case.engine.policy
    )


@pytest.mark.parametrize(
    "fault,reason",
    [
        ("not-stopped", "must be stopped"),
        ("finished", "must be stopped"),
        ("paused", "existing pause"),
        ("live", "prior engine"),
        ("unknown", "prior engine"),
        ("mux", "transport"),
        ("budget", "budget"),
        ("blocked", "not resumable"),
        ("generation", "latest launch"),
        ("record", "already has"),
        ("accounted-native", "accounting record"),
        ("adapter", "adapter changed"),
        ("no-resume", "does not support"),
        ("bound", "native binding"),
        ("wrong-end", "explicit stop"),
        ("duplicate-end", "explicit stop"),
        ("no-stop", "ordered stop"),
        ("later-start", "latest launch"),
        ("decision", "already has a decision"),
        ("journal", "unreadable journal"),
        ("unreaped", "proven reaped"),
        ("stale-reap", "predates"),
        ("hook", "hook evidence"),
        ("transcript", "does not identify"),
        ("old-artifact", "artifacts already exist"),
        ("missing-plan", "bound plan is missing"),
        ("no-binding", "bound native"),
    ],
)
def test_admission_refuses_without_mutating_state(stopped_native, monkeypatch, fault, reason):
    from bmad_loop import stopped_native as recovery
    from bmad_loop.model import SessionRecord

    case = stopped_native
    assert _prepare(case)["native_id"] == NATIVE
    task, state = case.task, case.engine.state
    journal = case.engine.journal
    if fault == "not-stopped":
        state.stopped = False
    elif fault == "finished":
        state.finished = True
    elif fault == "paused":
        state.paused_stage = "escalation"
    elif fault in ("live", "unknown"):
        monkeypatch.setattr(
            runs, "engine_liveness", lambda _: "alive" if fault == "live" else "unknown"
        )
    elif fault == "mux":
        monkeypatch.setattr(
            recovery, "mux_quiescent", lambda _: (_ for _ in ()).throw(ValueError("live transport"))
        )
    elif fault == "budget":
        task.session_resumes = 2
    elif fault == "blocked":
        task.resume_blocked = True
    elif fault == "generation":
        task.generation += 1
    elif fault == "record":
        task.sessions.append(SessionRecord(task_id="1-1-a-dev-1", role="dev", status="crashed"))
    elif fault == "accounted-native":
        task.sessions.append(
            SessionRecord(task_id="1-1-a-dev-0", role="dev", status="timeout", session_id=NATIVE)
        )
    elif fault == "no-resume":
        case.adapter.profile = dataclasses.replace(
            case.adapter.profile, resume_flag="", resume_subcommand=()
        )
    elif fault == "missing-plan":
        case.plan.unlink()
    elif fault == "no-binding":
        path = case.engine.run_dir / "journal.jsonl"
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        for row in rows:
            row.pop("native_binding", None)
        path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    elif fault == "old-artifact":
        (case.engine.run_dir / "tasks" / "1-1-a-dev-1-continue1").mkdir()
    elif fault in ("unreaped", "stale-reap"):
        path = case.original / "session-lifecycle.jsonl"
        row = json.loads(path.read_text())
        row["reaped" if fault == "unreaped" else "ts"] = False if fault == "unreaped" else 1
        path.write_text(json.dumps(row) + "\n")
    elif fault == "hook":
        path = next(
            runs.events_dir_for(case.project.project, state.run_id).glob("*SessionStart.json")
        )
        row = json.loads(path.read_text())
        row["session_id"] = "different-native"
        path.write_text(json.dumps(row))
    elif fault == "transcript":
        case.transcript.write_text('{"sessionId":"other-native"}\n')
    elif fault in ("adapter", "bound", "wrong-end", "no-stop"):
        path = case.engine.run_dir / "journal.jsonl"
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        for row in rows:
            if fault == "adapter" and row.get("kind") == "session-start":
                row["adapter"] = "other"
            if fault == "bound" and row.get("kind") == "session-bound":
                row["session_id"] = "other"
            if fault == "wrong-end" and row.get("kind") == "session-end":
                row["error"] = "crash"
        if fault == "no-stop":
            rows = [r for r in rows if r.get("kind") != "run-stop"]
        path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    elif fault == "duplicate-end":
        journal.append("session-end", task_id="1-1-a-dev-1", status="aborted", error="RunStopped")
    elif fault == "later-start":
        journal.append("session-start", task_id="other-dev-1", role="dev")
    elif fault == "decision":
        journal.append("dev-decision", story_key=task.story_key)
    elif fault == "journal":
        with (case.engine.run_dir / "journal.jsonl").open("a") as f:
            f.write('{"torn":\n')
    before = copy.deepcopy(state.to_dict())
    with pytest.raises(ValueError, match=reason):
        _prepare(case)
    assert state.to_dict() == before


def _arm(case):
    case.task.stopped_native = _prepare(case)
    case.engine.state.stopped = False
    case.engine._save()


@pytest.mark.parametrize(
    "fault",
    [
        "source",
        "plan",
        "ledger",
        "index",
        "generation",
        "budget",
        "adapter",
        "artifact",
        "transcript",
    ],
)
def test_dispatch_rechecks_admitted_work(stopped_native, monkeypatch, fault):
    from conftest import git

    from bmad_loop.engine import RunPaused

    case = stopped_native
    _arm(case)
    head = verify.rev_parse_head(case.project.project)
    if fault in ("source", "plan", "ledger"):
        path = {
            "source": case.project.project / "src.txt",
            "plan": case.plan,
            "ledger": case.project.deferred_work,
        }[fault]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text((path.read_text() if path.exists() else "") + "\nchanged after admission\n")
    elif fault == "index":
        git(case.project.project, "add", "unfinished.txt")
    elif fault == "generation":
        case.task.generation += 1
    elif fault == "budget":
        case.task.session_resumes += 1
    elif fault == "adapter":
        case.task.stopped_native["adapter"] = "other"
    elif fault == "artifact":
        case.task.stopped_native["artifact_id"] = "../../other"
    elif fault == "transcript":
        case.transcript.write_text('{"type":"user","sessionId":"other"}\n')
    with pytest.raises(RunPaused, match="cannot continue stopped native"):
        case.engine._finish_inflight()
    assert not case.adapter.sessions
    assert verify.rev_parse_head(case.project.project) == head
    assert case.task.stopped_native


@pytest.mark.parametrize("fault", ["source", "plan", "ledger"])
def test_session_hook_changes_do_not_launch_or_commit(stopped_native, monkeypatch, fault):
    from bmad_loop.engine import RunPaused

    case = stopped_native
    _arm(case)
    head = verify.rev_parse_head(case.project.project)

    def hook(_task, _role, prompt, env, _stage):
        path = {
            "source": case.project.project / "src.txt",
            "plan": case.plan,
            "ledger": case.project.deferred_work,
        }[fault]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text((path.read_text() if path.exists() else "") + "\nhook mutation\n")
        return prompt, env, None

    monkeypatch.setattr(case.engine, "_emit_session_gate", hook)
    with pytest.raises(RunPaused, match="cannot continue stopped native"):
        case.engine._finish_inflight()
    assert not case.adapter.sessions
    assert verify.rev_parse_head(case.project.project) == head


@pytest.mark.parametrize(
    "native,resumed", [(None, None), ("different", NATIVE), (NATIVE, "different")]
)
def test_actual_result_must_identify_original_native(stopped_native, native, resumed):
    from bmad_loop.engine import RunPaused

    case = stopped_native
    _arm(case)
    case.adapter.script.append(
        SessionResult(status="completed", session_id=native, resumed_from=resumed, result_json={})
    )
    with pytest.raises(RunPaused, match="original native"):
        case.engine._finish_inflight()
    assert len(case.task.sessions) == 1  # truthful result retained, never synthesized
    assert case.task.stopped_native and case.task.session_resumes == 1
    assert case.task.attempt == 1


def test_reserved_crash_never_relaunches_or_reuses_artifacts(stopped_native, monkeypatch):
    from bmad_loop.engine import RunPaused

    case = stopped_native
    _arm(case)
    monkeypatch.setattr(
        case.engine,
        "_run_session",
        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("host loss")),
    )
    with pytest.raises(RuntimeError, match="host loss"):
        case.engine._finish_inflight()
    case.engine.state = load_state(case.engine.run_dir)
    with pytest.raises(RunPaused, match="reserved"):
        case.engine._finish_inflight()
    assert not case.adapter.sessions
    assert case.engine.state.tasks[case.task.story_key].stopped_native


def _complete_native(case):
    def complete(spec):
        assert spec.resume_session_id == NATIVE
        write_spec(case.plan, "done", case.baseline)
        return SessionResult(
            status="completed",
            session_id=NATIVE,
            resumed_from=NATIVE,
            result_json={
                "workflow": "auto-dev",
                "story_key": case.task.story_key,
                "spec_file": str(case.plan),
                "baseline_commit": case.baseline,
                "escalations": [],
                "followup_review_recommended": False,
            },
        )

    return complete


@pytest.mark.parametrize("status", ["timeout", "stalled", "crashed"])
def test_continuation_timeout_does_not_brick_native_recovery(stopped_native, monkeypatch, status):
    """A recorded non-completed continuation must clear the latch and keep the
    unfinished tree so ordinary decide/retry can resume the same native.

    Ablation: refuse after `_run_session` whenever `recorded()` is false and the
    next resume hits the reserved arm forever while prepare also rejects a
    second `--continue-native`."""
    case = stopped_native
    _arm(case)
    before = (case.project.project / "unfinished.txt").read_text()
    case.adapter.script.append(SessionResult(status=status, session_id=NATIVE, resumed_from=NATIVE))
    case.adapter.script.append(_complete_native(case))
    reviewed = []
    monkeypatch.setattr(
        case.engine, "_review_and_commit", lambda t, **kw: reviewed.append(t.story_key)
    )
    case.engine._finish_inflight()
    assert len(case.adapter.sessions) == 2
    assert case.adapter.sessions[0].resume_session_id == NATIVE
    assert case.adapter.sessions[1].resume_session_id == NATIVE
    assert (case.project.project / "unfinished.txt").read_text() == before
    assert not case.engine.state.tasks[case.task.story_key].stopped_native
    assert reviewed == [case.task.story_key]


def test_reserved_timeout_record_recovers_on_next_resume(stopped_native, monkeypatch):
    """A prior refuse that left reserved=True with a timeout SessionRecord must
    still be recoverable without hand-editing state."""
    from bmad_loop.model import SessionRecord

    case = stopped_native
    _arm(case)
    held = case.task.stopped_native
    held["reserved"] = True
    case.task.sessions.append(
        SessionRecord(
            task_id=held["task_id"],
            log_task_id=held["artifact_id"],
            role="dev",
            status="timeout",
            session_id=NATIVE,
            resumed_from=NATIVE,
            adapter=case.engine.policy.adapter.resolved("dev").name,
        )
    )
    case.task.session_resumes = 1
    case.engine._save()
    case.adapter.script.append(_complete_native(case))
    reviewed = []
    monkeypatch.setattr(
        case.engine, "_review_and_commit", lambda t, **kw: reviewed.append(t.story_key)
    )
    case.engine._finish_inflight()
    assert not case.engine.state.tasks[case.task.story_key].stopped_native
    assert reviewed == [case.task.story_key]
    assert case.adapter.sessions[-1].resume_session_id == NATIVE
    assert (case.project.project / "unfinished.txt").read_text() == (
        "Keep the unfinished source.\n"
    )


def test_completed_result_crash_replays_without_second_native(stopped_native, monkeypatch):
    case = stopped_native
    _arm(case)
    case.adapter.script.append(
        SessionResult(
            status="completed",
            session_id=NATIVE,
            resumed_from=NATIVE,
            result_json={
                "workflow": "auto-dev",
                "story_key": case.task.story_key,
                "baseline_commit": case.baseline,
            },
        )
    )
    real = case.engine._run_session

    def crash(*a, **kw):
        real(*a, **kw)
        raise RuntimeError("after durable result")

    monkeypatch.setattr(case.engine, "_run_session", crash)
    with pytest.raises(RuntimeError, match="durable result"):
        case.engine._finish_inflight()
    state = load_state(case.engine.run_dir)
    case.engine.state = state
    replayed = []
    monkeypatch.setattr(
        case.engine, "_drive_story", lambda task, **kw: replayed.append(kw["dev_resume"])
    )
    case.engine._finish_inflight()
    assert len(case.adapter.sessions) == len(replayed) == 1
    assert replayed[0].session_id == NATIVE
    assert state.tasks[case.task.story_key].session_resumes == 1
    assert not state.tasks[case.task.story_key].stopped_native


def test_artifact_identity_roundtrip_and_legacy_default():
    from bmad_loop.model import SessionRecord, StoryTask
    from bmad_loop.platform_util import session_task_id

    original = SessionRecord("story-dev-1", "dev", "completed")
    assert original.artifact_task_id == "story-dev-1" and "log_task_id" not in original.to_dict()
    original.log_task_id = session_task_id("story", "dev", 1, 0, continuation=1)
    assert SessionRecord.from_dict(original.to_dict()).artifact_task_id == "story-dev-1-continue1"
    original.log_task_id = "../foreign"
    with pytest.raises(ValueError, match="unsafe"):
        _ = original.artifact_task_id
    task = StoryTask("story", 1)
    task.stopped_native = {"reserved": True, "native_id": NATIVE}
    assert StoryTask.from_dict(task.to_dict()).stopped_native == task.stopped_native


def test_nested_native_start_does_not_replace_bound_parent(stopped_native):
    case = stopped_native
    path = (
        runs.events_dir_for(case.project.project, case.engine.state.run_id)
        / "child-SessionStart.json"
    )
    path.write_text(
        json.dumps(
            {
                "event": "SessionStart",
                "ts": 2,
                "task_id": "1-1-a-dev-1",
                "session_id": "nested-child",
                "source": "startup",
                "transcript_path": str(case.transcript.parent / "child.jsonl"),
            }
        )
    )
    assert _prepare(case)["native_id"] == NATIVE


def test_mux_quiescence_confirms_negative_with_strict_probe(monkeypatch):
    from bmad_loop import stopped_native as recovery
    from bmad_loop.adapters.multiplexer import MultiplexerError

    class BrokenProbe:
        def has_session(self, _):
            return False

        def list_window_ids(self, _):
            raise MultiplexerError("unreachable multiplexer")

    monkeypatch.setattr(recovery, "get_multiplexer", lambda: BrokenProbe())
    monkeypatch.setattr(recovery, "mux_usable", lambda _: True)
    with pytest.raises(MultiplexerError, match="unreachable"):
        recovery.mux_quiescent("stopped")


def test_pre_session_hook_cannot_race_native_transport_ownership(stopped_native, monkeypatch):
    from bmad_loop import stopped_native as recovery
    from bmad_loop.engine import RunPaused

    case = stopped_native
    _arm(case)

    def newly_live(_):
        raise ValueError("the run still has a native transport session")

    def hook(_task, _role, prompt, env, _stage):
        monkeypatch.setattr(recovery, "mux_quiescent", newly_live)
        return prompt, env, None

    monkeypatch.setattr(case.engine, "_emit_session_gate", hook)
    with pytest.raises(RunPaused, match="native transport session"):
        case.engine._finish_inflight()
    assert not case.adapter.sessions
    assert case.task.stopped_native


def test_completed_continuation_runs_normal_story_boundary_hooks(stopped_native, monkeypatch):
    case = stopped_native
    _arm(case)
    case.adapter.script.append(
        SessionResult(
            status="completed",
            session_id=NATIVE,
            resumed_from=NATIVE,
            result_json={
                "workflow": "auto-dev",
                "story_key": case.task.story_key,
                "baseline_commit": case.baseline,
            },
        )
    )
    called = []

    def finish(task, **_):
        task.phase = Phase.DONE

    monkeypatch.setattr(case.engine, "_drive_story", finish)
    monkeypatch.setattr(
        case.engine, "_post_story_after_replay", lambda task: called.append(("post", task.phase))
    )
    monkeypatch.setattr(
        case.engine, "_after_story", lambda task: called.append(("after", task.phase))
    )
    case.engine._finish_inflight()
    assert called == [("post", Phase.DONE), ("after", Phase.DONE)]


def test_real_resume_preparation_persists_exact_native_authority(stopped_native, monkeypatch):
    from bmad_loop.journal import state_lock

    case = stopped_native
    monkeypatch.setattr(cli.policy_mod, "load", lambda _: case.engine.policy)
    monkeypatch.setattr(cli, "_require_base_skills", lambda *a, **kw: True)
    with state_lock(case.engine.run_dir):
        prepared = cli._prepare_resume_locked(
            case.project.project,
            case.engine.run_dir,
            continue_native=NATIVE,
        )
    assert not isinstance(prepared, int)
    saved = load_state(case.engine.run_dir)
    held = saved.tasks[case.task.story_key].stopped_native
    assert held["native_id"] == NATIVE and held["reserved"] is False
    assert saved.tasks[case.task.story_key].attempt == case.task.attempt
    assert any(e["kind"] == "stopped-native-armed" for e in case.engine.journal.entries())


def test_returned_hard_stop_records_provenance_for_explicit_continuation(stopped_native):
    from bmad_loop.engine import RunStopped

    case = stopped_native
    journal_path = case.engine.run_dir / "journal.jsonl"
    rows = case.engine.journal.entries()
    binding = next(e["native_binding"] for e in rows if e["kind"] == "session-bound")
    journal_path.write_text(
        "".join(
            json.dumps(row) + "\n"
            for row in rows
            if row["kind"] not in {"session-start", "session-bound", "session-end", "run-stop"}
        )
    )

    def stop(spec):
        case.engine.journal.append(
            "session-bound", task_id=spec.task_id, session_id=NATIVE, native_binding=binding
        )
        (case.original / "session-lifecycle.jsonl").write_text(
            json.dumps(
                {
                    "event": "kill-outcome",
                    "reaped": True,
                    "unreaped": [],
                    "ts": time.time_ns(),
                }
            )
            + "\n"
        )
        return SessionResult(status="aborted", session_id=NATIVE)

    case.adapter.script.append(stop)
    with pytest.raises(RunStopped):
        case.engine._run_session(case.task, "dev", "Continue the story.", 1)
    case.engine.journal.append("run-stop")
    ended = next(e for e in case.engine.journal.entries() if e["kind"] == "session-end")
    assert ended["error"] == "RunStopped"
    assert not case.task.sessions
    assert _prepare(case)["native_id"] == NATIVE
