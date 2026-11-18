"""Tests for the cross-platform process-lifecycle seam."""

from __future__ import annotations

import io
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path, PureWindowsPath
from types import SimpleNamespace

import pytest

from bmad_loop import process_host
from bmad_loop.process_host import (
    PosixProcessHost,
    ProcessHostError,
    WindowsProcessHost,
    get_process_host,
    register_process_host,
)


@pytest.fixture(autouse=True)
def _clear_host_cache():
    # get_process_host is lru_cached and env-driven; isolate every case.
    get_process_host.cache_clear()
    yield
    get_process_host.cache_clear()


@pytest.fixture
def host():
    return PosixProcessHost()


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="PosixProcessHost.is_alive uses os.kill(pid, 0); on Windows signal 0 is "
    "CTRL_C_EVENT, so the probe sends a real Ctrl+C to this process's console and "
    "aborts the run. Windows self-liveness is covered by the WindowsProcessHost test below.",
)
def test_is_alive_true_for_self(host):
    assert host.is_alive(os.getpid()) is True


@pytest.mark.skipif(sys.platform != "win32", reason="WindowsProcessHost is the win32 host")
def test_is_alive_true_for_self_windows():
    # The Windows host probes liveness via psutil (no os.kill), so checking self is
    # safe here — unlike the POSIX host's os.kill(pid, 0), which is CTRL_C on Windows.
    pytest.importorskip("psutil")
    assert WindowsProcessHost().is_alive(os.getpid()) is True


def test_is_alive_rejects_non_positive(host, monkeypatch):
    # 0/negative would target a process group via os.kill — the guard must short
    # out before os.kill is ever reached.
    def _boom(*_a, **_k):  # pragma: no cover - must not run
        raise AssertionError("os.kill must not be called for pid <= 0")

    monkeypatch.setattr(process_host.os, "kill", _boom)
    assert host.is_alive(0) is False
    assert host.is_alive(-1) is False


def test_terminate_rejects_non_positive(host, monkeypatch):
    def _boom(*_a, **_k):  # pragma: no cover - must not run
        raise AssertionError("os.kill must not be called for pid <= 0")

    monkeypatch.setattr(process_host.os, "kill", _boom)
    host.terminate(0)  # no raise, no signal
    host.terminate(-42)


def test_force_kill_rejects_non_positive(host, monkeypatch):
    def _boom(*_a, **_k):  # pragma: no cover - must not run
        raise AssertionError("os.kill must not be called for pid <= 0")

    monkeypatch.setattr(process_host.os, "kill", _boom)
    host.force_kill(0)  # no raise, no signal
    host.force_kill(-42)


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="identity via /proc is Linux-only")
def test_identity_stable_and_present_for_self(host):
    first = host.identity(os.getpid())
    second = host.identity(os.getpid())
    assert isinstance(first, float)
    assert first == second  # stable for the life of the pid → a usable reuse guard


def test_identity_none_for_non_positive(host):
    assert host.identity(0) is None
    assert host.identity(-1) is None


def test_alive_and_ours_rejects_non_positive(host):
    assert host.alive_and_ours(0, 1.0) is False
    assert host.alive_and_ours(-1, None) is False


def test_alive_and_ours_none_identity_degrades_to_is_alive(host, monkeypatch):
    # A legacy pid file (no persisted identity) can only fall back to bare existence.
    monkeypatch.setattr(host, "is_alive", lambda pid: pid == 4242)
    assert host.alive_and_ours(4242, None) is True
    assert host.alive_and_ours(9999, None) is False


def test_alive_and_ours_matches_only_same_identity(host, monkeypatch):
    # Same identity → our process; a different value (reused pid) or None (gone) → not.
    monkeypatch.setattr(host, "identity", lambda pid: -123.0)
    assert host.alive_and_ours(4242, -123.0) is True
    assert host.alive_and_ours(4242, -999.0) is False  # reused
    # Unreadable identity is not-ours whether the pid is gone or still running —
    # 'unknown' must never read as ours on the strict/destructive path. Stub
    # is_alive too: alive_and_ours (via liveness_of) probes it on this branch, and
    # the real PosixProcessHost.is_alive would os.kill on a native-Windows CI
    # runner (WinError 87).
    monkeypatch.setattr(host, "identity", lambda pid: None)
    monkeypatch.setattr(host, "is_alive", lambda pid: False)
    assert host.alive_and_ours(4242, -123.0) is False  # gone
    monkeypatch.setattr(host, "is_alive", lambda pid: True)
    assert host.alive_and_ours(4242, -123.0) is False  # live but unreadable → unknown


def test_liveness_of_reads_alive_dead_and_unknown(host, monkeypatch):
    monkeypatch.setattr(host, "identity", lambda pid: -123.0)
    assert host.liveness_of(4242, -123.0) == "alive"  # identity matches → ours
    assert host.liveness_of(4242, -999.0) == "dead"  # readable-but-different → reused

    monkeypatch.setattr(host, "identity", lambda pid: None)
    monkeypatch.setattr(host, "is_alive", lambda pid: True)
    assert host.liveness_of(4242, -123.0) == "unknown"
    monkeypatch.setattr(host, "is_alive", lambda pid: False)
    assert host.liveness_of(4242, -123.0) == "dead"


