"""Binary capture and bounded cleanup of an explicitly owned foreground child.

The caller constructs argv and spawns the process. This module neither launches
Git nor interprets output. Census completeness describes the observed process
tree only: a child that forks and escapes between observations can evade it.
Incomplete observations and pipes held past cleanup remain explicit uncertainty.
The command deadline includes spawn time once the callback returns; an OS spawn
blocked inside that synchronous callback cannot itself be interrupted here.
"""

from __future__ import annotations

import contextlib
import io
import math
import os
import selectors
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from typing import IO, Callable

from .process_host import ProcessHost, ProcessHostError, get_process_host

POLL_S = 0.02
CENSUS_S = 0.1
TERMINATE_GRACE_S = 0.25
KILL_GRACE_S = 0.75
DRAIN_S = 0.25
MAX_ACTORS = 4096


@dataclass(frozen=True)
class ActorEvidence:
    pid: int
    identity: float | None
    state: str
    terminate_sent: bool = False
    kill_sent: bool = False


@dataclass(frozen=True)
class CaptureOwnership:
    root_pid: int | None
    root_identity: float | None
    root_reaped: bool
    actors: tuple[ActorEvidence, ...]
    census_count: int
    census_complete: bool
    problems: tuple[str, ...]
    stdout_eof: bool
    stderr_eof: bool
    io_settled: bool
    stdout_captured: bool
    stderr_captured: bool
    cleanup_required: bool
    scope: str = "observed process tree; not universal daemon containment"

    @property
    def quiescent(self) -> bool:
        return (
            self.root_reaped
            and self.census_complete
            and not self.problems
            and self.stdout_eof
            and self.stderr_eof
            and self.io_settled
            and all(actor.state == "dead" for actor in self.actors)
        )


@dataclass(frozen=True)
class CaptureResult:
    returncode: int | None
    stdout: bytes
    stderr: bytes
    timed_out: bool
    interrupted: bool
    input_bytes_written: int
    input_complete: bool
    ownership: CaptureOwnership


@dataclass(frozen=True)
class ProcessTreeEvidence:
    """Owned actors only; protocol I/O settlement remains the caller's duty."""

    root_pid: int
    root_identity: float | None
    root_reaped: bool
    actors: tuple[ActorEvidence, ...]
    census_count: int
    census_complete: bool
    problems: tuple[str, ...]
    monitor_settled: bool
    scope: str = "observed process tree; excludes protocol I/O and universal daemon containment"

    @property
    def quiescent(self) -> bool:
        return (
            self.root_reaped
            and self.census_complete
            and not self.problems
            and self.monitor_settled
            and all(actor.state == "dead" for actor in self.actors)
        )


