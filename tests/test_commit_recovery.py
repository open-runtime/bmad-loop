"""Retry only a failed commit window over explicitly attested, verified work."""

import copy
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import EntrySpec, EpicSpec, git, write_plan, write_ticket_tree

from bmad_loop import bmadconfig, cli, commit_recovery, platform_util, policy, runs, verify
from bmad_loop.adapters.mock import MockAdapter
from bmad_loop.engine import RunPaused
from bmad_loop.journal import Journal, load_state, save_state
from bmad_loop.model import Phase, RunState, SessionRecord, StoryTask, TicketBinding
from bmad_loop.tickets_engine import TicketsEngine


@dataclass
class CommitCase:
    root: Path
    run_dir: Path
    head: str
    state: RunState
    task: StoryTask

    def evidence(self):
        return commit_recovery.evidence(
            self.state, self.task, self.head, run_dir=self.run_dir, project_root=self.root
        )

    def arm(self):
        return commit_recovery.arm(
            self.run_dir, self.task.story_key, self.head, project_root=self.root
        )


@pytest.fixture
def commit_case(project_v7):
    root = project_v7.project
    key = "205.9"
    epic = "epic-commit-recovery"
    write_ticket_tree(
        project_v7,
        [EpicSpec(205, epic, "Commit recovery", (EntrySpec(9, "Keep work", refined=True),))],
    )
    plan = write_plan(project_v7, epic, "story-keep-work-plan", 9, "ready-for-dev")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "ready plan")
    baseline = git(root, "rev-parse", "HEAD")
    plan.write_text(
        f"---\nticket: 9\nstatus: built\nbaseline_revision: {baseline}\n---\n\nKeep work.\n"
    )
    (root / "implementation.txt").write_text("Reviewed implementation.\n")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "reviewed work")
    head = git(root, "rev-parse", "HEAD")
    (root / policy.POLICY_FILE).parent.mkdir(parents=True, exist_ok=True)
    (root / policy.POLICY_FILE).write_text('[verify]\ncommands = ["true"]\n')
    result = {
        "workflow": "auto-dev",
        "story_key": key,
        "spec_file": str(plan),
        "baseline_commit": baseline,
        "status": "built",
        "escalations": [],
        "followup_review_recommended": False,
    }
    task = StoryTask(
        key,
        205,
        phase=Phase.ESCALATED,
        attempt=1,
        review_cycle=1,
        baseline_commit=baseline,
        baseline_untracked=[],
        spec_file=str(plan),
        accepted_dev_session_index=0,
        ticket=TicketBinding.from_dict(
            {"ref": key, "id": 9, "epic_slug": epic, "risk": "medium", "plan": str(plan)}
        ),
        sessions=[
            SessionRecord(
                f"{key}-dev-1",
                "dev",
                "completed",
                adapter="codex",
                session_id="dev-native",
                result_json=copy.deepcopy(result),
            ),
            SessionRecord(
                f"{key}-review-1",
                "review",
                "completed",
                adapter="claude",
                session_id="review-native",
                result_json=copy.deepcopy(result),
            ),
        ],
    )
    state = RunState(
        run_id="commit-recovery",
        project=str(root),
        repo_root=str(root),
        started_at="2026-11-05T06:00:00Z",
        source="tickets",
        stopped=True,
        paused_stage="escalation",
        paused_story_key=key,
        tasks={key: task},
        policy_snapshot=policy.load(root / policy.POLICY_FILE).to_dict(),
    )
    run_dir = root / ".bmad-loop/runs" / state.run_id
    save_state(run_dir, state)
    state.run_dir_identity = platform_util.root_identity_record(run_dir)
    save_state(run_dir, state)
    journal = Journal(run_dir, pin=state)
    for record in task.sessions:
        journal.set_active_log(record.task_id)
        journal.append("session-start", task_id=record.task_id, role=record.role)
        journal.append("session-bound", task_id=record.task_id, session_id=record.session_id)
        journal.append("session-end", task_id=record.task_id, status="completed")
        if record.role == "dev":
            journal.append(
                "dev-decision",
                story_key=key,
                action="proceed",
                session_status="completed",
                produced_work=True,
                env_fault=False,
            )
    journal.append(
        "review-result",
        story_key=key,
        cycle=1,
        status="built",
        followup_review_recommended=False,
        followup_damped=False,
    )
    journal.append(
        "verify-command-result",
        story_key=key,
        attempt=1,
        verification_stage="review",
        verification_sequence=2,
        command_index=0,
        command="true",
        returncode=0,
        spawn_error=None,
        capture_error=None,
    )
    journal.append("story-escalated", story_key=key, reason="commit failed: graph ambiguity")
    return CommitCase(root, run_dir, head, state, task)


