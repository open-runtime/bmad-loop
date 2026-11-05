"""A pool-paused attested primary resumes its native review, never dev."""

import copy
import dataclasses
from types import SimpleNamespace

import pytest
from conftest import _OK, git
from test_accepted_review_engine import _armed_engine
from test_accepted_review_recovery import accepted_implementation as _accepted_implementation

from bmad_loop import accepted_review, cli, verify
from bmad_loop.adapters.base import SessionResult
from bmad_loop.engine import RunPaused
from bmad_loop.journal import load_state, save_state
from bmad_loop.model import Phase, SessionRecord
from bmad_loop.policy import RecoveryPolicy, VerifyPolicy

accepted_implementation = _accepted_implementation


@pytest.fixture
def pool_review(accepted_implementation):
    case = accepted_implementation
    engine, adapter = _armed_engine(case)
    engine.policy = dataclasses.replace(
        engine.policy,
        recovery=RecoveryPolicy(max_session_resumes=2),
        verify=VerifyPolicy(commands=(_OK,)),
    )
    adapter.profile = SimpleNamespace(resumable=True)
    task = engine.state.tasks[case.task.story_key]
    task.phase = Phase.ESCALATED
    task.review_cycle = 1
    record = SessionRecord(
        task_id=f"{task.story_key}-review-1-g2",
        role="review",
        adapter=engine.policy.adapter.resolved("review").name,
        status="stalled",
        ended="pool",
        session_id="interrupted-primary",
    )
    task.sessions.append(record)
    engine.journal.set_active_log(record.task_id)
    engine.journal.append("session-start", task_id=record.task_id, role="review")
    engine.journal.append("session-bound", task_id=record.task_id, session_id=record.session_id)
    engine.journal.append(
        "session-end", task_id=record.task_id, status="stalled", env_fault=True, turn_error="pool"
    )
    engine.journal.append("story-escalated", story_key=task.story_key, reason="pool unavailable")
    engine.state.paused_stage = "escalation"
    engine.state.paused_story_key = task.story_key
    # This is legitimate partial work by the bound reviewer, not a new dev.
    case.plan.write_text(
        case.plan.read_text().replace("status: in-progress", "status: in-review")
        + "\nPrimary review in progress.\n"
    )
    save_state(case.run_dir, engine.state)
    return case, engine, adapter, task, record


def _successful_primary(case):
    def primary(spec):
        assert spec.role == "review"
        assert spec.resume_session_id == "interrupted-primary"
        assert case.implementation in spec.prompt
        case.plan.write_text(case.plan.read_text().replace("status: in-review", "status: built"))
        return SessionResult(
            status="completed",
            session_id="interrupted-primary",
            resumed_from="interrupted-primary",
            result_json={
                **case.task.sessions[-1].result_json,
                "status": "built",
                "followup_review_recommended": False,
            },
        )

    return primary


def test_public_resume_continues_bound_pool_primary_without_new_dev(
    pool_review, monkeypatch, capsys
):
    case, engine, adapter, task, record = pool_review
    records = copy.deepcopy(task.sessions)
    adapter.script.append(_successful_primary(case))

    def resumed(project, run_dir, **kwargs):
        assert project == case.root and run_dir == case.run_dir
        engine._finish_inflight()
        return 0

    monkeypatch.setattr(cli, "_resume_paused_run", resumed)
    assert cli.main(["resume", case.state.run_id, "--project", str(case.root)]) == 0
    assert [spec.role for spec in adapter.sessions] == ["review"]
    assert task.phase == Phase.DONE
    assert task.generation == 2 and task.attempt == 2 and task.review_cycle == 2
    assert task.sessions[: len(records)] == records
    assert task.accepted_dev_session_index == 0
    assert task.review_usage_relaunches == task.review_unlaunched_cycles == 0
    assert adapter.sessions[0].task_id == f"{task.story_key}-review-2-g2"
    assert "resume skips" not in capsys.readouterr().err


def test_pool_review_accepts_only_plan_edits(pool_review):
    case, engine, _adapter, task, _record = pool_review
    before = copy.deepcopy(task.to_dict())
    assert accepted_review.pool_review_refusal(engine.state, task, case.run_dir, case.root) is None
    assert task.to_dict() == before


