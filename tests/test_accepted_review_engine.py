"""Accepted-work recovery hands off to the real review loop, never a new dev."""

import copy
import dataclasses

import pytest
from conftest import _OK, review_effect, write_sprint
from test_accepted_review_recovery import accepted_implementation as _accepted_implementation
from test_accepted_review_recovery import (
    ignored_store_implementation as _ignored_store_implementation,
)
from test_engine import _dev_verify_crash_state, make_engine

from bmad_loop import bmadconfig, cli, runs, verify
from bmad_loop.adapters.mock import MockAdapter
from bmad_loop.engine import RunPaused
from bmad_loop.frontmatter import read_frontmatter, status_of
from bmad_loop.journal import Journal, load_state
from bmad_loop.model import Phase
from bmad_loop.policy import GatesPolicy, NotifyPolicy, Policy, ReviewPolicy, VerifyPolicy
from bmad_loop.tickets_engine import TicketsEngine

accepted_implementation = _accepted_implementation
ignored_store_implementation = _ignored_store_implementation


def _armed_engine(case):
    runs.rearm_for_implemented_review(
        case.run_dir, case.task.story_key, case.implementation, project_root=case.root
    )
    state = load_state(case.run_dir)
    state.stopped = False
    adapter = MockAdapter([])
    engine = TicketsEngine(
        paths=bmadconfig.load_paths(case.root, want_tickets=True),
        policy=Policy(gates=GatesPolicy(mode="none"), notify=NotifyPolicy(desktop=False)),
        adapter=adapter,
        run_dir=case.run_dir,
        state=state,
        journal=Journal(case.run_dir, pin=state),
    )
    return engine, adapter


def test_implemented_review_routes_real_ticket_without_reaccepting_dev(
    accepted_implementation, monkeypatch
):
    case = accepted_implementation
    engine, adapter = _armed_engine(case)
    task = engine.state.tasks[case.task.story_key]
    records = copy.deepcopy(task.sessions)
    calls = []
    monkeypatch.setattr(
        engine,
        "_review_and_commit",
        lambda task, **kw: calls.append(status_of(read_frontmatter(case.plan))),
    )

    engine._resume_after_dev_verify(task)

    assert calls == ["in-review"]
    assert task.accepted_dev_session_index == 0 and task.sessions == records
    assert task.baseline_commit == case.task.baseline_commit
    assert adapter.sessions == []
    assert read_frontmatter(case.plan)["verify_unmet"] == 1
    assert [e for e in engine.journal.entries() if e["kind"] == "ticket-mark"]


@pytest.mark.parametrize("changed", ["head", "plan", "accepted", "source"])
def test_implemented_review_refuses_drift_before_dispatch(
    accepted_implementation, monkeypatch, changed
):
    from conftest import git

    case = accepted_implementation
    engine, adapter = _armed_engine(case)
    task = engine.state.tasks[case.task.story_key]
    if changed == "head":
        git(case.root, "commit", "--allow-empty", "-m", "new head after attestation")
    elif changed == "plan":
        case.plan.write_text(case.plan.read_text() + "\nChanged acceptance scope.\n")
    elif changed == "accepted":
        task.sessions[0].session_id = "another-native"
    else:
        (case.root / "unfinished.py").write_text("work_in_progress()\n")
    monkeypatch.setattr(
        engine, "_review_and_commit", lambda task, **kw: pytest.fail("reviewed stale evidence")
    )

    with pytest.raises(RunPaused):
        engine._resume_after_dev_verify(task)

    assert task.phase == Phase.ESCALATED and adapter.sessions == []


def test_implemented_review_runs_primary_even_when_not_recommended(project, monkeypatch):
    from bmad_loop import accepted_review

    # This unit isolates review-policy routing after a genuine primary binding.
    # The full TicketsEngine case below covers first-launch admission.
    monkeypatch.setattr(accepted_review, "primary_was_bound", lambda *args: True)
    write_sprint(project, {"1-1-a": "ready-for-dev"})
    engine, adapter = make_engine(project, [review_effect(project, "1-1-a", clean=True)])
    _baseline, spec = _dev_verify_crash_state(project, engine, "done")
    task = engine.state.tasks["1-1-a"]
    task.spec_file = str(spec)
    task.followup_review_recommended = False
    task.implemented_review = {"version": 1}
    engine.policy = dataclasses.replace(engine.policy, verify=VerifyPolicy(commands=(_OK,)))

    engine._review_and_commit(task)

    assert [s.role for s in adapter.sessions] == ["review"]
    assert task.phase == Phase.DONE
    rows = engine.journal.entries()
    assert any(e["kind"] == "verify-command-result" and e["returncode"] == 0 for e in rows)
    assert not any(e["kind"] == "review-not-recommended" for e in rows)


