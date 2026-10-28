"""open-runtime fork: the wait loop's smart stall check (F4), turn-error classes
(F3, F5), hcom nudges (F7) and session resume launch (F6).

Driven like the parked-prompt tests in test_generic_tmux.py: the real
`_wait_loop` over a scripted watcher, a `_UnitMux` that records nudges and serves
a stubbed screen, and a frozen clock the watcher advances on idle ticks. The
session's transcript is a real file the hook events name, so the gate reads the
last turn exactly as it does in production.
"""

from __future__ import annotations

import json
import shlex
from pathlib import Path

import pytest
from test_generic_tmux import (
    _dev_handle,
    _dev_spec,
    _frozen_stall_clock,
    _hook_event,
    _lifecycle_events,
    _UnitMux,
    make_adapter,
    make_dev_adapter,
)

from bmad_loop.adapters import generic, hcomnudge
from bmad_loop.adapters.base import SessionResult, SessionSpec
from bmad_loop.adapters.hcomnudge import HcomOutcome
from bmad_loop.adapters.profile import get_profile
from bmad_loop.escalation import Action, decide_dev, pause_title
from bmad_loop.model import StoryTask, TokenUsage
from bmad_loop.policy import LimitsPolicy, Policy, RecoveryPolicy
from bmad_loop.signals import HookEvent

STATUS_REPORT = "Tests pass. The build is queued behind the shared build lock."
QUESTION = "Two designs fit.\n\nShould I keep the old schema or migrate it?"


def _claude_text(text):
    return {
        "type": "assistant",
        "message": {"role": "assistant", "content": [{"type": "text", "text": text}]},
    }


def _claude_tool(call_id, name):
    return {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": call_id, "name": name}],
        },
    }


API_ERROR = {
    "type": "assistant",
    "isApiErrorMessage": True,
    "error": "server_error",
    "message": {
        "role": "assistant",
        "content": [{"type": "text", "text": "API Error: Connection lost mid-response."}],
    },
}
POOL_ERROR = {
    "type": "assistant",
    "isApiErrorMessage": True,
    "error": "rate_limit",
    "message": {
        "role": "assistant",
        "content": [
            {
                "type": "text",
                "text": "API Error: Request rejected (429) · Local TeamRouter pool unavailable "
                "for claude-fable-5-1. Earliest estimated usable return: 2026-10-28T09:12:00Z",
            }
        ],
    },
}


def _transcript(tmp_path, entries, name="t.jsonl") -> str:
    path = tmp_path / name
    path.write_text("".join(json.dumps(e) + "\n" for e in entries), encoding="utf-8")
    return str(path)


def _codex_error_rollout(tmp_path, message) -> str:
    entries = [
        {"type": "event_msg", "payload": {"type": "task_started"}},
        {
            "type": "event_msg",
            "payload": {
                "type": "task_complete",
                "last_agent_message": None,
                "error": {"message": message, "codex_error_info": "other"},
            },
        },
    ]
    return _transcript(tmp_path, entries, name="rollout.jsonl")


def _event(kind, *, transcript=None, notification_type=None, **fields):
    return HookEvent(
        ts=1,
        event=kind,
        task_id="3-1-dev-1",
        session_id="sess",
        transcript_path=transcript,
        path=Path("x"),
        notification_type=notification_type,
        **fields,
    )


def _fork_adapter(
    tmp_path,
    monkeypatch,
    events,
    *,
    profile="claude",
    nudges=2,
    tick=11.0,
    cap=None,
    grace=10.0,
    **recovery,
):
    """A dev adapter under `[recovery]` ``recovery``, a 10 s grace, ``nudges``
    wake nudges, and a frozen clock the watcher advances ``tick`` seconds on every
    idle tick (11 s = past the grace each time)."""
    monkeypatch.setattr(generic, "RESULT_GRACE_S", 0.0)
    monkeypatch.setattr(generic, "RESULT_POLL_S", 0.0)
    mux = _UnitMux()
    policy = Policy(limits=LimitsPolicy(), recovery=RecoveryPolicy(**recovery))
    adapter, _ = make_dev_adapter(tmp_path, profile_name=profile, policy=policy, mux=mux)
    adapter._stall_grace_s = grace
    adapter._stall_nudges = nudges
    adapter._window_alive = lambda handle: True
    clock = _frozen_stall_clock(monkeypatch)
    script = list(events)
    calls = {"n": 0}

    class _Watcher:
        def wait_for(self, task_id, kinds, timeout_s, since_ns=0):
            calls["n"] += 1
            event = script.pop(0) if script else None
            if event is None:
                clock["t"] += tick
            return event

    adapter.watcher = _Watcher()
    spec = _dev_spec(tmp_path)
    if cap is not None:
        import dataclasses

        spec = dataclasses.replace(spec, stall_nudges_cap=cap)
    return adapter, mux, spec, calls


