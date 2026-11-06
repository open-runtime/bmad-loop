"""Worker bootstrap, output reader and context verifier for the stock Paradev transport.

Bundled runtime for the explicitly selected Paradev backend. Wrappers invoke
``python -m bmad_loop.adapters.paradev_runtime --verify`` after account isolation. The context contract requires proof that the only Paradev identity a worker may carry is the one the host set
for the PTY the backend created, confirmed against the backend's own launch record.


Adapted with the owner's explicit incorporation direction from their
open-runtime/paradev-tooling source at a5d76d363da3cca58db5e110942c1490a49f8fae.
The original transport has no separate top-level license; the vendored BMAD
license does not license that transport. This is a maintained package-local port.
"""

from __future__ import annotations

import json
import os
import signal
import stat as statmod
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from bmad_loop.adapters.tmux_base import posix_shell_argv
from bmad_loop.platform_util import LockUnavailableError, atomic_write_text, file_lock
from bmad_loop.verify import GitError, git_bytes

# Terminal-own context the host sets for a PTY it creates (host-service terminal/env.ts plus the
# per-terminal attribution token from terminal/terminal.ts). Account selection, agent ids and
# harness session ids are never part of it.
CONTEXT_KEYS = frozenset(
    {
        "SUPERSET_TERMINAL_ID",
        "SUPERSET_WORKSPACE_ID",
        "SUPERSET_WORKSPACE_PATH",
        "SUPERSET_ROOT_PATH",
        "SUPERSET_ORGANIZATION_ID",
        "SUPERSET_HOME_DIR",
        "SUPERSET_ENV",
        "SUPERSET_AGENT_HOOK_PORT",
        "SUPERSET_AGENT_HOOK_VERSION",
        "SUPERSET_HOST_AGENT_HOOK_URL",
        "SUPERSET_ACCOUNT_ATTRIBUTION_TOKEN",
    }
)
# Backend-owned outputs. The BMAD_LOOP_ prefix rides the wrappers' keep-rule for BMAD_LOOP_*.
CONTEXT_FILE = "BMAD_LOOP_PARADEV_CONTEXT_FILE"
TERMINAL_KEY = "BMAD_LOOP_PARADEV_TERMINAL_ID"
WORKSPACE_KEY = "BMAD_LOOP_PARADEV_WORKSPACE_ID"
OWNED_KEYS = frozenset({CONTEXT_FILE, TERMINAL_KEY, WORKSPACE_KEY})
# Legacy wrapper marker: an inbound copy is a parent leak, never attribution.
VERIFIED_KEY = "BMAD_FACTORY_PARADEV_VERIFIED"
LEAK_KEYS = OWNED_KEYS | {VERIFIED_KEY}
MAX_RECORD_BYTES = 1024 * 1024
GAP_MARKER = "\r\n[paradev-transport: output gap; stream reanchored at seq %s]\r\n"
EXIT_MARKER = "\r\n[paradev-transport: terminal ended]\r\n"
# Keyboard signals the PTY line discipline sends to the whole foreground group. The bootstrap
# is the PTY session leader, so it must outlive them; the worker it spawned still receives
# them with default dispositions (a caught disposition reverts to default across exec).
KEYBOARD_SIGNALS = tuple(
    s for s in (signal.SIGINT, getattr(signal, "SIGQUIT", None)) if s is not None
)


def now():
    # Microseconds keep same-second records totally ordered.
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def private_json(path, value):
    """Atomically replace ``path`` with ``value`` as owner-only JSON."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.exists():
        read_private_json(path)  # Refuse overwriting an unsafe or foreign record.
    atomic_write_text(
        path, json.dumps(value, sort_keys=True, allow_nan=False), follow_symlinks=False
    )


def append_private_jsonl(path, value):
    """Append one JSON line to an owner-only, append-only record."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as out:
        out.write(json.dumps(value, sort_keys=True) + "\n")


def read_private_json(path):
    """Read an owner-only regular file in an owner-only directory; anything else is invalid."""
    path = Path(path)
    parent = os.stat(path.parent)
    if parent.st_uid != os.getuid() or parent.st_mode & 0o077:
        raise ValueError("Invalid private transport record")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, encoding="utf-8") as source:
        stat = os.fstat(source.fileno())
        if (
            not statmod.S_ISREG(stat.st_mode)
            or stat.st_uid != os.getuid()
            or stat.st_mode & 0o077
            or stat.st_size > MAX_RECORD_BYTES
        ):
            raise ValueError("Invalid private transport record")
        return json.load(source)