@pytest.mark.parametrize(
    "fault",
    [
        "missing-attestation",
        "wrong-native",
        "wrong-cycle",
        "wrong-generation",
        "completed-result",
        "other-end",
        "resume-blocked",
        "wrong-site",
        "later-record",
        "accepted-identity",
        "source",
        "head",
        "ledger",
        "blocked-plan",
        "journal-tail",
        "duplicate-end",
        "later-launch",
        "missing-end",
        "wrong-end",
        "later-escalation",
        "exhausted-budget",
        "disabled-review",
    ],
)
def test_pool_review_refuses_ambiguous_or_changed_authority(pool_review, monkeypatch, fault):
    from bmad_loop import policy

    case, engine, _adapter, task, record = pool_review
    assert accepted_review.pool_review_refusal(engine.state, task, case.run_dir, case.root) is None
    if fault == "missing-attestation":
        task.implemented_review = None
    elif fault == "wrong-native":
        record.session_id = "not-the-bound-native"
    elif fault == "wrong-cycle":
        task.review_cycle += 1
    elif fault == "wrong-generation":
        task.generation += 1
    elif fault == "completed-result":
        record.result_json = {"escalations": [{"severity": "CRITICAL"}]}
    elif fault == "other-end":
        record.ended = "transport"
    elif fault == "resume-blocked":
        task.resume_blocked = True
    elif fault == "wrong-site":
        task.env_fault_site = "verify:review"
    elif fault == "later-record":
        task.sessions.append(
            SessionRecord(task_id="later", role="dev", status="crashed", adapter="codex")
        )
    elif fault == "accepted-identity":
        task.sessions[0].session_id = "changed-accepted"
    elif fault == "source":
        (case.root / "crate-readme.md").write_text("Uncommitted replacement.\n")
    elif fault == "head":
        git(case.root, "commit", "--allow-empty", "-qm", "new head")
    elif fault == "ledger":
        engine.paths.deferred_work.write_text("Unfinished ledger work.\n")
    elif fault == "blocked-plan":
        case.plan.write_text(case.plan.read_text().replace("status: in-review", "status: blocked"))
    elif fault == "journal-tail":
        with engine.journal.path.open("a") as stream:
            stream.write('{"kind":"session-start"')
    elif fault == "duplicate-end":
        engine.journal.append(
            "session-end",
            task_id=record.task_id,
            status="stalled",
            env_fault=True,
            turn_error="pool",
        )
    elif fault == "later-launch":
        engine.journal.append("session-start", task_id="later-review", role="review")
    elif fault in ("missing-end", "wrong-end"):
        entries = engine.journal.entries()
        target = next(
            e
            for e in entries
            if e.get("kind") == "session-end" and e.get("task_id") == record.task_id
        )
        if fault == "missing-end":
            entries.remove(target)
        else:
            target["turn_error"] = "transport"
        monkeypatch.setattr(type(engine.journal), "entries", lambda _self: entries)
    elif fault == "later-escalation":
        engine.journal.append("story-escalated", story_key=task.story_key, reason="new conflict")
    elif fault == "exhausted-budget":
        monkeypatch.setattr(
            policy,
            "load",
            lambda _path: dataclasses.replace(
                engine.policy, limits=dataclasses.replace(engine.policy.limits, max_review_cycles=1)
            ),
        )
    elif fault == "disabled-review":
        monkeypatch.setattr(
            policy,
            "load",
            lambda _path: dataclasses.replace(
                engine.policy, review=dataclasses.replace(engine.policy.review, enabled=False)
            ),
        )
    before = copy.deepcopy(task.to_dict())
    assert (
        accepted_review.pool_review_refusal(engine.state, task, case.run_dir, case.root) is not None
    )
    assert task.to_dict() == before


@pytest.mark.parametrize("fault", ["disabled", "exhausted", "adapter", "transcript"])
def test_pool_resume_never_falls_back_to_new_native(pool_review, fault):
    case, engine, adapter, task, record = pool_review
    if fault == "disabled":
        engine.policy = dataclasses.replace(
            engine.policy, recovery=RecoveryPolicy(max_session_resumes=0)
        )
    elif fault == "exhausted":
        task.session_resumes = 2
    elif fault == "adapter":
        record.adapter = "other-adapter"
    else:
        record.transcript_path = str(case.root / "missing-transcript.jsonl")
    before = copy.deepcopy(task.to_dict())
    with pytest.raises(RunPaused, match="native conversation"):
        engine._finish_inflight()
    assert task.to_dict() == before
    assert not adapter.sessions


