"""Recovery behavior at the open-runtime fork / upstream merge boundaries.

These use scripted adapters and sandbox git repositories, never live model CLIs.
"""

from __future__ import annotations

import dataclasses

import pytest
from conftest import dev_effect, git, write_ledger, write_sprint
from test_engine import _baseline_liar_effect, _harvest_policy, make_engine
from test_fork_session_resume import _resumable
from test_fork_wait_loop import API_ERROR, _fork_adapter, _transcript
from test_generic_tmux import (
    HTTP_429,
    QUOTA_RESETS,
    USAGE_SESSION_LINE,
    _decide_dev_relaunch,
    _dev_handle,
    _dev_spec,
    _drive_quota,
    _lifecycle_events,
    _parked_adapter,
    _quota_adapter,
    _quota_spec,
    _relaunch_spec,
    _stop_failure_event,
)

from bmad_loop import deferredwork, events
from bmad_loop.adapters import generic
from bmad_loop.adapters.base import USAGE_LIMIT_WAIT, SessionResult
from bmad_loop.escalation import Action
from bmad_loop.policy import RecoveryPolicy


def _relay_failure(error: str, message: str):
    """Both upstream stop_* fields and the fork fields the merged relay emits."""
    payload = {"error": error, "last_assistant_message": message}
    return dataclasses.replace(_stop_failure_event(error, message), **events._turn_fields(payload))


def _classify_errors(adapter):
    adapter.policy = dataclasses.replace(
        adapter.policy,
        recovery=RecoveryPolicy(stall_check="smart", classify_turn_errors=True),
    )


@pytest.mark.parametrize("dead", [False, True])
def test_classification_preserves_account_stop_failure(tmp_path, monkeypatch, dead):
    """Account errors retain the upstream human-action label, even after death.

    Removing the upstream dispatcher from the classify=True path loses the
    parked evidence (or routes this as a generic transport/environment error).
    """
    adapter, mux = _parked_adapter(
        tmp_path,
        monkeypatch,
        [_relay_failure("authentication_failed", "Invalid API key")],
        alive=lambda handle: not dead,
    )
    _classify_errors(adapter)

    result = adapter.wait_for_completion(_dev_handle(), _dev_spec(tmp_path))

    assert result.status == ("crashed" if dead else "stalled")
    assert result.parked is True
    assert result.parked_evidence == "StopFailure(authentication_failed): Invalid API key"
    assert result.stop_failure_action == "account"
    assert mux.sent == []


def test_classification_preserves_hook_only_quota_wait(tmp_path, monkeypatch):
    """A readable hook limit must still admit a wait when the pane is empty."""
    q = _quota_adapter(tmp_path, monkeypatch, screen="")
    _classify_errors(q.adapter)

    result = _drive_quota(
        q,
        _quota_spec(tmp_path, timeout_s=120.0, cap_s=200.0),
        events={2: _relay_failure("rate_limit", USAGE_SESSION_LINE)},
    )

    (started,) = _lifecycle_events(q.adapter, "quota-wait-started")
    assert started["evidence"].startswith("StopFailure(rate_limit) message matched")
    assert _lifecycle_events(q.adapter, "timeout-fired") == []
    assert result.status == "stalled" and result.parked
    assert result.usage_limit_class == USAGE_LIMIT_WAIT
    assert result.usage_limit_resets == QUOTA_RESETS
    assert result.quota_wait_s is not None and result.quota_wait_s > 0
    assert q.mux.sent == []


@pytest.mark.parametrize("dead", [False, True])
def test_classification_preserves_unmatched_429_relaunch(tmp_path, monkeypatch, dead):
    """Usage-relaunch mode takes precedence over the fork's generic pool pause."""
    adapter, mux = _parked_adapter(
        tmp_path,
        monkeypatch,
        [_relay_failure("rate_limit", HTTP_429)],
        alive=lambda handle: not dead,
    )
    _classify_errors(adapter)

    result = adapter.wait_for_completion(_dev_handle(), _relaunch_spec(_dev_spec(tmp_path)))

    assert result.status == ("crashed" if dead else "stalled")
    assert result.usage_limited == f"StopFailure(rate_limit): {HTTP_429}"
    assert result.usage_limit_class == USAGE_LIMIT_WAIT
    decision = _decide_dev_relaunch(result)
    assert (decision.action, decision.uncharged) == (Action.RETRY, True)
    assert mux.sent == []


@pytest.mark.parametrize("latest_action", ["transient", "ignore", None])
def test_new_failure_discards_older_pending_wait_line(tmp_path, monkeypatch, latest_action):
    """A superseded hook-only usage line cannot admit a later quota wait."""
    q = _quota_adapter(tmp_path, monkeypatch, screen="")
    _classify_errors(q.adapter)
    if latest_action is not None:
        q.adapter._stop_failure_types = {
            **q.adapter._stop_failure_types,
            "later_error": latest_action,
        }

    result = _drive_quota(
        q,
        _quota_spec(tmp_path, timeout_s=120.0, cap_s=200.0),
        events={
            2: _relay_failure("rate_limit", USAGE_SESSION_LINE),
            3: _relay_failure("later_error", "Connection interrupted"),
        },
    )

    assert len(_lifecycle_events(q.adapter, "stop-failure-quota-wait-pending")) == 1
    assert _lifecycle_events(q.adapter, "quota-wait-started") == []
    assert result.usage_limited is None
    assert not result.quota_wait_s