def _sent(mux):
    return [text for _, text in mux.sent]


# ---------------------------------------------------------------- F4 smart


def test_smart_idle_prompt_after_a_status_report_nudges_in_place(tmp_path, monkeypatch):
    """The 16a/17.1 case on Claude: a result-less turn ends on a status report,
    `idle_prompt` latches, the grace expires. Upstream parked it; the smart check
    nudges it to continue in the same session (twice here, then stalled — the
    engine retries or resumes it), never parked.

    Ablation: set `idle_latch = False` in the gate and the latch parks it again."""
    t = _transcript(tmp_path, [_claude_text(STATUS_REPORT)])
    adapter, mux, spec, _ = _fork_adapter(
        tmp_path,
        monkeypatch,
        [
            _event("Stop", transcript=t),
            _event("Notification", transcript=t, notification_type="idle_prompt"),
        ],
        stall_check="smart",
    )
    result = adapter.wait_for_completion(_dev_handle(), spec)

    assert (result.status, result.parked) == ("stalled", False)
    assert _sent(mux) == [generic.CONTINUE_NUDGE_TEXT] * 2
    assert _lifecycle_events(adapter, "idle-prompt-not-parked")
    assert [e["kind"] for e in _lifecycle_events(adapter, "nudge-sent")] == ["continue", "continue"]


def test_classic_check_keeps_the_upstream_park(tmp_path, monkeypatch):
    t = _transcript(tmp_path, [_claude_text(STATUS_REPORT)])
    adapter, mux, spec, _ = _fork_adapter(
        tmp_path,
        monkeypatch,
        [
            _event("Stop", transcript=t),
            _event("Notification", transcript=t, notification_type="idle_prompt"),
        ],
    )
    result = adapter.wait_for_completion(_dev_handle(), spec)

    assert (result.status, result.parked) == ("stalled", True)
    assert result.parked_evidence == "Notification(idle_prompt) -> IdlePrompt"
    assert _sent(mux) == []


def test_smart_idle_prompt_after_a_question_parks_with_the_question(tmp_path, monkeypatch):
    t = _transcript(tmp_path, [_claude_text(QUESTION)])
    adapter, mux, spec, _ = _fork_adapter(
        tmp_path,
        monkeypatch,
        [
            _event("Stop", transcript=t),
            _event("Notification", transcript=t, notification_type="idle_prompt"),
        ],
        stall_check="smart",
    )
    result = adapter.wait_for_completion(_dev_handle(), spec)

    assert (result.status, result.parked) == ("stalled", True)
    assert result.parked_evidence == (
        "Notification(idle_prompt) -> IdlePrompt; asked: "
        "Should I keep the old schema or migrate it?"
    )
    assert _sent(mux) == []
    decision = decide_dev(StoryTask(story_key="3-1", epic=3, attempt=1), result, None, Policy())
    assert decision.action == Action.PAUSE and decision.reason.startswith("parked: ")


def test_smart_question_without_a_latch_parks(tmp_path, monkeypatch):
    """Codex has no idle notification: a turn whose Stop message asks a question
    parks at the grace expiry instead of being nudged past it."""
    adapter, mux, spec, _ = _fork_adapter(
        tmp_path,
        monkeypatch,
        [_event("Stop", last_assistant_message="Which migration should I keep?")],
        profile="codex",
        stall_check="smart",
    )
    result = adapter.wait_for_completion(_dev_handle(), spec)

    assert result.parked and result.parked_evidence == "question: Which migration should I keep?"
    assert _sent(mux) == []


def test_smart_pending_tool_gets_the_targeted_nudge(tmp_path, monkeypatch):
    t = _transcript(tmp_path, [_claude_text("Running the suite."), _claude_tool("t1", "Bash")])
    adapter, mux, spec, _ = _fork_adapter(
        tmp_path,
        monkeypatch,
        [_event("Notification", transcript=t, notification_type="idle_prompt")],
        stall_check="smart",
        nudges=1,
    )
    adapter.wait_for_completion(_dev_handle(), spec)

    assert _sent(mux) == [generic.PENDING_NUDGE_TEXT.format(pending="Bash")]


