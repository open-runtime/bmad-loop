"""Native resumes require observed harness identity, independent of adapter labels."""

import dataclasses
import json
from types import MethodType, SimpleNamespace

import pytest
from conftest import dev_effect, review_effect, write_sprint
from test_engine import make_engine, resume_engine
from test_fork_session_resume import _policy
from test_generic_tmux import make_adapter

from bmad_loop import native_binding
from bmad_loop.adapters.base import SessionHandle, SessionResult, SessionSpec
from bmad_loop.adapters.generic import GenericAdapter
from bmad_loop.adapters.profile import get_profile
from bmad_loop.journal import Journal, load_state
from bmad_loop.model import UNATTRIBUTED_MODEL, Phase, SessionRecord, StoryTask, TokenUsage
from bmad_loop.policy import AdapterPolicy
from bmad_loop.verify import rev_parse_head


def _profile(name="codex"):
    profile = get_profile(name)
    if name == "codex":
        return dataclasses.replace(profile, resume_subcommand=("resume",))
    return dataclasses.replace(profile, resume_flag="--resume")


def _transcript(path, family="codex", session="native-1"):
    if family == "codex":
        row = {"type": "session_meta", "payload": {"id": session}}
    elif family == "claude":
        row = {"type": "user", "sessionId": session, "message": {"role": "user"}}
    elif family == "gemini":
        row = {"sessionId": session, "messages": []}
    else:
        row = {"type": "session.start", "data": {"sessionId": session}}
    path.write_text(json.dumps(row) + "\n", encoding="utf-8")
    return str(path)


@pytest.mark.parametrize("family", ["codex", "claude", "gemini", "copilot"])
def test_native_identity_comes_from_content_not_filename(tmp_path, family):
    path = _transcript(tmp_path / "misleading-name.jsonl", family)
    assert native_binding.transcript_identity(path) == (family, "native-1")


def test_unknown_and_redirected_transcripts_are_refused(tmp_path):
    path = tmp_path / "unknown.jsonl"
    path.write_text('{"session_id": "native-1"}\n')
    with pytest.raises(native_binding.NativeBindingError, match="no recognized"):
        native_binding.transcript_identity(str(path))
    link = tmp_path / "link.jsonl"
    link.symlink_to(path)
    with pytest.raises(native_binding.NativeBindingError, match="regular file"):
        native_binding.transcript_identity(str(link))


def test_bounded_identity_read_can_split_a_later_utf8_character(tmp_path, monkeypatch):
    monkeypatch.setattr(native_binding, "TRANSCRIPT_READ_CAP", 256)
    path = tmp_path / "native.jsonl"
    _transcript(path)
    header = path.read_bytes()
    path.write_bytes(header + b" " * (256 - len(header) - 1) + "é".encode() + b"\n")
    assert native_binding.transcript_identity(str(path)) == ("codex", "native-1")


def test_invalid_native_header_encoding_still_refuses(tmp_path):
    path = tmp_path / "native.jsonl"
    path.write_bytes(b"\xff\n")
    with pytest.raises(native_binding.NativeBindingError, match="cannot be read"):
        native_binding.transcript_identity(str(path))


def test_matching_native_and_launch_profile_can_resume(tmp_path):
    profile = _profile()
    transcript = _transcript(tmp_path / "native.jsonl")
    bound = native_binding.capture_binding(profile, profile.binary, "native-1", transcript)
    assert bound["harness"] == "codex"
    assert (
        native_binding.resume_refusal(bound, profile, profile.binary, "native-1", transcript)
        is None
    )


def test_profile_digest_survives_first_hook_before_transcript(tmp_path):
    profile = _profile()
    path = tmp_path / "later.jsonl"
    bound = native_binding.capture_binding(profile, profile.binary, "native-1", str(path))
    assert "error" in bound
    _transcript(path)
    assert (
        native_binding.resume_refusal(bound, profile, profile.binary, "native-1", str(path)) is None
    )


