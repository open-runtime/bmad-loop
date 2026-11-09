"""Managed index initialization races use explicit barriers, never timing luck."""

import errno
import multiprocessing
import sqlite3
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from bmad_loop import campaign_managed_communication as managed
from bmad_loop.campaign_model import CampaignError
from bmad_loop.platform_util import LockUnavailableError, file_lock


def _paused_creator(directory, connected, release):
    """Expose the empty file in another process while retaining its schema lock."""
    manager = managed.ManagedCommunication(directory)
    connect = sqlite3.connect

    def paused_connect(*args, **kwargs):
        db = connect(*args, **kwargs)
        connected.set()
        if not release.wait(30):
            db.close()
            raise AssertionError("test did not release schema creation")
        return db

    managed.sqlite3.connect = paused_connect
    with manager._db(write=True) as db:
        db.execute("INSERT INTO rooms VALUES (?,?)", ("creator", "{}"))


@pytest.mark.parametrize("operation", ["endpoints", "has_open", "writer"])
def test_initialization_waits_for_cross_process_schema(tmp_path, monkeypatch, operation):
    manager = managed.ManagedCommunication(tmp_path)
    context = multiprocessing.get_context("spawn")
    connected, release = context.Event(), context.Event()
    creator = context.Process(target=_paused_creator, args=(tmp_path, connected, release))
    attempted = threading.Event()

    @contextmanager
    def observed_lock(*args, **kwargs):
        try:
            with file_lock(*args, **kwargs):
                yield
        except LockUnavailableError:
            attempted.set()
            raise

    monkeypatch.setattr(managed, "file_lock", observed_lock)

    def contender():
        if operation == "writer":
            with manager._db(write=True) as db:
                db.execute("INSERT INTO rooms VALUES (?,?)", ("contender", "{}"))
            return "written"
        return getattr(manager, operation)()

    creator.start()
    try:
        assert connected.wait(30), "creator never exposed its SQLite file"
        # Establish that this is the original failure window, not an initialized DB.
        with sqlite3.connect(manager.path) as db:
            with pytest.raises(sqlite3.OperationalError, match="no such table"):
                db.execute("SELECT * FROM endpoints")
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(contender)
            pending.add_done_callback(lambda _: attempted.set())
            try:
                assert attempted.wait(30), "contender never attempted initialization"
                assert not pending.done(), "contender bypassed the schema barrier"
            finally:
                release.set()
            result = pending.result(timeout=30)
        assert result == {"endpoints": [], "has_open": False, "writer": "written"}[operation]
    finally:
        release.set()
        creator.join(30)
        if creator.is_alive():
            creator.terminate()
            creator.join(30)
    assert creator.exitcode == 0
    with manager._db() as db:
        rooms = {row[0] for row in db.execute("SELECT room FROM rooms")}
    assert rooms == ({"creator", "contender"} if operation == "writer" else {"creator"})


def test_initialization_lock_is_released_before_wal_transactions(tmp_path):
    manager = managed.ManagedCommunication(tmp_path)
    with manager._db(write=True) as writer:
        writer.execute("INSERT INTO rooms VALUES (?,?)", ("uncommitted", "{}"))
        # Reacquiring the exact sidecar proves it does not span the transaction.
        with file_lock(manager.directory / ".schema.lock", blocking=False):
            pass
        with manager._db() as reader:
            assert reader.execute("SELECT * FROM rooms").fetchall() == []
            assert reader.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        with manager._db(write=True) as second_writer:
            assert second_writer.execute("SELECT * FROM rooms").fetchall() == []
    with manager._db() as reader:
        assert reader.execute("SELECT room FROM rooms").fetchone()[0] == "uncommitted"


def test_read_of_absent_index_never_creates_state(tmp_path):
    manager = managed.ManagedCommunication(tmp_path)
    assert manager.endpoints() == []
    assert not manager.has_open()
    with pytest.raises(sqlite3.OperationalError, match="unable to open database"):
        with manager._db():
            pytest.fail("missing read-only database was opened")
    assert not manager.directory.exists()