class _Pipe:
    """One owner per raw fd; never close a pipe under an outstanding read/write."""

    def __init__(self, stream: IO[bytes], payload: bytes | None = None, *, writing=False):
        if isinstance(stream, io.TextIOBase):
            raise TypeError("owned capture requires binary pipes")
        self.stream = stream
        self.payload = payload or b""
        self.writing = writing
        self.data = bytearray()
        self.written = 0
        self.eof = False
        self.error: BaseException | None = None
        self.stop = threading.Event()
        self.done = threading.Event()
        self.lock = threading.Lock()
        self.abandoned = False
        self.thread = threading.Thread(target=self._run, daemon=True, name="bmad-owned-capture")

    def _transfer(self, fd: int) -> bool:
        if self.writing:
            if self.written == len(self.payload):
                self.eof = True
                return False
            try:
                count = os.write(fd, self.payload[self.written : self.written + 4096])
            except BlockingIOError:
                return True
            except BrokenPipeError:
                return False  # Child refusal is reflected in its exit and input_complete.
            if count <= 0:
                raise OSError("owned capture stdin made no progress")
            self.written += count
            if self.written == len(self.payload):
                self.eof = True
                return False
        else:
            try:
                data = os.read(fd, 32768)
            except BlockingIOError:
                return True
            if not data:
                self.eof = True
                return False
            with self.lock:
                self.data.extend(data)
        return True

    def _run(self) -> None:
        try:
            fd = self.stream.fileno()
            if self.writing and not self.payload:
                self.eof = True
            elif sys.platform == "win32":
                # Anonymous Windows pipes cannot use the POSIX selector seam.
                # On an uncertain drain this daemon retains its fd until the
                # outstanding operation ends; another thread never closes it.
                while not self.stop.is_set() and self._transfer(fd):
                    pass
            else:
                os.set_blocking(fd, False)
                with selectors.DefaultSelector() as selector:
                    selector.register(
                        fd, selectors.EVENT_WRITE if self.writing else selectors.EVENT_READ
                    )
                    while not self.stop.is_set():
                        if selector.select(POLL_S) and not self._transfer(fd):
                            break
        except BaseException as exc:
            self.error = exc
        finally:
            # Closing our stdin signals EOF. Reader streams can remain open
            # until the supervisor has copied their retained bytes.
            if self.writing:
                with contextlib.suppress(OSError):
                    self.stream.close()
            with self.lock:
                self.done.set()
                abandoned = self.abandoned
            if abandoned and not self.writing:
                with contextlib.suppress(OSError):
                    self.stream.close()

    def bytes(self) -> bytes:
        with self.lock:
            return bytes(self.data)

    def close(self) -> None:
        with self.lock:
            self.abandoned = True
            finished = self.done.is_set() or self.thread.ident is None
        if finished:
            with contextlib.suppress(OSError):
                self.stream.close()


class _Ownership:
    def __init__(self, proc: subprocess.Popen, host: ProcessHost, *, active=None):
        self.proc, self.host = proc, host
        self.active = active or (lambda: True)
        self.root_identity: float | None = None
        self.known: dict[tuple[int, float | None], ActorEvidence] = {}
        self.problems: list[str] = []
        self.complete = True
        self.censuses = 0

    def problem(self, value: str) -> None:
        self.complete = False
        value = value[:512]
        if value not in self.problems and len(self.problems) < 32:
            self.problems.append(value)

    def initialize(self) -> None:
        if self.proc.returncode is not None:
            self.known[(self.proc.pid, None)] = ActorEvidence(self.proc.pid, None, "dead")
            return  # Never stamp a reaped numeric PID which could now be a peer.
        try:
            self.root_identity = self.identity(self.proc.pid)
        except (OSError, ProcessHostError, ValueError) as exc:
            self.problem("root identity unavailable: " + type(exc).__name__)
        if self.proc.returncode is not None:
            self.root_identity = None  # Another owner thread may have reaped it during the read.
        self.known[(self.proc.pid, self.root_identity)] = ActorEvidence(
            self.proc.pid, self.root_identity, "unknown"
        )
        if self.root_identity is None and self.proc.poll() is None:
            self.problem("live root has no birth identity")

    def identity(self, pid: int) -> float | None:
        if not self.active():
            return None
        value = self.host.identity(pid)
        if not self.active():
            return None
        if value is not None and (type(value) not in (int, float) or not math.isfinite(value)):
            self.problem("actor birth identity is invalid")
            return None
        return value

    def state(self, key: tuple[int, float | None]) -> str:
        pid, identity = key
        if pid == self.proc.pid and self.proc.poll() is not None:
            return "dead"  # Popen independently reaped this exact root.
        if identity is None:
            return "unknown"
        if not self.active():
            return "unknown"
        try:
            state = self.host.liveness_of(pid, identity)
        except (OSError, ProcessHostError, ValueError) as exc:
            self.problem("actor liveness unavailable: " + type(exc).__name__)
            return "unknown"
        if state not in {"alive", "dead", "unknown"}:
            self.problem("actor liveness returned an invalid state")
            return "unknown"
        return state if self.active() else "unknown"

    def observe(self) -> None:
        # A reaped root is never enumerated: its numeric PID may now be a peer.
        pending = list(self.known)
        covered: set[tuple[int, float | None]] = set()
        for key in pending:
            if not self.active():
                return
            if key in covered or self.state(key) != "alive":
                continue
            pid, identity = key
            try:
                self.censuses += 1
                if not self.active():
                    return
                observation = self.host.observe_descendants(pid)
                if not self.active():
                    return
                current = self.identity(pid)
                # Popen still pins our unreaped root PID during this census.
                # Other observed parents have no such handle, so their birth
                # must still match before their newly found children are adopted.
                if current != identity and not (
                    pid == self.proc.pid and current is None and self.proc.poll() is not None
                ):
                    self.problem("census owner changed before attribution")
                    continue
                if type(observation.complete) is not bool:
                    self.problem("descendant census completeness is invalid")
                elif not observation.complete:
                    self.problem(observation.problem or "descendant census incomplete")
                for child, stamp in observation.members.items():
                    if (
                        type(child) is not int
                        or child <= 0
                        or (
                            stamp is not None
                            and (type(stamp) not in (int, float) or not math.isfinite(stamp))
                        )
                    ):
                        self.problem("descendant census returned an invalid actor")
                        continue
                    member = (child, stamp)
                    covered.add(member)
                    if stamp is None:
                        self.problem("observed descendant has no birth identity")
                    if member not in self.known:
                        if len(self.known) >= MAX_ACTORS:
                            self.problem("observed actor bound exceeded")
                            continue
                        self.known[member] = ActorEvidence(child, stamp, "unknown")
            except (OSError, ProcessHostError, ValueError, TypeError, AttributeError) as exc:
                self.problem("descendant census unavailable: " + type(exc).__name__)

    def signal(self, *, force: bool) -> None:
        from dataclasses import replace

        for key, row in list(self.known.items()):
            if not self.active():
                return
            if row.identity is None or self.state(key) != "alive":
                continue
            try:
                if self.identity(row.pid) != row.identity:
                    continue
                if not self.active():
                    return
                if force:
                    if row.kill_sent:
                        continue
                    issued = self.host.signal_owned(row.pid, row.identity, force=True)
                    if issued is True:
                        self.known[key] = replace(row, kill_sent=True)
                else:
                    if row.terminate_sent:
                        continue
                    issued = self.host.signal_owned(row.pid, row.identity, force=False)
                    if issued is True:
                        self.known[key] = replace(row, terminate_sent=True)
                if type(issued) is not bool:
                    self.problem("owned signal returned an invalid acknowledgement")
            except ProcessLookupError:
                continue
            except (OSError, ProcessHostError, ValueError) as exc:
                self.problem("owned signal failed: " + type(exc).__name__)

    def settled(self) -> bool:
        return self.proc.poll() is not None and all(self.state(key) == "dead" for key in self.known)


