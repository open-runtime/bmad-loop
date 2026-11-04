"""Fork review resumption must preserve session-authored ledger ownership."""

from conftest import dev_effect, git, review_effect, write_ledger, write_sprint
from test_engine import make_engine
from test_fork_session_resume import _policy, _resumable

from bmad_loop import deferredwork
from bmad_loop.adapters.base import SessionResult
from bmad_loop.policy import ReviewPolicy


def test_resumed_review_does_not_commit_its_unfinished_ledger_edit(project):
    """The resumed reviewer sees its original HEAD and uncommitted ledger edit.

    The first review launch may commit engine bookkeeping. A later launch that
    resumes the same conversation must not attribute that reviewer's unfinished
    ledger edits to the engine merely because its role is still ``review``.
    """
    write_sprint(project, {"epic-1": "backlog", "1-1-a": "ready-for-dev"})
    write_ledger(project, {"DW-0": "done"})
    ledger_rel = project.deferred_work.relative_to(project.project).as_posix()
    seen = {}

    def author_then_stall(spec):
        seen["before_author"] = git(project.project, "rev-parse", "HEAD")
        deferredwork.append_entry(
            project.deferred_work,
            title="Unfinished reviewer-owned ledger edit",
            origin="the interrupted reviewer",
            source_spec="specs/older.md",
            reason="draft finding, not engine bookkeeping",
        )
        transcript = project.project / "resumable-review-ledger.jsonl"
        transcript.write_text("{}\n", encoding="utf-8")
        return SessionResult(
            status="stalled",
            session_id="review-ledger-session",
            transcript_path=str(transcript),
        )

    finish = review_effect(project, "1-1-a", clean=True)

    def observe_resume(spec):
        seen["resume_id"] = spec.resume_session_id
        seen["resume_head"] = git(project.project, "rev-parse", "HEAD")
        seen["resume_dirt"] = git(project.project, "status", "--porcelain", "--", ledger_rel)
        seen["committed_ledger"] = git(project.project, "show", f"HEAD:{ledger_rel}")
        seen["working_ledger"] = project.deferred_work.read_text(encoding="utf-8")
        return finish(spec)

    engine, adapter = make_engine(
        project,
        [dev_effect(project, "1-1-a"), author_then_stall, observe_resume],
        policy=_policy(review=ReviewPolicy(enabled=True, trigger="always")),
    )
    _resumable(adapter)

    summary = engine.run()

    assert summary.done == 1 and not summary.paused
    assert [spec.role for spec in adapter.sessions] == ["dev", "review", "review"]
    assert seen["resume_id"] == "review-ledger-session"
    assert "Unfinished reviewer-owned ledger edit" in seen["working_ledger"]
    assert seen["resume_dirt"] != ""
    assert seen["resume_head"] == seen["before_author"]
    assert "Unfinished reviewer-owned ledger edit" not in seen["committed_ledger"]