def test_implemented_review_disabled_policy_cannot_silently_commit(project, monkeypatch):
    write_sprint(project, {"1-1-a": "ready-for-dev"})
    engine, adapter = make_engine(project, [])
    _baseline, spec = _dev_verify_crash_state(project, engine, "done")
    task = engine.state.tasks["1-1-a"]
    task.spec_file = str(spec)
    task.implemented_review = {"version": 1}
    engine.policy = dataclasses.replace(engine.policy, review=ReviewPolicy(enabled=False))
    head = verify.rev_parse_head(project.project)
    monkeypatch.setattr(
        engine, "_skip_review_and_commit", lambda *a, **kw: pytest.fail("skipped required primary")
    )

    with pytest.raises(RunPaused):
        engine._review_and_commit(task)

    assert task.phase == Phase.ESCALATED and task.commit_sha is None
    assert adapter.sessions == [] and verify.rev_parse_head(project.project) == head


def test_public_resolve_implemented_accepts_stopped_dev_running(
    accepted_implementation, monkeypatch
):
    case = accepted_implementation
    monkeypatch.setattr(cli, "_resolve_pre_session_gates", lambda *_a: None)

    assert (
        cli.main(
            [
                "resolve",
                case.state.run_id,
                "--project",
                str(case.root),
                "--story",
                case.task.story_key,
                "--review-implemented",
                case.implementation,
                "--no-resume",
            ]
        )
        == 0
    )

    task = load_state(case.run_dir).tasks[case.task.story_key]
    assert task.phase == Phase.DEV_VERIFY and task.accepted_dev_session_index == 0
    assert task.implemented_review["head"] == verify.rev_parse_head(case.root)


def test_recovery_rechecks_evidence_after_confirmation(accepted_implementation, monkeypatch):
    from conftest import git

    case = accepted_implementation
    before = (case.run_dir / "state.json").read_bytes()
    monkeypatch.setattr(cli, "_resolve_pre_session_gates", lambda *_a: None)

    def confirm(_question):
        git(case.root, "commit", "--allow-empty", "-m", "changed during confirmation")
        return True

    monkeypatch.setattr(cli, "_confirm", confirm)

    assert (
        cli.main(
            [
                "resolve",
                case.state.run_id,
                "--project",
                str(case.root),
                "--story",
                case.task.story_key,
                "--review-implemented",
                case.implementation,
            ]
        )
        == 1
    )

    assert (case.run_dir / "state.json").read_bytes() == before


def test_recovery_pre_native_crash_still_requires_pinned_head(accepted_implementation, monkeypatch):
    from conftest import git

    case = accepted_implementation
    engine, adapter = _armed_engine(case)
    task = engine.state.tasks[case.task.story_key]
    task.phase, task.review_cycle = Phase.REVIEW_RUNNING, 1
    git(case.root, "commit", "--allow-empty", "-m", "changed before first native")
    monkeypatch.setattr(
        engine, "_review_and_commit", lambda *a, **kw: pytest.fail("unpinned review")
    )

    with pytest.raises(RunPaused):
        engine._resume_implemented_review(task)

    assert task.phase == Phase.ESCALATED and adapter.sessions == []


def test_recovery_plan_transition_replays_after_host_loss(accepted_implementation, monkeypatch):
    case = accepted_implementation
    engine, _adapter = _armed_engine(case)
    task = engine.state.tasks[case.task.story_key]
    monkeypatch.setattr(engine, "_review_and_commit", lambda *a, **kw: None)

    engine._resume_after_dev_verify(task)
    engine._resume_after_dev_verify(task)

    assert status_of(read_frontmatter(case.plan)) == "in-review"
    assert task.accepted_dev_session_index == 0
    context = engine._implemented_review_context(task)
    assert case.original_baseline in context and case.implementation in context
    assert task.implemented_review["head"] in context
    assert "original-native" in context