def require_workspace_checkout(cwd, worktree):
    """Require cwd's git top-level and git directory to identify the workspace checkout.

    Directory aliases are accepted, but inherited git overrides and repository-local
    core.worktree settings must not let another checkout impersonate the workspace.
    Failures are value-free ValueErrors so both transport and standalone R1 callers can
    handle them without exposing git output, paths or environment values.
    """
    try:
        env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
        paths = []
        # Use separate calls: a path may contain newlines, so a multi-line result cannot
        # safely delimit the top-level and git directory. The per-worktree git directory,
        # not the shared common directory, distinguishes linked checkouts.
        for directory, flag in (
            (cwd, "--show-toplevel"),
            (cwd, "--absolute-git-dir"),
            (worktree, "--absolute-git-dir"),
        ):
            result = git_bytes(Path(directory), "rev-parse", flag, timeout_s=5, env=env)
            if result.returncode:
                raise ValueError
            # Preserve path bytes: universal-newline translation could turn a nested
            # directory named with a carriage return into its parent's path.
            path = result.stdout.removesuffix(b"\n").decode(sys.getfilesystemencoding())
            if not os.path.isabs(path):
                raise ValueError
            paths.append(path)
        top, cwd_git, workspace_git = paths
        if not os.path.samefile(top, worktree) or not os.path.samefile(cwd_git, workspace_git):
            raise ValueError
    except (OSError, ValueError, TypeError, subprocess.SubprocessError, GitError):
        raise ValueError("Directory does not belong to the workspace git checkout") from None


def restore_terminal_context(child, source):
    """Return ``child`` without any SUPERSET_* key, plus this PTY's own routing context.

    This is the record proof (R1) of the contract only. ``source`` is the wrapper's original
    environment; call this AFTER factory account and home isolation. No marker means an ordinary
    launch and leaves ``child`` unchanged. A marker that does not prove this terminal raises
    ValueError; callers must not launch on error. The host-row and lineage checks live in
    :func:`verify_context`. The launch cwd's git top-level must be the record's host-set
    workspace worktree, and both must resolve to the same git directory; a nested checkout
    cannot supply this workspace's record proof, even with a redirected core.worktree.
    """
    marker = source.get(CONTEXT_FILE)
    if not marker:
        return dict(child)
    path = Path(marker)
    record = read_private_json(path)
    launch = read_private_json(path.parent / "launch.json")
    context = record["context"]
    terminal, workspace = record["terminalId"], record["workspaceId"]
    expected = {"SUPERSET_TERMINAL_ID": terminal, "SUPERSET_WORKSPACE_ID": workspace}
    if (
        path.name != "context.json"
        or path.parent.name != terminal
        or launch["terminalId"] != terminal
        or launch["workspaceId"] != workspace
        or any(
            source.get(key) != value or context.get(key) != value for key, value in expected.items()
        )
        or source.get(TERMINAL_KEY, terminal) != terminal
        or source.get(WORKSPACE_KEY, workspace) != workspace
        or not isinstance(context.get("SUPERSET_WORKSPACE_PATH"), str)
    ):
        raise ValueError("Transport context does not belong to this terminal")
    require_workspace_checkout(launch["cwd"], context["SUPERSET_WORKSPACE_PATH"])
    result = {key: value for key, value in child.items() if not key.startswith("SUPERSET_")}
    result.update({key: value for key, value in context.items() if key in CONTEXT_KEYS})
    result[CONTEXT_FILE] = str(path)
    result[TERMINAL_KEY] = terminal
    result[WORKSPACE_KEY] = workspace
    return result


def worker_environment(folder, source, *, pid=None):
    """Record the host-set context for this PTY and build the worker command's environment.

    ``pid`` is the bootstrap's own process id, which is also the PTY root the host's daemon
    reports for the terminal (the initial command exec's into the bootstrap); the backend
    compares the two to tell this PTY from one the host respawned under the same id.
    """
    folder = Path(folder)
    launch = read_private_json(folder / "launch.json")
    if (
        source.get("SUPERSET_TERMINAL_ID") != launch["terminalId"]
        or source.get("SUPERSET_WORKSPACE_ID") != launch["workspaceId"]
        or folder.name != launch["terminalId"]
    ):
        raise ValueError("Host assigned unexpected terminal context")
    private_json(
        folder / "context.json",
        {
            "terminalId": launch["terminalId"],
            "workspaceId": launch["workspaceId"],
            "pid": int(pid if pid is not None else os.getpid()),
            "context": {k: v for k, v in source.items() if k in CONTEXT_KEYS},
            "recordedAt": now(),
        },
    )
    env = {
        **source,
        **{
            k: v
            for k, v in launch["env"].items()
            if not k.startswith("SUPERSET_") and k not in LEAK_KEYS
        },
    }
    env[CONTEXT_FILE] = str(folder / "context.json")
    env[TERMINAL_KEY] = launch["terminalId"]
    env[WORKSPACE_KEY] = launch["workspaceId"]
    return launch, restore_terminal_context(env, env)