def test_arm_retains_accepted_work_and_review(commit_case):
    case = commit_case
    original = copy.deepcopy(case.task.to_dict())
    case.arm()
    after = load_state(case.run_dir).tasks[case.task.story_key]
    assert after.phase == Phase.COMMITTING
    assert after.commit_retry["head"] == case.head
    for key in (
        "sessions",
        "baseline_commit",
        "accepted_dev_session_index",
        "attempt",
        "review_cycle",
        "generation",
    ):
        assert after.to_dict()[key] == original[key]
    assert verify.rev_parse_head(case.root) == case.head
    assert not verify.dirty_paths(case.root)


@pytest.mark.parametrize(
    "change,match",
    [
        ("phase", "finalization"),
        ("review", "review"),
        ("followup", "review"),
        ("wrong_failure", "commit failure"),
        ("missing_verify", "verification"),
        ("failed_verify", "verification"),
        ("changed_policy", "verification"),
        ("dirty", "clean"),
        ("head", "HEAD"),
        ("live", "stopped"),
    ],
)
def test_refusal_does_not_mutate(commit_case, monkeypatch, change, match):
    case = commit_case
    if change == "phase":
        case.task.phase = Phase.REVIEW_VERIFY
    elif change == "review":
        case.task.sessions[-1].status = "stalled"
    elif change == "followup":
        case.task.followup_review_recommended = True
    elif change in ("missing_verify", "failed_verify", "wrong_failure"):
        path = case.run_dir / "journal.jsonl"
        import json

        rows = [json.loads(line) for line in path.read_text().splitlines()]
        if change == "missing_verify":
            rows = [r for r in rows if r["kind"] != "verify-command-result"]
        elif change == "failed_verify":
            next(r for r in rows if r["kind"] == "verify-command-result")["returncode"] = 1
        else:
            rows[-1]["reason"] = "plugin vetoed pre_commit"
        path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    elif change == "changed_policy":
        (case.root / policy.POLICY_FILE).write_text('[verify]\ncommands = ["false"]\n')
    elif change == "dirty":
        (case.root / "implementation.txt").write_text("unreviewed change\n")
    elif change == "head":
        git(case.root, "commit", "--allow-empty", "-qm", "later head")
    elif change == "live":
        monkeypatch.setattr(runs, "engine_liveness", lambda _path: "alive")
    save_state(case.run_dir, case.state)
    before = (case.run_dir / "state.json").read_bytes()
    with pytest.raises(runs.RearmError, match=match):
        case.arm()
    assert (case.run_dir / "state.json").read_bytes() == before


def _engine(case):
    state = load_state(case.run_dir)
    state.stopped = False
    adapter = MockAdapter([])
    engine = TicketsEngine(
        paths=bmadconfig.load_paths(case.root, want_tickets=True),
        policy=policy.load(case.root / policy.POLICY_FILE),
        adapter=adapter,
        run_dir=case.run_dir,
        state=state,
        journal=Journal(case.run_dir, pin=state),
    )
    return engine, adapter, state.tasks[case.task.story_key]


def test_commit_retry_finishes_without_sessions_or_verification(commit_case, monkeypatch):
    case = commit_case
    case.arm()
    engine, adapter, task = _engine(case)
    monkeypatch.setattr(engine, "_review_verify_gate", lambda *_a: pytest.fail("repeated verify"))
    old_records = copy.deepcopy(task.sessions)
    engine._finalize_commit_phase(task)
    assert task.phase == Phase.DONE and task.commit_retry is None
    assert task.sessions == old_records and adapter.sessions == []
    assert task.commit_sha == verify.rev_parse_head(case.root)
    assert (case.root / "implementation.txt").read_text() == "Reviewed implementation.\n"
    assert len([r for r in engine.journal.entries() if r["kind"] == "verify-command-result"]) == 1