def test_liveness_of_legacy_identity_degrades_to_is_alive(host, monkeypatch):
    # A legacy pid file (no persisted identity) can only fall back to bare existence.
    monkeypatch.setattr(host, "is_alive", lambda pid: pid == 4242)
    assert host.liveness_of(4242, None) == "alive"
    assert host.liveness_of(9999, None) == "dead"
    assert host.liveness_of(0, None) == "dead"


@pytest.mark.parametrize("clock_offset", [-20.0, 20.0])
def test_macos_birth_identity_survives_clock_and_observer_changes(monkeypatch, clock_offset):
    monkeypatch.setattr(process_host.sys, "platform", "darwin")
    births = {123: 1700000000.25, 456: 1700000001.5}
    observer_offset = 0.0

    class Process:
        def __init__(self, pid):
            self.pid = pid
            self._ident = (pid, births[pid])

        def create_time(self):
            return births[self.pid] + observer_offset

        def ppid(self):
            return 123 if self.pid == 456 else 1

        def is_running(self):
            return self._ident == (self.pid, births[self.pid])

    monkeypatch.setattr(process_host, "_psutil", lambda: SimpleNamespace(Process=Process))
    monkeypatch.setattr(process_host, "_macos_child_pids", lambda pid: (456,) if pid == 123 else ())
    host = PosixProcessHost()
    monkeypatch.setattr(host, "is_alive", lambda pid: pid in births)
    stamp = host.identity(456)
    assert stamp == -1700000001.5
    assert host.descendants(123) == {456: stamp}
    observer_offset = clock_offset
    assert host.identity(456) == stamp
    assert host.liveness_of(456, stamp) == "alive"
    assert host.descendants(123) == {456: stamp}
    births[456] += 1  # real PID-generation change must still be rejected
    assert host.liveness_of(456, stamp) == "dead"


def test_macos_positive_legacy_stamp_is_unknown_while_pid_exists(monkeypatch):
    monkeypatch.setattr(process_host.sys, "platform", "darwin")
    host = PosixProcessHost()
    monkeypatch.setattr(host, "is_alive", lambda pid: pid == 123)
    # Even an apparent equality cannot establish which clock offset was persisted.
    monkeypatch.setattr(host, "identity", lambda pid: 1700000000.0)
    assert host.liveness_of(123, 1700000000.0) == "unknown"
    assert host.legacy_identity_unverifiable(123, 1700000000.0)
    assert not host.alive_and_ours(123, 1700000000.0)
    assert host.liveness_of(456, 1700000000.0) == "dead"
    assert not host.legacy_identity_unverifiable(456, 1700000000.0)
    assert host.liveness_of(123, None) == "alive"
    assert not host.legacy_identity_unverifiable(123, None)
    for invalid in (0.0, -1.0, -2.0, float("nan"), float("inf")):
        assert host.liveness_of(123, invalid) == "dead"


@pytest.mark.parametrize("ident", [None, (123, None), (123, float("nan")), (456, 100.0)])
def test_macos_missing_stable_identity_never_uses_epoch_fallback(monkeypatch, ident):
    monkeypatch.setattr(process_host.sys, "platform", "darwin")
    child = SimpleNamespace(pid=123, _ident=ident, create_time=lambda: 1700000000.0)
    monkeypatch.setattr(process_host, "_psutil", lambda: SimpleNamespace(Process=lambda pid: child))
    host = PosixProcessHost()
    monkeypatch.setattr(host, "is_alive", lambda pid: True)
    assert host.identity(123) is None
    assert host.liveness_of(123, -1700000000.0) == "unknown"


