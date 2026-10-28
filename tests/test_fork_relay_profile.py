"""open-runtime fork: what the hook relays forward (F3, F4, F7), the profile's
resume keys and `StopFailure` (F3, F6), the launch digest over them, and the
`[recovery]` policy table."""

from __future__ import annotations

import dataclasses
import json
import os
import subprocess
import sys

import pytest
from test_events import HOOK, _relay

from bmad_loop import events, runsetup
from bmad_loop.adapters.profile import (
    CANONICAL_EVENTS,
    TURN_FAILURE_EVENT,
    ProfileError,
    _load_toml,
    get_profile,
)
from bmad_loop.policy import Policy, PolicyError, RecoveryPolicy, loads
from bmad_loop.signals import _parse_event

STOP_PAYLOAD = {
    "session_id": "c193f159",
    "transcript_path": "/t/x.jsonl",
    "last_assistant_message": "Tests pass. Waiting for the build lock?",
    "background_tasks": [
        {"status": "running", "description": "cargo nextest run"},
        {"status": "completed", "description": "done already"},
        {"status": "running", "command": "sleep 900"},
        "not-a-dict",
    ],
    "error": "server_error",
    "error_details": "500 Internal server error",
}


# ------------------------------------------------------------------ relays


def test_shape_event_forwards_the_turn_fields_only_when_present():
    event = events.shape_event(1, "Stop", "t1", STOP_PAYLOAD, "unknown")
    assert event["last_assistant_message"] == STOP_PAYLOAD["last_assistant_message"]
    assert event["background_tasks"] == ["cargo nextest run", "sleep 900"]
    assert (event["error"], event["error_details"]) == ("server_error", "500 Internal server error")
    bare = events.shape_event(1, "Stop", "t1", {"session_id": "s"}, "unknown")
    for key in (
        "last_assistant_message",
        "background_tasks",
        "error",
        "error_details",
        "hcom_name",
    ):
        assert key not in bare


def test_the_last_message_keeps_its_tail():
    long = "x" * 9000 + "Should I continue?"
    event = events.shape_event(1, "Stop", "t1", {"last_assistant_message": long}, None)
    assert len(event["last_assistant_message"]) == events._TURN_MESSAGE_MAX
    assert event["last_assistant_message"].endswith("Should I continue?")


def test_hcom_identity_is_forwarded_from_the_environment():
    environ = {"HCOM_INSTANCE_NAME": "gari", "HCOM_DIR": "/rooms/rem/hcom"}
    event = events.shape_event(1, "SessionStart", "t1", {}, None, environ=environ)
    assert (event["hcom_name"], event["hcom_dir"]) == ("gari", "/rooms/rem/hcom")
    assert "hcom_name" not in events.shape_event(1, "SessionStart", "t1", {}, None, environ={})


