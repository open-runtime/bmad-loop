"""Zero-token fake-host qualification of the bundled native transport.

HostFixture is adapted from open-runtime/paradev-tooling a5d76d3.
"""

import copy
import json
import os

import pytest

from bmad_loop.adapters import multiplexer
from bmad_loop.adapters.paradev_backend import ParadevMultiplexer, ParadevTransportError
from bmad_loop.adapters.paradev_runtime import (
    CONTEXT_FILE,
    TERMINAL_KEY,
    WORKSPACE_KEY,
    private_json,
    worker_environment,
)

PTY_PID = 123


class HostFixture:
    """Stock host API as the backend sees it: terminals by id, workspaces, panes, no bodies."""

    def __init__(self, root):
        self.root, self.calls, self.cli_calls, self.terminals = str(root), [], [], {}
        self.unavailable = False
        self.lost_response = False  # the create runs on the host but its response is lost
        self.lost_before_create = False  # the create never reaches the host
        self.linger = False  # a disposal is acknowledged but the PTY stays listed
        self.kill_result = None  # override the disposal acknowledgement
        self.late_create = (
            None  # (terminal id, workspace id) to create when its disposal is requested
        )
        self.on_kill = None  # callback(terminal id) run when a disposal is requested
        self.workspaces = [{"id": "workspace", "projectId": "project", "worktreePath": self.root}]
        self.projects = [{"id": "project", "repoPath": self.root}]
        self.panes = {}
        self.panes_fail = False
        self.bindings = {}

    def rpc(self, name, data=None, mutate=False):
        self.calls.append((name, copy.deepcopy(data), mutate))
        if self.unavailable:
            raise RuntimeError("unavailable")
        if name == "workspace.list":
            return copy.deepcopy(self.workspaces)
        if name == "project.list":
            return copy.deepcopy(self.projects)
        if name == "terminal.daemon.listSessions":
            return [
                {"id": t["id"], "alive": t["alive"], "pid": t["pid"], "cols": 200, "rows": 50}
                for t in self.terminals.values()
            ]
        if name == "terminal.list":
            return {
                "sessions": [
                    {
                        "terminalId": t["id"],
                        "workspaceId": t["workspaceId"],
                        "exited": not t["alive"],
                        "customTitle": t.get("customTitle"),
                    }
                    for t in self.terminals.values()
                    if t["workspaceId"] == data.get("workspaceId")
                ]
            }
        if name == "terminal.launchSession":
            if self.lost_before_create:
                raise RuntimeError("lost before create")
            self.terminals[data["terminalId"]] = {
                "id": data["terminalId"],
                "alive": True,
                "pid": PTY_PID,
                "workspaceId": data["workspaceId"],
            }
            if self.lost_response:
                raise RuntimeError("lost create response")
            return {"terminalId": data["terminalId"], "status": "active"}
        if name == "terminal.killSession":
            if self.late_create and self.late_create[0] == data["terminalId"]:
                late, workspace = self.late_create
                self.late_create = None
                self.terminals[late] = {
                    "id": late,
                    "alive": True,
                    "pid": PTY_PID,
                    "workspaceId": workspace,
                }
            if self.on_kill:
                self.on_kill(data["terminalId"])
            if self.kill_result is not None:
                return copy.deepcopy(self.kill_result)
            if not self.linger:
                self.terminals.pop(data["terminalId"], None)
            return {"terminalId": data["terminalId"], "status": "disposed"}
        if name == "terminal.snapshot":
            return {
                "terminalId": data["terminalId"],
                "text": "worker screen",
                "rows": 50,
                "cols": 200,
            }
        if name == "terminal.send":
            return {"terminalId": data["terminalId"], "submitted": True}
        if name == "terminal.writeInput":
            return {"success": True}
        if name == "terminal.rename":
            self.terminals[data["terminalId"]]["customTitle"] = data["title"]
            return {"terminalId": data["terminalId"], "title": data["title"]}
        if name == "terminalAgents.get":
            binding = self.bindings.get(data["terminalId"])
            return copy.deepcopy(binding) if binding else None
        raise AssertionError(name)

    def cli(self, args):
        self.cli_calls.append(list(args))
        if self.panes_fail:
            raise RuntimeError("CLI_FAILED")
        workspace = args[args.index("--workspace") + 1]
        if args[:2] == ["panes", "list"]:
            return {
                "tabs": [{"panes": copy.deepcopy(self.panes.get(workspace, []))}],
                "sidebar": {"tabs": []},
            }
        if args[:2] == ["panes", "new-tab"]:
            terminal = args[args.index("--terminal") + 1]
            pane = {"id": "pane-" + terminal[:8], "terminalId": terminal}
            self.panes.setdefault(workspace, []).append(pane)
            # The real CLI answers paneId/tabId/terminalId, never a bare id.
            return {
                "paneId": pane["id"],
                "tabId": "tab-" + terminal[:4],
                "terminalId": terminal,
                "layout": {},
            }
        if args[:2] == ["panes", "focus"]:
            return {"paneId": args[args.index("--pane") + 1]}
        raise AssertionError(args)

    def launches(self):
        return [c for c in self.calls if c[0] == "terminal.launchSession"]

    def kills(self):
        return [c for c in self.calls if c[0] == "terminal.killSession"]


