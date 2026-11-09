"""Native child observation is bounded and never authenticates a replaced process."""

import ctypes
import errno
import select
import subprocess
import sys
from types import SimpleNamespace

import pytest

from bmad_loop import childrun, process_host


@pytest.fixture
def tree(monkeypatch):
    births = {10: 100.0, 20: 101.0, 30: 102.0, 40: 103.0}
    parents = {10: 1, 20: 10, 30: 20, 40: 999}
    edges = {10: [20, 40], 20: [30], 30: [10]}
    calls = []

    class Process:
        def __init__(self, pid):
            self.pid = pid
            self._ident = (pid, births[pid])

        def ppid(self):
            return parents[self.pid]

        def is_running(self):
            return self._ident == (self.pid, births.get(self.pid))

        def create_time(self):
            pytest.fail("adjusted clock identity is forbidden")

        def children(self, **kwargs):
            pytest.fail("host-wide psutil census is forbidden")

    def children(pid):
        calls.append(pid)
        return edges.get(pid, [])

    monkeypatch.setattr(process_host.sys, "platform", "darwin")
    monkeypatch.setattr(process_host, "_psutil", lambda: SimpleNamespace(Process=Process))
    monkeypatch.setattr(process_host, "_macos_child_pids", children)
    return births, parents, edges, calls, Process


def test_transitive_snapshot_retains_exact_births_and_rejects_unrelated_cycle(tree):
    assert process_host._macos_descendants(10) == {20: -101.0, 30: -102.0}
    assert tree[3] == [10, 20, 30]


@pytest.mark.parametrize(
    "fault", ["before-construction", "after-stamp", "parent-reuse", "older-child", "unknown-birth"]
)
def test_replacement_and_unverifiable_ancestry_are_omitted(tree, monkeypatch, fault):
    births, parents, edges, calls, Process = tree
    if fault == "before-construction":
        old = process_host._macos_child_pids

        def changed(pid):
            result = old(pid)
            if pid == 10:
                births[20] += 10
                parents[20] = 999
            return result

        monkeypatch.setattr(process_host, "_macos_child_pids", changed)
    elif fault == "after-stamp":
        old = Process.ppid

        def ppid(self):
            if self.pid == 20:
                births[20] += 10
            return old(self)

        monkeypatch.setattr(Process, "ppid", ppid)
    elif fault == "parent-reuse":
        old = process_host._macos_child_pids

        def changed(pid):
            result = old(pid)
            if pid == 10:
                births[10] += 10
            return result

        monkeypatch.setattr(process_host, "_macos_child_pids", changed)
    elif fault == "older-child":
        births[20] = 99.0
    else:
        births[20] = None
    assert process_host._macos_descendants(10) == {}
    assert 20 not in calls


def test_unavailable_native_observation_does_not_fall_back_to_host_census(tree, monkeypatch):
    def unavailable(pid):
        raise OSError("unavailable")

    monkeypatch.setattr(process_host, "_macos_child_pids", unavailable)
    assert process_host._macos_descendants(10) == {}
    assert process_host._macos_descendants(0) == {}
    assert process_host._macos_descendants(-1) == {}


def test_native_buffer_grows_without_accepting_truncation(monkeypatch):
    calls = []

    def reader(pid, buffer, size):
        calls.append(size)
        if len(calls) == 1:
            return size // ctypes.sizeof(ctypes.c_int)
        buffer[0] = 42
        buffer[1] = 0
        buffer[2] = 43
        return 3

    monkeypatch.setattr(process_host, "_macos_child_reader", lambda: reader)
    assert process_host._macos_child_pids(10) == (42, 43)
    assert calls == [256, 1024]


def test_native_buffer_growth_is_bounded_and_errors_are_visible_to_seam(monkeypatch):
    calls = []

    def full(pid, buffer, size):
        calls.append(size)
        return size // ctypes.sizeof(ctypes.c_int)

    monkeypatch.setattr(process_host, "_macos_child_reader", lambda: full)
    with pytest.raises(OSError, match="bounded snapshot capacity"):
        process_host._macos_child_pids(10)
    assert len(calls) == 5

    def denied(*args):
        ctypes.set_errno(errno.EPERM)
        return 0

    monkeypatch.setattr(process_host, "_macos_child_reader", lambda: denied)
    with pytest.raises(OSError):
        process_host._macos_child_pids(10)


@pytest.mark.skipif(sys.platform != "darwin", reason="actual libproc child enumeration")
def test_native_tree_matches_existing_identity_without_host_scan(monkeypatch):
    script = "import subprocess,sys; p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); print(p.pid,flush=True); sys.stdin.readline(); p.terminate(); p.wait()"
    proc = subprocess.Popen(
        [sys.executable, "-c", script], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True
    )
    job = None
    try:
        assert select.select([proc.stdout], [], [], 10)[0]
        job = int(proc.stdout.readline())
        stamp = process_host.get_process_host().identity(job)
        psutil = process_host._psutil()
        monkeypatch.setattr(
            psutil.Process, "children", lambda *a, **k: pytest.fail("global census")
        )
        assert process_host._macos_descendants(proc.pid) == {job: stamp}
        assert stamp is not None and stamp < -2
        proc.communicate("\n", timeout=10)
    finally:
        if proc.poll() is None:
            childrun.kill_tree(
                proc, known={job: process_host.get_process_host().identity(job)} if job else {}
            )
            proc.wait(timeout=10)


@pytest.mark.parametrize("error", [errno.EPERM, errno.ESRCH])
def test_unreadable_branch_retains_siblings_and_continues_other_branches(tree, monkeypatch, error):
    births, parents, edges, calls, Process = tree
    parents[40] = 10
    original = process_host._macos_child_pids

    def broken_branch(pid):
        if pid == 40:
            raise OSError(error, "branch unavailable")
        return original(pid)

    monkeypatch.setattr(process_host, "_macos_child_pids", broken_branch)
    assert process_host._macos_descendants(10) == {20: -101.0, 40: -103.0, 30: -102.0}
    assert 20 in calls and 30 in calls


def test_dynamic_descendant_growth_has_a_total_work_bound(monkeypatch):
    calls = []

    class Process:
        def __init__(self, pid):
            self.pid = pid
            self._ident = (pid, float(pid + 100))

        def ppid(self):
            return self.pid - 1

        def is_running(self):
            return True

    def children(pid):
        calls.append(pid)
        if len(calls) > 4:
            pytest.fail("dynamic walk exceeded the test hang guard")
        return (pid + 1,)

    monkeypatch.setattr(process_host.sys, "platform", "darwin")
    monkeypatch.setattr(process_host, "_psutil", lambda: SimpleNamespace(Process=Process))
    monkeypatch.setattr(process_host, "_macos_child_pids", children)
    monkeypatch.setattr(process_host, "_MACOS_TREE_LIMIT", 3)
    assert process_host._macos_descendants(10) == {11: -111.0, 12: -112.0, 13: -113.0}
    assert calls == [10, 11, 12]


def test_duplicate_candidates_consume_the_same_finite_budget(tree, monkeypatch):
    tree[2][10] = [10, 10, 20]
    monkeypatch.setattr(process_host, "_MACOS_TREE_LIMIT", 2)
    assert process_host._macos_descendants(10) == {}
    assert tree[3] == [10]
