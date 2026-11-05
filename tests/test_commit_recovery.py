"""Retry only a failed commit window over explicitly attested, verified work."""

import copy
import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import EntrySpec, EpicSpec, git, write_plan, write_ticket_tree

from bmad_loop import (
    bmadconfig,
    cli,
    commit_recovery,
    platform_util,
    policy,
    runs,
    ticketstore,
    verify,
)
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


VERIFY_COMMANDS = ["true", "printf build", "printf codegen"]


def _write_policy(root, extra=""):
    (root / policy.POLICY_FILE).write_text(
        "[verify]\ncommands = " + json.dumps(VERIFY_COMMANDS) + "\n" + extra
    )


def _journal_rows(case):
    return [json.loads(line) for line in (case.run_dir / "journal.jsonl").read_text().splitlines()]


def _replace_journal(case, rows):
    (case.run_dir / "journal.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows))


@pytest.fixture
def commit_case(project_v7, monkeypatch):
    """Historical205.9: stalled gen0, accepted gen1, two three-command verifies."""
    root = project_v7.project
    key = "205.9"
    epic = "epic-commit-recovery"
    before_verify = int(time.time()) - 300
    stamp = datetime.fromtimestamp(before_verify, timezone.utc).isoformat()
    write_ticket_tree(
        project_v7,
        [EpicSpec(205, epic, "Commit recovery", (EntrySpec(9, "Keep work", refined=True),))],
    )
    plan = write_plan(project_v7, epic, "story-keep-work-plan", 9, "ready-for-dev")
    with monkeypatch.context() as mp:
        mp.setenv("GIT_AUTHOR_DATE", stamp)
        mp.setenv("GIT_COMMITTER_DATE", stamp)
        git(root, "add", "-A")
        git(root, "commit", "-qm", "ready plan")
        baseline = git(root, "rev-parse", "HEAD")
        plan.write_text(
            f"---\nticket: 9\nstatus: built\nbaseline_revision: {baseline}\n---\n\nKeep work.\n"
        )
        (root / "implementation.txt").write_text("Reviewed implementation.\n")
        git(root, "add", "-A")
        git(root, "commit", "-qm", "accepted dev")
        (root / "review.txt").write_text("Reviewed correction.\n")
        git(root, "add", "-A")
        git(root, "commit", "-qm", "accepted review")
    head = git(root, "rev-parse", "HEAD")
    (root / policy.POLICY_FILE).parent.mkdir(parents=True, exist_ok=True)
    _write_policy(root)
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
        0,
        phase=Phase.ESCALATED,
        attempt=1,
        review_cycle=1,
        generation=1,
        baseline_commit=baseline,
        baseline_untracked=[],
        spec_file=str(plan),
        dispatched_spec_file=str(plan),
        accepted_dev_session_index=1,
        resolved_redrive=True,
        ticket_premark_status="built",
        ticket=TicketBinding.from_dict(
            {
                "ref": key,
                "id": "9",
                "epic_id": "205",
                "epic_slug": epic,
                "risk": "medium",
                "plan": str(plan),
            }
        ),
        sessions=[
            SessionRecord(
                f"{key}-dev-1", "dev", "stalled", adapter="claude", session_id="old-native"
            ),
            SessionRecord(
                f"{key}-dev-1-g1",
                "dev",
                "completed",
                adapter="codex",
                session_id="dev-native",
                result_json=copy.deepcopy(result),
            ),
            SessionRecord(
                f"{key}-review-1-g1",
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
        started_at=stamp,
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
    old, dev, review = task.sessions
    journal.set_active_log(old.task_id)
    journal.append("session-start", task_id=old.task_id, role="dev", story_key=key)
    journal.append("session-bound", task_id=old.task_id, session_id=old.session_id)
    journal.append("session-end", task_id=old.task_id, status="stalled")
    journal.append("dev-decision", story_key=key, action="pause", session_status="stalled")
    journal.append("story-escalated", story_key=key, reason="pool unavailable")
    journal.append("story-escalation-resolved", story_key=key, baseline=baseline, restore=False)
    journal.append("run-resume", code_root_changed=False)
    journal.append("resume-restart", story_key=key, phase="pending")
    journal.append("rollback-auto", story_key=key, baseline=baseline)
    for record, sequence in ((dev, 1), (review, 2)):
        journal.set_active_log(record.task_id)
        journal.append("session-start", task_id=record.task_id, role=record.role, story_key=key)
        journal.append("session-bound", task_id=record.task_id, session_id=record.session_id)
        journal.append("session-end", task_id=record.task_id, status="completed")
        if record.role == "review":
            journal.append(
                "review-result",
                story_key=key,
                cycle=1,
                status="built",
                followup_review_recommended=False,
                followup_damped=False,
            )
        for i, command in enumerate(VERIFY_COMMANDS):
            journal.append(
                "verify-command-result",
                story_key=key,
                attempt=1,
                verification_stage=record.role,
                verification_sequence=sequence,
                command_index=i,
                command=command,
                returncode=0,
                spawn_error=None,
                capture_error=None,
            )
        if record.role == "dev":
            journal.append(
                "dev-decision",
                story_key=key,
                action="proceed",
                session_status="completed",
                produced_work=True,
                env_fault=False,
            )
    journal.append("plugin-hook", plugin="sync-with-main", stage="pre_commit", rc=0)
    journal.append(
        "ticket-mark", ref=key, reason="accept", before="built", after="done", outcome="written"
    )
    journal.append("ticket-plan-restored", before="done", after="built", outcome="restored")
    journal.append("story-escalated", story_key=key, reason="commit failed: graph ambiguity")
    journal.append("run-paused", reason="commit failed", stage="escalation", story_key=key)
    case = CommitCase(root, run_dir, head, state, task)
    rows = _journal_rows(case)
    for i, row in enumerate(rows):
        row["ts"] = before_verify + 120 + i
    _replace_journal(case, rows)
    hook = next(r for r in rows if r["kind"] == "plugin-hook")
    (run_dir / "sync-with-main.json").write_text(
        json.dumps(
            {
                "chains": {
                    key: {
                        "chain_head": head,
                        "recorded_at": datetime.fromtimestamp(hook["ts"], timezone.utc).isoformat(),
                        "upstream": "origin/main",
                        "upstream_sha": baseline,
                    }
                },
                "conflicts": {},
            }
        )
    )
    return case


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
            next(
                r
                for r in rows
                if r["kind"] == "verify-command-result" and r["verification_stage"] == "review"
            )["returncode"] = 1
        else:
            next(r for r in reversed(rows) if r["kind"] == "story-escalated")[
                "reason"
            ] = "plugin vetoed pre_commit"
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
    assert task.generation == 1 and task.accepted_dev_session_index == 1
    assert len(task.sessions) == 3 and task.ticket.id == "9"
    engine._finish_inflight()
    assert task.phase == Phase.DONE and task.commit_retry is None
    assert task.sessions == old_records and adapter.sessions == []
    assert task.commit_sha == verify.rev_parse_head(case.root)
    assert (case.root / "implementation.txt").read_text() == "Reviewed implementation.\n"
    assert len([r for r in engine.journal.entries() if r["kind"] == "verify-command-result"]) == 6


def test_delayed_resume_rejects_head_drift(commit_case):
    case = commit_case
    case.arm()
    git(case.root, "commit", "--allow-empty", "-qm", "new unreviewed head")
    drifted = verify.rev_parse_head(case.root)
    engine, adapter, task = _engine(case)
    with pytest.raises(RunPaused, match="HEAD"):
        engine._finish_inflight()
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


def test_changed_ticket_store_refuses_without_rearming(commit_case, monkeypatch):
    case = commit_case
    before = (case.run_dir / "state.json").read_bytes()
    monkeypatch.setattr(
        ticketstore, "store_conflict", lambda *_args, **_kwargs: "ticket store moved"
    )
    with pytest.raises(runs.RearmError, match="ticket store moved"):
        case.arm()
    assert (case.run_dir / "state.json").read_bytes() == before


def test_completed_review_discharges_development_review_request(commit_case):
    case = commit_case
    case.task.sessions[case.task.accepted_dev_session_index].result_json[
        "followup_review_recommended"
    ] = True
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
        else "[verify]\ncommands = " + json.dumps(VERIFY_COMMANDS) + '\n[gates]\nmode = "none"\n'
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


@pytest.mark.parametrize("change", ["commit", "merge"])
def test_finish_inflight_refuses_unverified_head_even_when_attested(commit_case, change):
    """A supplied SHA cannot bless work added after the preserved review."""
    case = commit_case
    reviewed_head = case.head
    branch = git(case.root, "branch", "--show-current")
    if change == "merge":
        git(case.root, "checkout", "-qb", "unverified-main", case.task.baseline_commit)
        (case.root / "main-feature.txt").write_text("Untested main-side change.\n")
    else:
        (case.root / "implementation.txt").write_text("Untested replacement.\n")
    git(case.root, "add", "-A")
    git(case.root, "commit", "-qm", "unverified change after review")
    if change == "merge":
        git(case.root, "checkout", "-q", branch)
        git(case.root, "merge", "--no-ff", "-qm", "unverified main receive", "unverified-main")
    case.head = git(case.root, "rev-parse", "HEAD")
    assert case.head != reviewed_head
    assert not verify.dirty_paths(case.root)
    before = (case.run_dir / "state.json").read_bytes()
    with pytest.raises((runs.RearmError, RunPaused), match="verification source"):
        # Exercise the complete preserved COMMITTING path if admission regresses.
        case.arm()
        engine, adapter, task = _engine(case)
        engine._finish_inflight()
    assert (case.run_dir / "state.json").read_bytes() == before
    assert verify.rev_parse_head(case.root) == case.head


@pytest.mark.parametrize(
    "change,reason",
    [
        ("later_session", "another session ran after this review"),
        ("missing_dev_decision", "no successful decision belongs to the accepted development"),
        ("later_verify_failure", "a verification failure remains after the review"),
        ("stale_review_generation", "the latest review did not converge on this attempt"),
    ],
)
def test_preserved_review_guard_refuses_without_mutation(commit_case, change, reason):
    case = commit_case
    rows = _journal_rows(case)
    if change == "later_session":
        rows.append(
            {
                "kind": "session-start",
                "story_key": case.task.story_key,
                "task_id": "205.9-dev-2-g1",
                "role": "dev",
            }
        )
    elif change == "missing_dev_decision":
        accepted = case.task.sessions[case.task.accepted_dev_session_index]
        rows = [
            r
            for r in rows
            if not (r["kind"] == "dev-decision" and r.get("log_task") == accepted.task_id)
        ]
    elif change == "later_verify_failure":
        rows.append(
            {
                "kind": "review-verify-failed",
                "story_key": case.task.story_key,
                "reason": "later verifier failed",
            }
        )
    else:
        next(r for r in rows if r["kind"] == "review-result")["log_task"] = "205.9-review-1"
    _replace_journal(case, rows)
    before = (case.run_dir / "state.json").read_bytes()
    with pytest.raises(runs.RearmError, match=reason):
        case.arm()
    assert (case.run_dir / "state.json").read_bytes() == before
    assert verify.rev_parse_head(case.root) == case.head