@dataclass(frozen=True)
class _SavedTree:
    root_identity: float | None
    actors: tuple[tuple[tuple[int, float | None], ActorEvidence], ...]
    complete: bool
    problems: tuple[str, ...]
    censuses: int


class OwnedProcessTree:
    """Supervise an existing Popen without touching its pipes or blocking on census.

    Birth checks, census and cleanup primitives run on bounded-wait workers. The
    caller only consumes immutable publications. A blocked OS call cannot itself
    be canceled; after the deadline its thread stays visibly unsettled and may
    finish that call, but cannot start further polling or upgrade final evidence.
    """

    def __init__(self, proc: subprocess.Popen, *, host: ProcessHost | None = None):
        self._stop = threading.Event()
        self._cleanup_stop = threading.Event()
        # Stopping the poll loop must still let its in-flight census publish
        # before cleanup joins it. The return fence, not the poll-loop flag,
        # prevents additional host calls after the caller has returned.
        self._tree = _Ownership(
            proc, host or get_process_host(), active=lambda: not self._cleanup_stop.is_set()
        )
        self._tree.known[(proc.pid, None)] = ActorEvidence(
            proc.pid, None, "dead" if proc.returncode is not None else "unknown"
        )
        self._saved = self._snapshot(self._tree)
        self._cleanup_saved: _SavedTree | None = None
        self._thread: threading.Thread | None = None
        self._cleanup_thread: threading.Thread | None = None
        self._final: ProcessTreeEvidence | None = None
        self._started = False
        self._monitor_error: BaseException | None = None
        self._cleanup_error: BaseException | None = None

    @staticmethod
    def _snapshot(tree: _Ownership, *, states: bool = False) -> _SavedTree:
        from dataclasses import replace

        actors = tuple(
            (key, replace(row, state=tree.state(key)) if states else row)
            for key, row in tree.known.items()
        )
        return _SavedTree(
            tree.root_identity, actors, tree.complete, tuple(tree.problems), tree.censuses
        )

    def _copy(self, *, active=None) -> _Ownership:
        saved = self._saved
        tree = _Ownership(self._tree.proc, self._tree.host, active=active)
        tree.root_identity = saved.root_identity
        tree.known = dict(saved.actors)
        tree.complete = saved.complete
        tree.problems = list(saved.problems)
        tree.censuses = saved.censuses
        return tree

    def _evidence(self, *, final: bool = False) -> ProcessTreeEvidence:
        from dataclasses import replace

        saved = self._cleanup_saved or self._saved
        monitor_settled = self._thread is None or not self._thread.is_alive()
        cleanup_settled = self._cleanup_thread is None or not self._cleanup_thread.is_alive()
        reaped = self._tree.proc.poll() is not None  # Our Popen handle, never a PID census.
        actors = tuple(
            replace(row, state="dead") if row.pid == self._tree.proc.pid and reaped else row
            for _, row in saved.actors
        )
        problems = list(saved.problems)
        if final and not monitor_settled:
            problems.append("census monitor did not settle; retained inventory only")
        if final and not cleanup_settled:
            problems.append("owned cleanup worker did not settle")
        if final and (not self._started or not saved.actors):
            problems.append("owned root observation did not complete")
        if self._cleanup_error is not None:
            problems.append("owned cleanup failed: " + type(self._cleanup_error).__name__)
        complete = saved.complete and not (final and problems)
        return ProcessTreeEvidence(
            self._tree.proc.pid,
            saved.root_identity,
            reaped,
            actors,
            max(saved.censuses, self._tree.censuses),
            complete,
            tuple(dict.fromkeys(problems)),
            monitor_settled and cleanup_settled,
        )

    def _monitor(self) -> None:
        try:
            self._tree.initialize()
            if self._cleanup_stop.is_set():
                return
            self._saved = self._snapshot(self._tree)
            while not self._stop.is_set():
                if self._tree.root_identity is None and self._tree.proc.returncode is None:
                    self._tree.initialize()
                self._tree.observe()
                saved = self._snapshot(self._tree, states=True)
                if not self._cleanup_stop.is_set():
                    self._saved = saved
                self._stop.wait(CENSUS_S)
        except BaseException as exc:
            self._monitor_error = exc
            self._tree.problem("census monitor failed: " + type(exc).__name__)
            if not self._cleanup_stop.is_set():
                self._saved = self._snapshot(self._tree)
            self._stop.set()

    def start(self) -> OwnedProcessTree:
        if self._started or self._final is not None:
            raise RuntimeError("owned process monitor cannot be restarted")
        self._started = True
        try:
            self._thread = threading.Thread(
                target=self._monitor, daemon=True, name="bmad-owned-process-census"
            )
            self._thread.start()
        except BaseException as exc:
            self._tree.problem("census monitor startup failed: " + type(exc).__name__)
            self._saved = self._snapshot(self._tree)
            try:
                setattr(exc, "owned_process_tree", self.cleanup())
            except BaseException as cleanup_error:
                exc.add_note("Owned process cleanup failed: " + type(cleanup_error).__name__)
            raise
        return self

    def evidence(self) -> ProcessTreeEvidence:
        return self._final if self._final is not None else self._evidence()

    def raise_if_failed(self) -> None:
        """Surface a monitor exception without performing an observation here."""
        if self._monitor_error is not None:
            raise self._monitor_error

    def _merge_observed(self, tree: _Ownership) -> None:
        saved = self._saved
        if tree.root_identity is None:
            tree.root_identity = saved.root_identity
        for key, row in saved.actors:
            tree.known.setdefault(key, row)
        tree.complete = tree.complete and saved.complete
        for problem in saved.problems:
            tree.problem(problem)
        tree.censuses = max(tree.censuses, saved.censuses, self._tree.censuses)

    def _cleanup(self, terminate_until: float, until: float) -> None:
        def active():
            return not self._cleanup_stop.is_set() and time.monotonic() < until

        tree = self._copy(active=active)

        def settle_observed() -> None:
            while active():
                self._merge_observed(tree)
                tree.signal(force=time.monotonic() >= terminate_until)
                saved = self._snapshot(tree, states=True)
                if active():
                    self._cleanup_saved = saved
                if tree.settled():
                    return
                self._cleanup_stop.wait(min(POLL_S, max(0.0, until - time.monotonic())))

        try:
            settle_observed()
            self._stop.set()
            if self._thread is not None and self._thread.ident is not None:
                self._thread.join(min(DRAIN_S, max(0.0, until - time.monotonic())))
            self._merge_observed(tree)
            if self._thread is not None and self._thread.is_alive():
                tree.problem("census monitor did not settle; retained inventory only")
            # A last publication may name actors observed as the root exited.
            # Reuse the SAME deadlines; never start a synchronous fallback census.
            settle_observed()
            saved = self._snapshot(tree, states=True)
            if active():
                self._cleanup_saved = saved
        except BaseException as exc:
            self._cleanup_error = exc
            tree.problem("owned process cleanup failed: " + type(exc).__name__)
            if active():
                self._cleanup_saved = self._snapshot(tree)
        finally:
            self._stop.set()

    def cleanup(self) -> ProcessTreeEvidence:
        if self._final is not None:
            return self._final
        if not self._started:
            self.start()
        terminate_until = time.monotonic() + TERMINATE_GRACE_S
        until = terminate_until + KILL_GRACE_S
        interrupted = None
        try:
            self._cleanup_thread = threading.Thread(
                target=self._cleanup,
                args=(terminate_until, until),
                daemon=True,
                name="bmad-owned-process-cleanup",
            )
            self._cleanup_thread.start()
            self._cleanup_thread.join(max(0.0, until - time.monotonic()))
        except BaseException as exc:
            interrupted = exc
            self._cleanup_error = self._cleanup_error or exc
        finally:
            # No late worker may start another census/signal after this return.
            # An already blocked OS call is not cancelable and remains uncertain.
            self._stop.set()
            self._cleanup_stop.set()
            if (
                self._thread is not None
                and self._thread.ident is not None
                and (self._cleanup_thread is None or self._cleanup_thread.is_alive())
            ):
                try:
                    self._thread.join(min(DRAIN_S, max(0.0, until - time.monotonic())))
                except BaseException as exc:
                    interrupted = interrupted or exc
        self._final = self._evidence(final=True)
        failure = interrupted or self._cleanup_error
        if failure is not None:
            try:
                setattr(failure, "owned_process_tree", self._final)
            except (AttributeError, TypeError):
                failure.add_note("Owned process cleanup is uncertain")
            raise failure
        return self._final

    def __enter__(self) -> OwnedProcessTree:
        return self.start()

    def __exit__(self, exc_type, exc, traceback) -> bool:
        try:
            evidence = self.cleanup()
        except BaseException as cleanup_error:
            if exc is None:
                raise
            exc.add_note("Owned process cleanup failed: " + type(cleanup_error).__name__)
            evidence = self._final
        if exc is not None and evidence is not None:
            try:
                setattr(exc, "owned_process_tree", evidence)
            except (AttributeError, TypeError):
                exc.add_note("Owned process cleanup quiescent: " + str(evidence.quiescent))
        return False