def test_ignored_failure_is_not_reclassified_from_its_transcript(tmp_path, monkeypatch):
    """Profile ignore applies to the same error in hook and transcript paths."""
    transcript = _transcript(tmp_path, [API_ERROR])
    failure = dataclasses.replace(
        _relay_failure("server_error", "API Error: " "Connection lost mid-response."),
        transcript_path=transcript,
    )
    adapter, mux = _parked_adapter(tmp_path, monkeypatch, [failure])
    _classify_errors(adapter)
    adapter._stop_failure_types = {**adapter._stop_failure_types, "server_error": "ignore"}

    result = adapter.wait_for_completion(_dev_handle(), _dev_spec(tmp_path))

    assert result.transport_fault is None
    assert result.stop_failure_action == "ignore"
    assert [text for _, text in mux.sent] == [generic.CONTINUE_NUDGE_TEXT] * 2


def test_classified_transient_keeps_backoff_and_bounded_nudge(tmp_path, monkeypatch):
    """Canonical upstream fields still enter fork transport recovery exactly once."""
    message = "API Error: " "Connection lost mid-response."
    adapter, mux, spec, _ = _fork_adapter(
        tmp_path,
        monkeypatch,
        [_stop_failure_event("server_error", message)],
        classify_turn_errors=True,
        transport_nudges=1,
        tick=5.0,
        grace=100.0,
    )
    sent_at = []
    send = adapter.send_text

    def record_send(handle, text):
        sent_at.append(generic.time.monotonic())
        return send(handle, text)

    monkeypatch.setattr(adapter, "send_text", record_send)
    started = generic.time.monotonic()

    result = adapter.wait_for_completion(_dev_handle(), spec)

    assert [text for _, text in mux.sent] == [generic.TRANSPORT_NUDGE_TEXT.format(error=message)]
    assert len(sent_at) == 1
    assert sent_at[0] - started >= generic.TURN_ERROR_BACKOFF_S
    assert (result.status, result.transport_fault) == ("stalled", "transport")


def test_resume_after_earlier_rollback_does_not_commit_session_ledger(project):
    """A rollback's one-launch ledger permission must not leak into F6 resume.

    The first attempt fails a non-fixable baseline check. The second authors a
    ledger edit and stalls. Its resumed conversation must find that edit still
    uncommitted; the engine has not restored or harvested it in between.
    """
    write_sprint(project, {"epic-1": "backlog", "1-1-a": "ready-for-dev"})
    write_ledger(project, {"DW-0": "done"})
    ledger_rel = project.deferred_work.relative_to(project.project).as_posix()
    seen = {}

    def author_then_stall(spec):
        seen["before_author"] = git(project.project, "rev-parse", "HEAD")
        deferredwork.append_entry(
            project.deferred_work,
            title="Interrupted session owns this edit",
            origin="the second session",
            source_spec="specs/older.md",
            reason="unfinished session-authored work",
        )
        transcript = project.project / "resumable-ledger-session.jsonl"
        transcript.write_text("{}\n", encoding="utf-8")
        return SessionResult(
            status="stalled",
            session_id="ledger-session",
            transcript_path=str(transcript),
        )

    finish = dev_effect(project, "1-1-a", followup_review=False)

    def observe_resume(spec):
        seen["resume_id"] = spec.resume_session_id
        seen["resume_head"] = git(project.project, "rev-parse", "HEAD")
        seen["resume_dirt"] = git(project.project, "status", "--porcelain", "--", ledger_rel)
        seen["committed_ledger"] = git(project.project, "show", f"HEAD:{ledger_rel}")
        seen["working_ledger"] = project.deferred_work.read_text(encoding="utf-8")
        return finish(spec)

    policy = dataclasses.replace(
        _harvest_policy(attempts=3), recovery=RecoveryPolicy(max_session_resumes=2)
    )
    engine, adapter = make_engine(
        project,
        [_baseline_liar_effect(project), author_then_stall, observe_resume],
        policy=policy,
    )
    _resumable(adapter)

    summary = engine.run()

    assert summary.done == 1 and not summary.paused
    assert len(adapter.sessions) == 3
    assert seen["resume_id"] == "ledger-session"
    assert "Interrupted session owns this edit" in seen["working_ledger"]
    assert seen["resume_dirt"] != ""
    assert seen["resume_head"] == seen["before_author"]
    assert "Interrupted session owns this edit" not in seen["committed_ledger"]