def launch_worker(folder):
    folder = Path(folder)
    launch, env = worker_environment(folder, dict(os.environ))
    # The bootstrap stays the PTY session leader for the terminal's whole life, so a keyboard
    # interrupt typed by an operator reaches the worker (default dispositions after exec) but
    # must not end the bootstrap: an ended PTY has no exit record and would be a host-side exit.
    for signum in KEYBOARD_SIGNALS:
        signal.signal(signum, lambda *_: None)
    child = subprocess.Popen(posix_shell_argv(launch["command"]), cwd=launch["cwd"], env=env)
    private_json(folder / "worker.json", {"pid": child.pid, "startedAt": now()})
    while True:
        try:
            code = child.wait()
            break
        except InterruptedError:
            continue
    private_json(folder / "exit.json", {"returncode": code, "exitedAt": now()})
    # Keep the real PTY alive for BMAD to observe and dispose explicitly: a normal worker
    # exit must never become stock Paradev's terminal-exit auto-resume.
    while True:
        signal.pause()


class _OutputState:
    """The reader's durable state: the cursor is exact at the recorded log size."""

    def __init__(self, path, log_file):
        self.path, self.log = Path(path), str(log_file)
        self.state, self.epoch, self.seq, self.log_size, self.gaps = None, None, 0, 0, 0
        self.inode: int | None = None

    def load(self):
        if not self.path.exists():
            return None
        record = read_private_json(self.path)
        self.state, self.gaps = record.get("state"), int(record.get("gaps") or 0)
        self.epoch, self.seq, self.log_size = (
            record.get("epoch"),
            record.get("seq"),
            int(record.get("logSize") or 0),
        )
        return record

    def save(self, state, log_size):
        self.state, self.log_size = state, log_size
        private_json(
            self.path,
            {
                "state": state,
                "pid": os.getpid(),
                "log": self.log,
                "logInode": self.inode,
                "epoch": self.epoch,
                "seq": self.seq,
                "logSize": log_size,
                "gaps": self.gaps,
                "updatedAt": now(),
            },
        )

    def resume_cursor(self, actual_size):
        """Exact position for a restart: every byte past the recorded size advanced the cursor."""
        if self.epoch is None or self.seq is None or self.state in (None, "ended"):
            return None
        return (self.epoch, self.seq + max(0, actual_size - self.log_size))


def _merge_failure(folder, name):
    """Mark a runtime record failed without erasing what a restart needs (log path, cursor)."""
    path = Path(folder) / name
    record = {}
    if path.exists():
        try:
            record = read_private_json(path)
        except (OSError, ValueError):
            record = {}
    record.update({"state": "failed", "pid": os.getpid(), "at": now()})
    private_json(path, record)


