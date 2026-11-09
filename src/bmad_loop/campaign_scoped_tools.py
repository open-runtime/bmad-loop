"""Bounded command tools for disposable native reasoning workers.

The native harness keeps its supported read-only sandbox. Every writing command
runs separately under frozen BMAD filesystem grants. This worker is never called
from the coordinator tick. Commands may fork foreground helpers, but persistent
background services require a separate lifecycle contract and are terminated.
The macOS route independently scans its session and observed birth-stamped
descendants after teardown. A child that both detaches and reparents between
observations can evade this inventory; this is finite foreground qualification,
not universal daemon containment or permission to manage unrelated daemons.
"""

from __future__ import annotations

import argparse
import json
import os
import selectors
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from .campaign_execution_sandbox import command
from .campaign_headless import file_digest, read_json, write_json
from .campaign_jobs import digest
from .campaign_model import CampaignError
from .childrun import kill_tree
from .platform_util import detach_kwargs, file_lock, is_link_like
from .process_host import get_process_host

JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "command": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 256}
    },
    "required": ["command"],
    "additionalProperties": False,
}
DESCRIPTION = (
    "Run a foreground argv command in the owned workspace under exact BMAD write grants. "
    "Use an explicit shell argv for shell syntax. No persistent background jobs. "
    "Output, duration and total calls are bounded; failed or interrupted calls are not replayed."
)
_GATE = (
    "import os,sys,json; "
    "accepted=os.read(0,1)==b'1'; "
    "os.close(0); os.open(os.devnull,os.O_RDONLY); "
    "argv=json.loads(sys.argv[1]); "
    "os.execvpe(argv[0],argv,os.environ) if accepted else sys.exit(125)"
)


def _response(success, text):
    return {"success": bool(success), "contentItems": [{"type": "inputText", "text": text}]}


def _session_members(session, host, leader_identity=None):
    """Read back all session members, including children already reparented.

    A vanished PID is normal; an unreadable matching process or unavailable
    enumeration is not quiescence. Detached processes observed while their
    parent is still alive are separately retained in the caller's known tree.
    """
    if sys.platform != "darwin":
        raise CampaignError("Scoped process-session confinement is only qualified on macOS")
    import psutil

    if leader_identity is not None:
        current = host.identity(session)
        if current is not None and current != leader_identity:
            raise CampaignError("Scoped command session leader PID was reused")
    members = {}
    for pid in psutil.pids():
        try:
            if os.getsid(pid) != session:
                continue
            process = psutil.Process(pid)
            if process.status() == psutil.STATUS_ZOMBIE:
                continue  # No executable writer remains; the parent owns reaping.
            identity = host.identity(pid)
            if identity is None:
                raise CampaignError("Scoped command process identity is unreadable")
            # Bind both observations before granting signal authority.
            if os.getsid(pid) == session and host.identity(pid) == identity:
                members[pid] = identity
        except (ProcessLookupError, psutil.NoSuchProcess):
            continue
    return members


def _known_writers(known, host):
    import psutil

    remaining = {}
    for pid, identity in known.items():
        state = host.liveness_of(pid, identity)
        if state == "dead":
            continue
        try:
            if psutil.Process(pid).status() == psutil.STATUS_ZOMBIE:
                continue
        except psutil.NoSuchProcess:
            continue
        remaining[pid] = identity
    return remaining


