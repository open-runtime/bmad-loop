"""open-runtime fork: `adapters.turnstate`, the last-turn reader behind the smart
stall check (F4) and the turn-error classes (F3, F5).

Fixtures follow captured records: Claude 11.1-dev-1 transcript line 458 (an
`isApiErrorMessage` entry with `error: "server_error"`), the TeamRouter 429 line
of 1.2-dev-1, and the Codex rollout of 16.1-dev-1 (rollout lines 481-487: a
commentary status message, a `wait_agent` that timed out, then a `task_complete`
whose `error` is "failed to load workspace requirements")."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from bmad_loop.adapters import turnstate
from bmad_loop.adapters.turnstate import asks_question, classify_error, read_turn_state


def _write(path: Path, entries: list[dict]) -> Path:
    path.write_text("".join(json.dumps(e) + "\n" for e in entries), encoding="utf-8")
    return path


def _claude_text(text: str, **extra) -> dict:
    return {
        "type": "assistant",
        "message": {"role": "assistant", "content": [{"type": "text", "text": text}]},
        **extra,
    }


def _claude_tool(call_id: str, name: str) -> dict:
    return {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": call_id, "name": name, "input": {}}],
        },
    }


def _claude_result(call_id: str) -> dict:
    return {
        "type": "user",
        "message": {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": call_id, "content": "ok"}],
        },
    }


def _claude_prompt(text: str) -> dict:
    return {"type": "user", "message": {"role": "user", "content": text}}


API_ERROR_TRANSPORT = {
    "type": "assistant",
    "isApiErrorMessage": True,
    "error": "server_error",
    "message": {
        "role": "assistant",
        "content": [
            {
                "type": "text",
                "text": "API Error: Connection lost mid-response. The response above may "
                "be incomplete.",
            }
        ],
    },
}
POOL_TEXT = (
    "API Error: Request rejected (429) · Local TeamRouter pool unavailable for "
    "claude-fable-5-1. Eligible now: 0 of 28. Earliest estimated usable return: "
    "2026-10-28T09:12:00Z."
)
API_ERROR_POOL = {
    "type": "assistant",
    "isApiErrorMessage": True,
    "error": "rate_limit",
    "message": {"role": "assistant", "content": [{"type": "text", "text": POOL_TEXT}]},
}


# ------------------------------------------------------------ claude-jsonl


def test_claude_status_report_is_an_ended_turn_without_a_question(tmp_path):
    path = _write(
        tmp_path / "t.jsonl",
        [
            _claude_prompt("/bmad-build-auto ticket 16.1"),
            _claude_tool("t1", "Bash"),
            _claude_result("t1"),
            _claude_text("Tests pass. The build is queued behind the shared lock."),
        ],
    )
    state = read_turn_state(path, "claude-jsonl")
    assert state is not None
    assert state.last_message == "Tests pass. The build is queued behind the shared lock."
    assert (state.asks_question, state.pending_tools, state.pending_agent) == (False, (), False)
    assert state.turn_ended and state.error_class is None


def test_claude_question_is_detected(tmp_path):
    path = _write(
        tmp_path / "t.jsonl",
        [_claude_text("Two designs fit.\n\nShould I keep the old schema or migrate it?")],
    )
    assert read_turn_state(path, "claude-jsonl").asks_question


def test_claude_open_tool_and_open_agent_are_pending(tmp_path):
    path = _write(
        tmp_path / "t.jsonl",
        [
            _claude_text("Running the suite."),
            _claude_tool("t1", "Bash"),
            _claude_tool("a1", "Task"),
        ],
    )
    state = read_turn_state(path, "claude-jsonl")
    assert state.pending_tools == ("Bash", "Task")
    assert state.pending_agent and state.pending == "Task"
    assert not state.turn_ended


def test_claude_open_ask_user_question_is_a_question(tmp_path):
    path = _write(tmp_path / "t.jsonl", [_claude_tool("q1", "AskUserQuestion")])
    assert read_turn_state(path, "claude-jsonl").asks_question


def test_claude_api_error_is_a_transport_turn_end(tmp_path):
    """11.1-dev-1 transcript line 458."""
    path = _write(tmp_path / "t.jsonl", [_claude_text("Writing the plan."), API_ERROR_TRANSPORT])
    state = read_turn_state(path, "claude-jsonl")
    assert (state.error_code, state.error_class) == ("server_error", "transport")
    assert state.error_text.startswith("API Error: Connection lost mid-response")
    assert state.turn_ended


def test_claude_pool_refusal_names_its_return(tmp_path):
    path = _write(tmp_path / "t.jsonl", [API_ERROR_POOL])
    state = read_turn_state(path, "claude-jsonl")
    assert state.error_class == "pool"
    assert state.pool_return == "2026-10-28T09:12:00Z"


def test_claude_error_is_spent_by_a_later_message_or_prompt(tmp_path):
    recovered = _write(
        tmp_path / "a.jsonl", [API_ERROR_TRANSPORT, _claude_text("Continuing the plan.")]
    )
    assert read_turn_state(recovered, "claude-jsonl").error_class is None
    nudged = _write(tmp_path / "b.jsonl", [API_ERROR_TRANSPORT, _claude_prompt("continue")])
    assert read_turn_state(nudged, "claude-jsonl").error_class is None


# ------------------------------------------------------------ codex-rollout


def _codex(kind: str, payload: dict) -> dict:
    return {"timestamp": "2026-10-28T15:34:23Z", "type": kind, "payload": payload}


def _codex_16_1() -> list[dict]:
    return [
        _codex("event_msg", {"type": "task_started", "turn_id": "t1"}),
        _codex(
            "response_item",
            {
                "type": "message",
                "role": "assistant",
                "phase": "commentary",
                "content": [
                    {
                        "type": "output_text",
                        "text": "Verification is still waiting on the shared build lock.",
                    }
                ],
            },
        ),
        _codex(
            "response_item",
            {
                "type": "function_call",
                "name": "wait_agent",
                "namespace": "collaboration",
                "arguments": '{"timeout_ms":50000}',
                "call_id": "call_33",
            },
        ),
        _codex(
            "response_item",
            {
                "type": "function_call_output",
                "call_id": "call_33",
                "output": '{"message":"Wait timed out.","timed_out":true}',
            },
        ),
        _codex(
            "event_msg",
            {
                "type": "task_complete",
                "turn_id": "t1",
                "last_agent_message": None,
                "error": {
                    "message": "failed to load workspace requirements",
                    "codex_error_info": "other",
                },
            },
        ),
    ]


def test_codex_16_1_error_turn_is_an_environment_fault_with_a_pending_agent(tmp_path):
    path = _write(tmp_path / "r.jsonl", _codex_16_1())
    state = read_turn_state(path, "codex-rollout")
    assert state.turn_ended
    assert (state.error_class, state.error_text) == (
        "environment",
        "failed to load workspace requirements",
    )
    assert state.pending_agent and state.pending_tools == ()
    assert state.last_message == "Verification is still waiting on the shared build lock."
    assert state.phase == "commentary"


def test_codex_stream_disconnect_is_transport(tmp_path):
    entries = [
        _codex("event_msg", {"type": "task_started"}),
        _codex(
            "event_msg",
            {
                "type": "task_complete",
                "last_agent_message": None,
                "error": {
                    "message": "stream disconnected before completion: error sending request"
                },
            },
        ),
    ]
    state = read_turn_state(_write(tmp_path / "r.jsonl", entries), "codex-rollout")
    assert state.error_class == "transport"


def test_codex_final_answer_question_and_a_new_turn_clearing_the_error(tmp_path):
    entries = [
        *_codex_16_1(),
        _codex("event_msg", {"type": "task_started", "turn_id": "t2"}),
        _codex(
            "event_msg",
            {"type": "task_complete", "last_agent_message": "Which migration should I keep?"},
        ),
    ]
    state = read_turn_state(_write(tmp_path / "r.jsonl", entries), "codex-rollout")
    assert state.error_class is None
    assert state.asks_question and state.phase == "final_answer"


def test_codex_final_answer_from_the_agent_clears_the_wait(tmp_path):
    entries = [
        *_codex_16_1()[:4],
        _codex(
            "response_item",
            {
                "type": "agent_message",
                "content": [
                    {"type": "input_text", "text": "Message Type: FINAL_ANSWER\nPayload: done"}
                ],
            },
        ),
    ]
    state = read_turn_state(_write(tmp_path / "r.jsonl", entries), "codex-rollout")
    assert not state.pending_agent


def test_codex_open_exec_call_is_pending(tmp_path):
    entries = [
        _codex("event_msg", {"type": "task_started"}),
        _codex("response_item", {"type": "custom_tool_call", "name": "exec", "call_id": "c1"}),
    ]
    state = read_turn_state(_write(tmp_path / "r.jsonl", entries), "codex-rollout")
    assert state.pending_tools == ("exec",) and not state.turn_ended


# ------------------------------------------------------------ the pure parts


@pytest.mark.parametrize(
    ("code", "text", "expected"),
    [
        ("server_error", "API Error: Connection lost mid-response.", "transport"),
        # split so no tracked line carries a sentence the shipped patterns match
        ("overloaded", "API Error: 529 " + "Overloaded", "transport"),
        ("rate_limit", POOL_TEXT, "pool"),
        ("rate_limit", "API Error: Request rejected (429)", "pool"),
        ("billing_error", "Credit balance is too low", "usage"),
        (None, "You're out of usage credits. Run " + "/usage-credits", "usage"),
        ("authentication_failed", "Invalid API key", "environment"),
        ("other", "failed to load workspace requirements", "environment"),
        ("error", "stream disconnected before completion", "transport"),
        ("error", "exceeded retry limit, last status: 500", "transport"),
        ("error", "something new and unknown", "transport"),
        (None, None, None),
    ],
)
def test_classify_error(code, text, expected):
    assert classify_error(code, text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Should I proceed with the migration?", True),
        ("Which option do you prefer?**", True),
        ("I can keep it or drop it. Let me know which you prefer.", True),
        ("Please confirm the schema change before I continue.", True),
        ("Verification is still waiting on the shared build lock.", False),
        ("All 128 tests pass. Committing now.", False),
        ("The plan file is written; next I implement step 2.", False),
        ("", False),
        (None, False),
    ],
)
def test_asks_question(text, expected):
    assert asks_question(text) is expected


def test_unreadable_inputs_read_as_none(tmp_path):
    assert read_turn_state(tmp_path / "missing.jsonl", "claude-jsonl") is None
    assert read_turn_state(None, "claude-jsonl") is None
    path = _write(tmp_path / "t.jsonl", [_claude_text("hi")])
    assert read_turn_state(path, "gemini-chat") is None
    assert not turnstate.supports("none") and turnstate.supports("codex-rollout")


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs a FIFO")
def test_a_fifo_is_refused_without_blocking(tmp_path):
    fifo = tmp_path / "pipe.jsonl"
    os.mkfifo(fifo)
    assert read_turn_state(fifo, "claude-jsonl") is None


def test_only_the_tail_is_read_and_a_torn_first_line_is_dropped(tmp_path):
    big = _claude_text("x" * 5000)
    path = _write(tmp_path / "t.jsonl", [big, _claude_text("Final words?")])
    state = read_turn_state(path, "claude-jsonl", tail_bytes=200)
    assert state.last_message == "Final words?"
    assert state.asks_question