@pytest.mark.parametrize("fault", ["source", "ledger", "plan"])
def test_pool_resume_rechecks_after_session_hooks(pool_review, monkeypatch, fault):
    case, engine, adapter, task, _record = pool_review
    head = verify.rev_parse_head(case.root)
    adapter.script.append(_successful_primary(case))

    def hook(_task, _role, prompt, env, _stage):
        path = {
            "source": case.root / "crate-readme.md",
            "ledger": engine.paths.deferred_work,
            "plan": case.plan,
        }[fault]
        path.write_text(
            path.read_text() + "\nHook-owned changes.\n"
            if path.exists()
            else "Hook-owned changes.\n"
        )
        return prompt, env, None

    monkeypatch.setattr(engine, "_emit_session_gate", hook)
    with pytest.raises(RunPaused, match="cannot resume pool-paused review"):
        engine._finish_inflight()
    assert not adapter.sessions
    assert load_state(case.run_dir).tasks[task.story_key].phase == Phase.ESCALATED
    assert verify.rev_parse_head(case.root) == head


def test_pool_retry_crash_before_binding_never_launches_fresh_primary(pool_review, monkeypatch):
    from bmad_loop.journal import Journal
    from bmad_loop.tickets_engine import TicketsEngine

    case, engine, adapter, task, _record = pool_review

    def crash(_task):
        raise RuntimeError("host loss before native launch")

    monkeypatch.setattr(engine, "_reset_spec_for_review", crash)
    with pytest.raises(RuntimeError, match="host loss"):
        engine._finish_inflight()
    state = load_state(case.run_dir)
    assert (
        state.tasks[task.story_key].implemented_review["pool_resume_required"]
        == "interrupted-primary"
    )
    recovered = TicketsEngine(
        paths=engine.paths,
        policy=engine.policy,
        adapter=adapter,
        run_dir=case.run_dir,
        state=state,
        journal=Journal(case.run_dir, pin=state),
    )
    with pytest.raises(RunPaused, match="reserved pool-review continuation"):
        recovered._finish_inflight()
    assert not adapter.sessions
    assert state.tasks[task.story_key].review_cycle == 2
    assert state.tasks[task.story_key].review_unlaunched_cycles == 0
    assert load_state(case.run_dir).tasks[task.story_key].phase == Phase.ESCALATED


@pytest.mark.parametrize("event", ["session-resume", "session-bound"])
def test_pool_review_refuses_late_launch_identity(pool_review, event):
    case, engine, _adapter, task, record = pool_review
    assert accepted_review.pool_review_refusal(engine.state, task, case.run_dir, case.root) is None
    engine.journal.append(event, task_id=record.task_id, session_id=record.session_id)
    assert (
        accepted_review.pool_review_refusal(engine.state, task, case.run_dir, case.root) is not None
    )


@pytest.mark.parametrize("fault", ["critical", "transport", "unattested"])
def test_other_escalations_remain_terminal(pool_review, fault):
    _case, engine, adapter, task, record = pool_review
    if fault == "critical":
        record.result_json = {"escalations": [{"severity": "CRITICAL", "detail": "contradiction"}]}
    elif fault == "transport":
        record.ended = "transport"
    else:
        task.implemented_review = None
    before = copy.deepcopy(task.to_dict())
    engine._finish_inflight()
    assert task.to_dict() == before
    assert not adapter.sessions


def test_pool_retry_veto_keeps_native_continuation_required(pool_review, monkeypatch):
    case, engine, adapter, task, record = pool_review
    veto = SimpleNamespace(action="pause", plugin_id="test-plugin", reason="blocked")
    context = SimpleNamespace(stage="pre_review_session", resolved_veto=lambda: veto)
    monkeypatch.setattr(
        engine, "_emit_session_gate", lambda task, role, prompt, env, stage: (prompt, env, context)
    )
    with pytest.raises(RunPaused):
        engine._finish_inflight()
    assert not adapter.sessions
    saved = load_state(case.run_dir).tasks[task.story_key]
    assert saved.phase == Phase.ESCALATED
    assert saved.implemented_review.get("pool_resume_required") == "interrupted-primary"
    assert saved.sessions[-1].task_id == record.task_id