@pytest.fixture
def setup(project, tmp_path):
    if os.name != "posix":
        pytest.skip("Native Paradev context is qualified on POSIX hosts")
    host = HostFixture(project.project)
    clock = [1000.0]
    mux = ParadevMultiplexer(host=host, state_dir=tmp_path / "state", clock=lambda: clock[0])
    mux.new_session("run", project.project)
    return mux, host, project.project, clock


def bootstrap(mux, root, tid):
    return worker_environment(
        mux.root / "launches" / tid,
        {
            "SUPERSET_TERMINAL_ID": tid,
            "SUPERSET_WORKSPACE_ID": "workspace",
            "SUPERSET_WORKSPACE_PATH": str(root),
        },
        pid=PTY_PID,
    )


def test_actual_terminal_has_one_view_and_own_context(setup):
    mux, host, root, _ = setup
    assert not host.terminals
    tid = mux.new_window(
        "run",
        "dev",
        root,
        {"SUPERSET_TERMINAL_ID": "parent", CONTEXT_FILE: "/stale"},
        "fake-worker",
    )
    bootstrap(mux, root, tid)
    assert mux.window_alive("run", tid)
    assert mux.list_window_ids("run") == [tid]
    assert len(host.launches()) == 1
    mux._ensure_pane(tid, "workspace")
    assert len([c for c in host.cli_calls if c[:2] == ["panes", "new-tab"]]) == 1
    record = json.loads((mux.root / "launches" / tid / "launch.json").read_text(encoding="utf-8"))
    assert record["env"][TERMINAL_KEY] == tid
    assert record["env"][WORKSPACE_KEY] == "workspace"
    assert not any(k.startswith("SUPERSET_") for k in record["env"])
    assert record["env"][CONTEXT_FILE] != "/stale"
    assert not any(c[:2] == ["panes", "focus"] for c in host.cli_calls)


def test_lost_ack_adopts_same_worker_only_after_bootstrap(setup):
    mux, host, root, _ = setup
    host.lost_response = True
    with pytest.raises(ParadevTransportError, match="acknowledgment lost"):
        mux.new_window("run", "dev", root, {}, "fake-worker")
    tid = next(iter(host.terminals))
    bootstrap(mux, root, tid)
    host.lost_response = False
    assert mux.reconcile(tid) == "active"
    assert mux.new_window("run", "dev", root, {}, "fake-worker") == tid
    assert len(host.launches()) == 1
    with pytest.raises(ParadevTransportError, match="live worker"):
        mux.new_window("run", "dev", root, {}, "fake-worker")


def test_unobserved_launch_never_replaced_on_timeout(setup):
    mux, host, root, clock = setup
    host.lost_before_create = True
    with pytest.raises(ParadevTransportError):
        mux.new_window("run", "dev", root, {}, "fake-worker")
    host.unavailable = True
    clock[0] += 9999
    with pytest.raises(ParadevTransportError):
        mux.new_window("run", "dev", root, {}, "fake-worker")
    assert len(host.launches()) == 1


def test_missing_pane_is_attached_without_relaunch(setup):
    mux, host, root, _ = setup
    tid = mux.new_window("run", "dev", root, {}, "fake-worker")
    host.panes = {}
    mux._ensure_pane(tid, "workspace")
    assert len(host.launches()) == 1
    assert host.panes["workspace"][0]["terminalId"] == tid


