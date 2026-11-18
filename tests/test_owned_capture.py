"""Supervised binary process capture uses only scripted foreground programs."""

import dataclasses
import json
import math
import os
import subprocess
import sys
import threading
import time

import pytest

from bmad_loop.owned_capture import OwnedProcessTree, capture
from bmad_loop.process_host import DescendantObservation, ProcessHost, get_process_host


class RecordingHost(ProcessHost):
    def __init__(self):
        self.inner = get_process_host()
        self.signals = []

    def identity(self, pid):
        return self.inner.identity(pid)

    def liveness_of(self, pid, identity):
        return self.inner.liveness_of(pid, identity)

    def is_alive(self, pid):
        return self.inner.is_alive(pid)

    def hook_interpreter(self):
        return self.inner.hook_interpreter()

    def observe_descendants(self, pid):
        return self.inner.observe_descendants(pid)

    def terminate(self, pid):
        assert pid != os.getpid(), "capture attempted to signal its own caller"
        self.signals.append(("terminate", pid, self.identity(pid)))
        self.inner.terminate(pid)

    def force_kill(self, pid):
        assert pid != os.getpid(), "capture attempted to signal its own caller"
        self.signals.append(("kill", pid, self.identity(pid)))
        self.inner.force_kill(pid)

    def signal_owned(self, pid, identity, *, force):
        assert pid != os.getpid(), "capture attempted to signal its own caller"
        issued = self.inner.signal_owned(pid, identity, force=force)
        if issued:
            self.signals.append(("kill" if force else "terminate", pid, identity))
        return issued


