"""The Git chokepoint preserves its stream contract and owned timeout evidence."""

import dataclasses
import errno
import io
import json
import os
import shlex
import subprocess
import sys
import threading

import pytest
from conftest import git

from bmad_loop import owned_capture, verify
from bmad_loop.owned_capture import ActorEvidence, CaptureOwnership, CaptureResult
from bmad_loop.process_host import ProcessHostError, get_process_host


def scripted_argv(program, *args):
    return [sys.executable, "-c", program, "capture-probe", *args]


def captured(
    *,
    timed_out=False,
    complete=True,
    stdout_captured=True,
    stdout_eof=True,
    stderr_eof=True,
    io_settled=True,
):
    ownership = CaptureOwnership(
        root_pid=12345,
        root_identity=1.0,
        root_reaped=True,
        actors=(ActorEvidence(12345, 1.0, "dead"),),
        census_count=1,
        census_complete=complete,
        problems=() if complete else ("synthetic census incomplete",),
        stdout_eof=stdout_eof,
        stderr_eof=stderr_eof,
        io_settled=io_settled,
        stdout_captured=stdout_captured,
        stderr_captured=True,
        cleanup_required=timed_out,
    )
    return CaptureResult(0, b"captured\r\n", b"diagnostic\r", timed_out, False, 0, True, ownership)


@pytest.mark.parametrize("binary", [False, True])
def test_real_git_capture_returns_completed_process_with_exact_stream_types(project, binary):
    argv = scripted_argv(
        "import os;os.write(1,b'one\\r\\ntwo\\rthree\\n');os.write(2,b'err\\r\\n');raise SystemExit(7)"
    )
    result = verify._run_git(argv, project.project, binary=binary, timeout_s=10)
    expected = subprocess.run(argv, capture_output=True, text=not binary, timeout=10)
    assert isinstance(result, subprocess.CompletedProcess)
    assert result.args == argv and result.returncode == expected.returncode == 7
    assert result.stdout == expected.stdout
    assert result.stderr == expected.stderr
    assert isinstance(result.stdout, bytes if binary else str)
    assert result.owned_capture.ownership.stdout_captured
    assert result.owned_capture.ownership.stderr_captured


@pytest.mark.parametrize("payload", [b"", bytes(range(256)) * 32])
@pytest.mark.parametrize("binary", [False, True])
def test_real_git_capture_preserves_binary_input_and_non_utf8_output(project, payload, binary):
    argv = scripted_argv(
        "import sys;data=sys.stdin.buffer.read();sys.stdout.buffer.write(b'\\x00\\xff\\r\\n'+data);sys.stderr.buffer.write(b'\\x81\\x00\\r');raise SystemExit(9)"
    )
    result = verify._run_git(argv, project.project, binary=binary, input_data=payload, timeout_s=10)
    assert result.args == argv and result.returncode == 9
    assert result.stdout == b"\x00\xff\r\n" + payload
    assert result.stderr == b"\x81\x00\r"
    assert result.owned_capture.input_complete
    assert result.owned_capture.input_bytes_written == len(payload)


def test_real_git_capture_strict_decode_fault_remains_chained_git_error(project):
    invalid = None
    for candidate in (b"\xff", b"\x81", b"\x80", b"\xc0"):
        try:
            with io.TextIOWrapper(io.BytesIO(candidate), newline=None) as stream:
                stream.read()
        except UnicodeDecodeError:
            invalid = candidate
            break
    if invalid is None:
        pytest.skip("parent text codec has no invalid byte among the strict-decode probes")
    argv = scripted_argv(f"import os;os.write(1,{invalid!r})")
    with pytest.raises(verify.GitError, match="returned undecodable output") as error:
        verify._run_git(argv, project.project, timeout_s=10)
    assert isinstance(error.value.__cause__, UnicodeDecodeError)


@pytest.mark.parametrize("explicit", [False, True])
def test_real_git_capture_preserves_argv_and_locale_override_precedence(
    project, monkeypatch, explicit
):
    monkeypatch.setenv("LC_ALL", "synthetic-parent-locale")
    monkeypatch.setenv("CAPTURE_PARENT_ONLY", "parent")
    extra = ["contains spaces", "semi;colon", "$literal", "quote'and\"double", "unicode-λ"]
    argv = scripted_argv(
        "import json,os,sys;print(json.dumps({'argv':sys.argv[1:],'locale':os.getenv('LC_ALL'),'parent':os.getenv('CAPTURE_PARENT_ONLY'),'explicit':os.getenv('CAPTURE_EXPLICIT')}))",
        *extra,
    )
    env = (
        {"LC_ALL": "synthetic-explicit-locale", "CAPTURE_EXPLICIT": "explicit"}
        if explicit
        else None
    )
    if explicit and os.name == "nt":
        env["SystemRoot"] = os.environ["SystemRoot"]
    before = None if env is None else dict(env)
    result = verify._run_git(argv, project.project, env=env, timeout_s=10)
    observed = json.loads(result.stdout)
    assert result.args == argv
    assert observed["argv"] == ["capture-probe", *extra]
    assert observed["locale"] == "C"
    assert observed["parent"] == (None if explicit else "parent")
    assert observed["explicit"] == ("explicit" if explicit else None)
    assert env == before


