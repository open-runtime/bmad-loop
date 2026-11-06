"""BMAD's public multiplexer contract backed by stock Paradev terminals.

The registered window IS the worker PTY: one real terminal per ``new_window`` in the
explicitly selected registered workspace, driven only through the stock host API that the
tooling's ``Host`` client already speaks. This backend creates no tmux workers, no viewer
terminals and no native agent identity, and it answers "unknown" rather than guessing.

Durable records (owner-only JSON under the state root):

- ``sessions/<name>.json`` and ``windows/<terminalId>.json``, atomically replaced;
- ``journal.jsonl``, append-only state transitions (ids and states, never env or commands);
- ``launches/<terminalId>/`` runtime records written by ``native_transport_runtime``.

Window states: ``launching`` (record and launch.json written, host acknowledgement pending or
lost), ``active`` (acknowledged, or adopted after a verified bootstrap), ``void`` (reconciled: the
host never ran the worker and the minted id is disposed on the host), ``gone`` (worker exited
and the terminal vanished), ``disposed`` (killed through the host). ``list_window_ids`` lists
``launching`` and ``active`` windows.


Adapted with the owner's explicit incorporation direction from their
open-runtime/paradev-tooling source at a5d76d363da3cca58db5e110942c1490a49f8fae.
The original transport has no separate top-level license; the vendored BMAD
license does not license that transport. This is a maintained package-local port.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, cast

from bmad_loop import envvars
from bmad_loop.adapters.multiplexer import (
    ALLOWED_KEYS,
    MultiplexerError,
    TerminalMultiplexer,
    parse_target,
)
from bmad_loop.adapters.paradev_host import discover_cli
from bmad_loop.adapters.tmux_base import posix_exec_command, posix_parked_command
from bmad_loop.platform_util import LockUnavailableError, detach_kwargs, file_lock
from bmad_loop.runs import state_root

from .paradev_runtime import (
    CONTEXT_FILE,
    LEAK_KEYS,
    TERMINAL_KEY,
    WORKSPACE_KEY,
    append_private_jsonl,
    now,
    private_json,
    read_private_json,
    require_workspace_checkout,
)

BACKEND_NAME = "paradev"
WORKSPACE_OPTION = "@paradev_workspace"
STATE_DIR_KEY = envvars.PARADEV_STATE_DIR
LISTED_STATES = ("launching", "active")
FINAL_STATES = ("void", "gone", "disposed")
# A lost acknowledgement may be read as "never created" only after the host had this long to
# materialise the terminal, and only once the minted id is disposed on the host so a late
# create is refused there too.
VOID_GRACE_S = 20.0
# An acknowledged terminal is "starting" until its bootstrap has recorded itself or this long has
# passed; after that its liveness is unknown until a record appears or it is disposed.
BOOTSTRAP_GRACE_S = 30.0
READER_READY_S = 12.0
DISPOSE_WAIT_S = 3.0


class ParadevTransportError(MultiplexerError):
    """A Paradev transport operation failed or its outcome is unknown."""


class ParadevMultiplexer(TerminalMultiplexer):
    def __init__(self, *, host=None, state_dir=None, cli_path=None, clock=None):
        self.host = host
        self.cli_path = str(cli_path or discover_cli())
        self.root = Path(
            state_dir or envvars.paradev_state_dir() or state_root() / "paradev-transport-v1"
        ).resolve()
        self._clock = clock or time.time
        self._version = None
        self.unreported = []  # output-reader notes whose journal write itself failed

    # ------------------------------------------------------------------ host

    def _host(self):
        if self.host is None:
            try:
                from .paradev_host import Host

                self.host = Host()
            except Exception as exc:
                raise ParadevTransportError(
                    f"Paradev host client unavailable ({_code(exc)})"
                ) from None
        return self.host

    def _rpc(self, procedure, data=None, *, mutate=False):
        try:
            return self._host().rpc(procedure, data, mutate=mutate)
        except ParadevTransportError:
            raise
        except Exception as exc:
            raise ParadevTransportError(
                f"Paradev {procedure} failed ({_code(exc)}); state is unknown"
            ) from None

    def _cli(self, args):
        try:
            return self._host().cli(args)
        except ParadevTransportError:
            raise
        except Exception as exc:
            raise ParadevTransportError(
                f"Paradev CLI {args[0]} {args[1]} failed ({_code(exc)})"
            ) from None

    def _daemon(self):
        """Live PTYs by terminal id. Unlike terminal.list this distinguishes unavailable from empty."""
        rows = self._rpc("terminal.daemon.listSessions")
        if not isinstance(rows, list) or any(
            not isinstance(r, dict)
            or not isinstance(r.get("alive"), bool)
            or not isinstance(r.get("id"), str)
            for r in rows
        ):
            raise ParadevTransportError("Paradev daemon liveness is unknown")
        return {r["id"]: r for r in rows if r["alive"]}

    def _catalog(self):
        """Registered live workspaces and their projects' registered roots (repo path, worktree base)."""
        rows = self._rpc("workspace.list", {})
        projects = self._rpc("project.list")
        if not isinstance(rows, list) or not isinstance(projects, list):
            raise ParadevTransportError("Paradev workspace metadata unavailable")
        roots = {}
        for project in projects:
            if isinstance(project, dict) and project.get("id") and not project.get("deletedAt"):
                roots[project["id"]] = [
                    str(Path(project[key]).resolve())
                    for key in ("repoPath", "worktreeBaseDir")
                    if isinstance(project.get(key), str) and project[key]
                ]
        workspaces = [
            r
            for r in rows
            if isinstance(r, dict)
            and r.get("worktreePath")
            and not r.get("archivedAt")
            and r.get("projectId") in roots
            and r.get("worktreeExists") is not False
        ]
        return workspaces, roots

    def _select_workspace(self, cwd, env, session):
        """The explicit selector decides; cwd alone only when exactly one registered workspace matches.

        In every case the git top-level of ``cwd`` must be the selected workspace's worktree,
        and both must resolve to the same git directory. Paradev attributes git/PR state to
        the registered checkout, so a worker is never placed in a checkout it is not in
        (navoga's ruling of 2026-11-04 keeps this strict).
        """
        cwd = Path(cwd).resolve()
        selected = (env or {}).get(WORKSPACE_KEY)
        if not selected and session is not None:
            record = self._read("sessions", session)
            selected = (record or {}).get("options", {}).get(WORKSPACE_OPTION)
        rows, _ = self._catalog()
        if selected:
            matches = [r for r in rows if r.get("id") == selected]
            if len(matches) != 1:
                raise ParadevTransportError(
                    "Selected Paradev workspace is not a registered live workspace"
                )
            workspace = matches[0]
        else:
            # Exactly one registered worktree may contain cwd; nested registered worktrees are ambiguous
            # too (no depth preference), and the launcher resolves them with the explicit selector.
            candidates = [r for r in rows if cwd.is_relative_to(Path(r["worktreePath"]).resolve())]
            if len(candidates) != 1:
                raise ParadevTransportError(
                    f"Paradev requires one exact registered workspace for this directory; select it with "
                    f"{WORKSPACE_KEY} in the window env or the {WORKSPACE_OPTION} session option"
                )
            workspace = candidates[0]
        try:
            require_workspace_checkout(cwd, workspace["worktreePath"])
        except ValueError:
            raise ParadevTransportError(
                "Directory is outside the selected Paradev workspace checkout"
            ) from None
        return workspace

    # -------------------------------------------------------------- registry

    @contextmanager
    def _lock(self):
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        with file_lock(self.root / "registry.lock"):
            yield

    def _path(self, kind, name):
        if not name or "/" in name or name in (".", ".."):
            raise ParadevTransportError("Invalid Paradev registry name")
        return self.root / kind / (name + ".json")

    def _read(self, kind, name):
        path = self._path(kind, name)
        if not path.exists():
            return None
        try:
            record = read_private_json(path)
            if record.get("schema_version") != 1:
                raise ValueError(
                    "Unsupported Paradev transport record; explicit migration required"
                )
            return record
        except (OSError, ValueError) as exc:
            raise ParadevTransportError(
                f"Paradev registry record unreadable ({type(exc).__name__})"
            ) from None

    def _write(self, kind, record):
        record["schema_version"] = 1
        record["updatedAt"] = now()
        private_json(
            self._path(kind, record["name"] if kind == "sessions" else record["id"]), record
        )

    def _journal(self, event, **fields):
        append_private_jsonl(self.root / "journal.jsonl", {"at": now(), "event": event, **fields})

    def _windows(self, session=None, states=LISTED_STATES):
        folder = self.root / "windows"
        if not folder.exists():
            return []
        rows = []
        for path in sorted(folder.glob("*.json")):
            try:
                row = read_private_json(path)
                if row.get("schema_version") != 1:
                    raise ValueError("Unsupported Paradev transport record")
            except (OSError, ValueError):
                raise ParadevTransportError("Paradev registry record unreadable") from None
            if (session is None or row.get("session") == session) and (
                states is None or row.get("state") in states
            ):
                rows.append(row)
        # Launch order, not filename order: attemptedAt breaks same-timestamp ties.
        rows.sort(key=lambda r: (r.get("createdAt", ""), float(r.get("attemptedAt") or 0)))
        return rows

    def _window(self, target, states=LISTED_STATES):
        """The owned window a target names, or None. A seam token resolves by session and name."""
        parsed = parse_target(target)
        if parsed is None:
            row = self._read("windows", target)
            return row if row and (states is None or row.get("state") in states) else None
        session, name = parsed
        rows = [r for r in self._windows(session, states) if name is None or r.get("name") == name]
        if name is None:
            return rows[-1] if rows else None
        if len(rows) > 1:
            raise ParadevTransportError("Paradev target is ambiguous")
        return rows[0] if rows else None

    def _launch_dir(self, terminal_id):
        return self.root / "launches" / terminal_id

    def _bootstrap(self, terminal_id):
        """The bootstrap's own record (with its PTY pid), or None before it has run."""
        path = self._launch_dir(terminal_id) / "context.json"
        if not path.exists():
            return None
        try:
            return read_private_json(path)
        except (OSError, ValueError):
            raise ParadevTransportError("Worker bootstrap record unreadable") from None

    def _set_state(self, row, state, **fields):
        row["state"] = state
        row.update(fields)
        self._write("windows", row)
        self._journal(
            "window-" + state,
            id=row["id"],
            session=row["session"],
            name=row["name"],
            workspace=row["workspace"],
            **{k: v for k, v in fields.items() if k != "paneId"},
        )

    # -------------------------------------------------------------- sessions

    def has_session(self, name):
        return self._read("sessions", name) is not None

    def new_session(self, name, cwd, cols=None, lines=None):
        cwd = str(Path(cwd).resolve())
        with self._lock():
            if self._read("sessions", name) is not None:
                raise ParadevTransportError("Paradev session already exists")
            # No placeholder shell terminal: a Paradev terminal is a real, visible, workspace-bound
            # resource and a second one would not be the worker. Windows carry their own cwd.
            self._write(
                "sessions",
                {
                    "name": name,
                    "cwd": cwd,
                    "options": {},
                    "cols": cols,
                    "rows": lines,
                    "createdAt": now(),
                },
            )
            self._journal("session-created", session=name)

    def kill_session(self, name):
        with self._lock():
            targets = [r["id"] for r in self._windows(name)]
        for target in targets:
            self.kill_window(target)
        with self._lock():
            if self._windows(name):
                return
            path = self._path("sessions", name)
            if path.exists():
                path.unlink()
                self._journal("session-removed", session=name)

    def list_sessions(self):
        folder = self.root / "sessions"
        if not folder.exists():
            return []
        return sorted(path.stem for path in folder.glob("*.json"))

    def session_options(self, option):
        result = {}
        for name in self.list_sessions():
            record = self._read("sessions", name) or {}
            if option in record.get("options", {}):
                result[name] = record["options"][option]
        return result

    def set_session_option(self, name, option, value):
        with self._lock():
            record = self._read("sessions", name)
            if record is None:
                raise ParadevTransportError("Paradev session does not exist")
            record.setdefault("options", {})[option] = value
            self._write("sessions", record)

    # --------------------------------------------------------------- windows

    @staticmethod
    def _request(command, env):
        filtered = {
            k: v
            for k, v in (env or {}).items()
            if not k.startswith("SUPERSET_") and k not in LEAK_KEYS
        }
        if (env or {}).get(WORKSPACE_KEY):
            filtered[WORKSPACE_KEY] = env[WORKSPACE_KEY]
        return {"command": command, "env": filtered}

    @staticmethod
    def _digest(request):
        return hashlib.sha256(json.dumps(request, sort_keys=True).encode()).hexdigest()

    def new_window(self, session, name, cwd, env, command):
        workspace = self._select_workspace(cwd, env, session)
        cwd = str(Path(cwd).resolve())
        request = self._request(command, env)
        request["env"][WORKSPACE_KEY] = workspace["id"]
        request["env"][STATE_DIR_KEY] = str(
            self.root
        )  # a launched engine must address this registry
        digest = self._digest(request)
        reused = None
        with self._lock():
            parent = self._read("sessions", session)
            if parent is None:
                raise ParadevTransportError("Paradev session does not exist")
            daemon = None
            for old in self._windows(session, states=LISTED_STATES):
                if old["name"] != name:
                    continue
                if daemon is None:
                    daemon = self._daemon()
                old = self._reconcile_locked(old, daemon)
                if old["state"] != "active":
                    continue
                folder = self._launch_dir(old["id"])
                if old["id"] in daemon:
                    if (folder / "exit.json").exists():
                        raise ParadevTransportError(
                            "Dispose the exited worker before reusing its window name"
                        )
                    # Only a window adopted after a lost acknowledgement may be handed back for the
                    # retry of the very same request; a deliberate second launch of a live name is refused.
                    if (
                        old.get("acknowledged") is False
                        and old["workspace"] == workspace["id"]
                        and old["cwd"] == cwd
                        and old.get("requestDigest") == digest
                    ):
                        self._set_state(old, "active", acknowledged=True)
                        reused = old
                        break
                    raise ParadevTransportError(
                        "Paradev window name already belongs to a live worker"
                    )
                if (folder / "exit.json").exists():
                    self._set_state(old, "gone")
                    continue
                raise ParadevTransportError(
                    "Previous worker PTY disappeared without an exit record; "
                    "reconcile its exact terminal before retrying"
                )
            if reused is None:
                ident = str(uuid.uuid4())
                request["env"][TERMINAL_KEY] = ident
                request["env"][CONTEXT_FILE] = str(self._launch_dir(ident) / "context.json")
                private_json(
                    self._launch_dir(ident) / "launch.json",
                    {
                        **request,
                        "terminalId": ident,
                        "workspaceId": workspace["id"],
                        "session": session,
                        "window": name,
                        "cwd": cwd,
                        "createdAt": now(),
                    },
                )
                row = {
                    "id": ident,
                    "session": session,
                    "name": name,
                    "workspace": workspace["id"],
                    "cwd": cwd,
                    "state": "launching",
                    "options": {},
                    "requestDigest": digest,
                    "paneId": None,
                    "attemptedAt": self._clock(),
                    "createdAt": now(),
                }
                self._write("windows", row)
                self._journal(
                    "window-launching",
                    id=ident,
                    session=session,
                    name=name,
                    workspace=workspace["id"],
                )
                cols, rows = parent.get("cols"), parent.get("rows")
        if reused is not None:
            self._ensure_pane(reused["id"], reused["workspace"])
            return reused["id"]
        runtime = Path(__file__).with_name("paradev_runtime.py")
        initial = posix_exec_command(
            [sys.executable, "-B", str(runtime), "--launch", str(self._launch_dir(ident))]
        )
        payload = {
            "terminalId": ident,
            "workspaceId": workspace["id"],
            "cwd": cwd,
            "initialCommand": initial,
            **({"cols": cols} if cols else {}),
            **({"rows": rows} if rows else {}),
        }
        try:
            result = self._rpc("terminal.launchSession", payload, mutate=True)
        except ParadevTransportError:
            self._journal("launch-unacknowledged", id=ident, session=session, name=name)
            raise ParadevTransportError(
                "Paradev launch acknowledgment lost; the terminal may exist. "
                "Reconcile it before any new launch"
            ) from None
        if (
            not isinstance(result, dict)
            or result.get("terminalId") != ident
            or result.get("status") != "active"
        ):
            self._journal("launch-unacknowledged", id=ident, session=session, name=name)
            raise ParadevTransportError(
                "Paradev launch acknowledgment mismatched; state is unknown"
            )
        with self._lock():
            row = self._read("windows", ident)
            self._set_state(row, "active", acknowledged=True)
        self._ensure_pane(ident, workspace["id"])
        return ident

    def _ensure_pane(self, terminal_id, workspace_id, *, focus=False):
        """A live binding with no visible pane is a layout fact; attach the existing terminal."""
        try:
            panes = self._panes(workspace_id, terminal_id)
            if not panes:
                created = self._cli(
                    ["panes", "new-tab", "--workspace", workspace_id, "--terminal", terminal_id]
                )
                self._journal("pane-attach-requested", id=terminal_id, pane=_pane_id(created))
                panes = self._panes(
                    workspace_id, terminal_id
                )  # the layout, read again, is the truth
            if len(panes) != 1 or _pane_id(panes[0]) is None:
                # Zero panes after an attach, or several: unverified. Never a second terminal and
                # never a closed pane; the record says the pane is unknown.
                self._journal("pane-unverified", id=terminal_id, panes=len(panes))
                print(
                    f"Paradev pane layout unverified for the terminal ({len(panes)} matching panes)",
                    file=sys.stderr,
                )
                pane = None
            else:
                pane = _pane_id(panes[0])
                if focus:
                    self._cli(["panes", "focus", "--workspace", workspace_id, "--pane", pane])
        except (ParadevTransportError, AttributeError, KeyError, TypeError) as exc:
            self._journal("pane-unverified", id=terminal_id, error=type(exc).__name__)
            print("Paradev pane layout unverified for the terminal", file=sys.stderr)
            return None
        with self._lock():
            row = self._read("windows", terminal_id)
            if row is not None:
                row["paneId"] = pane
                self._write("windows", row)
        return pane

    def _panes(self, workspace_id, terminal_id):
        layout = self._cli(["panes", "list", "--workspace", workspace_id])
        if not isinstance(layout, dict) or not isinstance(layout.get("tabs"), list):
            raise ParadevTransportError("Paradev pane inventory is unavailable")
        tabs = [*layout["tabs"], *(layout.get("sidebar") or {}).get("tabs", [])]
        return [
            pane
            for tab in tabs
            for pane in tab.get("panes", [])
            if pane.get("terminalId") == terminal_id
        ]

    def _reconcile_locked(self, row, daemon):
        """Resolve a ``launching`` record against the bootstrap proof and the host; never relaunch."""
        if row["state"] != "launching":
            return row
        folder = self._launch_dir(row["id"])
        if row["id"] in daemon:
            bootstrap = self._bootstrap(row["id"])
            if bootstrap is None:
                raise ParadevTransportError(
                    "Previous Paradev launch is unresolved: its terminal is live but "
                    "the worker bootstrap has not confirmed; retry reconciliation"
                )
            if (
                bootstrap.get("terminalId") != row["id"]
                or bootstrap.get("workspaceId") != row["workspace"]
                or bootstrap.get("pid") != daemon[row["id"]].get("pid")
            ):
                raise ParadevTransportError(
                    "Worker bootstrap identity mismatched; inspect the terminal"
                )
            self._set_state(row, "active", acknowledged=False, adopted=True)
            return row
        if (folder / "context.json").exists() or (folder / "worker.json").exists():
            if (folder / "exit.json").exists():
                self._set_state(row, "gone")
                return row
            raise ParadevTransportError(
                "Previous worker ran and its PTY disappeared without an exit record; "
                "inspect native history before replacing it"
            )
        age = self._clock() - float(row.get("attemptedAt") or 0)
        if age < VOID_GRACE_S:
            raise ParadevTransportError(
                f"Previous Paradev launch is unresolved; the host may still be "
                f"creating it. Retry reconciliation in {int(VOID_GRACE_S - age) + 1}s"
            )
        rows = self._rpc("terminal.list", {"workspaceId": row["workspace"]})
        rows = rows.get("sessions") if isinstance(rows, dict) else rows
        if not isinstance(rows, list):
            raise ParadevTransportError(
                "Paradev terminal list unavailable; reconciliation deferred"
            )
        if any(isinstance(r, dict) and r.get("terminalId") == row["id"] for r in rows):
            raise ParadevTransportError(
                "Paradev lists the terminal but the daemon does not; reconciliation deferred"
            )
        # Dispose the minted id on the host first: a disposed row makes a late create for that id
        # refused, so "never created" cannot turn into a second worker later. The host's
        # acknowledgement is unconditional, so the proof is fresh daemon absence after it, and the
        # bootstrap records are read again behind that barrier: a worker that started in between
        # keeps the record unresolved.
        result = self._rpc(
            "terminal.killSession",
            {"terminalId": row["id"], "workspaceId": row["workspace"]},
            mutate=True,
        )
        if (
            not isinstance(result, dict)
            or result.get("terminalId") != row["id"]
            or result.get("status") != "disposed"
        ):
            raise ParadevTransportError(
                "Paradev did not confirm disposal of the unacknowledged terminal id"
            )
        if not self._wait_gone(row["id"]):
            raise ParadevTransportError(
                "Paradev disposed the unacknowledged terminal id but its PTY is still listed; "
                "reconciliation deferred"
            )
        if (folder / "context.json").exists() or (folder / "worker.json").exists():
            raise ParadevTransportError(
                "A worker started under the unacknowledged terminal id during reconciliation; "
                "inspect it before replacing it"
            )
        self._set_state(row, "void", tombstoned=True)
        return row

    def _wait_gone(self, terminal_id):
        """True once the daemon no longer lists the PTY; False when it is still listed after the wait."""
        deadline = time.monotonic() + DISPOSE_WAIT_S
        while True:
            try:
                alive = terminal_id in self._daemon()
            except ParadevTransportError:
                alive = None
            if alive is False:
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.1)

    def reconcile(self, target):
        """Explicit reconciliation of a lost launch. Returns the record's resulting state."""
        with self._lock():
            row = self._window(target, states=None)
            if row is None:
                raise ParadevTransportError("No owned Paradev terminal for that target")
            if row["state"] == "launching":
                row = self._reconcile_locked(row, self._daemon())
        if row["state"] == "active" and row.get("adopted"):
            self._ensure_pane(row["id"], row["workspace"])
        return row["state"]

    def new_parked_window(self, session, name, cwd, argv, return_opt):
        return self.new_window(session, name, cwd, {}, posix_parked_command(argv))

    def list_window_ids(self, session):
        daemon = self._daemon()
        with self._lock():
            rows = self._windows(session)
            result = []
            for row in rows:
                if row["state"] == "launching":
                    try:
                        row = self._reconcile_locked(row, daemon)
                    except ParadevTransportError:
                        result.append(row["id"])
                        continue
                if (
                    row["state"] == "active"
                    and row["id"] not in daemon
                    and (self._launch_dir(row["id"]) / "exit.json").exists()
                ):
                    self._set_state(row, "gone")
                if row["state"] in LISTED_STATES:
                    result.append(row["id"])
        return result

    def list_windows(self, session, fields):
        try:
            rows = self._windows(session)
        except ParadevTransportError as exc:
            print(f"Paradev window listing unavailable: {exc}", file=sys.stderr)
            return []
        result = []
        for index, row in enumerate(rows):
            values = {
                "window_id": row["id"],
                "window_name": row["name"],
                "window_index": index,
                "pane_id": row["id"],
                "pane_current_path": row["cwd"],
                **row.get("options", {}),
            }
            result.append(tuple(str(values.get(field, "")) for field in fields))
        return result

    def window_alive(self, session, window_id):
        with self._lock():
            row = self._read("windows", window_id)
        if row is None or row.get("session") != session or row["state"] not in LISTED_STATES:
            return False
        daemon = self._daemon()
        if row["state"] == "launching":
            with self._lock():
                row = self._reconcile_locked(self._read("windows", window_id), daemon)
            if row["state"] != "active":
                return False
        folder = self._launch_dir(window_id)
        bootstrap = self._bootstrap(window_id)
        if (
            bootstrap is not None
            and window_id in daemon
            and bootstrap.get("pid") != daemon[window_id].get("pid")
        ):
            raise ParadevTransportError(
                "Worker PTY was replaced under the same terminal id; liveness is unknown"
            )
        if (folder / "exit.json").exists():
            return False
        if (folder / "bootstrap-error.json").exists():
            return False  # the worker command never ran: a known, dead-on-arrival window
        if window_id not in daemon:
            raise ParadevTransportError(
                "Worker PTY is missing without an exit record; liveness is unknown"
            )
        bootstrap = self._bootstrap(window_id)
        if bootstrap is None:
            # The bootstrap records itself before the command runs. Past the grace the PTY is live
            # without any record of ours: startup is unknown, not a death (nothing was cancelled).
            age = self._clock() - float(row.get("attemptedAt") or 0)
            if age < BOOTSTRAP_GRACE_S:
                return True
            raise ParadevTransportError(
                "Worker bootstrap has not confirmed within the startup grace; liveness is unknown"
            )
        if bootstrap.get("pid") != daemon[window_id].get("pid"):
            raise ParadevTransportError(
                "Worker PTY was replaced under the same terminal id; liveness is unknown"
            )
        return True

    def kill_window(self, target):
        with self._lock():
            row = self._window(target, states=None)
        if row is None or row["state"] in FINAL_STATES:
            return
        bootstrap = self._bootstrap(row["id"])
        daemon = self._daemon()
        if (
            bootstrap is not None
            and row["id"] in daemon
            and bootstrap.get("pid") != daemon[row["id"]].get("pid")
        ):
            raise ParadevTransportError("Refusing disposal of a replaced worker PTY")
        if row["state"] in LISTED_STATES:
            self.native_identity(
                row["id"]
            )  # keep the last native identity with the record before disposal
        try:
            result = self._rpc(
                "terminal.killSession",
                {"terminalId": row["id"], "workspaceId": row["workspace"]},
                mutate=True,
            )
        except ParadevTransportError:
            print("Paradev cleanup unconfirmed; exact terminal remains owned", file=sys.stderr)
            return  # best effort by contract; never claim disposal
        if (
            not isinstance(result, dict)
            or result.get("terminalId") != row["id"]
            or result.get("status") != "disposed"
        ):
            print("Paradev cleanup not acknowledged; exact terminal remains owned", file=sys.stderr)
            return
        # The acknowledgement is the host's; the PTY's absence from the daemon is the proof.
        if not self._wait_gone(row["id"]):
            self._journal("dispose-pending", id=row["id"], session=row["session"], name=row["name"])
            print(
                "Paradev disposal acknowledged but the PTY is still listed; terminal remains owned",
                file=sys.stderr,
            )
            return
        with self._lock():
            row = self._read("windows", row["id"])
            if row is not None and row["state"] not in FINAL_STATES:
                self._set_state(row, "disposed")

    def _address(self, target):
        with self._lock():
            row = self._window(target)
        if row is None:
            raise ParadevTransportError("Paradev terminal is not owned by this backend")
        return row

    def select_window(self, target):
        try:
            row = self._address(target)
            self._ensure_pane(row["id"], row["workspace"], focus=True)
        except ParadevTransportError:
            print("Paradev pane selection unavailable", file=sys.stderr)

    def rename_window(self, target, name):
        with self._lock():
            row = self._window(target)
            if row is None:
                raise ParadevTransportError("Paradev window does not exist")
            if any(
                r["name"] == name and r["id"] != row["id"] for r in self._windows(row["session"])
            ):
                raise ParadevTransportError("Paradev window name already in use")
            row["name"] = name
            self._write("windows", row)
            self._journal("window-renamed", id=row["id"], session=row["session"], name=name)

    def set_display_title(self, window_id, title):
        """Set a terminal's display title separately from its registry window identity."""
        if not isinstance(title, str) or any(char in title for char in ("\x00", "\r", "\n")):
            raise ParadevTransportError("Paradev display title must be a single line of text")
        row = self._address(window_id)
        listing = self._rpc("terminal.list", {"workspaceId": row["workspace"]})
        terminals = listing.get("sessions") if isinstance(listing, dict) else None
        matches = (
            [
                terminal
                for terminal in terminals
                if isinstance(terminal, dict) and terminal.get("terminalId") == row["id"]
            ]
            if isinstance(terminals, list)
            else []
        )
        if len(matches) != 1 or matches[0].get("workspaceId") != row["workspace"]:
            raise ParadevTransportError(
                "Paradev title target is not listed in its original workspace"
            )
        current = matches[0].get("customTitle")
        previous = row.get("managedTitle")
        if current and current not in (previous, title):
            self._journal("title-operator-owned", id=row["id"])
            return  # Preserve the operator's title; stable window identity is unchanged.
        if current != title:
            self._rpc(
                "terminal.rename",
                {"terminalId": row["id"], "workspaceId": row["workspace"], "title": title},
                mutate=True,
            )
        observed = self._rpc("terminal.list", {"workspaceId": row["workspace"]})
        rows = (observed.get("sessions") or []) if isinstance(observed, dict) else []
        if not any(
            r.get("terminalId") == row["id"] and r.get("customTitle") == title for r in rows
        ):
            raise ParadevTransportError("Paradev display title update unconfirmed")
        with self._lock():
            record = self._read("windows", row["id"])
            if record is None:
                raise ParadevTransportError("Title target registry disappeared")
            record["managedTitle"] = title
            self._write("windows", record)

    def set_window_option(self, target, option, value):
        try:
            with self._lock():
                row = self._window(target)
                if row is None:
                    return
                row.setdefault("options", {})[option] = value
                self._write("windows", row)
        except ParadevTransportError:
            self._note_output(
                "window-option-write-unavailable",
                target,
                line="Paradev window option write unconfirmed",
            )

    def unset_window_option(self, target, option):
        try:
            with self._lock():
                row = self._window(target)
                if row is None:
                    return
                row.get("options", {}).pop(option, None)
                self._write("windows", row)
        except ParadevTransportError:
            self._note_output(
                "window-option-remove-unavailable",
                target,
                line="Paradev window option removal unconfirmed",
            )

    def show_window_option(self, target, option):
        try:
            with self._lock():
                row = self._window(target)
        except ParadevTransportError:
            self._note_output(
                "window-option-read-unavailable",
                target,
                line="Paradev window option observation unavailable",
            )
            return ""
        return str(row.get("options", {}).get(option, "")) if row else ""

    # ------------------------------------------------------------- pane I/O

    def pipe_pane(self, window_id, log_file):
        """Tee the terminal's raw output to ``log_file``.

        Never raises after the worker exists: a reader that cannot start is journaled and said
        on stderr, and the worker keeps running with an empty log, which the engine's own
        evidence gates read as "rendered nothing". A dead window is tolerated. A running reader
        is never stopped for another log file; the same path with a replaced file (the engine
        re-creates its log) is handed off to a new reader from the recorded cursor, and a reader
        that is no longer running is rebound to whatever log the engine asks for, cursor kept.
        """
        row = self._address(window_id)
        folder = self._launch_dir(row["id"])
        log_file = str(Path(log_file).resolve())
        try:
            self._tee(row, folder, log_file)
        except Exception as exc:  # the worker exists: nothing in the output path may escape
            self._note_output(
                "output-reader-failed",
                row["id"],
                verdict="setup:" + type(exc).__name__,
                line="Paradev output reader could not be set up; the worker keeps running without a log",
            )

    def _note_output(self, event, terminal_id, *, line, **fields):
        """Report an output-reader problem without ever raising: journal, else an in-memory note.

        The same exhaustion that stopped the reader can stop the report (no descriptors, no
        space), so a failed journal write falls back to ``self.unreported`` and the stderr line
        is best effort too. The worker stays owned either way.
        """
        note = {"at": None, "event": event, "id": terminal_id, **fields}
        try:
            note["at"] = now()
            self._journal(event, id=terminal_id, **fields)
        except Exception:
            self.unreported.append(note)
        try:
            print(line, file=sys.stderr)
        except Exception:
            self.unreported.append({"event": "stderr-unavailable", "id": terminal_id})

    def _tee(self, row, folder, log_file):
        try:
            if (folder / "exit.json").exists() and row["id"] not in self._daemon():
                return  # the window already died; nothing left to tee
        except ParadevTransportError:
            self._note_output(
                "output-daemon-unavailable",
                row["id"],
                line="Paradev output daemon observation unavailable; reader retains ownership",
            )
        running = self.output_status(row["id"])
        try:
            with file_lock(folder / "output.lock", blocking=False):
                pass
        except LockUnavailableError:
            if running.get("log") != log_file or not _same_inode(log_file, running.get("logInode")):
                self._note_output(
                    "output-reader-rebind-refused",
                    row["id"],
                    line="Paradev output reader still owns another log; stop it before rebinding",
                )
            return
        runtime = Path(__file__).with_name("paradev_runtime.py")
        process = subprocess.Popen(
            [sys.executable, "-B", str(runtime), "--tee", str(folder), log_file],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            **cast(Any, detach_kwargs()),
        )
        deadline = time.monotonic() + READER_READY_S
        verdict = None
        while time.monotonic() < deadline:
            status = self.output_status(row["id"])
            if status.get("pid") == process.pid and status.get("log") == log_file:
                if status.get("state") in ("ready", "gapped", "ended"):
                    return
                if status.get("state") == "failed":
                    verdict = "failed"
                    break
            if process.poll() is not None:
                verdict = "exited"
                break
            time.sleep(0.05)
        self._note_output(
            "output-reader-failed",
            row["id"],
            verdict=verdict or "timeout",
            line="Paradev output reader did not become ready; the worker keeps running without a log",
        )

    def output_status(self, window_id):
        path = self._launch_dir(window_id) / "output.json"
        if not path.exists():
            return {}
        try:
            return read_private_json(path)
        except (OSError, ValueError):
            return {"state": "unreadable"}

    def send_text(self, window_id, text):
        row = self._address(window_id)
        result = self._rpc(
            "terminal.send",
            {
                "terminalId": row["id"],
                "workspaceId": row["workspace"],
                "text": text,
                "submit": True,
            },
            mutate=True,
        )
        if (
            not isinstance(result, dict)
            or result.get("terminalId") != row["id"]
            or result.get("submitted") is not True
        ):
            raise ParadevTransportError(
                "Worker input delivery unconfirmed; do not blindly repeat it"
            )

    def send_keys(self, target, *keys):
        if not keys or any(key not in ALLOWED_KEYS for key in keys):
            raise ParadevTransportError("Paradev backend presses only Enter and Escape")
        row = self._address(target)
        data = "".join("\r" if key == "Enter" else "\x1b" for key in keys)
        result = self._rpc(
            "terminal.writeInput",
            {"terminalId": row["id"], "workspaceId": row["workspace"], "data": data},
            mutate=True,
        )
        if not isinstance(result, dict) or result.get("success") is not True:
            raise ParadevTransportError("Worker key delivery unconfirmed")

    def capture_pane(self, window_id):
        row = self._address(window_id)
        result = self._rpc(
            "terminal.snapshot", {"terminalId": row["id"], "workspaceId": row["workspace"]}
        )
        if not isinstance(result, dict) or not isinstance(result.get("text"), str):
            raise ParadevTransportError("Paradev screen unavailable")
        rows = result.get("rows")
        lines = result["text"].splitlines()
        return "\n".join(lines[-rows:] if isinstance(rows, int) and rows > 0 else lines)

    # ------------------------------------------------------- client / attach

    def attach_target_argv(self, target):
        row = self._address(target)
        return [
            self.cli_path,
            "terminals",
            "attach",
            "--local",
            "--workspace",
            row["workspace"],
            "--terminal",
            row["id"],
        ]

    def current_pane_id(self):
        value = os.environ.get("SUPERSET_TERMINAL_ID")
        if not value:
            return None
        try:
            with self._lock():
                row = self._read("windows", value)
        except ParadevTransportError:
            return None
        return value if row and row["state"] in LISTED_STATES else None

    def current_window_id(self):
        return self.current_pane_id()

    def current_session(self):
        ident = self.current_pane_id()
        if ident is None:
            return None
        with self._lock():
            row = self._read("windows", ident)
        return row["session"] if row else None

    def detach_client(self):
        return False  # there is no nested terminal client to detach

    def switch_client(self, target, last_fallback=False):
        self.select_window(target)
        return None  # a pane request cannot confirm that a viewing client moved

    # ---------------------------------------------------------------- probes

    def available(self):
        """The host manifest is readable and the output reader's dependency is importable."""
        if sys.platform != "darwin" or importlib.util.find_spec("websockets") is None:
            return False
        try:
            self._host()
            return True
        except ParadevTransportError:
            return False

    def version(self):
        if self._version is None:
            try:
                result = subprocess.run(
                    [self.cli_path, "--version"],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    timeout=10,
                    check=False,
                )
                text = (result.stdout or "").strip().splitlines()
                self._version = ("superset " + text[0]) if result.returncode == 0 and text else ""
            except (OSError, subprocess.SubprocessError):
                self._version = ""
        return self._version or None

    def window_pane_pids(self, target):
        try:
            row = self._daemon().get(self._address(target)["id"])
            pid = row.get("pid") if row else None
            return [pid] if type(pid) is int and pid > 0 else []
        except ParadevTransportError as exc:
            self._journal("pid-observation-unavailable", error=type(exc).__name__)
            raise

    # ------------------------------------------------------------- operator

    NATIVE_FIELDS = (
        "agentId",
        "agentSessionId",
        "definitionId",
        "startedAt",
        "endedAt",
        "endReason",
        "resumedIntoTerminalId",
    )

    def native_identity(self, window_id):
        """The host's agent binding for the terminal (native session id and lifecycle), or None.

        Read-only identity fields only; the binding is the host's, never minted or hidden here.
        """
        row = self._address(window_id)
        try:
            binding = self._rpc(
                "terminalAgents.get", {"workspaceId": row["workspace"], "terminalId": row["id"]}
            )
        except ParadevTransportError as exc:
            self._journal("native-observation-unavailable", id=window_id, error=type(exc).__name__)
            raise
        if not isinstance(binding, dict) or binding.get("terminalId") != row["id"]:
            return None
        identity = {key: binding.get(key) for key in self.NATIVE_FIELDS}
        self._remember_native(row["id"], identity)
        return identity

    def _remember_native(self, terminal_id, identity):
        """Persist the last observed native identity with the window record (ids and times only)."""
        with self._lock():
            row = self._read("windows", terminal_id)
            if row is None or row.get("native") == identity:
                return
            first = row.get("native") is None
            row["native"] = identity
            row["nativeObservedAt"] = now()
            row["nativeSource"] = "terminalAgents.get"
            self._write("windows", row)
        if first and identity.get("agentSessionId"):
            self._journal(
                "native-observed",
                id=terminal_id,
                agentId=identity.get("agentId"),
                agentSessionId=identity.get("agentSessionId"),
            )

    def status(self, *, native=False):
        """Owned sessions and windows with states (ids and names only; no env, no commands)."""
        with self._lock():
            sessions = {
                name: {
                    k: v for k, v in (self._read("sessions", name) or {}).items() if k != "options"
                }
                for name in self.list_sessions()
            }
            windows = [
                {
                    k: row.get(k)
                    for k in (
                        "id",
                        "session",
                        "name",
                        "workspace",
                        "state",
                        "paneId",
                        "native",
                        "nativeObservedAt",
                        "nativeSource",
                        "createdAt",
                    )
                }
                for row in self._windows(None, states=None)
            ]
        for row in windows:
            folder = self._launch_dir(row["id"])
            row["exited"] = (folder / "exit.json").exists()
            row["output"] = self.output_status(row["id"]).get("state")
            if native and row["state"] in LISTED_STATES:
                row["native"] = self.native_identity(row["id"])
                with self._lock():
                    record = self._read("windows", row["id"]) or {}
                    for key in ("nativeObservedAt", "nativeSource"):
                        row[key] = record.get(key)
        return {
            "stateRoot": str(self.root),
            "sessions": sessions,
            "windows": windows,
            "unreported": list(self.unreported),
        }


def _code(exc):
    return getattr(exc, "code", None) or type(exc).__name__


def _pane_id(record):
    if not isinstance(record, dict):
        return None
    value = record.get("paneId") or record.get("id")
    return value if isinstance(value, str) and value else None


def _same_inode(path, inode):
    if inode is None:
        return True  # an older record without an inode: trust the path
    try:
        return os.stat(path).st_ino == inode
    except OSError:
        return False


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    mux = ParadevMultiplexer()
    if argv[:1] == ["status"]:
        print(json.dumps(mux.status(native="--native" in argv), indent=2, sort_keys=True))
        return 0
    if argv[:1] == ["reconcile"] and len(argv) == 2:
        print(json.dumps({"target": argv[1], "state": mux.reconcile(argv[1])}))
        return 0
    print(
        "usage: python -m bmad_loop.adapters.paradev_backend status [--native] | reconcile <terminal-id|=session:window>",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