class ScopedTools:
    """One immutable grant and finite call budget, shared by native tool frontends."""

    def __init__(
        self,
        grants_path: Path,
        directory: Path,
        max_calls: int = 32,
        timeout_s: int = 60,
        max_output_bytes: int = 64 * 1024,
    ):
        for value, upper, label in (
            (max_calls, 1024, "calls"),
            (timeout_s, 3600, "timeout"),
            (max_output_bytes, 1024 * 1024, "output"),
        ):
            if type(value) is not int or not 0 < value <= upper:
                raise CampaignError(f"Invalid scoped tool {label} bound")
        self.grants_path = grants_path.absolute()
        self.directory = directory.absolute()
        if is_link_like(self.directory):
            raise CampaignError("Scoped tool directory is redirected")
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        grants = read_json(self.grants_path)
        self.grants_digest = file_digest(self.grants_path)
        self.workspace = Path(grants["workspace"]).resolve()
        if self.directory.resolve().is_relative_to(self.workspace):
            raise CampaignError("Scoped tool lifecycle records must be outside the workspace")
        protected = (self.directory.resolve(), self.grants_path.resolve())
        for raw in grants.get("runtime_write_paths", []):
            writable = Path(raw).resolve()
            if any(path == writable or path.is_relative_to(writable) for path in protected):
                raise CampaignError("Scoped command cannot write its lifecycle records or grants")
        self.max_calls = max_calls
        self.timeout_s = timeout_s
        self.max_output_bytes = max_output_bytes
        self.binding = {
            "schema_version": 1,
            "grants_path": str(self.grants_path),
            "grants_digest": self.grants_digest,
            "max_calls": max_calls,
            "timeout_s": timeout_s,
            "max_output_bytes": max_output_bytes,
        }
        with file_lock(self.directory / "tools.lock"):
            path = self.directory / "budget.json"
            if path.exists():
                if read_json(path).get("binding") != self.binding:
                    raise CampaignError("Scoped tool grants or budget changed")
            else:
                write_json(path, {"binding": self.binding, "calls": []})

    def execute(self, call_id: str, arguments: dict) -> dict:
        """Return a durable native tool response; never replay an uncertain call."""
        if not isinstance(call_id, str) or not call_id or len(call_id.encode()) > 1024:
            raise CampaignError("Scoped tool requires a bounded nonempty call ID")
        argv = arguments.get("command") if isinstance(arguments, dict) else None
        if (
            not isinstance(argv, list)
            or not 1 <= len(argv) <= 256
            or set(arguments) != {"command"}
            or any(not isinstance(arg, str) or "\0" in arg for arg in argv)
            or not argv[0]
            or sum(len(arg.encode()) + 1 for arg in argv) > 32768
        ):
            raise CampaignError("Scoped tool command must be a bounded argv array")
        key = digest(call_id)
        with file_lock(self.directory / "tools.lock"):
            if file_digest(self.grants_path) != self.grants_digest:
                raise CampaignError("Scoped tool execution grants changed")
            budget_path = self.directory / "budget.json"
            budget = read_json(budget_path)
            if budget.get("binding") != self.binding:
                raise CampaignError("Scoped tool budget binding changed")
            record_path = self.directory / (key + ".json")
            if record_path.exists():
                record = read_json(record_path)
                if record.get("call_id") != call_id or record.get("arguments") != arguments:
                    raise CampaignError("Scoped tool call ID was reused with changed arguments")
                if record.get("result") is not None:
                    return record["result"]
                return _response(
                    False, "Prior scoped command outcome is uncertain; it was not replayed."
                )
            if len(budget["calls"]) >= self.max_calls:
                return _response(False, "Scoped tool call budget is exhausted.")
            record = {"call_id": call_id, "arguments": arguments, "state": "prepared"}
            # Spending the slot first cannot lose an executed call on a crash.
            budget["calls"].append(key)
            write_json(budget_path, budget)
            write_json(record_path, record)
            try:
                if not Path(argv[0]).is_absolute():
                    executable = shutil.which(argv[0])
                    if executable is None:
                        raise CampaignError("Scoped command executable is unavailable")
                    argv = [executable, *argv[1:]]
                launch, workspace, environment = command(self.grants_path, argv)
                result, receipt = self._run(launch, workspace, environment, record_path, record)
            except (OSError, ValueError, CampaignError) as exc:
                result = _response(False, f"Scoped command refused: {type(exc).__name__}: {exc}")
                receipt = {"reason": str(exc), "processes_quiescent": record["state"] == "prepared"}
            record.update(state="finished", result=result, receipt=receipt)
            write_json(record_path, record)
            return result

    def _run(self, launch, workspace, environment, record_path, record):
        host = get_process_host()
        # Fail before launching if the readback backend is unavailable.
        _session_members(-1, host)
        known = {}
        process = None
        identity = None
        output = bytearray()
        emitted = 0
        reason = ""
        quiescent = False
        started = time.monotonic()
        with selectors.DefaultSelector() as streams:
            try:
                process_options: dict[str, Any] = detach_kwargs()
                process = subprocess.Popen(
                    [sys.executable, "-c", _GATE, json.dumps(launch)],
                    cwd=workspace,
                    env=environment,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    **process_options,
                )
                identity = host.identity(process.pid)
                if identity is None:
                    raise CampaignError("Scoped command root identity is unavailable")
                known[process.pid] = identity
                record.update(state="submitted", pid=process.pid, identity=identity)
                write_json(record_path, record)
                for pipe in (process.stdout, process.stderr):
                    assert pipe is not None
                    streams.register(pipe, selectors.EVENT_READ)
                assert process.stdin is not None
                process.stdin.write("1")
                process.stdin.flush()
                process.stdin.close()
                while True:
                    known.update(_session_members(process.pid, host, identity))
                    known.update(host.descendants(process.pid))
                    for pid, stamp in _known_writers(known, host).items():
                        try:
                            if (
                                host.liveness_of(pid, stamp) == "alive"
                                and os.getsid(pid) != process.pid
                            ):
                                reason = "Scoped command detached a process; persistent daemons are unsupported"
                                break
                        except ProcessLookupError:
                            continue
                    if reason:
                        break
                    record["processes"] = [
                        {"pid": pid, "identity": stamp} for pid, stamp in known.items()
                    ]
                    write_json(record_path, record)
                    for key, _ in streams.select(timeout=0.05):
                        data = os.read(key.fd, 8192)
                        if not data:
                            streams.unregister(key.fileobj)
                            continue
                        emitted += len(data)
                        output.extend(data[: max(0, self.max_output_bytes - len(output))])
                    if emitted > self.max_output_bytes:
                        reason = "Scoped command output bound exceeded"
                        break
                    if time.monotonic() - started > self.timeout_s:
                        reason = "Scoped command timeout exceeded"
                        break
                    if process.poll() is not None:
                        break
            finally:
                if process is not None:
                    if process.stdin is not None and not process.stdin.closed:
                        process.stdin.close()  # An unreleased launch gate exits without running argv.
                    # The leader may already be dead. Its session still identifies
                    # an immediately orphaned background child while one exists.
                    deadline = time.monotonic() + 3
                    try:
                        while True:
                            remaining = _session_members(process.pid, host, identity)
                            known.update(remaining)
                            if process.poll() is not None and remaining and not reason:
                                reason = (
                                    "Scoped command left background processes; they were terminated"
                                )
                            kill_tree(process, wait_s=0.05, known=known)
                            if not _session_members(
                                process.pid, host, identity
                            ) and not _known_writers(known, host):
                                quiescent = True
                                break
                            if time.monotonic() >= deadline:
                                break
                            time.sleep(0.05)
                    except (OSError, CampaignError):
                        # Preserve independent-readback failure, while still
                        # cleaning only identities already authenticated.
                        kill_tree(process, wait_s=0.05, known=known)
                        raise
                    # Drain only ready bytes after the owned session has ended.
                    for _ in range(128):
                        ready = streams.select(timeout=0)
                        if not ready:
                            break
                        for key, _ in ready:
                            data = os.read(key.fd, 8192)
                            if not data:
                                streams.unregister(key.fileobj)
                                continue
                            emitted += len(data)
                            output.extend(data[: max(0, self.max_output_bytes - len(output))])
                    for pipe in (process.stdout, process.stderr):
                        if pipe is not None:
                            pipe.close()
        if emitted > self.max_output_bytes:
            reason = "Scoped command output bound exceeded"
        if not quiescent:
            reason = "Scoped command process quiescence is unproven"
        returncode = process.returncode if process is not None else None
        success = returncode == 0 and quiescent and not reason
        receipt = {
            "returncode": returncode,
            "reason": reason,
            "processes_quiescent": quiescent,
            "quiescence_scope": "owned-session-and-observed-descendants",
            "process_session": process.pid if process is not None else None,
            "processes": [{"pid": pid, "identity": stamp} for pid, stamp in known.items()],
            "output_bytes": emitted,
            "elapsed_s": time.monotonic() - started,
        }
        text = (
            output.decode("utf-8", "replace")
            .encode("utf-8")[: self.max_output_bytes]
            .decode("utf-8", "ignore")
        )
        if reason:
            text += "\n" + reason
        return _response(success, text), receipt


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--grants", type=Path, required=True)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--max-calls", type=int, default=32)
    parser.add_argument("--timeout-s", type=int, default=60)
    parser.add_argument("--max-output-bytes", type=int, default=65536)
    args = parser.parse_args(argv)
    worker = ScopedTools(
        args.grants, args.directory, args.max_calls, args.timeout_s, args.max_output_bytes
    )
    while line := sys.stdin.buffer.readline(65537):
        try:
            if len(line) > 65536:
                while not line.endswith(b"\n"):
                    line = sys.stdin.buffer.readline(65537)
                    if not line:
                        break
                raise CampaignError("Scoped command request is oversized")
            request = json.loads(line)
            result = worker.execute(request["call_id"], request["arguments"])
        except (KeyError, OSError, ValueError, CampaignError) as exc:
            result = _response(False, f"Scoped command refused: {exc}")
        print(json.dumps(result), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