@pytest.mark.parametrize("change", ["profile", "binary", "extra_args", "session", "binding"])
def test_changed_or_unrecorded_launch_refuses_resume(tmp_path, change):
    profile = _profile()
    transcript = _transcript(tmp_path / "native.jsonl")
    bound = native_binding.capture_binding(profile, profile.binary, "native-1", transcript)
    if change == "profile":
        profile = dataclasses.replace(profile, env={"DIFFERENT": "1"})
    binary = "different-binary" if change == "binary" else profile.binary
    extra_args = ("--different",) if change == "extra_args" else None
    session = "different-native" if change == "session" else "native-1"
    if change == "binding":
        bound = None
    assert (
        native_binding.resume_refusal(
            bound, profile, binary, session, transcript, extra_args=extra_args
        )
        is not None
    )


def test_changed_transcript_identity_refuses_resume(tmp_path):
    profile = _profile()
    path = tmp_path / "native.jsonl"
    transcript = _transcript(path)
    bound = native_binding.capture_binding(profile, profile.binary, "native-1", transcript)
    _transcript(path, session="other-native")
    assert "another conversation" in native_binding.resume_refusal(
        bound, profile, profile.binary, "native-1", transcript
    )


def test_cross_harness_native_id_never_reaches_a_window(tmp_path):
    adapter = make_adapter(tmp_path, profile_name="codex")
    adapter.profile = _profile()
    transcript = _transcript(tmp_path / "claude.jsonl", "claude")
    binding = native_binding.capture_binding(
        adapter.profile, adapter.binary, "native-1", transcript
    )
    windows = []
    # Even an ablated guard must never launch a real coding CLI in a test.
    adapter._ensure_session = lambda: None
    adapter.mux = SimpleNamespace(
        new_window=lambda *args: windows.append(args) or "@one",
        pipe_pane=lambda *args: None,
    )
    spec = SessionSpec(
        task_id="continued-dev-2",
        role="dev",
        prompt="continue the same work",
        cwd=tmp_path,
        resume_session_id="native-1",
        resume_transcript_path=transcript,
        resume_binding=binding,
    )
    with pytest.raises(native_binding.NativeBindingError, match="belongs to claude"):
        adapter.start_session(spec)
    assert windows == []
    assert not (adapter.tasks_dir / spec.task_id).exists()


def test_session_bound_records_observed_harness_and_requested_profile(tmp_path):
    adapter = make_adapter(tmp_path, profile_name="codex")
    adapter.journal = Journal(adapter.run_dir)
    adapter.policy = dataclasses.replace(adapter.policy, recovery=_policy().recovery)
    transcript = _transcript(tmp_path / "claude.jsonl", "claude")
    adapter._note_session_bound(SessionHandle("dev-1", "@one"), "native-1", "startup", transcript)
    row = list(adapter.journal.entries())[-1]
    assert row["native_binding"]["profile"] == "codex"
    assert row["native_binding"]["harness"] == "claude"
    assert row["native_binding"]["observed_session_id"] == "native-1"


@pytest.mark.parametrize("family,paused", [("claude", True), ("codex", False)])
def test_retry_matches_actual_harness_and_preserves_work(project, family, paused):
    """Logical Codex -> actual Claude -> timeout must hold before rollback.

    The matching-Codex control really resumes and completes, so the refusal is
    not an unrelated inability to dispatch the second session.
    """
    write_sprint(project, {"1-1-a": "ready-for-dev"})
    profile = _profile()

    def timed_out(spec):
        path = _transcript(project.project / "native.jsonl", family)
        (project.project / "src.txt").write_text("Keep this unfinished implementation.\n")
        return SessionResult(
            status="timeout",
            session_id="native-1",
            transcript_path=path,
            native_binding=native_binding.capture_binding(profile, "codex", "native-1", path),
        )

    engine, adapter = make_engine(
        project,
        [timed_out, dev_effect(project, "1-1-a"), review_effect(project, "1-1-a", clean=True)],
        policy=_policy(rollback=True, adapter=AdapterPolicy(name="codex")),
    )
    adapter.profile = profile
    adapter.binary = "codex"
    adapter.extra_args = None
    adapter.native_resume_refusal = MethodType(GenericAdapter.native_resume_refusal, adapter)
    summary = engine.run()
    assert summary.paused is paused
    if paused:
        assert len(adapter.sessions) == 1
        assert (project.project / "src.txt").read_text() == "Keep this unfinished implementation.\n"
        assert any(row["kind"] == "native-resume-refused" for row in engine.journal.entries())
        state = load_state(engine.run_dir)
        assert state.tasks["1-1-a"].sessions[0].native_binding["harness"] == "claude"
    else:
        assert summary.done == 1
        assert adapter.sessions[1].resume_session_id == "native-1"
        assert adapter.sessions[1].resume_binding["harness"] == "codex"