def test_delayed_resume_rejects_head_drift(commit_case):
    case = commit_case
    case.arm()
    git(case.root, "commit", "--allow-empty", "-qm", "new unreviewed head")
    drifted = verify.rev_parse_head(case.root)
    engine, adapter, task = _engine(case)
    with pytest.raises(RunPaused, match="HEAD"):
        engine._finalize_commit_phase(task)
    assert task.phase == Phase.COMMITTING and adapter.sessions == []
    assert verify.rev_parse_head(case.root) == drifted


def test_public_resolve_retry_commit_no_resume(commit_case, monkeypatch):
    case = commit_case
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
                "--retry-commit",
                case.head,
                "--no-resume",
            ]
        )
        == 0
    )
    assert load_state(case.run_dir).tasks[case.task.story_key].phase == Phase.COMMITTING


def test_retry_keeps_pre_commit_veto(commit_case, monkeypatch):
    case = commit_case
    case.arm()
    engine, adapter, task = _engine(case)
    emit = engine._emit

    def held(event, *args, **kwargs):
        if event == "pre_commit":
            return SimpleNamespace(
                resolved_veto=lambda: SimpleNamespace(
                    action="pause", plugin_id="review-hold", reason="not released"
                )
            )
        return emit(event, *args, **kwargs)

    monkeypatch.setattr(engine, "_emit", held)
    with pytest.raises(RunPaused, match="not released"):
        engine._finalize_commit_phase(task)
    assert task.phase == Phase.ESCALATED and adapter.sessions == []
    assert verify.rev_parse_head(case.root) == case.head


@pytest.mark.parametrize("after_commit", [False, True])
def test_retry_retains_existing_committing_crash_replay(commit_case, monkeypatch, after_commit):
    case = commit_case
    case.arm()
    engine, _, task = _engine(case)
    finalize = verify.finalize_commit

    def crash(*args, **kwargs):
        if after_commit:
            finalize(*args, **kwargs)
        raise KeyboardInterrupt

    monkeypatch.setattr(verify, "finalize_commit", crash)
    with pytest.raises(KeyboardInterrupt):
        engine._finalize_commit_phase(task)
    saved = load_state(case.run_dir).tasks[task.story_key]
    assert saved.phase == Phase.COMMITTING and saved.commit_retry is None
    monkeypatch.setattr(verify, "finalize_commit", finalize)
    resumed, adapter, task = _engine(case)
    resumed._finalize_commit_phase(task)
    assert task.phase == Phase.DONE and adapter.sessions == []
    assert (case.root / "implementation.txt").read_text() == "Reviewed implementation.\n"


def test_confirmed_evidence_rechecked_under_lock(commit_case):
    case = commit_case
    observed = case.evidence()
    case.task.sessions[-1].session_id = "changed-review-native"
    save_state(case.run_dir, case.state)
    before = (case.run_dir / "state.json").read_bytes()
    with pytest.raises(runs.RearmError, match="binding|changed"):
        commit_recovery.arm(
            case.run_dir,
            case.task.story_key,
            case.head,
            project_root=case.root,
            expected_evidence=observed,
        )
    assert (case.run_dir / "state.json").read_bytes() == before


def test_completed_review_discharges_development_review_request(commit_case):
    case = commit_case
    case.task.sessions[0].result_json["followup_review_recommended"] = True
    save_state(case.run_dir, case.state)
    case.arm()
    assert load_state(case.run_dir).tasks[case.task.story_key].phase == Phase.COMMITTING


@pytest.mark.parametrize("change", ["source", "policy"])
def test_guard_refusal_can_resume_after_restoring_attestation(commit_case, change):
    case = commit_case
    case.arm()
    path = case.root / ("implementation.txt" if change == "source" else policy.POLICY_FILE)
    original = path.read_bytes()
    path.write_text(
        "different implementation\n"
        if change == "source"
        else '[verify]\ncommands = ["true"]\n[gates]\nmode = "none"\n'
    )
    engine, adapter, task = _engine(case)
    with pytest.raises(RunPaused, match="commit retry refused"):
        engine._finalize_commit_phase(task)
    assert task.phase == Phase.COMMITTING and task.commit_retry is not None
    assert adapter.sessions == []
    path.write_bytes(original)
    resumed, adapter, task = _engine(case)
    resumed._finalize_commit_phase(task)
    assert task.phase == Phase.DONE and adapter.sessions == []