def capture(
    spawn: Callable[[], subprocess.Popen[bytes]],
    *,
    timeout_s: float,
    input_data: bytes | None = None,
    host: ProcessHost | None = None,
) -> CaptureResult:
    """Send binary input once and collect raw output with owned-tree evidence.

    Factories retain stdin inheritance by returning ``stdin=None``. Explicit
    input requires a binary pipe. No ambient stop probe is consulted: callers
    also use this seam for preservation after a stop. On any exception, bounded
    cleanup runs and its result is attached as ``owned_capture`` before the
    original exception is re-raised. Output is retained in full for byte-exact
    Git compatibility; transfer chunks and waits are bounded, not total output.
    """
    if isinstance(timeout_s, bool) or not math.isfinite(timeout_s) or timeout_s < 0:
        raise ValueError("owned capture requires a finite nonnegative timeout")
    if input_data is not None and not isinstance(input_data, bytes):
        raise TypeError("owned capture input must be bytes")
    host = host or get_process_host()
    deadline = time.monotonic() + timeout_s
    proc = spawn()
    ownership = OwnedProcessTree(proc, host=host)
    tree: ProcessTreeEvidence | None = None
    pipes: list[_Pipe] = []
    output = error = writer = None
    timed_out = False
    failure: BaseException | None = None
    cleanup_required = False
    try:
        ownership.start()
        if input_data is not None and proc.stdin is None:
            raise ValueError("explicit input requires an owned binary stdin pipe")
        for stream, writing in ((proc.stdout, False), (proc.stderr, False), (proc.stdin, True)):
            if stream is None:
                continue
            pipe = _Pipe(stream, input_data, writing=writing)
            pipes.append(pipe)
            if writing:
                writer = pipe
            elif stream is proc.stdout:
                output = pipe
            else:
                error = pipe
        for pipe in pipes:
            pipe.thread.start()
        while True:
            now = time.monotonic()
            ownership.raise_if_failed()
            if now >= deadline:
                timed_out = True
                break
            for pipe in pipes:
                if pipe.error is not None:
                    raise pipe.error
            if time.monotonic() >= deadline:
                timed_out = True
                break
            if proc.poll() is not None:
                break
            threading.Event().wait(min(POLL_S, max(0.0, deadline - now)))
    except BaseException as exc:
        failure = exc
    finally:
        # Stop further input first. Draining output remains useful during cleanup.
        if writer is not None:
            writer.stop.set()
        try:
            observed = ownership.evidence()
            cleanup_required = not observed.root_reaped or any(
                actor.state != "dead" for actor in observed.actors
            )
        except BaseException as exc:
            cleanup_required = True
            failure = failure or exc
        try:
            tree = ownership.cleanup()
        except BaseException as exc:
            failure = failure or exc
            tree = ownership.evidence()
        until = time.monotonic() + DRAIN_S
        for pipe in pipes:
            if pipe.thread.ident is not None:
                pipe.done.wait(max(0.0, until - time.monotonic()))
        for pipe in pipes:
            pipe.stop.set()
        until = time.monotonic() + DRAIN_S
        for pipe in pipes:
            if pipe.thread.ident is not None:
                pipe.thread.join(max(0.0, until - time.monotonic()))
            pipe.close()
            if pipe.error is not None:
                failure = failure or pipe.error
        # Streams rejected before a pump was constructed still belong to us.
        managed = {id(pipe.stream) for pipe in pipes}
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            if stream is not None and id(stream) not in managed:
                with contextlib.suppress(OSError):
                    stream.close()
    assert tree is not None
    result = CaptureResult(
        proc.poll(),
        output.bytes() if output else b"",
        error.bytes() if error else b"",
        timed_out,
        isinstance(failure, (KeyboardInterrupt, SystemExit)),
        writer.written if writer else 0,
        (writer.eof if writer else input_data is None),
        CaptureOwnership(
            proc.pid,
            tree.root_identity,
            tree.root_reaped,
            tree.actors,
            tree.census_count,
            tree.census_complete,
            tree.problems,
            output.eof if output else proc.stdout is None,
            error.eof if error else proc.stderr is None,
            all(pipe.done.is_set() for pipe in pipes),
            proc.stdout is not None,
            proc.stderr is not None,
            cleanup_required,
        ),
    )
    if failure is not None:
        try:
            setattr(failure, "owned_capture", result)
        except (AttributeError, TypeError):
            failure.add_note("Owned capture cleanup quiescent: " + str(result.ownership.quiescent))
        raise failure
    return result