def test_smart_background_task_label_comes_from_the_stop(tmp_path, monkeypatch):
    adapter, mux, spec, _ = _fork_adapter(
        tmp_path,
        monkeypatch,
        [
            _event(
                "Stop", last_assistant_message="Waiting.", background_tasks=("cargo nextest run",)
            )
        ],
        stall_check="smart",
        nudges=1,
    )
    adapter.wait_for_completion(_dev_handle(), spec)

    assert _sent(mux) == [generic.PENDING_NUDGE_TEXT.format(pending="cargo nextest run")]


def test_smart_still_parks_a_permission_prompt(tmp_path, monkeypatch):
    adapter, mux, spec, _ = _fork_adapter(
        tmp_path,
        monkeypatch,
        [_hook_event("Notification", "permission_prompt")],
        stall_check="smart",
    )
    result = adapter.wait_for_completion(_dev_handle(), spec)

    assert result.parked and result.parked_evidence.endswith("PermissionPrompt")
    assert _sent(mux) == []


def test_smart_nudges_respect_the_cap(tmp_path, monkeypatch):
    """Every in-place nudge is a stall nudge attempt: the never-restored cap still
    ends a session that keeps answering without finishing."""
    t = _transcript(tmp_path, [_claude_text(STATUS_REPORT)])
    events = []
    for _ in range(5):
        events += [_event("Stop", transcript=t), None]
    adapter, mux, spec, _ = _fork_adapter(
        tmp_path, monkeypatch, events, stall_check="smart", nudges=1, cap=3
    )
    result = adapter.wait_for_completion(_dev_handle(), spec)

    assert result.status == "stalled"
    assert len(_sent(mux)) == 3


def test_turn_end_nudge_s_nudges_a_status_report_sooner(tmp_path, monkeypatch):
    """With `turn_end_nudge_s` the first in-place nudge after a status-report Stop
    comes at that delay (here one 6 s tick), not after the 10 s grace (two ticks)."""

    def ticks_to_first_nudge(**recovery):
        t = _transcript(tmp_path, [_claude_text(STATUS_REPORT)], name=f"t{len(recovery)}.jsonl")
        adapter, mux, spec, calls = _fork_adapter(
            tmp_path / f"run{len(recovery)}",
            monkeypatch,
            [_event("Stop", transcript=t, last_assistant_message=STATUS_REPORT)],
            stall_check="smart",
            nudges=1,
            tick=6.0,
            **recovery,
        )
        seen = {}
        inner = mux.send_text

        def send_text(window, text):
            seen.setdefault("calls", calls["n"])
            inner(window, text)

        mux.send_text = send_text
        adapter.wait_for_completion(_dev_handle(), spec)
        return seen["calls"]

    (tmp_path / "run0").mkdir()
    (tmp_path / "run1").mkdir()
    slow = ticks_to_first_nudge()
    fast = ticks_to_first_nudge(turn_end_nudge_s=5)
    assert fast < slow


# ------------------------------------------------------------- F3/F5 errors


def test_transport_error_is_nudged_once_then_pauses_uncharged(tmp_path, monkeypatch):
    """11.1-dev-1: the turn ended on `API Error: Connection lost mid-response`.
    One in-place transport nudge; the error stays, so the session ends with a
    transport fault the deciders pause uncharged as `transport fault:`."""
    t = _transcript(tmp_path, [_claude_text("Writing the plan."), API_ERROR])
    adapter, mux, spec, _ = _fork_adapter(
        tmp_path,
        monkeypatch,
        [_event("Notification", transcript=t, notification_type="idle_prompt")],
        classify_turn_errors=True,
    )
    handle = _dev_handle()
    result = adapter.wait_for_completion(handle, spec)

    assert _sent(mux) == [
        generic.TRANSPORT_NUDGE_TEXT.format(error="API Error: Connection lost mid-response.")
    ]
    assert (result.status, result.transport_fault) == ("stalled", "transport")
    assert result.transport_evidence == "API Error: Connection lost mid-response."
    classified = adapter._classify_env_fault(handle, spec, result)
    assert classified.env_fault and classified.env_fault_overruled is None
    decision = decide_dev(StoryTask(story_key="3-1", epic=3, attempt=1), classified, None, Policy())
    assert decision.action == Action.PAUSE
    assert decision.reason.startswith("transport fault: dev session stalled (API Error")
    assert pause_title(decision.reason, "3-1") == "Paused (transport fault): 3-1"