def test_pool_review_requires_recorded_adapter(pool_review):
    case, engine, _adapter, task, record = pool_review
    assert accepted_review.pool_review_refusal(engine.state, task, case.run_dir, case.root) is None
    record.adapter = ""
    assert (
        accepted_review.pool_review_refusal(engine.state, task, case.run_dir, case.root) is not None
    )


@pytest.mark.parametrize(
    "native,resumed",
    [
        (None, None),
        ("different-native", "interrupted-primary"),
        ("interrupted-primary", None),
        ("interrupted-primary", "interrupted-primary"),
    ],
)
def test_pool_retry_unproven_native_never_clears_obligation(pool_review, native, resumed):
    case, engine, adapter, task, _record = pool_review
    adapter.script.append(SessionResult(status="crashed", session_id=native, resumed_from=resumed))
    adapter.script.append(_successful_primary(case))
    with pytest.raises(RunPaused):
        engine._finish_inflight()
    saved = load_state(case.run_dir).tasks[task.story_key]
    assert saved.implemented_review.get("pool_resume_required")
    assert saved.phase == Phase.ESCALATED
    assert len(adapter.sessions) == 1


@pytest.mark.parametrize(
    "status,result",
    [
        ("crashed", None),
        ("stalled", None),
        ("timeout", None),
        ("completed", None),
        ("crashed", {"status": "built"}),
        ("stalled", {"status": "built"}),
        ("timeout", {"status": "built"}),
    ],
)
def test_incomplete_same_native_record_keeps_pool_obligation(pool_review, status, result):
    _case, _engine, _adapter, task, _record = pool_review
    task.review_cycle = 2
    task.implemented_review["pool_resume_required"] = "interrupted-primary"
    task.sessions.append(
        SessionRecord(
            task_id=f"{task.story_key}-review-2-g2",
            role="review",
            adapter="claude",
            status=status,
            session_id="interrupted-primary",
            resumed_from="interrupted-primary",
            result_json=result,
        )
    )
    assert not accepted_review.pool_resume_recorded(task)


@pytest.mark.parametrize("native", [None, "different-native"])
def test_pool_retry_crash_replay_requires_original_native(pool_review, native):
    case, engine, adapter, task, _record = pool_review
    task.phase = Phase.REVIEW_RUNNING
    task.review_cycle = 2
    task.implemented_review["pool_resume_required"] = "interrupted-primary"
    task.sessions.append(
        SessionRecord(
            task_id=f"{task.story_key}-review-2-g2",
            role="review",
            adapter="claude",
            status="completed",
            session_id=native,
            resumed_from="interrupted-primary",
            result_json={**case.task.sessions[-1].result_json, "status": "built"},
        )
    )
    save_state(case.run_dir, engine.state)
    with pytest.raises(RunPaused):
        engine._finish_inflight()
    saved = load_state(case.run_dir).tasks[task.story_key]
    assert saved.implemented_review.get("pool_resume_required") == "interrupted-primary"
    assert saved.phase == Phase.ESCALATED
    assert not adapter.sessions


def test_pool_retry_completed_replay_uses_same_native_record(pool_review):
    case, engine, adapter, task, _record = pool_review
    task.phase = Phase.REVIEW_RUNNING
    task.review_cycle = 2
    task.implemented_review["pool_resume_required"] = "interrupted-primary"
    task.sessions.append(
        SessionRecord(
            task_id=f"{task.story_key}-review-2-g2",
            role="review",
            adapter="claude",
            status="completed",
            session_id="interrupted-primary",
            resumed_from="interrupted-primary",
            result_json={**case.task.sessions[-1].result_json, "status": "built"},
        )
    )
    case.plan.write_text(case.plan.read_text().replace("status: in-review", "status: built"))
    save_state(case.run_dir, engine.state)
    engine._finish_inflight()
    assert task.phase == Phase.DONE
    assert not adapter.sessions
    assert task.review_cycle == 2
    assert task.sessions[-1].session_id == "interrupted-primary"