@pytest.mark.skipif(sys.platform != "darwin", reason="real macOS psutil clock adjustment")
def test_macos_native_stamp_matches_another_observer_across_clock_adjustment(monkeypatch):
    psutil = pytest.importorskip("psutil")
    native = pytest.importorskip("psutil._psosx")

    host = PosixProcessHost()
    pid = os.getpid()
    monkeypatch.setattr(native, "boot_time", lambda: native.INIT_BOOT_TIME)
    epoch_before = psutil.Process(pid).create_time()
    before = host.identity(pid)
    assert before is not None and before < -2
    monkeypatch.setattr(native, "boot_time", lambda: native.INIT_BOOT_TIME + 8)
    assert psutil.Process(pid).create_time() == epoch_before + 8
    assert host.identity(pid) == before
    assert host.liveness_of(pid, before) == "alive"
    # Another interpreter has its own import-era boot epoch, while the raw kernel
    # birth identity must agree. Neither observer changes the host clock.
    child = subprocess.run(
        [
            sys.executable,
            "-c",
            "from bmad_loop.process_host import PosixProcessHost; "
            f"print(PosixProcessHost().identity({pid}))",
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert float(child.stdout) == before


@pytest.mark.skipif(
    not sys.platform.startswith("linux"), reason="descendants /proc walk is exercised on Linux"
)
def test_descendants_enumerates_transitively(host, tmp_path):
    """A child-of-child (grandchild) is found: the /proc walk is transitive, not
    just direct children. The outer sh backgrounds an inner sh that itself
    backgrounds a sleep and records that grandchild's pid; descendants(outer) must
    contain it. The whole tree runs in its own session so the finally can nuke it
    with a single killpg even if the assertion fails."""
    gc_file = tmp_path / "gc.pid"
    # outer sh -> inner sh (stays a shell: compound body, not exec'd) -> grandchild sleep
    script = f"sh -c 'sleep 300 & echo $! > {gc_file}; wait' & sleep 300"
    proc = subprocess.Popen(["sh", "-c", script], start_new_session=True)
    try:
        deadline = time.monotonic() + 10
        while not gc_file.is_file() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert gc_file.is_file(), "grandchild pid was never recorded"
        gc_pid = int(gc_file.read_text(encoding="utf-8").strip())
        kids = host.descendants(proc.pid)
        assert gc_pid in kids, f"grandchild {gc_pid} not in transitive descendants {kids}"
        # The identity rides along from the SAME /proc read the ancestry came from
        # and must equal the canonical per-pid stamp (the /proc starttime).
        assert kids[gc_pid] == process_host._proc_starttime(gc_pid)
    finally:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, OSError):
            pass
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass


@pytest.mark.skipif(
    not sys.platform.startswith("linux"), reason="descendants /proc walk is exercised on Linux"
)
def test_descendants_dead_or_unknown_pid_is_empty(host):
    """The seam contract: a reaped pid, a never-allocated high pid, and a
    non-positive pid all enumerate to {} rather than raising."""
    proc = subprocess.Popen(["true"])
    proc.wait(timeout=10)  # reap it — its pid is now gone
    assert host.descendants(proc.pid) == {}
    assert host.descendants(2**31 - 1) == {}  # never allocated
    assert host.descendants(0) == {}
    assert host.descendants(-1) == {}


def test_psutil_descendants_maps_children_and_never_raises(monkeypatch):
    """The non-Linux/Windows descendant path maps ``Process.children(recursive=True)``
    to a pid → create_time identity snapshot stamped from the SAME enumerated
    ``Process`` objects, and any failure (a raising Process, or a missing-psutil
    ProcessHostError from ``_psutil()`` itself) degrades to {} — the never-raise
    seam contract that the Linux ``/proc`` walk cannot exercise on Linux CI."""

    class _FakeChild:
        def __init__(self, pid, created):
            self.pid = pid
            self._created = created
            self._ident = (pid, created)

        def create_time(self):
            return self._created

        def is_running(self):
            return True  # same generation we enumerated — identity confirmed

    class _FakeProc:
        def __init__(self, pid):
            self.pid = pid

        def children(self, recursive=False):
            assert recursive is True  # transitive tree, not just direct children
            return [_FakeChild(11, 111.0), _FakeChild(22, 222.0)]

    class _FakePsutil:
        Process = _FakeProc

    monkeypatch.setattr(process_host, "_psutil", lambda: _FakePsutil)
    sign = -1 if sys.platform == "darwin" else 1
    assert process_host._psutil_descendants(123) == {11: sign * 111.0, 22: sign * 222.0}

    class _BoomProc:
        def __init__(self, pid):
            raise RuntimeError("no such process")  # psutil.NoSuchProcess analogue

    class _BoomPsutil:
        Process = _BoomProc

    monkeypatch.setattr(process_host, "_psutil", lambda: _BoomPsutil)
    assert process_host._psutil_descendants(123) == {}

    def _missing():
        raise process_host.ProcessHostError("psutil missing")

    monkeypatch.setattr(process_host, "_psutil", _missing)
    assert process_host._psutil_descendants(123) == {}


def test_psutil_descendants_omits_pid_reused_before_identity_capture(monkeypatch):
    """PID-generation rollover between enumeration and the identity stamp, in BOTH
    shapes psutil actually produces — the snapshot must OMIT any member it cannot
    authenticate, keep confirmable siblings, and never raise.

    ``_GoneChild`` is the easy shape: the pid vanished and was not reused, so
    ``create_time()`` raises a ``NoSuchProcess`` analogue.

    ``_ReusedChild`` is the shape that actually bites on macOS and the reason the
    ``is_running()`` revalidation exists (#184 review). Per
    ``psutil.Process._get_ident``, the ``LINUX or NETBSD or OSX`` branch binds a
    *monotonic* ident and leaves ``self._create_time`` unset, so ``create_time()``
    is a raw call-time read that happily returns the RECYCLED process's stamp — no
    exception at all. Stamping that would let teardown authenticate and signal an
    unrelated process. Only ``is_running()`` (construction-bound ident vs. a fresh
    ``Process``) reports the generation change. An exception-only fake would pass
    against the buggy code, so this case is what makes the test a regression test."""

    class _GoneChild:
        pid = 11

        def create_time(self):
            raise RuntimeError("process no longer exists")  # NoSuchProcess analogue

        def is_running(self):  # pragma: no cover - create_time raises first
            raise AssertionError("must not be reached: identity read comes first")

    class _ReusedChild:
        """Enumerated as generation A; the pid was recycled before the stamp, so
        ``create_time()`` returns generation B's identity WITHOUT raising."""

        pid = 33
        _ident = (33, 333.0)

        def create_time(self):
            return 999.0  # generation B's stamp — plausible, and utterly wrong

        def is_running(self):
            return False  # construction-bound ident no longer matches this pid

    class _OkChild:
        pid = 22
        _ident = (22, 222.0)

        def create_time(self):
            return 222.0

        def is_running(self):
            return True

    class _FakeProc:
        def __init__(self, pid):
            self.pid = pid

        def children(self, recursive=False):
            return [_GoneChild(), _ReusedChild(), _OkChild()]

    class _FakePsutil:
        Process = _FakeProc

    monkeypatch.setattr(process_host, "_psutil", lambda: _FakePsutil)
    snapshot = process_host._psutil_descendants(123)
    assert snapshot == {22: -222.0 if sys.platform == "darwin" else 222.0}
    # The recycled pid must be absent outright, not stamped with the newcomer's
    # identity — a stamped entry authenticates at teardown and gets signalled.
    assert 33 not in snapshot


def test_default_host_matches_platform(monkeypatch):
    monkeypatch.delenv("BMAD_LOOP_PROCESS_HOST", raising=False)
    get_process_host.cache_clear()
    expected = WindowsProcessHost if sys.platform == "win32" else PosixProcessHost
    assert isinstance(get_process_host(), expected)


def test_env_override_selects_by_name(monkeypatch):
    # The registry selects by name without monkeypatching sys.platform — the hook
    # PR #19's WindowsProcessHost registration relies on.
    monkeypatch.setenv("BMAD_LOOP_PROCESS_HOST", "posix")
    get_process_host.cache_clear()
    assert isinstance(get_process_host(), PosixProcessHost)

    monkeypatch.setenv("BMAD_LOOP_PROCESS_HOST", "windows")
    get_process_host.cache_clear()
    assert isinstance(get_process_host(), WindowsProcessHost)


def test_unknown_forced_name_raises(monkeypatch):
    # An explicit but unregistered override is a misconfiguration: fail loudly rather
    # than silently fall back to POSIX (on win32 os.kill(pid, 0) is destructive).
    monkeypatch.setenv("BMAD_LOOP_PROCESS_HOST", "bogus")
    get_process_host.cache_clear()
    with pytest.raises(ProcessHostError, match="bogus"):
        get_process_host()


def test_register_invalidates_cached_selection(monkeypatch):
    # register_process_host() must clear the singleton cache so a host registered
    # after a prior get_process_host() call is honored without a manual cache_clear.
    monkeypatch.delenv("BMAD_LOOP_PROCESS_HOST", raising=False)
    saved_hosts = list(process_host._HOSTS)
    saved_loaded = process_host._BUILTINS_LOADED
    try:
        get_process_host()  # populate the cache (and load builtins)
        assert get_process_host.cache_info().currsize == 1
        register_process_host("fake", lambda p: False, lambda: PosixProcessHost())
        # no manual cache_clear() — registration is responsible for invalidating it
        assert get_process_host.cache_info().currsize == 0
    finally:
        process_host._HOSTS[:] = saved_hosts
        process_host._BUILTINS_LOADED = saved_loaded
        get_process_host.cache_clear()


def test_shell_quote_posix_uses_shlex():
    # POSIX quoting wraps a path with spaces in single quotes (shlex.quote).
    assert PosixProcessHost().shell_quote("/a b/c.py") == "'/a b/c.py'"


@pytest.mark.parametrize(
    "path,quoted",
    [
        (r"C:\Users\me\.local\bin\bmad-loop.exe", "C:/Users/me/.local/bin/bmad-loop.exe"),
        (r"C:\Program Files\x y\bmad-loop.exe", '"C:/Program Files/x y/bmad-loop.exe"'),
    ],
    ids=["no-spaces", "spaces"],
)
def test_shell_quote_windows_forward_slashes_then_list2cmdline(path, quoted):
    # Hook runners run these under Git Bash, which eats unquoted backslashes
    # (#773): separators become forward slashes, and a path with spaces is still
    # double-quoted (subprocess.list2cmdline), never POSIX single-quoted.
    assert WindowsProcessHost().shell_quote(path) == quoted


_UNSAFE_MATRIX = [
    ("C:/a&b/bmad-loop.exe", ("&",)),
    ("C:/x%y^z/bmad-loop.exe", ("%", "^")),
    ("C:/x%y%z/bmad-loop.exe", ("%",)),
    ("C:/Program Files (x86)/a&b/bmad-loop.exe", ()),
    ("C:/my $dir/bmad-loop.exe", ("$",)),
    ("C:/50% off/bmad-loop.exe", ("%",)),
    ("C:/Users/me/.venv/Scripts/bmad-loop.exe", ()),
    (r"C:\a&b\bmad-loop.exe", ("&",)),
    ("C:/a{b,c}/bmad-loop.exe", (",", "{", "}")),
]
_UNSAFE_IDS = [
    "bare-amp",
    "bare-sorted-several",
    "bare-deduped",
    "quoted-harmless",
    "quoted-dollar",
    "quoted-percent",
    "clean",
    "backslashes",
    "brace-expansion",
]


@pytest.mark.parametrize("path,chars", _UNSAFE_MATRIX, ids=_UNSAFE_IDS)
def test_unsafe_shell_chars_windows(path, chars):
    """DW-346: the Windows host scans its OWN quoted output — list2cmdline quotes
    only on whitespace, so an unspaced path leaves every metacharacter bare, while
    a quoted one leaves only the in-quote expanders live. Ablation: an override
    returning `()` fails every non-empty row."""
    assert WindowsProcessHost().unsafe_shell_chars(path) == chars


@pytest.mark.parametrize("path", [p for p, _ in _UNSAFE_MATRIX] + ["/opt/a&b;c$`'/bmad-loop"])
def test_unsafe_shell_chars_posix_never_flags(path):
    # shlex.quote single-quotes every sh metacharacter; nothing expands inside.
    assert PosixProcessHost().unsafe_shell_chars(path) == ()


def test_hook_interpreter_is_absolute_on_posix(host):
    import sys

    assert PosixProcessHost().hook_interpreter() == PosixProcessHost().shell_quote(
        str(Path(sys.executable).absolute())
    )


def test_hook_interpreter_windows_uses_absolute_path():
    import sys

    assert WindowsProcessHost().hook_interpreter() == WindowsProcessHost().shell_quote(
        str(Path(sys.executable).absolute())
    )


def test_hook_interpreter_windows_uses_forward_slashes(monkeypatch):
    class _Absolute:
        def __init__(self, raw):
            self._raw = raw

        def absolute(self):
            return PureWindowsPath(self._raw)

    monkeypatch.setattr(process_host, "Path", _Absolute)
    monkeypatch.setattr(process_host.sys, "executable", r"C:\Program Files\Py 3\python.exe")
    assert WindowsProcessHost().hook_interpreter() == '"C:/Program Files/Py 3/python.exe"'
    monkeypatch.setattr(process_host.sys, "executable", r"C:\Py\python.exe")
    assert WindowsProcessHost().hook_interpreter() == "C:/Py/python.exe"


def _git_bash() -> str | None:
    """Git for Windows' bash — never System32's WSL launcher, which PATH may
    list first."""
    git = shutil.which("git")
    if git:
        bash = Path(git).resolve().parent.parent / "bin" / "bash.exe"
        if bash.is_file():
            return str(bash)
    bash = shutil.which("bash")
    return bash if bash and "system32" not in bash.lower() else None


def _windows_relay_shaped_command() -> str:
    # A real .exe at a backslash path standing where the relay executable goes.
    if " " in sys.executable:
        pytest.skip("paths with spaces are unsupported on the PowerShell fallback")
    quoted = WindowsProcessHost().shell_quote(sys.executable)
    return f'{quoted} -c "import sys; print(sys.argv[1:])" relay Stop'


@pytest.mark.skipif(sys.platform != "win32", reason="executes a Windows hook command")
def test_windows_hook_command_runs_under_git_bash():
    bash = _git_bash()
    if bash is None:
        pytest.skip("Git Bash is not installed")
    command = _windows_relay_shaped_command()
    ran = subprocess.run([bash, "-c", command], capture_output=True, text=True, timeout=60)
    assert ran.returncode == 0, ran.stderr
    assert "['relay', 'Stop']" in ran.stdout


@pytest.mark.skipif(sys.platform != "win32", reason="executes a Windows hook command")
def test_windows_hook_command_runs_under_powershell():
    shell = shutil.which("powershell") or shutil.which("pwsh")
    if shell is None:
        pytest.skip("PowerShell is not installed")
    command = _windows_relay_shaped_command()
    ran = subprocess.run(
        [shell, "-NoProfile", "-NonInteractive", "-Command", command],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert ran.returncode == 0, ran.stderr
    assert "['relay', 'Stop']" in ran.stdout


def test_hook_interpreter_routed_through_selected_host(monkeypatch):
    # The env override drives the prefix end-to-end, so a Windows host changes the
    # registered hook command with no `sys.platform` branch at the call site.
    monkeypatch.setenv("BMAD_LOOP_PROCESS_HOST", "windows")
    get_process_host.cache_clear()
    import sys

    assert get_process_host().hook_interpreter() == WindowsProcessHost().shell_quote(
        str(Path(sys.executable).absolute())
    )


@pytest.fixture(params=["darwin", "win32"])
def strict_process_tree(monkeypatch, request):
    monkeypatch.setattr(process_host.sys, "platform", request.param)
    actors = {
        100: {"birth": 10.0, "parent": 1},
        200: {"birth": 20.0, "parent": 100},
        300: {"birth": 30.0, "parent": 200},
    }
    calls = []
    control = {"enumerated": [300, 200], "native": {100: (200,), 200: (300,), 300: ()}}

    class Gone(Exception):
        pass

    class Process:
        def __init__(self, pid):
            if pid not in actors:
                raise Gone(pid)
            self.pid = pid
            self._ident = (pid, actors[pid]["birth"])

        def create_time(self):
            if self.pid not in actors:
                raise Gone(self.pid)
            row = actors[self.pid]
            if row.get("unreadable"):
                raise PermissionError("synthetic identity failure")
            return row["birth"]

        def ppid(self):
            if self.pid not in actors:
                raise Gone(self.pid)
            row = actors[self.pid]
            if row.get("unreadable"):
                raise PermissionError("synthetic parent failure")
            return row["parent"]

        def is_running(self):
            row = actors.get(self.pid)
            return (
                row is not None
                and not row.get("reused")
                and self._ident == (self.pid, row["birth"])
            )

        def children(self, recursive=False):
            assert request.param == "win32", "macOS must not use a global psutil census"
            assert recursive is True
            calls.append(("psutil-tree", self.pid))
            if control.get("census_error"):
                raise control["census_error"]
            selected = [Process(pid) for pid in control["enumerated"]]
            callback = control.get("after_enumeration")
            if callback:
                callback()
            return selected

    def native(pid, **kwargs):
        calls.append(("native-children", pid))
        callback = control.get("after_enumeration")
        if callback and pid == 100:
            callback()
        value = control["native"].get(pid, ())
        if isinstance(value, Exception):
            raise value
        return value

    package = SimpleNamespace(
        Process=Process, NoSuchProcess=Gone, pid_exists=lambda pid: pid in actors
    )
    monkeypatch.setattr(process_host, "_psutil", lambda: package)
    monkeypatch.setattr(process_host, "_macos_child_pids", native)
    monkeypatch.setattr(
        process_host.os, "kill", lambda *a: pytest.fail("observation signalled a process")
    )
    host = PosixProcessHost() if request.param == "darwin" else WindowsProcessHost()
    return host, actors, control, calls, (-1 if request.param == "darwin" else 1)


def test_strict_descendants_return_authenticated_tree_and_preserve_legacy_map(strict_process_tree):
    host, actors, control, calls, sign = strict_process_tree
    observed = host.observe_descendants(100)
    assert observed.complete and observed.problem == ""
    assert observed.members == {200: sign * 20.0, 300: sign * 30.0}
    assert host.descendants(100) == observed.members
    if sign == 1:
        assert calls == [("psutil-tree", 100), ("psutil-tree", 100)]
    else:
        assert all(kind == "native-children" for kind, _pid in calls)


def test_strict_healthy_empty_differs_from_missing_backend(strict_process_tree, monkeypatch):
    host, actors, control, calls, sign = strict_process_tree
    control["enumerated"] = []
    control["native"][100] = ()
    assert host.observe_descendants(100) == process_host.DescendantObservation({}, True)

    def unavailable():
        raise ProcessHostError("synthetic backend unavailable")

    monkeypatch.setattr(process_host, "_psutil", unavailable)
    observation = host.observe_descendants(100)
    assert observation.members == {} and not observation.complete and observation.problem
    assert host.descendants(100) == {}  # Existing callers keep their old degrade.


@pytest.mark.parametrize(
    "fault", ["missing_identity", "unreadable", "reused", "foreign_parent", "older_child"]
)
def test_strict_bad_member_retains_stamped_sibling_but_never_healthy_coverage(
    strict_process_tree, fault
):
    host, actors, control, calls, sign = strict_process_tree
    actors[400] = {"birth": 40.0, "parent": 100}
    control["enumerated"] = [200, 300, 400]
    control["native"][100] = (200, 400)
    if fault == "missing_identity":
        actors[200]["birth"] = None
    elif fault == "foreign_parent":
        actors[200]["parent"] = 999
    elif fault == "older_child":
        actors[200]["birth"] = 5.0
    else:
        actors[200][fault] = True
    result = host.observe_descendants(100)
    assert result.members == {400: sign * 40.0}
    assert not result.complete and result.problem


def test_strict_root_generation_change_is_incomplete(strict_process_tree):
    host, actors, control, calls, sign = strict_process_tree
    control["enumerated"] = []
    control["native"][100] = ()
    control["after_enumeration"] = lambda: actors[100].update(birth=50.0)
    result = host.observe_descendants(100)
    assert result.members == {} and not result.complete and result.problem


def test_strict_proven_root_disappearance_is_observed_empty_not_historical_proof(
    strict_process_tree,
):
    host, actors, control, calls, sign = strict_process_tree
    control["enumerated"] = []
    control["native"][100] = ()
    control["after_enumeration"] = lambda: actors.pop(100, None)
    assert host.observe_descendants(100) == process_host.DescendantObservation({}, True)
    assert host.observe_descendants(100) == process_host.DescendantObservation({}, True)


def test_strict_census_bound_keeps_partial_members_and_marks_incomplete(
    strict_process_tree, monkeypatch
):
    host, actors, control, calls, sign = strict_process_tree
    actors[400] = {"birth": 40.0, "parent": 100}
    control["enumerated"] = [200, 400, 300]
    control["native"][100] = (200, 400)
    monkeypatch.setattr(process_host, "_DESCENDANT_OBSERVATION_LIMIT", 2 if sign == -1 else 1)
    result = host.observe_descendants(100)
    assert result.members == {200: sign * 20.0}
    assert not result.complete and result.problem


@pytest.mark.parametrize("platform", ["darwin", "linux", "win32"])
@pytest.mark.parametrize("pid", [0, -1, True, None])
def test_strict_invalid_root_does_not_observe_or_signal(monkeypatch, platform, pid):
    monkeypatch.setattr(process_host.sys, "platform", platform)
    for seam in ("_psutil", "_linux_descendant_observation", "_macos_descendant_observation"):
        monkeypatch.setattr(
            process_host, seam, lambda *a: pytest.fail("invalid pid reached backend")
        )
    host = WindowsProcessHost() if platform == "win32" else PosixProcessHost()
    result = host.observe_descendants(pid)
    assert not result.complete and result.members == {}


def test_strict_unknown_backend_has_no_healthy_fallback(monkeypatch):
    monkeypatch.setattr(process_host.sys, "platform", "unsupported-platform")
    monkeypatch.setattr(
        process_host, "_psutil", lambda: pytest.fail("unsupported backend queried psutil")
    )
    result = PosixProcessHost().observe_descendants(100)
    assert not result.complete and "unsupported" in result.problem


def test_strict_linux_uses_scoped_thread_children_and_atomic_stat_fields(monkeypatch):
    monkeypatch.setattr(process_host.sys, "platform", "linux")
    actors = {10: (1, 100), 20: (10, 200), 30: (10, 300), 40: (20, 400)}
    threads = {10: (10, 11), 20: (20,), 30: (30,), 40: (40,)}
    children = {(10, 10): "20", (10, 11): "30", (20, 20): "40", (30, 30): "", (40, 40): ""}
    directories = []

    def read_stat(path, **kwargs):
        assert path.name == "stat"
        pid = int(path.parent.name)
        parent, birth = actors[pid]
        fields = ["S", str(parent)] + ["0"] * 17 + [str(birth)]
        return f"{pid} (name with ) delimiter) " + " ".join(fields)

    def list_threads(path):
        assert path.name == "task", "strict Linux observation must not scan /proc globally"
        directories.append(str(path))
        return iter(path / str(tid) for tid in threads[int(path.parent.name)])

    def open_children(path, **kwargs):
        assert path.name == "children"
        return io.StringIO(children[(int(path.parents[2].name), int(path.parent.name))])

    monkeypatch.setattr(Path, "read_text", read_stat)
    monkeypatch.setattr(Path, "iterdir", list_threads)
    monkeypatch.setattr(Path, "open", open_children)
    monkeypatch.setattr(process_host, "_psutil", lambda: pytest.fail("Linux imported psutil"))
    result = PosixProcessHost().observe_descendants(10)
    assert result == process_host.DescendantObservation({20: 200.0, 30: 300.0, 40: 400.0}, True)
    assert set(directories) == {f"/proc/{pid}/task" for pid in actors}


@pytest.mark.parametrize("fault", ["root_identity", "root_read", "child_read", "thread_census"])
def test_strict_linux_unknown_observation_is_not_empty_success(monkeypatch, fault):
    monkeypatch.setattr(process_host.sys, "platform", "linux")

    def record(pid):
        if fault == "root_identity" and pid == 10:
            return None, 1
        if (fault == "root_read" and pid == 10) or (fault == "child_read" and pid == 20):
            raise PermissionError("synthetic /proc refusal")
        return (100.0, 1) if pid == 10 else (200.0, 10)

    def children(pid, budget):
        if fault == "thread_census":
            raise PermissionError("synthetic child-list refusal")
        return (20,) if pid == 10 else ()

    monkeypatch.setattr(process_host, "_linux_observed_record", record)
    monkeypatch.setattr(process_host, "_linux_observed_children", children)
    result = PosixProcessHost().observe_descendants(10)
    assert not result.complete and result.problem
    assert result.members == {}


def test_strict_linux_zombie_root_and_explicit_disappearance_are_observable(monkeypatch):
    monkeypatch.setattr(process_host.sys, "platform", "linux")
    fields = ["Z", "1"] + ["0"] * 17 + ["100"]
    monkeypatch.setattr(Path, "read_text", lambda *a, **k: "10 (zombie) " + " ".join(fields))
    monkeypatch.setattr(process_host, "_linux_observed_children", lambda pid, budget: ())
    assert PosixProcessHost().observe_descendants(10) == process_host.DescendantObservation(
        {}, True
    )

    def absent(pid):
        raise ProcessLookupError(pid)

    monkeypatch.setattr(process_host, "_linux_observed_record", absent)
    assert PosixProcessHost().observe_descendants(10) == process_host.DescendantObservation(
        {}, True
    )


@pytest.mark.parametrize("available", [False, True])
def test_strict_linux_absence_requires_an_available_procfs_backend(monkeypatch, available):
    monkeypatch.setattr(process_host.sys, "platform", "linux")

    def missing(*a, **kw):
        raise FileNotFoundError("synthetic missing process")

    monkeypatch.setattr(Path, "read_text", missing)
    monkeypatch.setattr(
        process_host, "_proc_identity_state", lambda pid: (123.0, "S") if available else None
    )
    observed = PosixProcessHost().observe_descendants(10)
    assert observed.members == {}
    assert observed.complete is available
    assert bool(observed.problem) is not available


def test_strict_macos_native_invalid_pid_is_incomplete_without_changing_legacy_behavior(
    monkeypatch,
):
    monkeypatch.setattr(process_host.sys, "platform", "darwin")

    def reader(pid, children, capacity):
        children[0] = 0
        return 1

    monkeypatch.setattr(process_host, "_macos_child_reader", lambda: reader)
    assert process_host._macos_child_pids(100) == ()
    with pytest.raises(OSError, match="invalid pid"):
        process_host._macos_child_pids(100, strict=True)
    parent = SimpleNamespace(pid=100, _ident=(100, 10.0), ppid=lambda: 1, is_running=lambda: True)
    monkeypatch.setattr(
        process_host, "_psutil", lambda: SimpleNamespace(Process=lambda pid: parent)
    )
    observed = PosixProcessHost().observe_descendants(100)
    assert not observed.complete and observed.problem
    assert PosixProcessHost().descendants(100) == {}


def test_strict_windows_partial_iterator_fault_keeps_observed_member(monkeypatch):
    monkeypatch.setattr(process_host.sys, "platform", "win32")
    child = SimpleNamespace(
        pid=200, create_time=lambda: 20.0, ppid=lambda: 100, is_running=lambda: True
    )

    def children(recursive=False):
        assert recursive
        yield child
        raise PermissionError("synthetic interrupted census")

    parent = SimpleNamespace(
        pid=100,
        create_time=lambda: 10.0,
        ppid=lambda: 1,
        is_running=lambda: True,
        children=children,
    )
    monkeypatch.setattr(
        process_host, "_psutil", lambda: SimpleNamespace(Process=lambda pid: parent)
    )
    result = WindowsProcessHost().observe_descendants(100)
    assert result.members == {200: 20.0}
    assert not result.complete and "PermissionError" in result.problem


def test_strict_branch_failure_reports_incomplete_census(strict_process_tree):
    host, actors, control, calls, sign = strict_process_tree
    if sign == -1:
        control["native"][200] = PermissionError("synthetic branch failure")
    else:
        control["census_error"] = PermissionError("synthetic tree failure")
    result = host.observe_descendants(100)
    assert result.members == ({200: -20.0} if sign == -1 else {})
    assert not result.complete and "PermissionError" in result.problem


def test_strict_repeated_pid_generation_change_retains_old_stamp_and_marks_incomplete(monkeypatch):
    monkeypatch.setattr(process_host.sys, "platform", "linux")
    calls = []

    def record(pid):
        if pid == 10:
            return 10.0, 1
        calls.append(pid)
        return (20.0 if len(calls) == 1 else 25.0), 10

    monkeypatch.setattr(process_host, "_linux_observed_record", record)
    monkeypatch.setattr(
        process_host, "_linux_observed_children", lambda pid, budget: (20, 20) if pid == 10 else ()
    )
    observed = PosixProcessHost().observe_descendants(10)
    assert observed.members == {20: 20.0}
    assert not observed.complete and "identity changed" in observed.problem


@pytest.mark.parametrize("force", [False, True])
def test_signal_owned_posix_rechecks_birth_and_signals_only_exact_pid(monkeypatch, force):
    host = PosixProcessHost()
    actions = []
    monkeypatch.setattr(host, "liveness_of", lambda pid, identity: "alive")
    monkeypatch.setattr(host, "identity", lambda pid: 123.0)
    monkeypatch.setattr(host, "terminate", lambda pid: actions.append(("terminate", pid)))
    monkeypatch.setattr(host, "force_kill", lambda pid: actions.append(("kill", pid)))
    assert host.signal_owned(71, 123.0, force=force)
    assert actions == [("kill" if force else "terminate", 71)]
    actions.clear()
    assert not host.signal_owned(71, 122.0, force=force)
    assert actions == []
    monkeypatch.setattr(host, "liveness_of", lambda pid, identity: "unknown")
    with pytest.raises(ProcessHostError, match="unreadable"):
        host.signal_owned(71, 123.0, force=force)
    assert actions == []


@pytest.mark.parametrize("host_class", [PosixProcessHost, WindowsProcessHost])
@pytest.mark.parametrize(
    "pid,identity",
    [
        (0, 1.0),
        (-1, 1.0),
        (True, 1.0),
        (71, None),
        (71, True),
        (71, float("nan")),
        (71, float("inf")),
    ],
)
def test_signal_owned_invalid_actor_never_reaches_observation(
    monkeypatch, host_class, pid, identity
):
    host = host_class()
    monkeypatch.setattr(host, "liveness_of", lambda *a: pytest.fail("invalid actor observed"))
    monkeypatch.setattr(process_host, "_psutil", lambda: pytest.fail("invalid actor observed"))
    assert not host.signal_owned(pid, identity, force=True)


@pytest.mark.parametrize("force", [False, True])
@pytest.mark.parametrize("state", ["owned", "wrong_birth", "reused", "unreadable"])
def test_signal_owned_windows_uses_same_psutil_actor_without_recursive_helper(
    monkeypatch, force, state
):
    events = []

    class Process:
        def __init__(self, pid):
            assert pid == 71
            self.pid = pid
            events.append("constructed")

        def create_time(self):
            events.append("birth")
            if state == "unreadable":
                raise PermissionError("synthetic birth refusal")
            return 124.0 if state == "wrong_birth" else 123.0

        def is_running(self):
            events.append("generation")
            return state != "reused"

        def terminate(self):
            events.append(("terminate", self.pid))

        def kill(self):
            events.append(("kill", self.pid))

        def children(self, **kwargs):
            pytest.fail("single-actor signal queried or signalled descendants")

    monkeypatch.setattr(process_host, "_psutil", lambda: SimpleNamespace(Process=Process))
    monkeypatch.setattr(
        process_host.subprocess, "run", lambda *a, **kw: pytest.fail("taskkill helper started")
    )
    monkeypatch.setattr(
        process_host.os, "kill", lambda *a: pytest.fail("POSIX signal used on Windows seam")
    )
    host = WindowsProcessHost()
    if state == "unreadable":
        with pytest.raises(ProcessHostError) as caught:
            host.signal_owned(71, 123.0, force=force)
        assert isinstance(caught.value.__cause__, PermissionError)
    else:
        assert host.signal_owned(71, 123.0, force=force) is (state == "owned")
    if state == "owned":
        assert events == [
            "constructed",
            "birth",
            "generation",
            ("kill" if force else "terminate", 71),
        ]
    else:
        assert not any(isinstance(event, tuple) for event in events)


def test_signal_owned_unsupported_backend_does_not_use_legacy_tree_kill(monkeypatch):
    host = WindowsProcessHost()
    monkeypatch.setattr(host, "force_kill", lambda pid: pytest.fail("unsafe legacy fallback"))
    with pytest.raises(ProcessHostError, match="unsupported"):
        process_host.ProcessHost.signal_owned(host, 71, 123.0, force=True)