def test_pool_refusal_pauses_at_once_with_its_return(tmp_path, monkeypatch):
    t = _transcript(tmp_path, [POOL_ERROR])
    adapter, mux, spec, _ = _fork_adapter(
        tmp_path,
        monkeypatch,
        [_event("Notification", transcript=t, notification_type="idle_prompt")],
        classify_turn_errors=True,
    )
    handle = _dev_handle()
    result = adapter._classify_env_fault(handle, spec, adapter.wait_for_completion(handle, spec))

    assert _sent(mux) == []
    assert (result.transport_fault, result.pool_return) == ("pool", "2026-10-28T09:12:00Z")
    reason = decide_dev(
        StoryTask(story_key="3-1", epic=3, attempt=1), result, None, Policy()
    ).reason
    assert reason.startswith("pool unavailable: dev session stalled (")
    assert "return 2026-10-28T09:12:00Z" in reason


def test_codex_environment_error_turn_pauses_without_a_nudge(tmp_path, monkeypatch):
    """16.1-dev-1: Codex fired no hook after `failed to load workspace
    requirements`; the session is decided from the rollout, with no nudge into a
    config a nudge cannot fix."""
    rollout = _codex_error_rollout(tmp_path, "failed to load workspace requirements")
    adapter, mux, spec, _ = _fork_adapter(
        tmp_path,
        monkeypatch,
        [_event("SessionStart", transcript=rollout)],
        profile="codex",
        classify_turn_errors=True,
    )
    result = adapter.wait_for_completion(_dev_handle(), spec)

    assert _sent(mux) == []
    assert result.transport_fault == "environment"
    assert result.transport_evidence == "failed to load workspace requirements"


def test_the_heartbeat_decides_an_error_turn_before_the_grace(tmp_path, monkeypatch):
    """The rollout scan on the heartbeat (every 30 s) opens the gate as soon as an
    environment error turn shows, instead of after the grace (here 100 s, in 5 s
    ticks: about 7 watcher calls instead of more than 20)."""
    rollout = _codex_error_rollout(tmp_path, "failed to load workspace requirements")
    adapter, mux, spec, calls = _fork_adapter(
        tmp_path,
        monkeypatch,
        [_event("SessionStart", transcript=rollout)],
        profile="codex",
        classify_turn_errors=True,
        tick=5.0,
        grace=100.0,
    )
    result = adapter.wait_for_completion(_dev_handle(), spec)

    assert result.transport_fault == "environment"
    assert calls["n"] <= 10
    rows = _lifecycle_events(adapter, "turn-error")
    assert [(r["turn_error"], r["source"]) for r in rows] == [("environment", "transcript")]


def test_errors_are_not_classified_by_default(tmp_path, monkeypatch):
    rollout = _codex_error_rollout(tmp_path, "failed to load workspace requirements")
    adapter, mux, spec, _ = _fork_adapter(
        tmp_path, monkeypatch, [_event("SessionStart", transcript=rollout)], profile="codex"
    )
    result = adapter.wait_for_completion(_dev_handle(), spec)

    assert result.transport_fault is None
    assert _sent(mux) == [generic.STALL_NUDGE_TEXT] * 2


def test_stop_failure_hook_classifies_without_a_transcript(tmp_path, monkeypatch):
    adapter, mux, spec, _ = _fork_adapter(
        tmp_path,
        monkeypatch,
        [
            _event(
                "StopFailure",
                error="rate_limit",
                error_details="429 Too Many Requests",
                last_assistant_message="API Error: Request rejected (429)",
            )
        ],
        classify_turn_errors=True,
    )
    result = adapter.wait_for_completion(_dev_handle(), spec)

    assert result.transport_fault == "pool"
    assert result.transport_evidence == "429 Too Many Requests"


def test_stop_failure_hook_is_inert_without_classification(tmp_path, monkeypatch):
    adapter, mux, spec, _ = _fork_adapter(
        tmp_path, monkeypatch, [_event("StopFailure", error="server_error")]
    )
    result = adapter.wait_for_completion(_dev_handle(), spec)

    assert result.transport_fault is None
    assert _sent(mux) == [generic.STALL_NUDGE_TEXT] * 2