def test_recovery_started_primary_keeps_its_partial_work(accepted_implementation, monkeypatch):
    case = accepted_implementation
    engine, adapter = _armed_engine(case)
    task = engine.state.tasks[case.task.story_key]
    task.phase, task.review_cycle = Phase.REVIEW_RUNNING, 1
    engine.journal.append(
        "session-bound", task_id=f"801.3-review-1-g{task.generation}", session_id="real-primary"
    )
    case.plan.write_text(case.plan.read_text() + "\nPartial primary review finding.\n")
    calls = []
    monkeypatch.setattr(engine, "_review_and_commit", lambda *a, **kw: calls.append(kw))

    engine._resume_implemented_review(task)

    assert calls == [{"resumed_loop": True}]
    assert "Partial primary review finding." in case.plan.read_text()
    assert adapter.sessions == []


def test_recovery_green_fix_uses_current_acceptance_before_returning_to_review(
    project, monkeypatch
):
    write_sprint(project, {"1-1-a": "ready-for-dev"})
    engine, _adapter = make_engine(project, [])
    _baseline, spec = _dev_verify_crash_state(project, engine, "done")
    task = engine.state.tasks["1-1-a"]
    task.spec_file = str(spec)
    task.accepted_dev_session_index = 0
    task.review_cycle = 1
    task.implemented_review = {"version": 1}
    calls = []
    monkeypatch.setattr(engine, "_review_and_commit", lambda *a, **kw: calls.append(kw))
    monkeypatch.setattr(
        engine, "_resume_implemented_review", lambda *a: pytest.fail("reused pre-fix evidence")
    )

    engine._resume_after_dev_verify(task)

    assert calls == [{"resumed_loop": True}]
    assert engine._accepted_dev_session_matches(task)


@pytest.mark.parametrize("boundary", ["entry", "confirmation", "dispatch"])
def test_recovery_refuses_changed_ticket_store_without_rearming(
    accepted_implementation, monkeypatch, boundary, capsys
):
    from bmad_loop import accepted_review, ticketstore
    from bmad_loop.journal import save_state

    case = accepted_implementation
    topology = ticketstore.assess_store(bmadconfig.load_paths(case.root, want_tickets=True), "none")
    assert isinstance(topology, ticketstore.StoreTopology)
    case.state.store_topology = topology.to_dict()
    save_state(case.run_dir, case.state)
    assert case.refusal() is None

    def move_store():
        # The old bound plan remains readable; only the active store changes.
        store = case.root.parent / "different-store"
        store.mkdir()
        config = case.root / ticketstore.USER_CONFIG_REL
        config.write_text(
            ticketstore.redirected_user_config_text(
                config.read_text(), main_store=store, initiative=None
            )
        )
        assert case.plan.is_file()

    if boundary == "dispatch":
        engine, _adapter = _armed_engine(case)
        task = engine.state.tasks[case.task.story_key]
        assert (
            accepted_review.pending_review_refusal(engine.state, task, case.run_dir, case.root)
            is None
        )
        before = (case.run_dir / "state.json").read_bytes()
        move_store()
        refusal = accepted_review.pending_review_refusal(
            engine.state, task, case.run_dir, case.root
        )
        assert refusal is not None and "ticket store changed" in refusal
    else:
        before = (case.run_dir / "state.json").read_bytes()
        argv = [
            "resolve",
            case.state.run_id,
            "--project",
            str(case.root),
            "--story",
            case.task.story_key,
            "--review-implemented",
            case.implementation,
        ]
        if boundary == "entry":
            move_store()
            argv += ["--no-resume"]
            monkeypatch.setattr(
                cli,
                "_resolve_implemented_review",
                lambda *a: pytest.fail("passed early store gate"),
            )
        else:

            def confirm(_question):
                move_store()
                return True

            monkeypatch.setattr(cli, "_confirm", confirm)
        assert cli.main(argv) == 1
        assert "ticket store changed" in capsys.readouterr().err
    assert (case.run_dir / "state.json").read_bytes() == before


def test_recovery_refuses_live_policy_switch_to_worktree(accepted_implementation, monkeypatch):
    from bmad_loop import policy

    case = accepted_implementation
    assert case.refusal() is None
    monkeypatch.setattr(
        policy, "load", lambda _path: policy.Policy(scm=policy.ScmPolicy(isolation="worktree"))
    )
    assert case.refusal() is not None


