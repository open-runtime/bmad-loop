"""Bundled context and output-stream qualification; zero coding tokens.

Output cases adapted from open-runtime/paradev-tooling a5d76d3.
"""

import json
import os
import unittest
from unittest.mock import patch

import pytest

from bmad_loop.adapters.paradev_runtime import (
    GAP_MARKER,
    main,
    private_json,
    read_output,
    read_private_json,
    restore_terminal_context,
    verify_context,
    worker_environment,
)
from bmad_loop.platform_util import file_lock


@pytest.mark.skipif(
    os.name != "posix", reason="Paradev context transport is qualified on POSIX hosts"
)
class TestOutput(unittest.TestCase):
    @pytest.fixture(autouse=True)
    def sandbox(self, project, tmp_path):
        self.root = project.project
        self.folder = tmp_path / "launches" / "worker"
        private_json(
            self.folder / "launch.json",
            {
                "terminalId": "worker",
                "workspaceId": "workspace",
                "cwd": str(self.root),
                "command": "fake-worker",
                "env": {},
            },
        )

    def stream(self, batches):
        calls, sent = [], []

        class Host:
            endpoint = "http://127.0.0.1:1234"
            _token = "test-token"

        class Socket:
            def __init__(self, messages):
                self.messages = messages

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def send(self, message):
                sent.append(message)

            def __iter__(self):
                for message in self.messages:
                    if isinstance(message, Exception):
                        raise message
                    yield json.dumps(message) if isinstance(message, dict) else message

        def connect(url, **kwargs):
            calls.append((url, kwargs))
            batch = batches.pop(0)
            if isinstance(batch, Exception):
                raise batch
            return Socket(batch)

        return calls, sent, Host, connect

    def test_raw_output_ignores_synthetic_screen_and_reconnects_exactly(self):
        calls, _, host, connect = self.stream(
            [
                OSError("connection refused on the first attempt"),
                [
                    b"synthetic screen",
                    {"type": "attached"},
                    {"type": "title", "title": "x"},
                    {"type": "synced", "epoch": "epoch", "seq": 100, "mode": "tail"},
                    b"hello",
                    OSError("disconnect"),
                ],
                [
                    b"new synthetic screen",
                    {"type": "synced", "epoch": "epoch", "seq": 105, "mode": "exact"},
                    b" world",
                    {"type": "exit", "exitCode": 0},
                ],
            ]
        )
        with patch("bmad_loop.adapters.paradev_runtime.time.sleep"):
            read_output(self.folder, self.root / "pane.log", host_factory=host, connect=connect)
        text = (self.root / "pane.log").read_bytes()
        self.assertTrue(text.startswith(b"hello world"))
        self.assertIn(b"terminal ended", text)
        self.assertIn("seq=new", calls[1][0])
        self.assertIn("seq=epoch%3A105", calls[2][0])
        self.assertNotIn("token", calls[1][0])
        self.assertEqual(calls[1][1]["additional_headers"]["Authorization"], "Bearer test-token")
        self.assertIsNone(calls[1][1]["proxy"])
        status = read_private_json(self.folder / "output.json")
        self.assertEqual(
            (status["state"], status["gaps"], status["log"]),
            ("ended", 0, str((self.root / "pane.log").resolve())),
        )
        self.assertEqual(status["logInode"], os.stat(self.root / "pane.log").st_ino)

    def test_host_client_failure_is_retried_not_fatal(self):
        _, _, host, connect = self.stream(
            [[{"type": "synced", "epoch": "a", "seq": 0, "mode": "tail"}, {"type": "exit"}]]
        )
        attempts = []

        def factory():
            attempts.append(1)
            if len(attempts) == 1:
                raise RuntimeError("HOST_MANIFEST_INVALID")
            return host()

        with patch("bmad_loop.adapters.paradev_runtime.time.sleep"):
            read_output(self.folder, self.root / "pane.log", host_factory=factory, connect=connect)
        self.assertEqual(len(attempts), 2)
        self.assertEqual(read_private_json(self.folder / "output.json")["state"], "ended")

    def test_lost_position_is_marked_in_band_and_streaming_continues(self):
        _, _, host, connect = self.stream(
            [
                [{"type": "synced", "epoch": "a", "seq": 0, "mode": "tail"}, b"first", OSError()],
                [
                    {"type": "synced", "epoch": "a", "seq": 100, "mode": "reanchor"},
                    b"later",
                    {"type": "exit"},
                ],
            ]
        )
        with patch("bmad_loop.adapters.paradev_runtime.time.sleep"):
            read_output(self.folder, self.root / "pane.log", host_factory=host, connect=connect)
        text = (self.root / "pane.log").read_bytes()
        self.assertTrue(text.startswith(b"first" + (GAP_MARKER % 100).encode() + b"later"))
        status = read_private_json(self.folder / "output.json")
        self.assertEqual((status["state"], status["gaps"]), ("ended", 1))
        self.assertEqual(
            status["logSize"], len(text)
        )  # the marker is counted at the checkpoint, never as output

    def test_reanchor_at_the_exact_cursor_is_continuous(self):
        _, _, host, connect = self.stream(
            [
                [{"type": "synced", "epoch": "a", "seq": 0, "mode": "tail"}, b"first", OSError()],
                [
                    {"type": "synced", "epoch": "a", "seq": 5, "mode": "reanchor"},
                    b"-second",
                    {"type": "exit"},
                ],
            ]
        )
        with patch("bmad_loop.adapters.paradev_runtime.time.sleep"):
            read_output(self.folder, self.root / "pane.log", host_factory=host, connect=connect)
        self.assertTrue((self.root / "pane.log").read_bytes().startswith(b"first-second"))
        self.assertEqual(read_private_json(self.folder / "output.json")["gaps"], 0)

    def test_reader_restart_resumes_from_the_exact_recorded_cursor(self):
        calls, _, host, connect = self.stream(
            [
                [
                    {"type": "synced", "epoch": "a", "seq": 0, "mode": "tail"},
                    b"hello",
                    OSError("crash"),
                ],
            ]
        )
        with patch("bmad_loop.adapters.paradev_runtime.time.sleep"):
            with self.assertRaises(RuntimeError):
                read_output(
                    self.folder,
                    self.root / "pane.log",
                    host_factory=host,
                    connect=connect,
                    max_failures=1,
                )
        self.assertEqual(read_private_json(self.folder / "output.json")["state"], "failed")
        calls2, _, host, connect = self.stream(
            [
                [
                    {"type": "synced", "epoch": "a", "seq": 5, "mode": "exact"},
                    b" world",
                    {"type": "exit"},
                ],
            ]
        )
        read_output(self.folder, self.root / "pane.log", host_factory=host, connect=connect)
        self.assertIn("seq=a%3A5", calls2[0][0])
        self.assertTrue((self.root / "pane.log").read_bytes().startswith(b"hello world"))
        read_output(
            self.folder, self.root / "other.log", host_factory=host, connect=connect
        )  # ended: no-op
        read_output(
            self.folder, self.root / "pane.log", host_factory=host, connect=connect
        )  # ended: no-op

    def test_reader_rebinds_to_a_replaced_log_and_keeps_the_host_cursor(self):
        private_json(
            self.folder / "output.json",
            {
                "state": "ready",
                "pid": 1,
                "log": str(self.root / "old.log"),
                "logInode": 1,
                "epoch": "a",
                "seq": 5,
                "logSize": 5,
                "gaps": 0,
            },
        )
        (self.root / "old.log").write_bytes(b"hello")
        calls, _, host, connect = self.stream(
            [
                [
                    {"type": "synced", "epoch": "a", "seq": 5, "mode": "exact"},
                    b" world",
                    {"type": "exit"},
                ],
            ]
        )
        read_output(self.folder, self.root / "new.log", host_factory=host, connect=connect)
        self.assertIn("seq=a%3A5", calls[0][0])
        self.assertTrue((self.root / "new.log").read_bytes().startswith(b" world"))
        status = read_private_json(self.folder / "output.json")
        self.assertEqual(
            (status["state"], status["log"]), ("ended", str((self.root / "new.log").resolve()))
        )

    def test_overlapping_reader_leaves_the_live_output_record_alone(self):
        private_json(
            self.folder / "output.json",
            {
                "state": "ready",
                "pid": 99,
                "log": str(self.root / "pane.log"),
                "epoch": "a",
                "seq": 3,
                "logSize": 3,
                "gaps": 0,
            },
        )
        with file_lock(self.folder / "output.lock", blocking=False):
            read_output(
                self.folder,
                self.root / "pane.log",
                host_factory=lambda: None,
                connect=lambda *a, **k: None,
            )
            self.assertEqual(main(["--tee", str(self.folder), str(self.root / "pane.log")]), 0)
        record = read_private_json(self.folder / "output.json")
        self.assertEqual((record["state"], record["pid"], record["seq"]), ("ready", 99, 3))

    def test_restart_with_unsaved_bytes_and_a_failed_first_dial_keeps_the_exact_cursor(self):
        private_json(
            self.folder / "output.json",
            {
                "state": "failed",
                "pid": 1,
                "log": str((self.root / "pane.log").resolve()),
                "epoch": "a",
                "seq": 100,
                "logSize": 100,
                "gaps": 0,
            },
        )
        (self.root / "pane.log").write_bytes(
            b"x" * 110
        )  # ten bytes landed after the last checkpoint
        _, _, host, connect = self.stream([OSError("first dial fails")])
        with patch("bmad_loop.adapters.paradev_runtime.time.sleep"):
            with self.assertRaises(RuntimeError):
                read_output(
                    self.folder,
                    self.root / "pane.log",
                    host_factory=host,
                    connect=connect,
                    max_failures=1,
                )
        record = read_private_json(self.folder / "output.json")
        self.assertEqual(
            (record["seq"], record["logSize"]), (110, 110)
        )  # normalised before the failure was saved
        calls, _, host, connect = self.stream(
            [
                [
                    {"type": "synced", "epoch": "a", "seq": 110, "mode": "exact"},
                    b"y",
                    {"type": "exit"},
                ]
            ]
        )
        read_output(self.folder, self.root / "pane.log", host_factory=host, connect=connect)
        self.assertIn("seq=a%3A110", calls[0][0])
        self.assertTrue((self.root / "pane.log").read_bytes().startswith(b"x" * 110 + b"y"))

    def test_refused_handshake_statuses_fail_and_other_statuses_retry(self):
        class Refused(Exception):
            def __init__(self, code):
                self.response = type("Response", (), {"status_code": code})()

        _, _, host, connect = self.stream([Refused(404)])
        with self.assertRaises(RuntimeError):
            read_output(
                self.folder,
                self.root / "pane.log",
                host_factory=host,
                connect=connect,
                fatal_status=(Refused,),
            )
        self.assertEqual(read_private_json(self.folder / "output.json")["state"], "failed")
        (self.folder / "output.json").unlink()
        _, _, host, connect = self.stream(
            [
                Refused(503),
                [{"type": "synced", "epoch": "a", "seq": 0, "mode": "tail"}, {"type": "exit"}],
            ]
        )
        with patch("bmad_loop.adapters.paradev_runtime.time.sleep"):
            read_output(
                self.folder,
                self.root / "pane.log",
                host_factory=host,
                connect=connect,
                fatal_status=(Refused,),
            )
        self.assertEqual(read_private_json(self.folder / "output.json")["state"], "ended")

    def test_reader_answers_pings_and_stops_when_the_terminal_is_gone(self):
        _, sent, host, connect = self.stream(
            [
                [
                    {"type": "ping"},
                    {"type": "synced", "epoch": "a", "seq": 0, "mode": "tail"},
                    b"x",
                    {"type": "error", "code": "session-gone", "message": "gone"},
                ],
            ]
        )
        read_output(self.folder, self.root / "pane.log", host_factory=host, connect=connect)
        self.assertEqual(json.loads(sent[0]), {"type": "pong"})
        self.assertEqual(read_private_json(self.folder / "output.json")["state"], "ended")