def test_missing_native_binding_pending_and_title_does_not_mint_it(setup):
    mux, host, root, _ = setup
    tid = mux.new_window("run", "dev", root, {}, "fake-worker")
    mux.set_display_title(tid, "story · review · astra")
    assert mux.native_identity(tid) is None
    assert mux._address(tid)["name"] == "dev"
    host.bindings[tid] = {"terminalId": tid, "agentId": "codex", "agentSessionId": "session-1"}
    assert mux.native_identity(tid)["agentSessionId"] == "session-1"


def test_host_failure_is_visible_and_preserves_worker(setup):
    mux, host, root, _ = setup
    tid = mux.new_window("run", "dev", root, {}, "fake-worker")
    host.unavailable = True
    with pytest.raises(ParadevTransportError):
        mux.window_alive("run", tid)
    with pytest.raises(ParadevTransportError):
        mux.native_identity(tid)
    assert mux._address(tid)["state"] == "active"


def test_foreign_or_nested_checkout_refused(setup, tmp_path):
    mux, host, root, _ = setup
    host.workspaces[0]["worktreePath"] = str(tmp_path)
    with pytest.raises(ParadevTransportError, match="checkout"):
        mux.new_window("run", "dev", root, {}, "fake-worker")
    assert not host.launches()


def test_exited_worker_cleanup_keeps_native_record_and_output(setup):
    mux, host, root, _ = setup
    tid = mux.new_window("run", "dev", root, {}, "fake-worker")
    bootstrap(mux, root, tid)
    host.bindings[tid] = {"terminalId": tid, "agentSessionId": "session-1"}
    private_json(mux.root / "launches" / tid / "exit.json", {"returncode": 0})
    assert not mux.window_alive("run", tid)
    mux.kill_window(tid)
    assert tid not in host.terminals
    row = json.loads((mux.root / "windows" / f"{tid}.json").read_text(encoding="utf-8"))
    assert row["state"] == "disposed"
    assert row["native"]["agentSessionId"] == "session-1"
    assert (mux.root / "launches" / tid / "exit.json").exists()


def test_live_daemon_replacement_is_unknown(setup):
    mux, host, root, _ = setup
    tid = mux.new_window("run", "dev", root, {}, "fake-worker")
    bootstrap(mux, root, tid)
    host.terminals[tid]["pid"] = PTY_PID + 1
    with pytest.raises(ParadevTransportError, match="replaced"):
        mux.window_alive("run", tid)


def test_legacy_external_record_requires_explicit_migration(setup):
    mux, _, root, _ = setup
    row = mux.root / "sessions" / "run.json"
    private_json(row, {"name": "run", "cwd": str(root)})
    with pytest.raises(ParadevTransportError, match="unreadable"):
        mux.has_session("run")


def test_bundled_backend_never_automatically_matches():
    multiplexer._load_builtin_backends()
    matches = [predicate for name, predicate, _ in multiplexer._BACKENDS if name == "paradev"]
    assert len(matches) == 1
    assert all(not matches[0](platform) for platform in ("darwin", "linux", "win32", "other"))


def test_backend_collision_refuses_selection(monkeypatch):
    multiplexer._load_builtin_backends()
    monkeypatch.setattr(
        multiplexer,
        "_BACKENDS",
        [*multiplexer._BACKENDS, ("paradev", lambda _: True, ParadevMultiplexer)],
    )
    with pytest.raises(multiplexer.MultiplexerError, match="Conflicting external"):
        multiplexer._factory_by_name("paradev")


def test_exited_old_actor_does_not_prove_replacement_quiescent(setup):
    mux, host, root, _ = setup
    tid = mux.new_window("run", "dev", root, {}, "fake-worker")
    bootstrap(mux, root, tid)
    private_json(mux.root / "launches" / tid / "exit.json", {"returncode": 0})
    host.terminals[tid]["pid"] = PTY_PID + 1
    with pytest.raises(ParadevTransportError, match="replaced"):
        mux.window_alive("run", tid)
    with pytest.raises(ParadevTransportError, match="replaced"):
        mux.kill_window(tid)
    assert not host.kills()


def test_operator_title_retained_without_renaming_stable_window(setup):
    mux, host, root, _ = setup
    tid = mux.new_window("run", "dev", root, {}, "fake-worker")
    mux.set_display_title(tid, "unit · dev")
    host.terminals[tid]["customTitle"] = "owner focus"
    mux.set_display_title(tid, "unit · review")
    assert host.terminals[tid]["customTitle"] == "owner focus"
    assert mux._address(tid)["name"] == "dev"