# ---------------------------------------------------------------- F7 hcom


def test_nudges_go_over_hcom_when_the_session_is_in_a_room(tmp_path, monkeypatch):
    sent: list[tuple] = []

    def fake_send(name, room, text, *, thread, **_):
        sent.append((name, room, text, thread))
        return HcomOutcome(True, "sent")

    monkeypatch.setattr(generic, "send_nudge", fake_send)
    adapter, mux, spec, _ = _fork_adapter(
        tmp_path,
        monkeypatch,
        [_event("SessionStart", hcom_name="gari", hcom_dir="/rooms/rem/hcom")],
        nudge_via="hcom",
        nudges=1,
    )
    adapter.wait_for_completion(_dev_handle(), spec)

    assert _sent(mux) == []
    assert sent == [
        ("gari", "/rooms/rem/hcom", generic.STALL_NUDGE_TEXT, f"bmad-loop-{adapter.run_dir.name}")
    ]
    assert _lifecycle_events(adapter, "nudge-via-hcom")


def test_an_hcom_miss_falls_back_to_tmux(tmp_path, monkeypatch):
    monkeypatch.setattr(generic, "send_nudge", lambda *a, **k: HcomOutcome(False, "status:active"))
    adapter, mux, spec, _ = _fork_adapter(
        tmp_path,
        monkeypatch,
        [_event("SessionStart", hcom_name="galo")],
        nudge_via="hcom",
        nudges=1,
    )
    adapter.wait_for_completion(_dev_handle(), spec)

    assert _sent(mux) == [generic.STALL_NUDGE_TEXT]
    (row,) = _lifecycle_events(adapter, "nudge-hcom-fallback")
    assert row["reason"] == "status:active"


def test_no_hcom_identity_falls_back_to_tmux(tmp_path, monkeypatch):
    monkeypatch.setattr(generic, "send_nudge", lambda *a, **k: pytest.fail("no identity"))
    adapter, mux, spec, _ = _fork_adapter(tmp_path, monkeypatch, [], nudge_via="hcom", nudges=1)
    adapter.wait_for_completion(_dev_handle(), spec)

    assert _sent(mux) == [generic.STALL_NUDGE_TEXT]
    assert _lifecycle_events(adapter, "nudge-hcom-fallback")[0]["reason"] == "no-identity"


class _Proc:
    def __init__(self, rc=0, out="", err=""):
        self.returncode, self.stdout, self.stderr = rc, out, err


def test_hcom_send_nudge_checks_listening_then_sends_a_request(monkeypatch):
    calls: list[tuple[list[str], dict]] = []
    monkeypatch.setenv("HCOM_INSTANCE_NAME", "the-orchestrators-own")

    def runner(argv, **kw):
        calls.append((argv, kw["env"]))
        if argv[1] == "list":
            return _Proc(out=json.dumps({"name": "gari", "status": "listening"}))
        return _Proc()

    outcome = hcomnudge.send_nudge(
        "gari",
        "/rooms/x",
        "continue please",
        thread="bmad-loop-r1",
        binary="/bin/hcom",
        runner=runner,
    )

    assert outcome == HcomOutcome(True, "sent")
    (list_argv, list_env), (send_argv, send_env) = calls
    assert list_argv == ["/bin/hcom", "list", "gari", "--json"]
    assert send_argv == [
        "/bin/hcom",
        "send",
        "@gari",
        "--intent",
        "request",
        "--thread",
        "bmad-loop-r1",
        "--from",
        "bmad-loop",
        "--quiet",
        "--",
        "continue please",
    ]
    for env in (list_env, send_env):
        assert env["HCOM_DIR"] == "/rooms/x"
        assert "HCOM_INSTANCE_NAME" not in env


@pytest.mark.parametrize(
    ("listing", "expected"),
    [
        (_Proc(out=json.dumps({"status": "active"})), "status:active"),
        (_Proc(out=json.dumps([{"name": "gari", "status": "blocked"}])), "status:blocked"),
        (_Proc(rc=1, err="no such agent"), "list-failed: rc 1: no such agent"),
        (_Proc(out="not json"), "list-failed: not JSON"),
    ],
)
def test_hcom_send_nudge_refuses_anything_but_listening(listing, expected):
    calls = []

    def runner(argv, **kw):
        calls.append(argv)
        return listing

    outcome = hcomnudge.send_nudge("gari", None, "x", thread="t", binary="/bin/hcom", runner=runner)
    assert outcome == HcomOutcome(False, expected)
    assert all(argv[1] == "list" for argv in calls)