def test_real_ticket_recovery_reviews_verifies_and_commits(accepted_implementation):
    from bmad_loop.adapters.base import SessionResult

    case = accepted_implementation
    engine, adapter = _armed_engine(case)
    task = engine.state.tasks[case.task.story_key]
    engine.policy = dataclasses.replace(engine.policy, verify=VerifyPolicy(commands=(_OK,)))

    def primary(spec):
        assert spec.role == "review"
        assert case.implementation in spec.prompt
        case.plan.write_text(case.plan.read_text().replace("status: in-review", "status: built"))
        return SessionResult(
            status="completed",
            result_json={
                **case.task.sessions[-1].result_json,
                "status": "built",
                "followup_review_recommended": False,
            },
        )

    adapter.script.append(primary)
    engine._resume_after_dev_verify(task)

    assert task.phase == Phase.DONE and task.commit_sha
    assert task.accepted_dev_session_index == 0
    assert [spec.role for spec in adapter.sessions] == ["review"]
    assert status_of(read_frontmatter(case.plan)) == "done"
    assert read_frontmatter(case.plan)["verify_unmet"] == 1
    assert any(
        e["kind"] == "verify-command-result" and e["returncode"] == 0
        for e in engine.journal.entries()
    )


@pytest.mark.parametrize(
    "drift", ["head", "plan", "plan-status", "store", "accepted", "ledger", "unstripped-plan"]
)
def test_first_primary_refuses_post_hook_drift(accepted_implementation, monkeypatch, drift):
    from conftest import git

    from bmad_loop import ticketstore

    case = accepted_implementation
    case.plan.write_text(
        case.plan.read_text() + "\n## Auto Run Result\n\nStatus: built\nRetained evidence.\n"
    )
    before_strip = case.plan.read_text().replace("status: in-progress", "status: in-review")
    engine, adapter = _armed_engine(case)
    task = engine.state.tasks[case.task.story_key]
    adapter.script.append(lambda _spec: pytest.fail("launched after recovery evidence drift"))
    original_head = verify.rev_parse_head(case.root)

    def gate(_task, _role, prompt, env, _stage):
        if drift == "head":
            git(case.root, "commit", "--allow-empty", "-m", "hook changed head")
        elif drift == "plan":
            case.plan.write_text(case.plan.read_text() + "\nHook changed acceptance scope.\n")
        elif drift == "plan-status":
            case.plan.write_text(
                case.plan.read_text().replace("status: in-review", "status: built")
            )
        elif drift == "unstripped-plan":
            case.plan.write_text(before_strip)
        elif drift == "accepted":
            task.sessions[0].session_id = "hook-changed-native"
        elif drift == "ledger":
            ledger = bmadconfig.load_paths(case.root).deferred_work
            ledger.parent.mkdir(parents=True, exist_ok=True)
            ledger.write_text("Hook-owned unfinished deferred work.\n")
        else:
            store = case.root.parent / "hook-store"
            store.mkdir()
            config = case.root / ticketstore.USER_CONFIG_REL
            config.write_text(
                ticketstore.redirected_user_config_text(
                    config.read_text(), main_store=store, initiative=None
                )
            )
        return prompt, env, None

    monkeypatch.setattr(engine, "_emit_session_gate", gate)
    with pytest.raises(RunPaused):
        engine._resume_after_dev_verify(task)
    assert adapter.sessions == []

    if drift == "ledger":
        assert verify.rev_parse_head(case.root) == original_head
        assert "Hook-owned unfinished" in bmadconfig.load_paths(case.root).deferred_work.read_text()