@pytest.mark.skipif(
    os.name != "posix", reason="Paradev context transport is qualified on POSIX hosts"
)
def test_terminal_context_preserves_account_and_rejects_parent(project, tmp_path):
    folder = tmp_path / "launches" / "worker"
    private_json(
        folder / "launch.json",
        {
            "terminalId": "worker",
            "workspaceId": "workspace",
            "cwd": str(project.project),
            "env": {},
            "command": "fake",
        },
    )
    source = {
        "SUPERSET_TERMINAL_ID": "worker",
        "SUPERSET_WORKSPACE_ID": "workspace",
        "SUPERSET_WORKSPACE_PATH": str(project.project),
        "SUPERSET_ACCOUNT_ATTRIBUTION_TOKEN": "worker-token",
        "SUPERSET_AGENT_ID": "parent-agent",
        "HCOM_DIR": "/room",
    }
    _, env = worker_environment(folder, source)
    restored = restore_terminal_context(
        {"CODEX_HOME": "/isolated", "SUPERSET_AGENT_ID": "parent"}, env
    )
    assert restored["CODEX_HOME"] == "/isolated"
    assert restored["SUPERSET_ACCOUNT_ATTRIBUTION_TOKEN"] == "worker-token"
    assert restored["SUPERSET_TERMINAL_ID"] == "worker"
    assert "SUPERSET_AGENT_ID" not in restored
    with pytest.raises(ValueError, match="context"):
        restore_terminal_context({}, {**env, "SUPERSET_TERMINAL_ID": "parent"})

    class Host:
        def rpc(self, method, data=None):
            if method == "terminal.list":
                return {"sessions": [{"terminalId": "worker", "exited": False}]}
            return [{"id": "worker", "alive": True, "pid": 500}]

    assert verify_context(
        env, pid=900, host_factory=Host, ps=lambda pid: str({900: 500, 500: 1}.get(pid, 1))
    )["verified"]
    verdict = verify_context(env, pid=900, host_factory=Host, ps=lambda _: "1")
    assert verdict["checks"]["lineage"] == "failed"
    assert not verdict["verified"]
