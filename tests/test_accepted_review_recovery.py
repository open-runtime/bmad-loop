"""Explicit recovery of accepted implementation already inside a later baseline.

The sandbox mirrors a pool-failed primary, a generic re-arm, a plan-only retry,
and a stopped second dev with no SessionRecord. No live run is changed.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from pathlib import Path

import pytest
from conftest import EntrySpec, EpicSpec, git, write_plan, write_ticket_tree

from bmad_loop import accepted_review, bmadconfig, platform_util, runs, verify
from bmad_loop.journal import Journal, load_state, save_state
from bmad_loop.model import Phase, RunState, SessionRecord, StoryTask, TicketBinding

KEY = "801.3"
EPIC = "epic-docs"


@dataclass
class AcceptedImplementation:
    root: Path
    run_dir: Path
    plan: Path
    state: RunState
    task: StoryTask
    original_baseline: str
    implementation: str

    def refusal(self, implementation: str | None = None) -> str | None:
        return runs.review_implemented_refusal(
            self.state,
            self.task,
            KEY,
            self.implementation if implementation is None else implementation,
            run_dir=self.run_dir,
            project_root=self.root,
        )


def _commit(root: Path, message: str) -> str:
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", message)
    return git(root, "rev-parse", "HEAD")


@pytest.fixture
def accepted_implementation(project_v7):
    root = project_v7.project
    write_ticket_tree(
        project_v7,
        [EpicSpec(801, EPIC, "Docs", (EntrySpec(3, "Crate docs", refined=True),))],
    )
    plan = write_plan(project_v7, EPIC, "story-crate-docs-plan", 3, "ready-for-dev")
    original_baseline = _commit(root, "plan before the original dev")
    (root / "crate-readme.md").write_text("Original accepted implementation.\n", encoding="utf-8")
    plan.write_text(
        f"---\nticket: 3\nstatus: built\nbaseline_revision: {original_baseline}\n"
        "verify_unmet: 1\n---\n\n## Notes\n\nHeld map links remain deferred.\n",
        encoding="utf-8",
    )
    implementation = _commit(root, "original dev implementation")
    (root / "main-readme.md").write_text("Later main receipt.\n", encoding="utf-8")
    baseline = _commit(root, "receive main before generic re-arm")

    def result(attempt_baseline: str) -> dict:
        return {
            "workflow": "auto-dev",
            "story_key": KEY,
            "spec_file": str(plan),
            "baseline_commit": attempt_baseline,
            "status": "built",
            "escalations": [],
            "park_asserted": False,
            "artifact_only": False,
            "followup_review_recommended": False,
        }

    task = StoryTask(
        KEY,
        801,
        phase=Phase.DEV_RUNNING,
        attempt=2,
        generation=1,
        baseline_commit=baseline,
        baseline_untracked=[],
        spec_file=str(plan),
        dispatched_spec_file=str(plan),
        dispatched_spec_snapshot=plan.read_bytes(),
        accepted_dev_session_index=0,
        resolved_redrive=True,
        ticket=TicketBinding.from_dict(
            {"ref": KEY, "id": 3, "epic_slug": EPIC, "risk": "low", "plan": str(plan)}
        ),
        sessions=[
            SessionRecord(
                task_id=f"{KEY}-dev-1",
                role="dev",
                status="completed",
                adapter="codex",
                session_id="original-native",
                result_json=result(original_baseline),
            ),
            SessionRecord(
                task_id=f"{KEY}-review-1",
                role="review",
                status="stalled",
                adapter="claude",
                session_id="pool-review-native",
                ended="pool",
            ),
            SessionRecord(
                task_id=f"{KEY}-dev-1-g1",
                role="dev",
                status="completed",
                adapter="codex",
                session_id="plan-only-native",
                result_json=result(baseline),
            ),
        ],
    )
    state = RunState(
        run_id="accepted-review",
        project=str(root),
        repo_root=str(root),
        started_at="2026-11-05T06:00:00Z",
        source="tickets",
        stopped=True,
        tasks={KEY: task},
    )
    run_dir = root / ".bmad-loop" / "runs" / state.run_id
    save_state(run_dir, state)
    state.run_dir_identity = platform_util.root_identity_record(run_dir)
    save_state(run_dir, state)
    journal = Journal(run_dir, pin=state)
    journal.set_active_log(f"{KEY}-dev-1")
    journal.append("session-start", task_id=f"{KEY}-dev-1", role="dev")
    journal.append("session-bound", task_id=f"{KEY}-dev-1", session_id="original-native")
    journal.append("session-end", task_id=f"{KEY}-dev-1", status="completed")
    journal.append(
        "dev-decision",
        story_key=KEY,
        attempt=1,
        session_status="completed",
        action="proceed",
        produced_work=True,
        env_fault=False,
    )
    journal.set_active_log(f"{KEY}-review-1")
    journal.append("session-start", task_id=f"{KEY}-review-1", role="review")
    journal.append("session-bound", task_id=f"{KEY}-review-1", session_id="pool-review-native")
    journal.append("session-end", task_id=f"{KEY}-review-1", status="stalled")
    journal.append("story-escalation-resolved", story_key=KEY, baseline=baseline)
    journal.set_active_log(f"{KEY}-dev-1-g1")
    journal.append("session-start", task_id=f"{KEY}-dev-1-g1", role="dev")
    journal.append("session-bound", task_id=f"{KEY}-dev-1-g1", session_id="plan-only-native")
    journal.append("session-end", task_id=f"{KEY}-dev-1-g1", status="completed")
    journal.append(
        "dev-decision",
        story_key=KEY,
        attempt=1,
        session_status="completed",
        action="retry",
        reason="no changes in worktree since baseline commit",
        produced_work=True,
        env_fault=False,
    )
    journal.set_active_log(f"{KEY}-dev-2-g1")
    journal.append("session-start", task_id=f"{KEY}-dev-2-g1", role="dev")
    journal.append("session-bound", task_id=f"{KEY}-dev-2-g1", session_id="stopped-native")
    journal.append("session-end", task_id=f"{KEY}-dev-2-g1", status="aborted", error="RunStopped")
    journal.append("run-stop")
    # The stopped current dev has no completed record. Its only residue is the
    # bound plan; source and historical sessions are still the accepted bytes.
    plan.write_text(
        plan.read_text(encoding="utf-8")
        .replace("status: built", "status: in-progress")
        .replace(original_baseline, baseline),
        encoding="utf-8",
    )
    return AcceptedImplementation(
        root, run_dir, plan, state, task, original_baseline, implementation
    )


def test_explicit_recovery_accepts_retained_implementation_without_inventing_dev_end(
    accepted_implementation,
):
    case = accepted_implementation
    before = copy.deepcopy(case.state.to_dict())
    assert len(case.task.sessions) == 3
    assert case.task.sessions[-1].task_id == f"{KEY}-dev-1-g1"
    assert (
        git(
            case.root, "merge-base", "--is-ancestor", case.implementation, case.task.baseline_commit
        )
        == ""
    )

    assert case.refusal() is None

    assert case.state.to_dict() == before
    assert "status: in-progress" in case.plan.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "fault",
    [
        "unfinished",
        "missing-result",
        "wrong-story",
        "wrong-spec",
        "wrong-baseline",
        "wrong-task",
        "future-generation",
        "later-failed",
    ],
)
def test_explicit_recovery_requires_the_completed_plan_only_attempt(accepted_implementation, fault):
    case = accepted_implementation
    assert case.refusal() is None
    latest = case.task.sessions[-1]
    if fault == "unfinished":
        latest.status = "timeout"
    elif fault == "missing-result":
        latest.result_json = None
    elif fault == "wrong-story":
        latest.result_json["story_key"] = "802.3"
    elif fault == "wrong-spec":
        latest.result_json["spec_file"] = str(case.plan.with_name("other-plan.md"))
    elif fault == "wrong-baseline":
        latest.result_json["baseline_commit"] = case.original_baseline
    elif fault == "wrong-task":
        latest.task_id = "802.3-dev-1-g1"
    elif fault == "future-generation":
        latest.task_id = f"{KEY}-dev-1-g4"
    else:
        case.task.sessions.append(
            SessionRecord(
                task_id=f"{KEY}-dev-2-g1",
                role="dev",
                status="timeout",
                session_id="later-partial-native",
            )
        )
    assert case.refusal() is not None


def test_public_recovery_keeps_source_sessions_acceptance_and_owed_review(accepted_implementation):
    case = accepted_implementation
    source = (case.root / "crate-readme.md").read_bytes()
    plan = case.plan.read_bytes()
    sessions = [s.to_dict() for s in case.task.sessions]
    head = git(case.root, "rev-parse", "HEAD")

    outcome = runs.rearm_for_implemented_review(
        case.run_dir,
        KEY,
        case.implementation,
        project_root=case.root,
    )

    task = load_state(case.run_dir).tasks[KEY]
    assert outcome.story_key == KEY
    assert task.phase == Phase.DEV_VERIFY
    assert task.accepted_dev_session_index == 0
    assert task.generation == case.task.generation + 1
    assert [s.to_dict() for s in task.sessions] == sessions
    assert task.baseline_commit == case.task.baseline_commit
    assert task.commit_sha is None
    assert task.implemented_review["implementation_commit"] == case.implementation
    assert task.implemented_review["head"] == head
    armed = [e for e in Journal(case.run_dir).entries() if e["kind"] == "implemented-review-armed"]
    assert armed[-1]["commit"] == case.implementation
    assert armed[-1]["task_id"] == f"{KEY}-dev-1"
    assert (case.root / "crate-readme.md").read_bytes() == source
    assert case.plan.read_bytes() == plan
    assert git(case.root, "rev-parse", "HEAD") == head


def test_pending_recovery_rechecks_the_completed_plan_only_record(accepted_implementation):
    case = accepted_implementation
    runs.rearm_for_implemented_review(
        case.run_dir, KEY, case.implementation, project_root=case.root
    )
    state = load_state(case.run_dir)
    task = state.tasks[KEY]
    assert (
        RunState.from_dict(state.to_dict()).tasks[KEY].implemented_review == task.implemented_review
    )
    assert accepted_review.pending_review_refusal(state, task, case.run_dir, case.root) is None
    task.sessions[-1].session_id = "substituted-plan-only-native"
    assert accepted_review.pending_review_refusal(state, task, case.run_dir, case.root) is not None


@pytest.mark.parametrize(
    "fault",
    [
        "no-index",
        "out-of-range",
        "review-index",
        "wrong-story",
        "wrong-task",
        "wrong-native",
        "wrong-spec",
        "not-completed",
        "labelled",
        "no-result",
        "malformed-result",
    ],
)
def test_explicit_recovery_requires_this_story_real_accepted_dev(accepted_implementation, fault):
    case = accepted_implementation
    assert case.refusal() is None
    accepted = case.task.sessions[0]
    if fault == "no-index":
        case.task.accepted_dev_session_index = None
    elif fault == "out-of-range":
        case.task.accepted_dev_session_index = 99
    elif fault == "review-index":
        case.task.accepted_dev_session_index = 1
    elif fault == "wrong-story":
        accepted.result_json["story_key"] = "802.3"
    elif fault == "wrong-task":
        accepted.task_id = "802.3-dev-1"
    elif fault == "wrong-native":
        accepted.session_id = "unrelated-native"
    elif fault == "wrong-spec":
        accepted.result_json["spec_file"] = str(case.plan.with_name("other-plan.md"))
    elif fault == "not-completed":
        accepted.status = "timeout"
    elif fault == "labelled":
        accepted.label = "review-lenses.check"
    elif fault == "no-result":
        accepted.result_json = None
    elif fault == "malformed-result":
        accepted.result_json = ["not a result mapping"]
    assert case.refusal() is not None


def test_public_recovery_refusal_leaves_state_source_and_index_unchanged(accepted_implementation):
    case = accepted_implementation
    assert case.refusal() is None
    (case.root / "crate-readme.md").write_text("Unaccepted partial source.\n", encoding="utf-8")
    before = {
        "state": (case.run_dir / "state.json").read_bytes(),
        "journal": (case.run_dir / "journal.jsonl").read_bytes(),
        "plan": case.plan.read_bytes(),
        "source": (case.root / "crate-readme.md").read_bytes(),
        "index": git(case.root, "ls-files", "--stage"),
    }
    with pytest.raises(runs.RearmError):
        runs.rearm_for_implemented_review(
            case.run_dir, KEY, case.implementation, project_root=case.root
        )
    assert (case.run_dir / "state.json").read_bytes() == before["state"]
    assert (case.run_dir / "journal.jsonl").read_bytes() == before["journal"]
    assert case.plan.read_bytes() == before["plan"]
    assert (case.root / "crate-readme.md").read_bytes() == before["source"]
    assert git(case.root, "ls-files", "--stage") == before["index"]


@pytest.mark.parametrize("fault", ["missing", "other-story", "later-generation-only", "unreadable"])
def test_explicit_recovery_requires_proceed_for_the_accepted_session(
    accepted_implementation, fault
):
    case = accepted_implementation
    assert case.refusal() is None
    journal = case.run_dir / "journal.jsonl"
    text = journal.read_text(encoding="utf-8")
    if fault == "missing":
        text = "\n".join(line for line in text.splitlines() if '"dev-decision"' not in line) + "\n"
    elif fault == "other-story":
        text = text.replace('"story_key": "801.3"', '"story_key": "802.3"')
    elif fault == "later-generation-only":
        # Both generations call their first dev "attempt=1". A later PROCEED
        # must never launder an earlier rejected record into accepted evidence.
        text = text.replace('"action": "proceed"', '"action": "retry"')
        text = text.replace('"action": "retry", "reason":', '"action": "proceed", "reason":')
    else:
        # Corruption may hide an intervening dispatch. An earlier PROCEED is
        # insufficient authority when the session chronology cannot be read.
        text = "{unreadable session event\n" + text
    journal.write_text(text, encoding="utf-8")
    assert case.refusal() is not None


@pytest.mark.parametrize("kind", ["empty", "abbreviated", "baseline", "blob", "not-ancestor"])
def test_explicit_recovery_requires_full_reachable_product_commit(accepted_implementation, kind):
    case = accepted_implementation
    assert case.refusal() is None
    if kind == "empty":
        supplied = ""
    elif kind == "abbreviated":
        supplied = case.implementation[:8]
    elif kind == "baseline":
        supplied = case.original_baseline
    elif kind == "blob":
        supplied = git(case.root, "rev-parse", "HEAD:crate-readme.md")
    else:
        # An unattached commit is an object, but is not in the retained baseline.
        supplied = git(
            case.root, "commit-tree", "HEAD^{tree}", "-p", "HEAD", "-m", "unrelated future"
        )
    assert case.refusal(supplied) is not None


@pytest.mark.parametrize("kind", ["tracked", "untracked", "ledger", "reverted-implementation"])
def test_explicit_recovery_cannot_claim_changed_source(accepted_implementation, kind):
    case = accepted_implementation
    assert case.refusal() is None
    product = case.root / "crate-readme.md"
    if kind == "untracked":
        (case.root / "partial.py").write_text("unfinished()\n", encoding="utf-8")
    elif kind == "ledger":
        ledger = bmadconfig.load_paths(case.root).deferred_work
        ledger.parent.mkdir(parents=True, exist_ok=True)
        ledger.write_text("Uncommitted deferred work must be preserved first.\n", encoding="utf-8")
    elif kind == "reverted-implementation":
        product.unlink()
        old_baseline = case.task.baseline_commit
        case.task.baseline_commit = _commit(case.root, "implementation removed before re-arm")
        case.task.sessions[-1].result_json["baseline_commit"] = case.task.baseline_commit
        case.plan.write_text(
            case.plan.read_text(encoding="utf-8").replace(old_baseline, case.task.baseline_commit),
            encoding="utf-8",
        )
    else:
        product.write_text("Partial later dev change.\n", encoding="utf-8")
    assert case.refusal() is not None


def test_explicit_recovery_rejects_a_nominated_plan_only_commit(accepted_implementation):
    case = accepted_implementation
    assert case.refusal() is None
    # Even an accepted pointer/PROCEED pair cannot turn a plan-only commit into
    # product evidence. Both revisions exist and ancestry is otherwise valid.
    before = git(case.root, "rev-parse", "HEAD")
    nominated = _commit(case.root, "plan metadata alone")
    case.task.sessions[0].result_json["baseline_commit"] = before
    case.task.baseline_commit = nominated
    case.task.sessions[-1].result_json["baseline_commit"] = nominated
    case.plan.write_text(
        case.plan.read_text(encoding="utf-8").replace(before, nominated), encoding="utf-8"
    )
    refusal = case.refusal(nominated)
    assert refusal is not None
    assert "only plan or ledger" in refusal


def test_explicit_recovery_allows_committed_independent_main_updates(accepted_implementation):
    case = accepted_implementation
    (case.root / "main-readme.md").write_text("Another committed main receipt.\n", encoding="utf-8")
    _commit(case.root, "independent main update after baseline")
    assert case.refusal() is None


@pytest.mark.parametrize(
    "fault",
    ["live", "finished", "isolated", "later-story", "plan-review", "restore", "wrong-ticket"],
)
def test_explicit_recovery_keeps_lifecycle_and_plan_boundaries(accepted_implementation, fault):
    case = accepted_implementation
    assert case.refusal() is None
    if fault == "live":
        case.state.stopped = False
    elif fault == "finished":
        case.state.finished = True
    elif fault == "isolated":
        case.task.worktree_path = str(case.root / "mounted-unit")
    elif fault == "later-story":
        case.state.tasks["801.4"] = StoryTask("801.4", 801, phase=Phase.DONE)
    elif fault == "plan-review":
        case.task.plan_review_owed = True
    elif fault == "restore":
        case.task.restore_patch = "intent-gap.patch"
    else:
        case.plan.write_text(
            case.plan.read_text(encoding="utf-8").replace("ticket: 3", "ticket: 4"),
            encoding="utf-8",
        )
    assert case.refusal() is not None


@pytest.mark.parametrize("evidence_doc", [False, True])
def test_ordinary_proof_accepts_verification_document_but_not_just_plan(
    accepted_implementation,
    evidence_doc,
):
    """A verification-only story's required checked-in evidence is real output.

    This preserves the ordinary no-changes gate without requiring filler code.
    The historical accepted pointer alone cannot excuse a bare plan update.
    """
    case = accepted_implementation
    case.plan.write_text(
        case.plan.read_text(encoding="utf-8").replace("status: in-progress", "status: built"),
        encoding="utf-8",
    )
    if evidence_doc:
        record = case.root / "docs" / "verification.md"
        record.parent.mkdir(exist_ok=True)
        record.write_text(
            f"Verified existing behavior at {case.task.baseline_commit}.\n", encoding="utf-8"
        )
        _commit(case.root, "record verification evidence without product edits")

    outcome = verify.verify_dev_tickets(
        case.task,
        bmadconfig.load_paths(case.root),
        case.task.sessions[-1].result_json,
        plan=case.plan,
        ticket_id="3",
    )

    assert outcome.ok is evidence_doc
    if not evidence_doc:
        assert outcome.reason == "no changes in worktree since baseline commit"
