"""Linux terminated descendants remain dead while their new parent has not reaped."""

from pathlib import Path

import pytest

from bmad_loop import childrun, process_host

PID = 43210
BIRTH = 9474813.0


@pytest.fixture
def proc_host(tmp_path, monkeypatch):
    proc = tmp_path / "proc"
    stat = proc / str(PID) / "stat"
    stat.parent.mkdir(parents=True)
    monkeypatch.setattr(process_host.sys, "platform", "linux")
    monkeypatch.setattr(
        process_host, "Path", lambda value: proc if value == "/proc" else Path(value)
    )
    host = process_host.PosixProcessHost()
    # kill(pid, 0) also succeeds for zombies. Exercise that exact distinction.
    monkeypatch.setattr(host, "is_alive", lambda pid: pid == PID)
    signals = []
    monkeypatch.setattr(host, "terminate", lambda pid: signals.append((pid, "term")))
    monkeypatch.setattr(host, "force_kill", lambda pid: signals.append((pid, "kill")))
    monkeypatch.setattr(host, "descendants", lambda pid: {})
    return host, stat, signals


def write_stat(path, state, birth=BIRTH):
    fields = [state, "1", *(["0"] * 17), str(int(birth))]
    path.write_text(f"{PID} (child (with spaces)) {' '.join(fields)}\n", encoding="utf-8")


@pytest.mark.parametrize("state", ["Z", "X"])
@pytest.mark.parametrize("recorded", [BIRTH, None])
def test_terminated_unreaped_identity_is_dead_and_not_signalled(proc_host, state, recorded):
    host, stat, signals = proc_host
    write_stat(stat, state)
    assert host.identity(PID) == BIRTH  # Keep the immutable birth evidence.
    assert host.is_alive(PID)  # Bare PID existence alone cannot prove life.
    assert host.liveness_of(PID, recorded) == "dead"
    assert not host.alive_and_ours(PID, recorded)
    childrun._reap_descendants(host, {PID: BIRTH}, wait_s=0)
    assert signals == []


@pytest.mark.parametrize("state", ["R", "S", "D", "T"])
def test_live_same_identity_still_requires_cleanup(proc_host, state):
    host, stat, signals = proc_host
    write_stat(stat, state)
    assert host.liveness_of(PID, BIRTH) == "alive"
    assert host.alive_and_ours(PID, BIRTH)
    childrun._reap_descendants(host, {PID: BIRTH}, wait_s=0)
    assert signals == [(PID, "term"), (PID, "kill")]


def test_reused_live_pid_is_not_signalled(proc_host):
    host, stat, signals = proc_host
    write_stat(stat, "S", BIRTH + 1)
    assert host.liveness_of(PID, BIRTH) == "dead"
    childrun._reap_descendants(host, {PID: BIRTH}, wait_s=0)
    assert signals == []


@pytest.mark.parametrize("fault", ["unreadable", "malformed", "truncated-zombie"])
def test_unreadable_existing_identity_remains_unknown_and_not_signalled(
    proc_host, monkeypatch, fault
):
    host, stat, signals = proc_host
    if fault == "unreadable":
        original = Path.read_text

        def read(path, *args, **kwargs):
            if path == stat:
                raise PermissionError("unreadable process")
            return original(path, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", read)
    else:
        stat.write_text(
            "not a process stat" if fault == "malformed" else f"{PID} (child) Z\n", encoding="utf-8"
        )
    assert host.identity(PID) is None
    assert host.liveness_of(PID, BIRTH) == "unknown"
    childrun._reap_descendants(host, {PID: BIRTH}, wait_s=0)
    assert signals == []