def test_git_capture_keeps_configured_and_per_call_deadlines(monkeypatch, tmp_path):
    observed, spawned = [], []
    argv = ["git", "-C", str(tmp_path), "status"]

    def factory(cmd, **kwargs):
        spawned.append((cmd, kwargs))
        return object()

    def capture(spawn, *, timeout_s, input_data):
        observed.append(timeout_s)
        spawn()
        return captured()

    monkeypatch.setattr(verify.subprocess, "Popen", factory)
    monkeypatch.setattr(owned_capture, "capture", capture)
    previous = verify._git_timeout_s
    try:
        verify.configure_git_timeout(7)
        verify._run_git(argv, tmp_path)
        verify._run_git(argv, tmp_path, timeout_s=0.25)
        verify._run_git(argv, tmp_path)
    finally:
        verify.configure_git_timeout(previous)
    assert observed == [7, 0.25, 7]
    assert all(cmd == argv for cmd, _ in spawned)
    assert all(options["stdin"] is None for _, options in spawned)
    assert all(options["env"]["LC_ALL"] == "C" for _, options in spawned)


@pytest.mark.parametrize("number", [errno.ENOENT, errno.EMFILE, errno.ENOMEM])
def test_git_capture_spawn_failure_retains_git_error_subclass_and_cause(
    monkeypatch, tmp_path, number
):
    failure = OSError(number, "synthetic spawn failure")

    def fail(*a, **kw):
        raise failure

    monkeypatch.setattr(verify.subprocess, "Popen", fail)
    with pytest.raises(verify.GitSpawnError, match="git status failed to spawn") as error:
        verify._run_git(["git", "-C", str(tmp_path), "status"], tmp_path)
    assert isinstance(error.value, verify.GitError)
    assert error.value.__cause__ is failure


@pytest.mark.parametrize("complete", [False, True])
def test_git_timeout_keeps_partial_streams_and_independent_cleanup_evidence(
    monkeypatch, tmp_path, complete
):
    result = captured(timed_out=True, complete=complete)
    monkeypatch.setattr(owned_capture, "capture", lambda *a, **kw: result)
    argv = ["git", "-C", str(tmp_path), "status"]
    with pytest.raises(verify.GitTimeoutError, match="timed out after 0.25s") as error:
        verify._run_git(argv, tmp_path, timeout_s=0.25)
    assert isinstance(error.value, verify.GitError)
    assert error.value.capture is result
    assert error.value.capture.ownership.quiescent is complete
    cause = error.value.__cause__
    assert isinstance(cause, subprocess.TimeoutExpired)
    assert cause.cmd == argv and cause.timeout == 0.25
    assert cause.stdout == result.stdout and cause.stderr == result.stderr


def test_ordinary_read_remains_usable_when_cleanup_ownership_is_unknown(monkeypatch, tmp_path):
    result = captured(complete=False)
    monkeypatch.setattr(owned_capture, "capture", lambda *a, **kw: result)
    answer = verify._run_git(["git", "-C", str(tmp_path), "status"], tmp_path)
    assert answer.returncode == 0 and answer.stdout == "captured\n"
    assert answer.owned_capture is result
    assert not answer.owned_capture.ownership.quiescent
    assert answer.owned_capture.ownership.problems


@pytest.mark.parametrize("field", ["stdout_captured", "stdout_eof", "stderr_eof", "io_settled"])
def test_incomplete_output_is_not_a_successful_empty_git_answer(monkeypatch, tmp_path, field):
    result = captured(**{field: False})
    monkeypatch.setattr(owned_capture, "capture", lambda *a, **kw: result)
    with pytest.raises(verify.GitError, match="capture did not complete") as error:
        verify._run_git(["git", "-C", str(tmp_path), "status"], tmp_path)
    assert error.value.owned_capture is result


def test_post_spawn_capture_fault_is_not_misreported_as_a_spawn_failure(monkeypatch, tmp_path):
    evidence = captured(complete=False)
    failure = OSError(errno.EIO, "synthetic pipe read failed")
    failure.owned_capture = evidence

    def fail(*a, **kw):
        raise failure

    monkeypatch.setattr(owned_capture, "capture", fail)
    with pytest.raises(verify.GitError) as error:
        verify._run_git(["git", "-C", str(tmp_path), "status"], tmp_path)
    assert not isinstance(error.value, verify.GitSpawnError)
    assert "capture failed after spawning" in str(error.value)
    assert error.value.__cause__ is failure
    assert error.value.owned_capture is evidence


def test_capture_backend_failure_remains_inside_git_error_boundary(monkeypatch, tmp_path):
    failure = ProcessHostError("synthetic backend unavailable")

    def fail(*a, **kw):
        raise failure

    monkeypatch.setattr(owned_capture, "capture", fail)
    with pytest.raises(verify.GitError) as error:
        verify._run_git(["git", "-C", str(tmp_path), "status"], tmp_path)
    assert error.value.__cause__ is failure


