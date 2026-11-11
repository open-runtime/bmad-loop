"""Completed headless jobs dispose only their authenticated parked transport."""

import copy

import pytest
from test_campaign_headless import prepared as prepared
from test_paradev_transport import HostFixture, bootstrap

from bmad_loop import campaign_headless
from bmad_loop.adapters.paradev_backend import ParadevMultiplexer, ParadevTransportError
from bmad_loop.adapters.paradev_runtime import private_json
from bmad_loop.campaign_headless import HeadlessRunner, read_json, write_json
from bmad_loop.campaign_model import CampaignError


@pytest.fixture
def completed(project, prepared, tmp_path, monkeypatch):
    _, _, request, candidate, capability = prepared
    host = HostFixture(project.project)
    mux = ParadevMultiplexer(host=host, state_dir=tmp_path / "native-transport")
    runner = HeadlessRunner(mux)
    path = runner.prepare(
        tmp_path / "presented-job",
        request,
        candidate,
        capability,
        launch_id="completed-launch",
        generation=7,
        presentation={"workspace": str(project.project), "workspace_id": "workspace"},
    )
    launched = runner.launch(path)
    tid = launched["native_id"]
    bootstrap(mux, project.project, tid)
    folder = mux.root / "launches" / tid
    private_json(folder / "exit.json", {"returncode": 0})
    (folder / "output.log").write_text("retained transcript\n")
    host.bindings[tid] = {"terminalId": tid, "agentSessionId": "actual-native-session"}
    host.terminals["manual-peer"] = {
        "id": "manual-peer",
        "alive": True,
        "pid": 999,
        "workspaceId": "workspace",
    }
    host.panes["workspace"].append({"id": "manual-pane", "terminalId": "manual-peer"})
    envelope = read_json(path)
    identity = {key: envelope[key] for key in ("launch_id", "generation", "request_digest")}
    write_json(path.parent / "startup.json", {**identity, "pid": 41, "identity": 41.0})
    write_json(path.parent / "processes.json", {"processes": [{"pid": 42, "identity": 42.0}]})
    write_json(
        path.parent / "receipt.json",
        {**identity, "outcome": "completed", "processes_quiescent": True},
    )
    actors = {41: "dead", 42: "dead"}

    class Processes:
        def liveness_of(self, pid, identity):
            assert identity == float(pid)
            return actors[pid]

    monkeypatch.setattr(campaign_headless, "get_process_host", Processes)
    monkeypatch.setattr("bmad_loop.adapters.paradev_backend.DISPOSE_WAIT_S", 0)
    assert not mux.window_alive(envelope["session"], tid)
    assert tid in host.terminals  # The exited worker's bootstrap is still parked.
    return runner, path, mux, host, tid, actors


def test_finished_job_disposes_parked_bootstrap_once_and_keeps_history(completed):
    runner, path, mux, host, tid, _ = completed
    before = {
        p: p.read_bytes()
        for p in (path, path.parent / "receipt.json", mux.root / "launches" / tid / "output.log")
    }
    panes = copy.deepcopy(host.panes)
    assert runner.observe(path)["state"] == "finished"
    assert tid not in host.terminals
    assert host.terminals["manual-peer"]["alive"]
    assert host.panes == panes
    record = read_json(mux.root / "windows" / f"{tid}.json")
    assert record["state"] == "disposed"
    assert record["native"]["agentSessionId"] == "actual-native-session"
    assert {p: p.read_bytes() for p in before} == before
    assert runner.observe(path)["state"] == "finished"
    assert len(host.kills()) == 1


@pytest.mark.parametrize(
    "fault", ["live-worker", "unknown-descendant", "receipt", "containment", "replaced-pty"]
)
def test_unproven_completion_never_disposes_terminal(completed, fault):
    runner, path, mux, host, tid, actors = completed
    if fault == "live-worker":
        actors[41] = "alive"
    elif fault == "unknown-descendant":
        actors[42] = "unknown"
    elif fault == "receipt":
        receipt = read_json(path.parent / "receipt.json")
        write_json(path.parent / "receipt.json", {**receipt, "launch_id": "other"})
    elif fault == "containment":
        receipt = read_json(path.parent / "receipt.json")
        write_json(path.parent / "receipt.json", {**receipt, "processes_quiescent": False})
    else:
        host.terminals[tid]["pid"] += 1
    if fault in {"receipt", "replaced-pty"}:
        with pytest.raises((CampaignError, ParadevTransportError)):
            runner.observe(path)
    else:
        assert runner.observe(path)["state"] == "unknown"
    assert not host.kills()
    assert tid in host.terminals


@pytest.mark.parametrize("fault", ["lost-before", "lost-after", "lingering", "bad-ack"])
def test_disposal_requires_readback_and_recovers_without_relaunch(completed, fault):
    runner, path, mux, host, tid, _ = completed
    if fault.startswith("lost"):

        def lose_ack(terminal):
            if fault == "lost-after":
                host.terminals.pop(terminal, None)
            raise RuntimeError("lost disposal response")

        host.on_kill = lose_ack
    elif fault == "lingering":
        host.linger = True
    else:
        host.kill_result = {"terminalId": "different", "status": "disposed"}
    result = runner.observe(path)
    assert result["state"] == "unknown"
    assert "disposal" in result["reason"].lower()
    assert read_json(mux.root / "windows" / f"{tid}.json")["state"] == "active"
    host.on_kill = None
    host.linger = False
    host.kill_result = None
    assert runner.observe(path)["state"] == "finished"
    assert tid not in host.terminals
    assert len(host.launches()) == 1
    assert host.terminals["manual-peer"]["alive"]


@pytest.mark.parametrize("fault", ["window", "bootstrap", "workspace"])
def test_missing_native_ownership_leaves_completed_transport_unknown(completed, monkeypatch, fault):
    runner, path, mux, host, tid, _ = completed
    context = mux.root / "launches" / tid / "context.json"
    if fault == "window":
        (path.parent / "window.json").unlink()
        monkeypatch.setattr(mux, "list_windows", lambda *_: [])
    elif fault == "bootstrap":
        context.unlink()
    else:
        private_json(context, {**read_json(context), "workspaceId": "other"})
    result = runner.observe(path)
    assert result["state"] == "unknown" and "disposal" in result["reason"]
    assert not host.kills()
    assert tid in host.terminals


@pytest.mark.parametrize("fault", ["session", "exit"])
def test_backend_completed_disposal_refuses_unbound_or_unfinished_target(completed, fault):
    _, path, mux, host, tid, _ = completed
    session = read_json(path)["session"]
    if fault == "session":
        session = "foreign-session"
    else:
        (mux.root / "launches" / tid / "exit.json").unlink()
    with pytest.raises(ParadevTransportError):
        mux.dispose_completed_window(session, tid)
    assert not host.kills()
    assert tid in host.terminals


def test_truthy_disposal_response_does_not_prove_terminal_absence(completed, monkeypatch):
    runner, path, mux, host, tid, _ = completed
    monkeypatch.setattr(mux, "dispose_completed_window", lambda *_: {"acknowledged": True})
    assert runner.observe(path)["state"] == "unknown"
    assert tid in host.terminals
