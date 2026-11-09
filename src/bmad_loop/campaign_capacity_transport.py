"""Bounded, no-session native status transport; never sends a model prompt.

The caller supplies only fixed read-only protocol messages. Output and elapsed
time are bounded across both pipes. Child ownership and cleanup use the same
process-host seam as operator commands, including known reparented descendants.
Provider payloads are not included in errors (they may contain account data).
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from .childrun import hard_stop_pending, kill_tree
from .process_host import get_process_host

MAX_OUTPUT_BYTES = 256 * 1024
POLL_S = 0.05
EXIT_GRACE_S = 1.0
_STATUS_NOTIFICATIONS = {
    "_x.ai/models/update",
    "x.ai/models/update",
    "_x.ai/mcp/servers_updated",
    "x.ai/mcp/servers_updated",
}


class ProbeError(ValueError):
    """A native status observation is incomplete or unverifiable."""


class StatusProcess:
    """One owned process, bounded pipe capture, and an absolute job deadline."""

    def __init__(self, argv: list[str], project: Path, env: dict[str, str], deadline: float):
        if hard_stop_pending():
            raise ProbeError("native capacity observation interrupted")
        if time.monotonic() >= deadline:
            raise ProbeError("native capacity observation timed out")
        self.host = get_process_host()
        self.deadline = deadline
        self.known: dict[int, float | None] = {}
        # The cumulative byte counter bounds capture across both streams.
        # A separate item limit would reject harmless fragmented pipe reads.
        self.messages: queue.Queue[bytes | None] = queue.Queue()
        self.stopped = threading.Event()
        self.lock = threading.Lock()
        self.total = 0
        self.problem = ""
        self.buffer = b""
        self.eof = False
        self.protocol = False
        # This handle participates in stream-agnostic childrun cleanup. Wire I/O
        # is binary so Windows cannot translate JSON-RPC LF frames to CRLF.
        self.proc: subprocess.Popen[Any] = subprocess.Popen(
            argv,
            cwd=project,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.threads = []
        try:
            for stream, stdout in ((self.proc.stdout, True), (self.proc.stderr, False)):
                thread = threading.Thread(target=self._read, args=(stream, stdout), daemon=True)
                self.threads.append(thread)
                thread.start()
        except BaseException:
            kill_tree(self.proc, known=self.known)
            raise

    def _read(self, stream, stdout: bool) -> None:
        try:
            while not self.stopped.is_set():
                piece = os.read(stream.fileno(), 4096)
                if not piece:
                    if stdout:
                        self.messages.put_nowait(None)
                    return
                with self.lock:
                    self.total += len(piece)
                    if self.total > MAX_OUTPUT_BYTES:
                        self.problem = "native capacity output exceeded its bound"
                        return
                if stdout:
                    self.messages.put_nowait(piece)
        except (OSError, ValueError):
            if not self.stopped.is_set():
                self.problem = "native capacity stream failed"

    def __enter__(self):
        return self

    def _check(self) -> None:
        if self.problem:
            raise ProbeError(self.problem)
        if hard_stop_pending():
            raise ProbeError("native capacity observation interrupted")
        if time.monotonic() >= self.deadline:
            raise ProbeError("native capacity observation timed out")
        if self.proc.poll() is None:
            self.known.update(self.host.descendants(self.proc.pid))

    def _piece(self) -> bytes:
        while True:
            self._check()
            try:
                piece = self.messages.get(timeout=POLL_S)
            except queue.Empty:
                continue
            if piece is None:
                self.eof = True
                return b""
            return piece

    def json_output(self) -> object:
        """Read one ordinary JSON command, with EOF on its input immediately."""
        assert self.proc.stdin is not None
        self.proc.stdin.close()
        while not self.eof:
            self.buffer += self._piece()
        try:
            return json.loads(self.buffer)
        except (ValueError, UnicodeError, RecursionError) as exc:
            raise ProbeError("native capacity returned invalid JSON") from exc

    def notify(self, method: str) -> None:
        self._send({"jsonrpc": "2.0", "method": method})

    def _send(self, message: dict) -> None:
        self._check()
        assert self.proc.stdin is not None
        try:
            self.proc.stdin.write((json.dumps(message) + "\n").encode("utf-8"))
            self.proc.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise ProbeError("native capacity input closed") from exc

    def request(self, identifier: int, method: str, params: dict) -> dict:
        self.protocol = True
        self._send({"jsonrpc": "2.0", "id": identifier, "method": method, "params": params})
        while True:
            while b"\n" not in self.buffer:
                if self.eof:
                    raise ProbeError("native capacity protocol closed before its response")
                self.buffer += self._piece()
            line, self.buffer = self.buffer.split(b"\n", 1)
            try:
                message = json.loads(line)
            except (ValueError, UnicodeError, RecursionError) as exc:
                raise ProbeError("native capacity returned invalid protocol JSON") from exc
            if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
                raise ProbeError("native capacity protocol schema mismatch")
            if "method" in message:
                if "id" in message:
                    raise ProbeError("native capacity requested an unauthorized client action")
                # No model work is requested. A provider that nevertheless
                # emits turn/session events has violated this status contract.
                if (
                    not isinstance(message["method"], str)
                    or message["method"] not in _STATUS_NOTIFICATIONS
                ):
                    raise ProbeError("native capacity emitted an unexpected notification")
                continue
            if type(message.get("id")) is not int or message["id"] != identifier:
                raise ProbeError("native capacity response identity mismatch")
            if "error" in message:
                raise ProbeError("native capacity status request was refused")
            result = message.get("result")
            if not isinstance(result, dict):
                raise ProbeError("native capacity result schema mismatch")
            return result

    def __exit__(self, exc_type, exc, traceback):
        cleanup_problem = ""
        cleanup_uncertain = False
        primary = exc
        forced = False
        try:
            if self.proc.stdin and not self.proc.stdin.closed:
                self.proc.stdin.close()
            # Successful native status clients may drain background metadata
            # on EOF. Retain the same whole-job budget; fault cleanup is short.
            until = (
                self.deadline
                if exc is None
                else min(self.deadline, time.monotonic() + EXIT_GRACE_S)
            )
            while self.proc.poll() is None and time.monotonic() < until:
                self.known.update(self.host.descendants(self.proc.pid))
                try:
                    self.proc.wait(timeout=POLL_S)
                except subprocess.TimeoutExpired:
                    pass
            forced = self.proc.poll() is None
        except BaseException as error:
            primary = primary or error
        finally:
            # Census faults and cancellation must not bypass cleanup of the
            # process and already observed descendants. Retain the first error.
            try:
                kill_tree(self.proc, known=self.known)
                if self.proc.poll() is None or any(
                    self.host.liveness_of(pid, identity) != "dead"
                    for pid, identity in self.known.items()
                ):
                    cleanup_uncertain = True
                elif forced:
                    cleanup_problem = "native capacity process did not exit on EOF"
                elif self.proc.returncode:
                    cleanup_problem = "native capacity process failed"
            except BaseException as error:
                cleanup_uncertain = True
                primary = primary or error
            try:
                # Consume natural EOF before setting stopped: buffered tail may
                # contain forbidden agent activity or an output-bound violation.
                for thread in self.threads:
                    thread.join(timeout=EXIT_GRACE_S)
                readers_alive = any(thread.is_alive() for thread in self.threads)
                if primary is None and self.protocol and not readers_alive:
                    while not self.messages.empty():
                        piece = self.messages.get_nowait()
                        if piece is not None:
                            self.buffer += piece
                    for line in self.buffer.splitlines():
                        try:
                            message = json.loads(line)
                        except (ValueError, UnicodeError, RecursionError):
                            cleanup_problem = "native capacity trailing protocol is invalid"
                            break
                        if (
                            not isinstance(message, dict)
                            or message.get("jsonrpc") != "2.0"
                            or "id" in message
                            or not isinstance(message.get("method"), str)
                            or message.get("method") not in _STATUS_NOTIFICATIONS
                        ):
                            cleanup_problem = "native capacity emitted unexpected trailing activity"
                            break
                for stream in (self.proc.stdout, self.proc.stderr):
                    if stream and not readers_alive:
                        stream.close()
                if readers_alive:
                    cleanup_uncertain = True
            except BaseException as error:
                cleanup_uncertain = True
                primary = primary or error
            finally:
                self.stopped.set()
        if cleanup_uncertain:
            cleanup_problem = "native capacity cleanup is unconfirmed"
            if primary is not None:
                primary.add_note(cleanup_problem)
                # A fixed marker survives observe()'s sanitized public boundary.
                setattr(primary, "_native_capacity_cleanup_unconfirmed", True)
        if primary is not None and primary is not exc:
            raise primary
        if exc is None and (cleanup_problem or self.problem):
            raise ProbeError(cleanup_problem or self.problem)