@pytest.mark.parametrize("after_strip", [False, True])
@pytest.mark.parametrize("crash_count", [1, 2])
def test_recovery_replays_crash_after_real_marker_removal(
    accepted_implementation, monkeypatch, after_strip, crash_count
):
    from bmad_loop.adapters.base import SessionResult

    case = accepted_implementation
    case.plan.write_text(
        case.plan.read_text()
        + "\n## Auto Run Result\n\nStatus: built\nRetained completion evidence.\n"
    )
    engine, adapter = _armed_engine(case)
    task = engine.state.tasks[case.task.story_key]
    engine.policy = dataclasses.replace(
        engine.policy, limits=dataclasses.replace(engine.policy.limits, max_review_cycles=1)
    )
    reset = engine._reset_spec_for_review

    def crash_after_strip(task):
        if after_strip:
            reset(task)
            assert "## Auto Run Result" not in case.plan.read_text()
        raise RuntimeError("host lost at marker removal")

    monkeypatch.setattr(engine, "_reset_spec_for_review", crash_after_strip)
    with pytest.raises(RuntimeError, match="host lost at marker removal"):
        engine._resume_after_dev_verify(task)
    assert adapter.sessions == []

    state = load_state(case.run_dir)
    recovered = TicketsEngine(
        paths=engine.paths,
        policy=dataclasses.replace(engine.policy, verify=VerifyPolicy(commands=(_OK,))),
        adapter=adapter,
        run_dir=case.run_dir,
        state=state,
        journal=Journal(case.run_dir, pin=state),
    )

    def primary(spec):
        assert spec.role == "review"
        assert case.implementation in spec.prompt
        case.plan.write_text(case.plan.read_text().replace("status: in-review", "status: built"))
        return SessionResult(
            status="completed",
            result_json={
                **case.task.sessions[-1].result_json,
                "status": "built",
                "followup_review_recommended": False,
            },
        )

    if crash_count == 2:
        second_reset = recovered._reset_spec_for_review

        def second_crash(task):
            if after_strip:
                second_reset(task)
            raise RuntimeError("second host loss")

        monkeypatch.setattr(recovered, "_reset_spec_for_review", second_crash)
        with pytest.raises(RuntimeError, match="second host loss"):
            recovered._finish_inflight()
        assert state.tasks[case.task.story_key].review_unlaunched_cycles == 1
        state = load_state(case.run_dir)
        recovered = TicketsEngine(
            paths=engine.paths,
            policy=recovered.policy,
            adapter=adapter,
            run_dir=case.run_dir,
            state=state,
            journal=Journal(case.run_dir, pin=state),
        )

    adapter.script.append(primary)
    recovered._finish_inflight()
    resumed_task = state.tasks[case.task.story_key]
    assert resumed_task.phase == Phase.DONE
    assert [spec.role for spec in adapter.sessions] == ["review"]
    assert resumed_task.accepted_dev_session_index == 0
    assert read_frontmatter(case.plan)["verify_unmet"] == 1
    assert adapter.sessions[0].task_id == f"{case.task.story_key}-review-{crash_count + 1}-g2"
    assert resumed_task.review_unlaunched_cycles == crash_count
    assert resumed_task.usage_relaunches == 0


def test_ignored_store_hook_ledger_edit_never_launches_or_commits(
    ignored_store_implementation, monkeypatch
):
    case, ledger = ignored_store_implementation
    engine, adapter = _armed_engine(case)
    task = engine.state.tasks[case.task.story_key]
    head = verify.rev_parse_head(case.root)
    adapter.script.append(lambda _spec: pytest.fail("launched after ignored ledger mutation"))

    def hook(_task, _role, prompt, env, _stage):
        ledger.write_text("Hook-owned unfinished work.\n")
        return prompt, env, None

    monkeypatch.setattr(engine, "_emit_session_gate", hook)
    with pytest.raises(RunPaused):
        engine._resume_after_dev_verify(task)
    assert not adapter.sessions
    assert verify.rev_parse_head(case.root) == head
    assert ledger.read_text() == "Hook-owned unfinished work.\n"


def test_recovery_does_not_refund_same_reservation_twice(accepted_implementation, monkeypatch):
    from bmad_loop.journal import save_state

    case = accepted_implementation
    engine, _adapter = _armed_engine(case)
    task = engine.state.tasks[case.task.story_key]
    task.phase = Phase.REVIEW_RUNNING
    task.review_cycle = 1
    monkeypatch.setattr(engine, "_review_and_commit", lambda *a, **kw: None)
    engine._resume_implemented_review(task)
    assert task.review_unlaunched_cycles == 1
    assert task.phase == Phase.REVIEW_VERIFY
    # This is the state durably saved before any new reservation. A repeated
    # resume at this boundary must retain that single credit.
    save_state(case.run_dir, engine.state)
    engine.state = load_state(case.run_dir)
    task = engine.state.tasks[case.task.story_key]
    engine._resume_implemented_review(task)
    assert task.review_unlaunched_cycles == 1
    assert task.charged_review_cycles == 0
    assert task.usage_relaunches == 0