def read_output(
    folder, log_file, *, host_factory=None, connect=None, max_failures=120, fatal_status=None
):
    """Append the terminal's raw byte stream to ``log_file``; never a screen snapshot.

    Reconnects resume from the exact host byte cursor. A position the host can no longer honour
    is marked in-band and in ``output.json`` (``gaps``) rather than silently replayed or dropped.
    A restarted reader continues from the recorded cursor. Returns when the terminal ends.
    """
    folder, log_file = Path(folder), Path(log_file).resolve()
    try:
        lock = file_lock(folder / "output.lock", blocking=False)
        lock.__enter__()
    except LockUnavailableError:
        return
    try:
        if host_factory is None:
            from bmad_loop.adapters.paradev_host import Host

            host_factory = Host
        if connect is None:
            from websockets.exceptions import ConnectionClosed, InvalidHandshake, InvalidStatus
            from websockets.sync.client import connect

            retry_errors = (OSError, TimeoutError, ConnectionClosed, InvalidHandshake)
            fatal_status = (InvalidStatus,)
        else:
            retry_errors = (OSError, TimeoutError)
            fatal_status = tuple(fatal_status or ())
        launch = read_private_json(folder / "launch.json")
        status = _OutputState(folder / "output.json", log_file)
        log_file.parent.mkdir(parents=True, exist_ok=True)
        status.load()
        # A different log path is a deliberate rebind (the engine replaced its file): the host
        # cursor from the prior record is kept and output continues into the new file.
        if status.state == "ended":
            return
        with open(log_file, "ab", buffering=0) as out:
            status.inode = os.fstat(out.fileno()).st_ino
            size = out.seek(0, os.SEEK_END)
            cursor = status.resume_cursor(size)
            if cursor is not None:
                # Normalise the checkpoint to the computed position before any failure path can save
                # it: a restart that never reaches `synced` must not persist the older cursor.
                status.epoch, status.seq, status.log_size = cursor[0], cursor[1], size
            failures = 0
            while True:
                synced = False
                try:
                    try:
                        host = host_factory()
                    except Exception as error:  # the manifest may be rewritten during a host update
                        raise OSError("Host client unavailable") from error
                    query = urlencode(
                        {
                            "workspaceId": launch["workspaceId"],
                            "seq": f"{cursor[0]}:{cursor[1]}" if cursor else "new",
                        }
                    )
                    url = (
                        host.endpoint.replace("http:", "ws:", 1)
                        + "/terminal/"
                        + launch["terminalId"]
                        + "?"
                        + query
                    )
                    with connect(
                        url,
                        additional_headers={"Authorization": "Bearer " + host._token},
                        open_timeout=10,
                        max_size=None,
                        proxy=None,
                    ) as socket:
                        for message in socket:
                            if isinstance(message, bytes):
                                if synced:
                                    out.write(message)
                                    size += len(message)
                                    status.seq += len(message)
                                continue
                            event = json.loads(message)
                            kind = event.get("type")
                            if kind == "synced":
                                anchor = (event["epoch"], int(event["seq"]))
                                status.epoch, status.seq = anchor
                                state = "ready"
                                if cursor and anchor != cursor:
                                    # Bytes between the cursor and the anchor are unrecoverable: say so
                                    # in-band. The record is saved first, sized as if the marker were
                                    # already written, so a restart never counts marker bytes as output.
                                    marker = (GAP_MARKER % status.seq).encode()
                                    status.gaps += 1
                                    state = "gapped"
                                    status.save(state, size + len(marker))
                                    out.write(marker)
                                    size += len(marker)
                                synced, failures = True, 0
                                status.save(state, size)
                            elif kind == "exit":
                                marker = EXIT_MARKER.encode()
                                status.save("ended", size + len(marker))
                                out.write(marker)
                                return
                            elif kind == "error":
                                if event.get("code") == "session-gone":
                                    status.save("ended", size)
                                    return
                                raise OSError("Terminal stream rejected")
                            elif kind == "ping":
                                socket.send(json.dumps({"type": "pong"}))
                            if synced:
                                status.save(
                                    (
                                        status.state
                                        if status.state in ("ready", "gapped")
                                        else "ready"
                                    ),
                                    size,
                                )
                                cursor = (status.epoch, status.seq)
                    raise OSError("Terminal stream closed without exit")
                except fatal_status as error:
                    code = getattr(getattr(error, "response", None), "status_code", None)
                    if code in (401, 403, 404):
                        status.save("failed", size)
                        raise RuntimeError("Terminal output refused") from None
                    failures += 1
                    if failures >= max_failures:
                        status.save("failed", size)
                        raise RuntimeError("Terminal output unavailable") from None
                    status.save("reconnecting", size)
                    time.sleep(min(failures, 5))
                except retry_errors:
                    if synced:
                        cursor = (status.epoch, status.seq)
                    failures += 1
                    if failures >= max_failures:
                        status.save("failed", size)
                        raise RuntimeError("Terminal output unavailable") from None
                    status.save("reconnecting", size)
                    time.sleep(min(failures, 5))

    finally:
        lock.__exit__(None, None, None)


def _parent_pid(pid, ps=None):
    ps = ps or (
        lambda p: (
            subprocess.run(
                ["ps", "-o", "ppid=", "-p", str(p)],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            ).stdout
        )
    )
    try:
        value = int(ps(pid).strip() or 0)
    except (ValueError, OSError, subprocess.SubprocessError):
        return None
    return value or None