@pytest.mark.skipif(os.name == "nt", reason="foreground POSIX Git hook fixture")
def test_timed_git_hook_descendants_are_dead_before_late_mutation(project, tmp_path, monkeypatch):
    if sys.platform == "darwin":
        pytest.importorskip("psutil")
    inner = get_process_host()
    identities, roots, signals = {}, {}, []
    hook_pid = tmp_path / "hook-pid.json"
    child_pid = tmp_path / "child-pid.json"
    release = tmp_path / "release"
    late = tmp_path / "late-write"

    class GuardedHost:
        def identity(self, pid):
            stamp = inner.identity(pid)
            if pid in roots and pid not in identities and stamp is not None:
                identities[pid] = stamp
            return stamp

        def liveness_of(self, pid, identity):
            return inner.liveness_of(pid, identity)

        def observe_descendants(self, pid):
            assert pid in roots or pid in identities
            result = inner.observe_descendants(pid)
            identities.update(
                {key: stamp for key, stamp in result.members.items() if stamp is not None}
            )
            return result

        def terminate(self, pid):
            pytest.fail("capture used legacy unbound signalling")

        def force_kill(self, pid):
            pytest.fail("capture used legacy unbound signalling")

        def signal_owned(self, pid, identity, *, force):
            assert pid != os.getpid()
            assert pid in roots or pid in identities, "capture tried to signal an unowned peer"
            assert identities.get(pid) == identity
            sent = inner.signal_owned(pid, identity, force=force)
            if sent:
                signals.append((pid, identity))
            return sent

    host = GuardedHost()
    child_script = tmp_path / "foreground-child.py"
    child_script.write_text(
        "import json,os,time\nfrom pathlib import Path\n"
        "from bmad_loop.process_host import get_process_host\n"
        f"Path({str(child_pid)!r}).write_text(json.dumps([os.getpid(),get_process_host().identity(os.getpid())]))\n"
        "until=time.monotonic()+15\n"
        f"while not Path({str(release)!r}).exists() and time.monotonic()<until: time.sleep(.02)\n"
        f"if Path({str(release)!r}).exists(): Path({str(late)!r}).write_text('late mutation')\n"
    )
    hook_script = tmp_path / "foreground-hook.py"
    hook_script.write_text(
        "import json,os,subprocess,sys\nfrom pathlib import Path\n"
        "from bmad_loop.process_host import get_process_host\n"
        f"Path({str(hook_pid)!r}).write_text(json.dumps([os.getpid(),get_process_host().identity(os.getpid())]))\n"
        f"raise SystemExit(subprocess.call([sys.executable,{str(child_script)!r}]))\n"
    )
    hook = project.project / ".git/hooks/pre-commit"
    hook.write_text(
        f"#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(str(hook_script))}\n"
    )
    hook.chmod(0o755)
    (project.project / "owned.txt").write_text("owned change\n")
    git(project.project, "add", "owned.txt")
    real_spawn = subprocess.Popen

    def spawn(argv, **kwargs):
        process = real_spawn(argv, **kwargs)
        roots[process.pid] = process
        stamp = inner.identity(process.pid)
        if stamp is not None:
            identities[process.pid] = stamp
        return process

    monkeypatch.setattr(verify.subprocess, "Popen", spawn)
    monkeypatch.setattr(owned_capture, "get_process_host", lambda: host)
    try:
        argv = ["git", "-C", str(project.project), "commit", "-qm", "scripted timeout"]
        with pytest.raises(verify.GitTimeoutError) as error:
            verify._run_git(argv, project.project, timeout_s=3)
        assert hook_pid.is_file() and child_pid.is_file(), "hook did not reach its startup premise"
        recorded = [json.loads(path.read_text()) for path in (hook_pid, child_pid)]
        proof = error.value.capture.ownership
        assert proof.quiescent, dataclasses.asdict(proof)
        assert proof.stdout_eof and proof.stderr_eof and proof.census_complete
        assert proof.root_reaped and proof.census_count > 0
        for pid, stamp in recorded:
            assert stamp is not None and inner.liveness_of(pid, stamp) == "dead"
            assert any(
                actor.pid == pid and actor.identity == stamp and actor.state == "dead"
                for actor in proof.actors
            )
        assert set(pid for pid, _stamp in signals).issubset({*roots, *(pid for pid, _ in recorded)})
        release.write_text("attempt a late mutation now\n")
        threading.Event().wait(0.1)
        assert not late.exists()
    finally:
        # Only exact identities created by this fixture may be stopped if its
        # assertions fail; no process search or peer signalling is permitted.
        for path in (child_pid, hook_pid):
            if path.is_file():
                pid, stamp = json.loads(path.read_text())
                if stamp is not None and inner.liveness_of(pid, stamp) == "alive":
                    inner.signal_owned(pid, stamp, force=True)
        for process in roots.values():
            if process.poll() is None:
                process.kill()
                process.wait(timeout=2)