def test_hcom_send_nudge_without_a_binary(monkeypatch):
    monkeypatch.setattr(hcomnudge.shutil, "which", lambda name: None)
    assert hcomnudge.send_nudge("gari", None, "x", thread="t") == HcomOutcome(False, "no-binary")


def test_hcom_send_failure_is_reported(monkeypatch):
    def runner(argv, **kw):
        if argv[1] == "list":
            return _Proc(out=json.dumps({"status": "listening"}))
        return _Proc(rc=2, err="room locked")

    outcome = hcomnudge.send_nudge("gari", None, "x", thread="t", binary="/bin/hcom", runner=runner)
    assert outcome == HcomOutcome(False, "send-failed: rc 2: room locked")


# ---------------------------------------------------------------- F6 resume


def _resume_profile(name, **fields):
    import dataclasses

    return dataclasses.replace(get_profile(name), **fields)


def test_claude_resume_argv_takes_the_flag_and_no_new_session_id(tmp_path):
    adapter = make_adapter(tmp_path)
    adapter.profile = _resume_profile("claude", resume_flag="--resume")
    spec = SessionSpec(
        task_id="3-1-dev-2",
        role="dev",
        prompt="Continue the workflow.",
        cwd=tmp_path,
        model="opus",
        resume_session_id="c193f159",
    )
    argv = shlex.split(adapter.build_command(spec, session_id="minted"))

    assert argv[1:4] == ["--resume", "c193f159", "Continue the workflow."]
    assert "--session-id" not in argv and argv[-2:] == ["--model", "opus"]


def test_codex_resume_argv_puts_the_subcommand_first(tmp_path):
    adapter = make_adapter(tmp_path, profile_name="codex")
    adapter.profile = _resume_profile(
        "codex",
        resume_subcommand=("resume",),
        resume_prompt_template="[bmad-loop] {prompt}",
    )
    spec = SessionSpec(
        task_id="3-1-dev-2",
        role="dev",
        prompt="Continue.",
        cwd=tmp_path,
        resume_session_id="01a18313",
    )
    argv = shlex.split(adapter.build_command(spec))
    prof = adapter.profile

    assert argv[1:3] == ["resume", *prof.launch_args][:2]
    assert argv[1 + 1 + len(prof.launch_args) :][:2] == ["01a18313", "[bmad-loop] Continue."]
    assert argv[-len(prof.bypass_args) :] == list(prof.bypass_args)


def test_a_profile_without_resume_keys_launches_fresh(tmp_path):
    adapter = make_adapter(tmp_path)
    spec = SessionSpec(
        task_id="3-1-dev-2",
        role="dev",
        prompt="/bmad-build-auto 3-1",
        cwd=tmp_path,
        resume_session_id="c193f159",
    )
    argv = shlex.split(adapter.build_command(spec, session_id="minted"))
    assert "--resume" not in argv and argv[-2:] == ["--session-id", "minted"]


def test_a_resumed_session_is_charged_only_for_its_own_turns(tmp_path):
    """The resumed conversation appends to the same transcript: usage is read off
    the whole file, so the share held at launch is subtracted."""

    def usage_line(i, o):
        return (
            json.dumps(
                {
                    "type": "assistant",
                    "message": {"id": f"m{i}{o}", "usage": {"input_tokens": i, "output_tokens": o}},
                }
            )
            + "\n"
        )

    path = tmp_path / "t.jsonl"
    path.write_text(usage_line(100, 10), encoding="utf-8")
    adapter = make_adapter(tmp_path)
    adapter.profile = _resume_profile("claude", resume_flag="--resume")
    spec = SessionSpec(
        task_id="3-1-dev-2",
        role="dev",
        prompt="c",
        cwd=tmp_path,
        resume_session_id="s",
        resume_transcript_path=str(path),
    )
    adapter._note_resume_base(spec)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(usage_line(30, 7))

    usage = adapter.read_usage(SessionResult(status="stalled", transcript_path=str(path)))
    assert (usage.input_tokens, usage.output_tokens) == (30, 7)
    assert adapter.read_usage(SessionResult(status="stalled", transcript_path=None)) is None
    assert adapter._rebased("other", TokenUsage(input_tokens=5)) == TokenUsage(input_tokens=5)