def test_restart_rejects_foreign_native_before_resetting_its_work(project):
    write_sprint(project, {"1-1-a": "ready-for-dev"})
    policy = _policy(rollback=True, adapter=AdapterPolicy(name="codex"))
    engine, _ = make_engine(project, [], policy=policy)
    task = StoryTask(story_key="1-1-a", epic=1, phase=Phase.DEV_RUNNING, attempt=1)
    task.baseline_commit = rev_parse_head(project.project)
    transcript = _transcript(project.project / "native.jsonl", "claude")
    profile = _profile()
    task.record_session(
        SessionRecord(
            task_id="1-1-a-dev-1",
            role="dev",
            status="timeout",
            adapter="codex",
            session_id="native-1",
            transcript_path=transcript,
            native_binding=native_binding.capture_binding(profile, "codex", "native-1", transcript),
        )
    )
    engine.state.tasks[task.story_key] = task
    engine._save()
    (project.project / "src.txt").write_text("Preserve this interrupted source.\n")
    resumed, adapter = resume_engine(project, engine, [], policy=policy)
    adapter.profile = profile
    adapter.binary = "codex"
    adapter.extra_args = None
    adapter.native_resume_refusal = MethodType(GenericAdapter.native_resume_refusal, adapter)
    summary = resumed.run()
    assert summary.paused
    assert (project.project / "src.txt").read_text() == "Preserve this interrupted source.\n"
    assert adapter.sessions == []
    assert any(row["kind"] == "native-resume-refused" for row in resumed.journal.entries())


@pytest.mark.parametrize("already_accounted,expected", [(True, 30), (False, 130)])
def test_unrecorded_stopped_usage_is_counted_once(tmp_path, already_accounted, expected):
    profile = _profile("claude")
    adapter = make_adapter(tmp_path)
    adapter.profile = profile
    path = tmp_path / "native.jsonl"

    def entry(message_id, inputs):
        return (
            json.dumps(
                {
                    "type": "assistant",
                    "sessionId": "native-1",
                    "message": {
                        "id": message_id,
                        "model": "claude-model",
                        "usage": {"input_tokens": inputs},
                    },
                }
            )
            + "\n"
        )

    path.write_text(entry("before", 100))
    spec = SessionSpec(
        task_id="continued-dev-2",
        role="dev",
        prompt="continue",
        cwd=tmp_path,
        resume_session_id="native-1",
        resume_transcript_path=str(path),
        resume_usage_accounted=already_accounted,
    )
    adapter._note_resume_base(spec)
    with path.open("a") as stream:
        stream.write(entry("after", 30))
    usage = adapter.read_usage(SessionResult(status="completed", transcript_path=str(path)))
    assert usage.input_tokens == expected
    assert usage.models == ("claude-model",)
    assert usage.by_model == {"claude-model": TokenUsage(input_tokens=expected)}


@pytest.mark.parametrize("baseline_split", [True, False])
def test_resumed_usage_retains_model_split_and_read_diagnostics(tmp_path, baseline_split):
    adapter = make_adapter(tmp_path)
    adapter._usage_base["native.jsonl"] = TokenUsage(
        input_tokens=100,
        models=("old",),
        by_model={"old": TokenUsage(input_tokens=100)} if baseline_split else None,
    )
    usage = TokenUsage(
        input_tokens=130,
        output_tokens=7,
        unread=("child.jsonl: OSError",),
        models=("old", "new"),
        by_model={
            "old": TokenUsage(input_tokens=100),
            "new": TokenUsage(input_tokens=30, output_tokens=7),
        },
    )
    result = adapter._rebased("native.jsonl", usage)
    assert result == TokenUsage(input_tokens=30, output_tokens=7)
    assert result.models == ("old", "new")
    assert result.unread == ("child.jsonl: OSError",)
    expected_key = "new" if baseline_split else UNATTRIBUTED_MODEL
    assert result.by_model == {expected_key: TokenUsage(input_tokens=30, output_tokens=7)}
    total = TokenUsage()
    for part in result.by_model.values():
        total.add(part)
    assert total == result