@pytest.mark.parametrize("contents", [b"not a SQLite database", b""])
def test_corrupt_or_incomplete_schema_is_not_an_empty_index(tmp_path, contents):
    manager = managed.ManagedCommunication(tmp_path)
    manager.directory.mkdir()
    manager.path.write_bytes(contents)
    with pytest.raises(sqlite3.DatabaseError):
        manager.endpoints()


@pytest.mark.parametrize("redirected", ["directory", "database", "lock", "lock-without-database"])
@pytest.mark.parametrize("write", [False, True])
@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlinks")
def test_redirected_index_paths_are_refused(tmp_path, redirected, write):
    manager = managed.ManagedCommunication(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    manager.directory.mkdir()
    if redirected == "directory":
        manager.directory.rmdir()
        manager.directory.symlink_to(elsewhere, target_is_directory=True)
    elif redirected == "database":
        manager.path.symlink_to(elsewhere / "index")
    else:
        if redirected == "lock":
            manager.path.touch()
        (manager.directory / ".schema.lock").symlink_to(elsewhere / "lock")
    with pytest.raises(CampaignError, match="redirected"):
        with manager._db(write=write):
            pytest.fail("redirected index was opened")
    assert list(elsewhere.iterdir()) == []


@pytest.mark.parametrize("stage", ["pragma", "schema"])
def test_initialization_failure_closes_connection_and_releases_lock(tmp_path, monkeypatch, stage):
    manager = managed.ManagedCommunication(tmp_path)
    connections = []
    connect = sqlite3.connect

    class FailingConnection(sqlite3.Connection):
        closed = False

        def execute(self, *args, **kwargs):
            if stage == "pragma":
                raise sqlite3.OperationalError("injected pragma failure")
            return super().execute(*args, **kwargs)

        def executescript(self, *args, **kwargs):
            raise sqlite3.OperationalError("injected schema failure")

        def close(self):
            self.closed = True
            super().close()

    def failing_connect(*args, **kwargs):
        db = connect(*args, **kwargs, factory=FailingConnection)
        connections.append(db)
        return db

    monkeypatch.setattr(managed.sqlite3, "connect", failing_connect)
    with pytest.raises(sqlite3.OperationalError, match=f"injected {stage} failure"):
        with manager._db(write=True):
            pytest.fail("broken initialization yielded a connection")
    assert len(connections) == 1 and connections[0].closed
    with file_lock(manager.directory / ".schema.lock", blocking=False):
        pass


def test_initialization_contention_has_a_visible_deadline(tmp_path, monkeypatch):
    manager = managed.ManagedCommunication(tmp_path)
    clock = iter([100, 111])
    monkeypatch.setattr(
        managed,
        "time",
        SimpleNamespace(
            monotonic=lambda: next(clock),
            sleep=lambda _: pytest.fail("retried after the initialization deadline"),
        ),
    )
    with file_lock(manager.directory / ".schema.lock", blocking=False):
        with pytest.raises(CampaignError, match="initialization timed out") as error:
            with manager._db(write=True):
                pytest.fail("busy initialization yielded a connection")
    assert isinstance(error.value.__cause__, LockUnavailableError)
    assert not manager.path.exists()


def test_initialization_lock_faults_are_not_retried_as_contention(tmp_path, monkeypatch):
    manager = managed.ManagedCommunication(tmp_path)
    calls = []

    @contextmanager
    def broken_lock(*args, **kwargs):
        calls.append(args)
        raise LockUnavailableError(errno.ENOLCK, "locks unavailable")
        yield  # pragma: no cover - contextmanager protocol

    monkeypatch.setattr(managed, "file_lock", broken_lock)
    with pytest.raises(LockUnavailableError, match="locks unavailable"):
        with manager._db(write=True):
            pytest.fail("broken lock yielded a connection")
    assert len(calls) == 1
    assert not manager.path.exists()