def ancestor_pids(pid, ps=None, limit=128):
    chain, current = [], pid
    while current and current > 1 and len(chain) < limit:
        chain.append(current)
        current = _parent_pid(current, ps)
    return chain


def verify_context(source, *, pid=None, host_factory=None, ps=None):
    """Decide whether ``source`` carries this process's own terminal context (contract section 3).

    Returns a dict with ``verified``, per-check results (``ok``, ``failed``, ``skipped`` or
    ``unavailable``), the ``context`` to restore when verified (exactly the restore set, so it
    includes the terminal's own attribution token and is meant for the wrapper's pipe only), and
    a value-free ``reason``.
    """
    checks = {
        "ids": "failed",
        "record": "skipped",
        "host_row": "unavailable",
        "lineage": "unavailable",
    }
    reasons = []
    terminal, workspace = source.get("SUPERSET_TERMINAL_ID"), source.get("SUPERSET_WORKSPACE_ID")
    if not source.get(TERMINAL_KEY) and not source.get(CONTEXT_FILE):
        return {
            "verified": False,
            "checks": checks,
            "context": {},
            "reason": "no transport marker: ordinary launch",
        }
    if (
        terminal
        and workspace
        and source.get(TERMINAL_KEY) == terminal
        and source.get(WORKSPACE_KEY) == workspace
    ):
        checks["ids"] = "ok"
    else:
        reasons.append("ids")
    restored = None
    if source.get(CONTEXT_FILE):
        try:
            restored = restore_terminal_context({}, source)
            checks["record"] = "ok"
        except (OSError, ValueError, KeyError, TypeError):
            checks["record"] = "failed"
            reasons.append("record")
    try:
        if host_factory is None:
            from bmad_loop.adapters.paradev_host import Host

            host_factory = Host
        host = host_factory()
        rows = host.rpc("terminal.list", {"workspaceId": workspace})
        rows = rows.get("sessions") if isinstance(rows, dict) else rows
        listed = any(
            isinstance(r, dict) and r.get("terminalId") == terminal and not r.get("exited")
            for r in rows or []
        )
        checks["host_row"] = "ok" if listed else "failed"
        if not listed:
            reasons.append("host_row")
        daemon = host.rpc("terminal.daemon.listSessions")
        root = next(
            (
                r.get("pid")
                for r in daemon
                if isinstance(r, dict) and r.get("id") == terminal and r.get("alive")
            ),
            None,
        )
        if isinstance(root, int) and root > 0:
            chain = ancestor_pids(pid or os.getpid(), ps)
            checks["lineage"] = "ok" if root in chain else "failed"
            if root not in chain:
                reasons.append("lineage")
        else:
            checks["lineage"] = "failed"
            reasons.append("lineage")
    except Exception:
        reasons.append("host")
    verified = all(value in ("ok", "skipped") for value in checks.values())
    # The restore set comes from the record the host-set context was captured in, never from a
    # live value that may have drifted since the bootstrap.
    origin = restored if restored is not None else source
    context = {k: v for k, v in origin.items() if k in CONTEXT_KEYS} if verified else {}
    return {
        "verified": verified,
        "checks": checks,
        "context": context,
        "reason": "verified" if verified else "failed: " + ", ".join(reasons),
    }


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["--verify"]:
        pid = int(argv[argv.index("--pid") + 1]) if "--pid" in argv else None
        verdict = verify_context(dict(os.environ), pid=pid)
        print(json.dumps(verdict, sort_keys=True))
        return 0 if verdict["verified"] else 1
    folder = Path(argv[1]) if len(argv) > 1 else None
    try:
        if argv[:1] == ["--launch"] and len(argv) == 2:
            launch_worker(folder)
        elif argv[:1] == ["--tee"] and len(argv) == 3:
            read_output(folder, argv[2])
        else:
            raise ValueError("Invalid runtime operation")
    except SystemExit:
        raise
    except BaseException:
        # No exception body: commands, credentials and transcripts stay private. A reader
        # failure keeps its log path and cursor so the reader can be restarted.
        if folder is not None:
            try:
                _merge_failure(
                    folder, "output.json" if argv[:1] == ["--tee"] else "bootstrap-error.json"
                )
            except OSError:
                pass
        return 1
    return 0


if __name__ == "__main__":
    # Direct script launch works from an installed package or a source checkout.
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    raise SystemExit(main())
