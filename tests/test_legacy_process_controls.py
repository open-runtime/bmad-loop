"""Writer and terminal boundaries retain ambiguous live legacy macOS owners."""

from __future__ import annotations

import contextlib
import os
from types import SimpleNamespace

import pytest
from test_cli import _escalated_run
from test_engine_worktree import _finished_kept_unit, _state_bytes
from test_unitreplay import _mint, _run_dirs

from bmad_loop import cli, runs, unitreplay
from bmad_loop.journal import load_state
from bmad_loop.model import Phase
from bmad_loop.tui import launch


@pytest.fixture
def legacy_owner(monkeypatch):
    """Real persisted positive stamp, with the host's PID observation controlled.

    Native clock and host semantics are covered in test_process_host. This seam
    lets writer-boundary tests run on every OS without signalling a real process.
    """
    present = {"value": True}
    pid = os.getpid()

    def ambiguous(observed_pid, identity):
        return present["value"] and observed_pid == pid and identity == 1700000000.5

    host = SimpleNamespace(
        legacy_identity_unverifiable=ambiguous,
        liveness_of=lambda p, i: "unknown" if ambiguous(p, i) else "dead",
        alive_and_ours=lambda p, i: False,
    )
    monkeypatch.setattr(runs, "get_process_host", lambda: host)

    def publish(run_dir):
        (run_dir / "engine.pid").write_text(f"{pid} 1700000000.5", encoding="utf-8")
        assert runs.read_named_pid_identity(run_dir / "engine.pid") == (pid, 1700000000.5)

    return present, publish


@pytest.mark.parametrize(
    "command,options",
    [
        ("resume", []),
        ("resolve", ["--force", "--no-interactive", "--resume"]),
        ("resolve", ["--force", "--adopt-branch", "--no-interactive", "--resume"]),
        ("resolve", ["--force", "--reverify", "--no-interactive", "--resume"]),
        ("resolve", ["--force", "--abandon", "--no-interactive"]),
    ],
)
def test_cli_refuses_live_legacy_owner_even_with_force(
    tmp_path, monkeypatch, capsys, legacy_owner, command, options
):
    _present, publish = legacy_owner
    run_dir = _escalated_run(tmp_path)
    publish(run_dir)
    before = _state_bytes(run_dir)
    monkeypatch.setattr(cli, "_prepare_resume_locked", lambda *a, **k: pytest.fail("double drive"))
    assert cli.main([command, "--project", str(tmp_path), "r1", *options]) == 1
    assert "legacy macOS process identity" in capsys.readouterr().err
    assert _state_bytes(run_dir) == before


def test_locked_resume_rechecks_legacy_owner_before_preparation(
    tmp_path, monkeypatch, capsys, legacy_owner
):
    present, publish = legacy_owner
    run_dir = _escalated_run(tmp_path)
    present["value"] = False

    @contextlib.contextmanager
    def rival_claim(_run_dir):
        publish(run_dir)
        present["value"] = True
        yield

    monkeypatch.setattr(cli, "state_lock", rival_claim)
    monkeypatch.setattr(cli, "_prepare_resume_locked", lambda *a, **k: pytest.fail("double drive"))
    assert cli._resume_paused_run(tmp_path, run_dir) == 1
    assert "legacy macOS process identity" in capsys.readouterr().err


def test_resolve_rechecks_legacy_owner_at_locked_rearm(tmp_path, monkeypatch, capsys, legacy_owner):
    present, publish = legacy_owner
    run_dir = _escalated_run(tmp_path)
    before = _state_bytes(run_dir)
    present["value"] = False

    @contextlib.contextmanager
    def rival_claim(_run_dir):
        publish(run_dir)
        present["value"] = True
        yield

    monkeypatch.setattr(cli, "state_lock", rival_claim)
    monkeypatch.setattr(runs, "rearm_escalation", lambda *a, **k: pytest.fail("rearmed live owner"))
    assert (
        cli.main(
            ["resolve", "--project", str(tmp_path), "r1", "--force", "--no-interactive", "--resume"]
        )
        == 1
    )
    assert "legacy macOS process identity" in capsys.readouterr().err
    assert _state_bytes(run_dir) == before


def test_legacy_owner_exit_allows_normal_resolve(tmp_path, monkeypatch, legacy_owner):
    present, publish = legacy_owner
    run_dir = _escalated_run(tmp_path)
    publish(run_dir)
    present["value"] = False
    resumed = []
    monkeypatch.setattr(cli, "_resume_paused_run", lambda project, rd: resumed.append(rd) or 0)
    assert (
        cli.main(
            ["resolve", "--project", str(tmp_path), "r1", "--force", "--no-interactive", "--resume"]
        )
        == 0
    )
    assert resumed == [run_dir]
    assert load_state(run_dir).tasks["s1"].phase == Phase.PENDING


def test_locked_replay_cannot_force_past_legacy_owner(project, tmp_path, legacy_owner):
    present, publish = legacy_owner
    engine, _marker = _finished_kept_unit(project, tmp_path)
    publish(engine.run_dir)
    before = _state_bytes(engine.run_dir)
    with pytest.raises(unitreplay.ReplayError, match="legacy macOS process identity"):
        _mint(project, engine, force=True)
    assert _run_dirs(engine) == []
    assert _state_bytes(engine.run_dir) == before
    present["value"] = False
    assert _mint(project, engine, force=True).is_dir()


def test_parked_control_window_kept_until_legacy_owner_exits(tmp_path, monkeypatch, legacy_owner):
    present, publish = legacy_owner
    run_id = "20260101-000000-aaaa"
    run_dir = _escalated_run(tmp_path, run_id)
    publish(run_dir)
    mux = SimpleNamespace(current_window_id=lambda: "%current", capture_pane=lambda win: "parked")
    monkeypatch.setattr(launch, "get_multiplexer", lambda: mux)
    monkeypatch.setattr(launch, "mux_usable", lambda mux: True)
    monkeypatch.setattr(launch, "session_exists", lambda name: True)
    monkeypatch.setattr(runs, "ctl_session_for", lambda project, mux: "ctl")
    monkeypatch.setattr(launch, "_list_ctl_windows", lambda *a: [("%worker", f"run-{run_id}", "")])
    monkeypatch.setattr(launch, "parked_screen", lambda text: True)
    candidates, uncertain = launch._ctl_window_candidates(tmp_path)
    assert candidates == []
    assert len(uncertain) == 1 and "legacy macOS owner PID" in uncertain[0][1]
    present["value"] = False
    assert launch._ctl_window_candidates(tmp_path) == ([("%worker", f"run-{run_id}")], [])