def test_both_relay_twins_forward_the_same_turn_and_hcom_fields(tmp_path, monkeypatch):
    """The hook shapes inline in `main()`, so the twin is pinned behaviourally."""
    hook_run, relay_run = tmp_path / "hook", tmp_path / "relay"
    env = {
        "PATH": os.environ.get("PATH", ""),
        **({"SYSTEMROOT": os.environ.get("SYSTEMROOT", "")} if os.name == "nt" else {}),
        "BMAD_LOOP_RUN_DIR": str(hook_run),
        "BMAD_LOOP_TASK_ID": "1-1-a-dev-1",
        "HCOM_INSTANCE_NAME": "gari",
        "HCOM_DIR": "/rooms/rem/hcom",
    }
    proc = subprocess.run(
        [sys.executable, str(HOOK), "Stop"],
        input=json.dumps(STOP_PAYLOAD),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    monkeypatch.setenv("HCOM_INSTANCE_NAME", "gari")
    monkeypatch.setenv("HCOM_DIR", "/rooms/rem/hcom")
    assert _relay("Stop", STOP_PAYLOAD, monkeypatch, relay_run, task_id="1-1-a-dev-1") == 0

    from_hook = json.loads(next((hook_run / "events").glob("*.json")).read_text())
    from_relay = json.loads(next((relay_run / "events").glob("*.json")).read_text())
    assert from_relay | {"ts": 0} == from_hook | {"ts": 0}
    assert from_relay["hcom_name"] == "gari"
    assert from_relay["background_tasks"] == ["cargo nextest run", "sleep 900"]


def test_hook_event_parses_the_new_fields(tmp_path):
    path = tmp_path / "1-t1-Stop.json"
    path.write_text(
        json.dumps(
            {
                "ts": 1,
                "event": "Stop",
                "task_id": "t1",
                "last_assistant_message": "hi",
                "background_tasks": ["a", 3, "b"],
                "error": "rate_limit",
                "error_details": "429",
                "hcom_name": "gari",
                "hcom_dir": "/r",
            }
        )
    )
    event = _parse_event(path)
    assert event.last_assistant_message == "hi"
    assert event.background_tasks == ("a", "b")
    assert (event.error, event.error_details) == ("rate_limit", "429")
    assert (event.hcom_name, event.hcom_dir) == ("gari", "/r")
    path.write_text(json.dumps({"ts": 1, "event": "Stop", "task_id": "t1", "error": 5}))
    old = _parse_event(path)
    assert old.background_tasks is None and old.error is None and old.hcom_name is None


# ------------------------------------------------------------------ profile


def _profile_doc(**extra) -> str:
    lines = [
        'name = "x"',
        'binary = "claude"',
        *(f"{k} = {json.dumps(v)}" for k, v in extra.items()),
        "[hooks]",
        'dialect = "claude-settings-json"',
        'config_path = ".claude/settings.json"',
        'events = { Stop = "Stop", StopFailure = "StopFailure" }',
    ]
    return "\n".join(lines) + "\n"


def test_stop_failure_is_a_canonical_event():
    assert TURN_FAILURE_EVENT == "StopFailure" and TURN_FAILURE_EVENT in CANONICAL_EVENTS
    profile = _load_toml(_profile_doc(), "x.toml")
    assert profile.hooks.events["StopFailure"] == "StopFailure"


def test_resume_keys_parse_and_render():
    claude = _load_toml(_profile_doc(resume_flag="--resume"), "x.toml")
    assert claude.resumable and claude.render_resume_prompt("go on") == "go on"
    codex = _load_toml(
        _profile_doc(resume_subcommand=["resume"], resume_prompt_template="[loop] {prompt}"),
        "x.toml",
    )
    assert codex.resume_subcommand == ("resume",)
    assert codex.render_resume_prompt("go {on}") == "[loop] go {on}"
    assert not get_profile("claude").resumable


@pytest.mark.parametrize(
    "extra",
    [
        {"resume_flag": "resume"},
        {"resume_flag": "--resume=x"},
        {"resume_flag": "--re sume"},
        {"resume_subcommand": ["--resume"]},
        {"resume_subcommand": ["re sume"]},
        {"resume_flag": "--resume", "resume_subcommand": ["resume"]},
        {"resume_flag": "--resume", "resume_prompt_template": "no placeholder"},
        {"resume_flag": 3},
    ],
)
def test_bad_resume_keys_are_profile_errors(extra):
    with pytest.raises(ProfileError):
        _load_toml(_profile_doc(**extra), "x.toml")


def test_the_digest_covers_resume_tokens_only_when_set(tmp_path):
    """Argv tokens are the launch surface; a profile that cannot resume keeps
    the digest byte-identical to one stamped before the keys existed."""
    policy = Policy()
    claude = get_profile("claude")
    profiles = {role: claude for role in runsetup.ROLES}
    plain = runsetup.config_digest(policy, tmp_path, profiles=profiles)
    resumable = dataclasses.replace(claude, resume_flag="--resume")
    changed = runsetup.config_digest(
        policy, tmp_path, profiles={role: resumable for role in runsetup.ROLES}
    )
    assert plain != changed


# ------------------------------------------------------------------ policy


def test_recovery_defaults_keep_upstream_behaviour():
    recovery = Policy().recovery
    assert recovery == RecoveryPolicy()
    assert (recovery.stall_check, recovery.nudge_via, recovery.reload_policy) == (
        "classic",
        "tmux",
        "off",
    )
    assert recovery.max_session_resumes == 0 and recovery.classify_turn_errors is False
    # the two defect fixes default on
    assert recovery.post_story_on_resume and recovery.restamp_plan_baseline


def test_recovery_table_parses():
    policy = loads(
        "[recovery]\n"
        'stall_check = "smart"\nturn_end_nudge_s = 90\nclassify_turn_errors = true\n'
        'transport_nudges = 2\nnudge_via = "hcom"\nmax_session_resumes = 2\n'
        'reload_policy = "story-boundary"\npost_story_on_resume = false\n'
    )
    r = policy.recovery
    assert (r.stall_check, r.turn_end_nudge_s, r.classify_turn_errors) == ("smart", 90, True)
    assert (r.transport_nudges, r.nudge_via, r.max_session_resumes) == (2, "hcom", 2)
    assert (r.reload_policy, r.post_story_on_resume) == ("story-boundary", False)
    assert policy.to_dict()["recovery"]["stall_check"] == "smart"


@pytest.mark.parametrize(
    "text",
    [
        '[recovery]\nstall_check = "clever"\n',
        '[recovery]\nnudge_via = "pigeon"\n',
        '[recovery]\nreload_policy = "always"\n',
        "[recovery]\nmax_session_resumes = -1\n",
        "[recovery]\nturn_end_nudge_s = true\n",
        '[recovery]\nclassify_turn_errors = "yes"\n',
    ],
)
def test_bad_recovery_values_are_policy_errors(text):
    with pytest.raises(PolicyError):
        loads(text)


def test_an_upstream_engine_ignores_the_recovery_table():
    """Backward compatibility the other way: the table is unknown to upstream's
    parser, which ignores unknown sections, so a policy that sets it still loads
    there. Pinned here as the property this fork relies on: `_section` reads only
    the names it asks for."""
    from bmad_loop import policy as policy_mod

    assert policy_mod._section({"recovery": {"x": 1}}, "gates") == {}