def spawn(script, *, stdin=subprocess.DEVNULL):
    return lambda: subprocess.Popen(
        [sys.executable, "-c", script],
        stdin=stdin,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def test_binary_input_and_both_output_pipes_preserve_every_byte_under_backpressure():
    payload = bytes(range(256)) * 8192
    prefix = b"\xff\x00\r\n" * 65536
    errors = b"\xfe\r\n" * 65536
    script = (
        "import os,sys; "
        "sys.stdout.buffer.write(b'\\xff\\x00\\r\\n'*65536);sys.stdout.buffer.flush();"
        "sys.stderr.buffer.write(b'\\xfe\\r\\n'*65536);sys.stderr.buffer.flush();"
        "data=sys.stdin.buffer.read();sys.stdout.buffer.write(data);sys.stdout.buffer.flush()"
    )
    result = capture(spawn(script, stdin=subprocess.PIPE), input_data=payload, timeout_s=10)
    assert result.returncode == 0 and not result.timed_out
    assert result.stdout == prefix + payload
    assert result.stderr == errors
    assert result.input_bytes_written == len(payload) and result.input_complete
    assert result.ownership.quiescent, dataclasses.asdict(result.ownership)


def test_output_is_raw_and_decoding_belongs_to_the_caller():
    result = capture(
        spawn("import os;os.write(1,b'\\xff\\r\\n');os.write(2,b'\\x80')"), timeout_s=5
    )
    assert result.stdout == b"\xff\r\n" and result.stderr == b"\x80"
    with pytest.raises(UnicodeDecodeError):
        result.stdout.decode("utf-8")
    assert result.ownership.quiescent


def test_factory_runs_once_and_no_input_preserves_its_stdin_choice():
    processes = []

    def factory():
        process = spawn("import sys; print(len(sys.stdin.buffer.read()))")()
        assert process.stdin is None
        processes.append(process)
        return process

    result = capture(factory, timeout_s=5)
    assert len(processes) == 1 and result.stdout == b"0\n"
    assert result.input_bytes_written == 0 and result.input_complete


def test_capture_uses_only_exact_actor_signaling_not_legacy_recursive_kills():
    class ExactOnly(RecordingHost):
        def terminate(self, pid):
            raise AssertionError("legacy PID-only termination is forbidden")

        def force_kill(self, pid):
            raise AssertionError("legacy recursive force-kill is forbidden")

    host = ExactOnly()
    process = spawn("import time;time.sleep(60)")()
    try:
        result = capture(lambda: process, timeout_s=0.2, host=host)
        assert result.timed_out and result.ownership.quiescent
        assert any(pid == process.pid and stamp is not None for _, pid, stamp in host.signals)
    finally:
        if process.poll() is None:
            stamp = host.inner.identity(process.pid)
            if stamp is not None:
                host.inner.signal_owned(process.pid, stamp, force=True)
            process.wait(timeout=5)


def test_stdin_backpressure_cannot_hide_the_command_deadline():
    started = time.monotonic()
    result = capture(
        spawn("import time;time.sleep(60)", stdin=subprocess.PIPE),
        input_data=b"x" * (4 * 1024 * 1024),
        timeout_s=0.3,
    )
    assert result.timed_out and time.monotonic() - started < 5
    assert result.input_bytes_written < 4 * 1024 * 1024
    assert not result.input_complete
    assert result.ownership.quiescent, dataclasses.asdict(result.ownership)


@pytest.mark.parametrize("parent_exits", [False, True])
def test_observed_foreground_hook_or_pipe_child_cannot_outlive_capture(tmp_path, parent_exits):
    pidfile = tmp_path / "hook-pid"
    late = tmp_path / "late-write"
    child = (
        "import json,os,time;from pathlib import Path;from bmad_loop.process_host import get_process_host;Path(%r).write_text(json.dumps([os.getpid(),get_process_host().identity(os.getpid())]));time.sleep(60);Path(%r).write_text('late')"
        % (str(pidfile), str(late))
    )
    parent = (
        "import subprocess,sys,time;from pathlib import Path;"
        f"child=subprocess.Popen([sys.executable,'-c',{child!r}]);"
        f"ready=Path({str(pidfile)!r})\n"
        "while not ready.exists():time.sleep(.01)\n"
        f"time.sleep({.2 if parent_exits else 60})"
    )
    host = RecordingHost()
    try:
        result = capture(spawn(parent), timeout_s=3, host=host)
        assert pidfile.is_file(), "scripted hook did not reach its startup premise"
        child_pid, child_stamp = json.loads(pidfile.read_text())
        actor = next(row for row in result.ownership.actors if row.pid == child_pid)
        assert (
            actor.identity == child_stamp and actor.identity is not None and actor.state == "dead"
        )
        assert result.ownership.quiescent, dataclasses.asdict(result.ownership)
        assert result.timed_out is not parent_exits
        assert any(pid == child_pid for _, pid, _ in host.signals)
        assert not late.exists()
    finally:
        # Only a child named by this fixture's exact file may need cleanup on a
        # failed assertion; no host-wide process scan or process-group signal.
        if pidfile.exists():
            pid, stamp = json.loads(pidfile.read_text())
            if stamp is not None and host.inner.liveness_of(pid, stamp) == "alive":
                host.inner.signal_owned(pid, stamp, force=True)


def test_unobserved_pipe_holder_keeps_output_and_quiescence_incomplete(tmp_path):
    pidfile = tmp_path / "escaped-pid"
    child = (
        "import json,os,time;from pathlib import Path;from bmad_loop.process_host import get_process_host;Path(%r).write_text(json.dumps([os.getpid(),get_process_host().identity(os.getpid())]));time.sleep(60)"
        % str(pidfile)
    )
    parent = (
        "import subprocess,sys,time;from pathlib import Path;"
        f"subprocess.Popen([sys.executable,'-c',{child!r}]);"
        f"p=Path({str(pidfile)!r});"
        "\nwhile not p.exists():time.sleep(.01)\n"
    )

    class Unobserved(RecordingHost):
        def observe_descendants(self, pid):
            return DescendantObservation({}, True)

    host = Unobserved()
    started = time.monotonic()
    try:
        result = capture(spawn(parent), timeout_s=5, host=host)
        assert result.returncode == 0 and not result.timed_out
        assert not result.ownership.stdout_eof and not result.ownership.stderr_eof
        assert not result.ownership.quiescent
        assert time.monotonic() - started < 4
        assert not host.signals
    finally:
        if pidfile.exists():
            pid, stamp = json.loads(pidfile.read_text())
            if stamp is not None and host.inner.liveness_of(pid, stamp) == "alive":
                host.inner.signal_owned(pid, stamp, force=True)


def test_failed_census_is_not_a_healthy_empty_tree():
    class MissingCensus(RecordingHost):
        def observe_descendants(self, pid):
            return DescendantObservation({}, False, "fixture census unavailable")

    result = capture(
        spawn("import time;print('complete',flush=True);time.sleep(.15)"),
        timeout_s=5,
        host=MissingCensus(),
    )
    assert result.returncode == 0 and result.stdout == b"complete\n"
    assert not result.ownership.census_complete
    assert not result.ownership.quiescent
    assert "fixture census unavailable" in result.ownership.problems


def test_malformed_census_is_visible_uncertainty_not_healthy_empty():
    class Malformed(RecordingHost):
        def observe_descendants(self, pid):
            return None

    result = capture(
        spawn("import time;print('complete',flush=True);time.sleep(.15)"),
        timeout_s=5,
        host=Malformed(),
    )
    assert result.returncode == 0 and result.stdout == b"complete\n"
    assert not result.ownership.census_complete and not result.ownership.quiescent
    assert any("AttributeError" in problem for problem in result.ownership.problems)


def test_reused_descendant_identity_never_signals_the_unrelated_process():
    class Reused(RecordingHost):
        def observe_descendants(self, pid):
            stamp = self.inner.identity(os.getpid())
            assert stamp is not None
            return DescendantObservation({os.getpid(): math.nextafter(stamp, math.inf)}, True)

    host = Reused()
    result = capture(spawn("import time;time.sleep(.2)"), timeout_s=5, host=host)
    assert any(row.pid == os.getpid() and row.state == "dead" for row in result.ownership.actors)
    assert not any(pid == os.getpid() for _, pid, _ in host.signals)
    assert result.ownership.quiescent


def test_unstamped_descendant_keeps_quiescence_unknown():
    class Unstamped(RecordingHost):
        def observe_descendants(self, pid):
            return DescendantObservation({os.getpid(): None}, False, "fixture missing child birth")

    host = Unstamped()
    result = capture(spawn("import time;time.sleep(.15)"), timeout_s=5, host=host)
    assert result.returncode == 0
    assert not result.ownership.quiescent
    assert not host.signals


def test_root_identity_reuse_is_not_permission_to_signal_another_generation():
    class ReusedRoot(RecordingHost):
        observed = False

        def identity(self, pid):
            stamp = self.inner.identity(pid)
            return math.nextafter(stamp, math.inf) if self.observed and stamp is not None else stamp

        def observe_descendants(self, pid):
            self.observed = True
            return DescendantObservation({}, False, "fixture root generation changed")

        def liveness_of(self, pid, identity):
            return "dead" if self.observed else self.inner.liveness_of(pid, identity)

    host = ReusedRoot()
    process = spawn("import time;time.sleep(60)")()
    try:
        result = capture(lambda: process, timeout_s=0.2, host=host)
        assert result.timed_out and not result.ownership.root_reaped
        assert not result.ownership.quiescent and not host.signals
    finally:
        process.kill()
        process.wait(timeout=5)


def test_exception_unwind_cleans_up_and_preserves_the_original_exception():
    interrupt = KeyboardInterrupt("fixture cancellation")

    class Interrupted(RecordingHost):
        once = True

        def observe_descendants(self, pid):
            if self.once:
                self.once = False
                raise interrupt
            return super().observe_descendants(pid)

    with pytest.raises(KeyboardInterrupt) as caught:
        capture(spawn("import time;time.sleep(60)"), timeout_s=5, host=Interrupted())
    assert caught.value is interrupt
    assert caught.value.owned_capture.interrupted
    assert caught.value.owned_capture.ownership.root_reaped
    # The census itself was interrupted; stopping the known root cannot prove
    # that no unobserved child existed in that interrupted observation.
    assert not caught.value.owned_capture.ownership.census_complete
    assert not caught.value.owned_capture.ownership.quiescent


def test_invalid_text_factory_still_cleans_its_owned_process():
    processes = []

    def factory():
        process = subprocess.Popen(
            [sys.executable, "-c", "import time;time.sleep(60)"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        processes.append(process)
        return process

    with pytest.raises(TypeError, match="binary pipes") as caught:
        capture(factory, timeout_s=5)
    assert processes[0].poll() is not None
    assert caught.value.owned_capture.ownership.root_reaped


def test_owned_tree_keeps_text_protocol_and_lf_framing_caller_owned():
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import sys;data=sys.stdin.buffer.read();print(data.hex());print('protocol-stderr',file=sys.stderr)",
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    process.stdin.reconfigure(newline="\n")
    with OwnedProcessTree(process) as tree:
        stdout, stderr = process.communicate("café\n", timeout=5)
    assert stdout == "636166c3a90a\n" and stderr == "protocol-stderr\n"
    assert tree.evidence().quiescent, dataclasses.asdict(tree.evidence())
    assert tree.cleanup() == tree.evidence()
    with pytest.raises(RuntimeError, match="cannot be restarted"):
        tree.start()


def test_owned_tree_does_not_observe_a_reaped_root_pid():
    process = spawn("print('already finished')")()
    process.communicate(timeout=5)

    class NoPidReads(RecordingHost):
        def identity(self, pid):
            raise AssertionError("reaped PID could belong to a different process")

        def observe_descendants(self, pid):
            raise AssertionError("reaped PID cannot confer descendant ownership")

    with OwnedProcessTree(process, host=NoPidReads()) as tree:
        pass
    assert tree.evidence().root_identity is None
    assert tree.evidence().quiescent


def test_owned_tree_observes_foreground_child_during_opaque_validator_wait(tmp_path):
    pidfile = tmp_path / "interactive-hook-pid"
    release_parent = tmp_path / "release-parent"
    child_observed = threading.Event()
    child = (
        "import json,os,time;from pathlib import Path;from bmad_loop.process_host import get_process_host;Path(%r).write_text(json.dumps([os.getpid(),get_process_host().identity(os.getpid())]));time.sleep(60)"
        % str(pidfile)
    )
    parent = (
        f"import subprocess,sys,time;from pathlib import Path;subprocess.Popen([sys.executable,'-c',{child!r}]);"
        f"release=Path({str(release_parent)!r})\n"
        "while not release.exists():time.sleep(.01)\n"
    )

    class Observed(RecordingHost):
        def observe_descendants(self, pid):
            result = super().observe_descendants(pid)
            if result.members:
                child_observed.set()
            return result

    host = Observed()
    process = spawn(parent)()
    fault = ValueError("validator refused the prepared transaction")
    try:
        with pytest.raises(ValueError) as caught:
            with OwnedProcessTree(process, host=host):
                # No protocol polling calls the supervisor while this opaque
                # validator waits; the separate monitor must capture the child.
                deadline = time.monotonic() + 3
                while not pidfile.exists() and time.monotonic() < deadline:
                    threading.Event().wait(0.02)
                assert pidfile.exists(), "scripted child startup premise was not reached"
                assert child_observed.wait(2), "opaque validator wait lost its background census"
                release_parent.touch()
                process.wait(timeout=5)
                raise fault
        assert caught.value is fault
        evidence = caught.value.owned_process_tree
        pid, stamp = json.loads(pidfile.read_text())
        assert any(
            row.pid == pid and row.identity == stamp and row.state == "dead"
            for row in evidence.actors
        )
        assert evidence.quiescent and evidence.monitor_settled
        assert any(target == pid for _, target, _ in host.signals)
        assert not process.stdout.closed and not process.stderr.closed
    finally:
        for pipe in (process.stdout, process.stderr):
            pipe.close()
        if pidfile.exists():
            pid, stamp = json.loads(pidfile.read_text())
            if stamp is not None and host.inner.liveness_of(pid, stamp) == "alive":
                host.inner.signal_owned(pid, stamp, force=True)


def test_owned_tree_incomplete_census_never_becomes_healthy_after_root_exit():
    class Incomplete(RecordingHost):
        def observe_descendants(self, pid):
            return DescendantObservation({}, False, "interactive census incomplete")

    process = spawn("import time;print('success',flush=True);time.sleep(.2)")()
    with OwnedProcessTree(process, host=Incomplete()) as tree:
        stdout, _ = process.communicate(timeout=5)
    evidence = tree.evidence()
    assert process.returncode == 0 and stdout == b"success\n"
    assert evidence.root_reaped and evidence.monitor_settled
    assert not evidence.census_complete and not evidence.quiescent
    assert "interactive census incomplete" in evidence.problems


def test_stalled_census_does_not_block_owned_cleanup_or_claim_complete_inventory():
    entered, release = threading.Event(), threading.Event()

    class Stalled(RecordingHost):
        def observe_descendants(self, pid):
            entered.set()
            release.wait(10)
            return DescendantObservation({}, True)

    host = Stalled()
    process = spawn("import time;time.sleep(60)")()
    tree = OwnedProcessTree(process, host=host).start()
    try:
        assert entered.wait(2)
        started = time.monotonic()
        evidence = tree.cleanup()
        assert time.monotonic() - started < 3
        assert evidence.root_reaped
        assert not evidence.monitor_settled and not evidence.quiescent
        assert any("retained inventory only" in problem for problem in evidence.problems)
        assert any(target == process.pid for _, target, _ in host.signals)
    finally:
        release.set()
        tree._thread.join(timeout=2)
        assert not tree._thread.is_alive()
        for pipe in (process.stdout, process.stderr):
            pipe.close()
    assert tree.evidence() == evidence  # Late census cannot upgrade the returned proof.


@pytest.mark.parametrize("blocked", ["identity", "census", "liveness", "signal"])
def test_capture_deadline_bounds_stalled_ownership_primitives(monkeypatch, blocked):
    """Synthetic actors exercise deadlines without launching or signaling any PID."""
    from bmad_loop import owned_capture as module

    class Child:
        pid = 12345
        returncode = None
        stdin = stdout = stderr = None

        def poll(self):
            return self.returncode

    child = Child()
    entered, release = threading.Event(), threading.Event()
    calls = []
    caller = threading.get_ident()

    class Host:
        def note(self, name):
            calls.append((name, threading.get_ident()))
            if name == blocked:
                entered.set()
                release.wait(2)

        def identity(self, pid):
            self.note("identity")
            return 1.0

        def liveness_of(self, pid, identity):
            self.note("liveness")
            return "alive" if child.returncode is None else "dead"

        def observe_descendants(self, pid):
            self.note("census")
            return DescendantObservation({}, True)

        def signal_owned(self, pid, identity, *, force):
            self.note("signal")
            child.returncode = -9 if force else -15
            return True

    trees = []

    class Tracked(OwnedProcessTree):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            trees.append(self)

    monkeypatch.setattr(module, "OwnedProcessTree", Tracked)
    try:
        started = time.monotonic()
        result = capture(lambda: child, timeout_s=0.01, host=Host())
        elapsed = time.monotonic() - started
        assert entered.is_set(), "the blocked ownership primitive was never exercised"
        budget = 0.01 + module.TERMINATE_GRACE_S + module.KILL_GRACE_S + 2 * module.DRAIN_S
        assert elapsed < budget + 0.2, (elapsed, budget)
        assert result.timed_out and not result.ownership.quiescent
        assert not result.ownership.census_complete
        assert all(thread != caller for _, thread in calls)
        final = trees[0].evidence()
        before_release = len(calls)
    finally:
        release.set()
        for tree in trees:
            for worker in (tree._thread, tree._cleanup_thread):
                if worker is not None:
                    worker.join(timeout=3)
                    assert not worker.is_alive()
    assert len(calls) == before_release  # No fresh polling after the capture returned.
    assert trees[0].evidence() == final and not final.quiescent


def test_cleanup_never_reenters_census_on_the_caller_thread():
    class Child:
        pid = 12345
        returncode = None
        stdin = stdout = stderr = None

        def poll(self):
            return self.returncode

    child = Child()
    caller = threading.get_ident()
    calls = []

    class Host:
        def identity(self, pid):
            return 1.0

        def liveness_of(self, pid, identity):
            return "alive" if child.returncode is None else "dead"

        def observe_descendants(self, pid):
            assert threading.get_ident() != caller, "synchronous cleanup census"
            calls.append(pid)
            return DescendantObservation({}, True)

        def signal_owned(self, pid, identity, *, force):
            child.returncode = -15
            return True

    result = capture(lambda: child, timeout_s=0.03, host=Host())
    assert calls and result.timed_out and result.ownership.quiescent


def test_complete_bytes_remain_complete_when_census_is_stalled(monkeypatch):
    from bmad_loop import owned_capture as module

    entered, release = threading.Event(), threading.Event()

    class Stalled(RecordingHost):
        def observe_descendants(self, pid):
            entered.set()
            release.wait(2)
            return DescendantObservation({}, True)

    trees = []

    class Tracked(OwnedProcessTree):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            trees.append(self)

    monkeypatch.setattr(module, "OwnedProcessTree", Tracked)
    try:
        result = capture(
            spawn("import time;print('complete bytes',flush=True);time.sleep(.1)"),
            timeout_s=5,
            host=Stalled(),
        )
        assert entered.is_set()
        assert result.returncode == 0 and result.stdout == b"complete bytes\n"
        assert result.ownership.stdout_eof and result.ownership.stderr_eof
        assert result.ownership.io_settled
        assert not result.ownership.quiescent and not result.ownership.census_complete
    finally:
        release.set()
        for tree in trees:
            for worker in (tree._thread, tree._cleanup_thread):
                if worker is not None:
                    worker.join(timeout=3)
                    assert not worker.is_alive()
