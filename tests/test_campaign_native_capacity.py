"""Native allowance reads use fake CLIs exclusively; no model tokens or accounts."""

import dataclasses
import json
import os
import shlex
import threading
import time
from datetime import datetime, timezone

import pytest
from conftest import pid_gone, write_script_launcher

from bmad_loop import campaign_capacity_transport as transport
from bmad_loop import campaign_native_capacity as native
from bmad_loop.adapters.profile import get_profile
from bmad_loop.campaign_capacity_transport import ProbeError
from bmad_loop.campaign_model import RoleCandidate
from bmad_loop.campaign_scheduler import route_unavailable
from bmad_loop.process_host import ProcessHostError

NOW = 1_794_207_000.0
GROK = RoleCandidate("grok", "grok-4.7", "xhigh")
MUSE = RoleCandidate("muse", "muse-spark-1.3", "high")


def grok():
    return {
        "initialize": {"_meta": {"modelState": {"availableModels": [{"modelId": GROK.model}]}}},
        "billing": {
            "subscription_tier": "Native subscription fixture",
            "config": {
                "creditUsagePercent": 56,
                "currentPeriod": {
                    "type": "USAGE_PERIOD_TYPE_WEEKLY",
                    "start": datetime.fromtimestamp(NOW - 1000, timezone.utc).isoformat(),
                    "end": datetime.fromtimestamp(NOW + 1000, timezone.utc).isoformat(),
                },
            },
        },
    }


def muse():
    return {
        "models": {
            "source": "providerCatalog",
            "providerId": "meta",
            "models": [{"modelId": MUSE.model, "providerId": "meta"}],
        },
        "usage": {
            "schemaVersion": 1,
            "reason": None,
            "allowance": {
                "tier": "Native Muse subscription fixture",
                "source": "mint",
                "asOfMs": (NOW - 1) * 1000,
                "window": {"usedPercent": 0, "resetsAtMs": (NOW + 600) * 1000},
                "weekly": {"usedPercent": 25, "resetsAtMs": (NOW + 1000) * 1000},
            },
        },
    }


def test_known_native_allowance_uses_campaign_concurrency_not_guessed_slots():
    for observation in (
        native.from_grok(grok(), GROK, observed_at=NOW - 1, now=NOW),
        native.from_muse(muse(), MUSE, now=NOW),
    ):
        assert observation.available and observation.model_access and observation.known
        assert not observation.concurrency_known and observation.slots == 0
        assert not route_unavailable(observation, now=NOW)
        assert route_unavailable(observation, now=NOW + 120)
        assert observation.budget_remaining is None  # percent is not campaign currency


@pytest.mark.parametrize(
    "bad", [True, False, -1, 101, float("nan"), float("inf"), "50", None, 10**1000]
)
def test_invalid_allowance_numbers_are_not_capacity(bad):
    a, b = grok(), muse()
    a["billing"]["config"]["creditUsagePercent"] = bad
    b["usage"]["allowance"]["weekly"]["usedPercent"] = bad
    with pytest.raises(ProbeError):
        native.from_grok(a, GROK, observed_at=NOW, now=NOW)
    with pytest.raises(ProbeError):
        native.from_muse(b, MUSE, now=NOW)


@pytest.mark.parametrize("offset", [-121, 1])
def test_native_observations_expire_and_future_stamps_are_refused(offset):
    a = muse()
    a["usage"]["allowance"]["asOfMs"] = (NOW + offset) * 1000
    with pytest.raises(ProbeError, match="stale or future"):
        native.from_muse(a, MUSE, now=NOW)
    with pytest.raises(ProbeError, match="stale or future"):
        native.from_grok(grok(), GROK, observed_at=NOW + offset, now=NOW)


@pytest.mark.parametrize("bad", [True, "soon", None, float("nan"), float("inf"), NOW * 1000])
def test_muse_requires_a_valid_future_reset(bad):
    data = muse()
    data["usage"]["allowance"]["window"]["resetsAtMs"] = bad
    with pytest.raises(ProbeError):
        native.from_muse(data, MUSE, now=NOW)


@pytest.mark.parametrize(
    "key,bad",
    [
        ("start", "not-a-date"),
        ("end", "2026-11-09T00:00:00"),
        ("start", "2999-01-01T00:00:00Z"),
        ("end", "2000-01-01T00:00:00Z"),
        ("type", "UNKNOWN"),
    ],
)
def test_grok_requires_current_dated_billing_period(key, bad):
    data = grok()
    data["billing"]["config"]["currentPeriod"][key] = bad
    with pytest.raises(ProbeError):
        native.from_grok(data, GROK, observed_at=NOW, now=NOW)


@pytest.mark.parametrize(
    "source", ["bundledCatalog", "fakeCatalog", "configCatalog", "unresolvedCatalog"]
)
def test_muse_requires_provider_catalog(source):
    data = muse()
    data["models"]["source"] = source
    with pytest.raises(ProbeError, match="provider observation"):
        native.from_muse(data, MUSE, now=NOW)


def test_missing_model_and_exhausted_allowance_are_known_refusals():
    for model in ("grok-4.7-wrong", ""):
        observed = native.from_grok(
            grok(), dataclasses.replace(GROK, model=model), observed_at=NOW, now=NOW
        )
        assert observed.known and not observed.model_access and not observed.available
    observed = native.from_muse(muse(), dataclasses.replace(MUSE, model="unknown"), now=NOW)
    assert observed.known and not observed.model_access and not observed.available
    a, b = grok(), muse()
    a["billing"]["config"]["creditUsagePercent"] = 100
    # Additional purchased credit is not permission to spend beyond the subscription.
    a["billing"]["config"]["prepaidBalance"] = {"val": 1000}
    b["usage"]["allowance"]["window"]["usedPercent"] = 100
    for observed in (
        native.from_grok(a, GROK, observed_at=NOW, now=NOW),
        native.from_muse(b, MUSE, now=NOW),
    ):
        assert observed.known and observed.model_access and not observed.available


def test_capacity_remains_model_level_effort_is_checked_by_route_readiness():
    assert native.from_grok(grok(), GROK, observed_at=NOW, now=NOW) == native.from_grok(
        grok(), dataclasses.replace(GROK, effort="medium"), observed_at=NOW, now=NOW
    )


@pytest.mark.parametrize("tier", [None, "", " ", True, 1])
def test_missing_subscription_identity_does_not_infer_included_allowance(tier):
    data = grok()
    data["billing"]["subscription_tier"] = tier
    with pytest.raises(ProbeError, match="unidentified"):
        native.from_grok(data, GROK, observed_at=NOW, now=NOW)
    data = muse()
    data["usage"]["allowance"]["tier"] = tier
    with pytest.raises(ProbeError, match="unidentified"):
        native.from_muse(data, MUSE, now=NOW)


def test_muse_exact_model_must_belong_to_selected_provider():
    data = muse()
    data["models"]["models"][0]["providerId"] = "other"
    observed = native.from_muse(data, MUSE, now=NOW)
    assert not observed.available and not observed.model_access


@pytest.mark.parametrize("reason", ["server_sent_no_usage", "private provider message"])
def test_muse_empty_usage_is_unknown_with_only_the_known_native_reason(
    tmp_path, monkeypatch, reason
):
    data = muse()
    data["usage"] = {"schemaVersion": 1, "allowance": None, "reason": reason}
    monkeypatch.setattr(native, "_read_muse", lambda *args: data)
    original_which = native.shutil.which
    monkeypatch.setattr(
        native.shutil,
        "which",
        lambda command, **kwargs: (
            "muse" if command == "muse" else original_which(command, **kwargs)
        ),
    )
    observed = native.observe(MUSE, tmp_path, now=NOW)
    assert observed is not None and not observed.known and not observed.available
    assert route_unavailable(observed, now=NOW)
    assert observed.observed_at is None and observed.expires_at is None
    if reason == "server_sent_no_usage":
        assert observed.reason == "native Muse allowance unavailable: server_sent_no_usage"
    else:
        assert observed.reason == "native capacity payload schema mismatch"
        assert reason not in observed.reason


_FAKE = """open(__file__ + '.entered', 'w').write('python entered\\n')
import json, os, subprocess, sys, time
from pathlib import Path
Path(__file__ + '.imports-ready').touch()
config = json.loads(Path(os.environ['CAPACITY_FIXTURE']).read_text())
root = Path(os.environ['CAPACITY_FIXTURE']).parent
with (root/'pids').open('a') as stream: stream.write(str(os.getpid())+'\\n')
with (root/'argv.jsonl').open('a') as stream:
    stream.write(json.dumps({'argv':sys.argv[1:], 'auto_update':os.environ.get('MUSE_NO_AUTO_UPDATE')})+'\\n')
mode = config.get('mode', '')
if mode in ('timeout', 'descendant'):
    child = subprocess.Popen([sys.executable, '-c', 'import time;time.sleep(60)'])
    with (root/'pids').open('a') as stream: stream.write(str(child.pid)+'\\n')
if mode == 'timeout':
    time.sleep(60)
if mode == 'overflow':
    os.write(1, b'x'*(300*1024)); time.sleep(60)
if mode == 'deep-output' and sys.argv[1:] == ['usage', '--json']:
    print('['*10000+']'*10000, flush=True); raise SystemExit(0)
if sys.argv[1:] == ['usage', '--json']:
    print(json.dumps(config['usage']), flush=True)
    raise SystemExit(0)
initialized = False
for line in sys.stdin:
    msg = json.loads(line)
    with (root/'requests.jsonl').open('a') as stream: stream.write(json.dumps(msg)+'\\n')
    method = msg['method']
    if method in ('session/new','session/load','session/prompt','turn/start','authenticate'):
        (root/'inference').touch(); raise SystemExit(9)
    if method == 'initialized': initialized = True; continue
    if mode == 'unauthorized':
        print(json.dumps({'jsonrpc':'2.0','id':99,'method':'session/request_permission','params':{'secret':'do-not-expose'}}),flush=True)
    if mode == 'turn':
        print(json.dumps({'jsonrpc':'2.0','method':'turn/started','params':{}}),flush=True)
    if mode == 'invalid-method':
        print(json.dumps({'jsonrpc':'2.0','method':config['notification_method'],'params':{}}),flush=True)
    if mode == 'deep-protocol':
        print('['*10000+']'*10000, flush=True)
    if mode == 'metadata':
        print(json.dumps({'jsonrpc':'2.0','method':'_x.ai/mcp/servers_updated','params':{'mcpServers':[]}}),flush=True)
    if method == 'initialize': result = config.get('initialize', {})
    elif method == '_x.ai/billing': result = config['billing']
    elif method == 'model/list':
        if not initialized: raise SystemExit(8)
        result = config['models']
    else: raise SystemExit(7)
    if mode == 'malformed': print('bad-json',flush=True)
    elif mode == 'refused': print(json.dumps({'jsonrpc':'2.0','id':msg['id'],'error':{'message':'secret-do-not-expose'}}),flush=True)
    else: print(json.dumps({'jsonrpc':'2.0','id':msg['id'],'result':result}),flush=True)
    if mode == 'descendant': time.sleep(0.2)
if mode == 'trailing-turn':
    print(json.dumps({'jsonrpc':'2.0','method':'turn/started','params':{}}),flush=True)
if mode == 'trailing-invalid-method':
    print(json.dumps({'jsonrpc':'2.0','method':config['notification_method'],'params':{}}),flush=True)
if mode == 'deep-trailing':
    print('['*10000+']'*10000, flush=True)
if mode == 'metadata':
    print(json.dumps({'jsonrpc':'2.0','method':'_x.ai/mcp/servers_updated','params':{'mcpServers':[]}}),flush=True)
if mode == 'graceful-delay': time.sleep(1.5)
if mode == 'shutdown-wait': time.sleep(60)
raise SystemExit(6 if mode == 'nonzero' else 0)
"""


def fixture(tmp_path, monkeypatch, provider, mode=""):
    data = grok() if provider == "grok" else muse()
    data["mode"] = mode
    config = tmp_path / "config.json"
    config.write_text(json.dumps(data))
    binary = write_script_launcher(tmp_path, "fake-native", _FAKE)
    # Keep both launch stages observable when the host stalls before Python can
    # emit protocol data; neither marker changes the observer's finite budget.
    launcher = binary.read_text()
    marker = tmp_path / "launcher-entered"
    if os.name == "nt":
        launcher = f'@echo entered>"{marker}"\n' + launcher
    else:
        launcher = launcher.replace(
            "exec ", f"printf 'entered\\n' > {shlex.quote(str(marker))}\nexec ", 1
        )
    binary.write_text(launcher)
    monkeypatch.setenv("CAPACITY_FIXTURE", str(config))
    profile = get_profile(provider)
    original_which = native.shutil.which
    monkeypatch.setattr(
        native.shutil,
        "which",
        lambda command, **kwargs: (
            str(binary) if command == provider else original_which(command, **kwargs)
        ),
    )
    monkeypatch.setattr(native, "get_profile", lambda name, project: profile)
    return binary, data


@pytest.mark.parametrize("candidate", [GROK, MUSE])
def test_real_fake_native_protocol_eof_success_and_no_inference(tmp_path, monkeypatch, candidate):
    binary, _ = fixture(tmp_path, monkeypatch, candidate.profile)
    observed = native.observe(candidate, tmp_path, now=NOW)
    assert observed and observed.available and observed.model_access, (
        observed,
        sorted(path.name for path in tmp_path.iterdir()),
    )
    assert not observed.concurrency_known
    requests = [json.loads(line) for line in (tmp_path / "requests.jsonl").read_text().splitlines()]
    assert [row["method"] for row in requests] == (
        ["initialize", "_x.ai/billing"]
        if candidate.profile == "grok"
        else ["initialize", "initialized", "model/list"]
    )
    assert not (tmp_path / "inference").exists()
    for pid in (tmp_path / "pids").read_text().splitlines():
        assert pid_gone(int(pid))
    launches = [json.loads(line) for line in (tmp_path / "argv.jsonl").read_text().splitlines()]
    if candidate.profile == "muse":
        assert all(row["auto_update"] == "1" for row in launches)
        assert launches[0]["argv"] == ["usage", "--json"]
        assert "--no-session-log" in launches[1]["argv"]
    else:
        assert launches[0]["argv"] == ["agent", "--no-leader", "stdio"]


@pytest.mark.parametrize(
    "mode,reason",
    [
        ("unauthorized", "unauthorized client action"),
        ("turn", "unexpected notification"),
        ("malformed", "invalid protocol JSON"),
        ("refused", "request was refused"),
        ("nonzero", "process failed"),
        ("overflow", "exceeded its bound"),
        ("trailing-turn", "unexpected trailing activity"),
    ],
)
def test_protocol_failure_is_unknown_and_owned_child_is_gone(tmp_path, monkeypatch, mode, reason):
    fixture(tmp_path, monkeypatch, "grok", mode)
    result = native.observe(GROK, tmp_path, now=NOW)
    assert result and not result.known and not result.available
    assert reason in result.reason, (result, sorted(path.name for path in tmp_path.iterdir()))
    assert "secret" not in result.reason
    assert all(pid_gone(int(pid)) for pid in (tmp_path / "pids").read_text().splitlines())


def test_timeout_cleans_up_root_and_actual_observed_descendant(tmp_path, monkeypatch):
    fixture(tmp_path, monkeypatch, "grok", "timeout")
    monkeypatch.setattr(native, "TIMEOUT_S", 3.0)
    started = time.monotonic()
    result = native.observe(GROK, tmp_path, now=NOW)
    assert result and not result.known and "timed out" in result.reason
    assert time.monotonic() - started < 12
    pids = (tmp_path / "pids").read_text().splitlines()
    assert len(pids) == 2
    assert all(pid_gone(int(pid)) for pid in pids)


def test_eof_reaps_observed_child_after_parent_exit(tmp_path, monkeypatch):
    fixture(tmp_path, monkeypatch, "grok", "descendant")
    observed = native.observe(GROK, tmp_path, now=NOW)
    assert observed and observed.available
    pids = (tmp_path / "pids").read_text().splitlines()
    assert len(pids) == 2
    assert all(pid_gone(int(pid)) for pid in pids)


def test_reader_drains_trailing_activity_before_setting_stop_flag(tmp_path, monkeypatch):
    """Force buffered tail to remain unread until __exit__ joins the reader."""
    fixture(tmp_path, monkeypatch, "grok", "trailing-turn")
    joining = threading.Event()
    original_queue = transport.queue.Queue
    original_join = threading.Thread.join

    class DelayedQueue(original_queue):
        def put_nowait(self, value):
            super().put_nowait(value)
            if isinstance(value, bytes) and b"creditUsagePercent" in value:
                assert joining.wait(5), "cleanup never reached the reader join"

    def join(thread, timeout=None):
        joining.set()
        return original_join(thread, timeout=timeout)

    monkeypatch.setattr(transport.queue, "Queue", DelayedQueue)
    monkeypatch.setattr(threading.Thread, "join", join)
    result = native.observe(GROK, tmp_path, now=NOW)
    assert result and not result.known and "unexpected trailing activity" in result.reason
    assert all(pid_gone(int(pid)) for pid in (tmp_path / "pids").read_text().splitlines())


def test_readonly_mcp_inventory_notifications_do_not_block_native_capacity(tmp_path, monkeypatch):
    """Native Grok emits this metadata at project startup and shutdown."""
    fixture(tmp_path, monkeypatch, "grok", "metadata")
    result = native.observe(GROK, tmp_path, now=NOW)
    assert result and result.available and result.model_access
    assert all(pid_gone(int(pid)) for pid in (tmp_path / "pids").read_text().splitlines())


def test_fragmented_valid_metadata_is_bounded_by_bytes_not_chunk_count(tmp_path, monkeypatch):
    fixture(tmp_path, monkeypatch, "grok", "metadata")
    original_read = transport.os.read
    pending_counts = []
    totals = []

    class FragmentedStatusProcess(transport.StatusProcess):
        def _send(self, message):
            super()._send(message)
            if message.get("method") == "initialize":
                # Pause consumption until >65 one-byte fragments have queued.
                # A count-limited queue instead sets problem before this point.
                while self.messages.qsize() < 100:
                    self._check()
                    threading.Event().wait(0.001)
                pending_counts.append(self.messages.qsize())
                totals.append(self.total)

    monkeypatch.setattr(
        transport.os, "read", lambda fd, size: original_read(fd, 1 if size == 4096 else size)
    )
    monkeypatch.setattr(native, "StatusProcess", FragmentedStatusProcess)
    result = native.observe(GROK, tmp_path, now=NOW)
    assert result and result.available and result.model_access
    assert pending_counts and pending_counts[0] >= 100
    assert totals[0] < transport.MAX_OUTPUT_BYTES
    assert all(pid_gone(int(pid)) for pid in (tmp_path / "pids").read_text().splitlines())


def test_native_rpc_writes_explicit_utf8_lf_on_binary_pipes(tmp_path, monkeypatch):
    fixture(tmp_path, monkeypatch, "grok")
    original_popen = transport.subprocess.Popen
    launches = []
    frames = []

    class UnicodeStatusProcess(transport.StatusProcess):
        def request(self, identifier, method, params):
            if method == "initialize":
                params = {**params, "clientInfo": {"name": "bmad-capacité", "version": "1"}}
            return super().request(identifier, method, params)

    class RecordingInput:
        def __init__(self, stream):
            self.stream = stream

        def write(self, frame):
            assert isinstance(frame, bytes)
            frames.append(frame)
            return self.stream.write(frame)

        def __getattr__(self, name):
            return getattr(self.stream, name)

    def spawn(*args, **kwargs):
        child = original_popen(*args, **kwargs)
        launches.append(kwargs)
        child.stdin = RecordingInput(child.stdin)
        return child

    monkeypatch.setattr(transport.subprocess, "Popen", spawn)
    monkeypatch.setattr(native, "StatusProcess", UnicodeStatusProcess)
    result = native.observe(GROK, tmp_path, now=NOW)
    assert result and result.available
    assert len(launches) == 1
    assert not launches[0].get("text") and launches[0].get("encoding") is None
    assert len(frames) == 2
    assert all(frame.endswith(b"\n") and b"\r" not in frame for frame in frames)
    assert [json.loads(frame.decode("utf-8"))["method"] for frame in frames] == [
        "initialize",
        "_x.ai/billing",
    ]
    assert json.loads(frames[0].decode("utf-8"))["params"]["clientInfo"]["name"] == "bmad-capacité"
    assert all(pid_gone(int(pid)) for pid in (tmp_path / "pids").read_text().splitlines())


@pytest.mark.parametrize("method", [[], {}], ids=["list", "object"])
@pytest.mark.parametrize(
    "mode,reason",
    [
        ("invalid-method", "unexpected notification"),
        ("trailing-invalid-method", "unexpected trailing activity"),
    ],
)
def test_unhashable_notification_method_is_sanitized_unknown(
    tmp_path, monkeypatch, method, mode, reason
):
    _, data = fixture(tmp_path, monkeypatch, "grok", mode)
    data["notification_method"] = method
    (tmp_path / "config.json").write_text(json.dumps(data))
    result = native.observe(GROK, tmp_path, now=NOW)
    assert result and not result.known and not result.available
    assert reason in result.reason
    assert all(pid_gone(int(pid)) for pid in (tmp_path / "pids").read_text().splitlines())


@pytest.mark.parametrize(
    "candidate,mode,reasons",
    [
        (MUSE, "deep-output", ("invalid JSON", "payload schema mismatch")),
        (GROK, "deep-protocol", ("invalid protocol JSON", "protocol schema mismatch")),
        (GROK, "deep-trailing", ("trailing protocol is invalid", "unexpected trailing activity")),
    ],
)
def test_deeply_nested_native_json_is_sanitized_unknown(
    tmp_path, monkeypatch, candidate, mode, reasons
):
    """Decoder depth limits differ by Python; the parsed array is never a schema."""
    fixture(tmp_path, monkeypatch, candidate.profile, mode)
    result = native.observe(candidate, tmp_path, now=NOW)
    assert result and not result.known and not result.available
    assert any(reason in result.reason for reason in reasons)
    assert all(pid_gone(int(pid)) for pid in (tmp_path / "pids").read_text().splitlines())


@pytest.mark.parametrize(
    "candidate,mode,reason",
    [
        (MUSE, "deep-output", "invalid JSON"),
        (GROK, "deep-protocol", "invalid protocol JSON"),
        (GROK, "deep-trailing", "trailing protocol is invalid"),
    ],
)
def test_decoder_recursion_error_is_sanitized_unknown(
    tmp_path, monkeypatch, candidate, mode, reason
):
    """Force only the known bad frame's decoder fault, without a depth assumption."""
    fixture(tmp_path, monkeypatch, candidate.profile, mode)
    original_loads = transport.json.loads
    frame = "[" * 10000 + "]" * 10000
    rejected = []

    def decode(value, *args, **kwargs):
        if value in (frame, frame + "\n", frame.encode(), (frame + "\n").encode()):
            rejected.append(True)
            raise RecursionError("synthetic decoder stack limit")
        return original_loads(value, *args, **kwargs)

    monkeypatch.setattr(transport.json, "loads", decode)
    result = native.observe(candidate, tmp_path, now=NOW)
    assert rejected
    assert result and not result.known and not result.available
    assert reason in result.reason
    assert all(pid_gone(int(pid)) for pid in (tmp_path / "pids").read_text().splitlines())


def test_normal_eof_drain_uses_existing_whole_job_deadline(tmp_path, monkeypatch):
    fixture(tmp_path, monkeypatch, "grok", "graceful-delay")
    result = native.observe(GROK, tmp_path, now=NOW)
    assert result and result.available and result.model_access
    assert all(pid_gone(int(pid)) for pid in (tmp_path / "pids").read_text().splitlines())


@pytest.mark.parametrize("cancel", [False, True], ids=["host-error", "keyboard-interrupt"])
@pytest.mark.parametrize("cleanup_fails", [False, True], ids=["cleanup", "double-fault"])
def test_eof_unwind_always_attempts_owned_cleanup_and_preserves_error(
    tmp_path, monkeypatch, cancel, cleanup_fails
):
    fixture(tmp_path, monkeypatch, "grok", "shutdown-wait")
    original_kill = transport.kill_tree
    failure = KeyboardInterrupt() if cancel else ProcessHostError("private-host-detail")
    children = []
    cleanup_attempts = []

    class CensusFault:
        def __init__(self, host):
            self.host = host

        def descendants(self, pid):
            raise failure

        def __getattr__(self, name):
            return getattr(self.host, name)

    class FaultAtEOF(transport.StatusProcess):
        def request(self, identifier, method, params):
            result = super().request(identifier, method, params)
            if method == "_x.ai/billing":
                children.append(self)
                self.host = CensusFault(self.host)
            return result

    def cleanup(proc, *, known):
        cleanup_attempts.append(proc.pid)
        if cleanup_fails:
            raise RuntimeError("private-cleanup-detail")
        original_kill(proc, known=known)

    monkeypatch.setattr(native, "StatusProcess", FaultAtEOF)
    monkeypatch.setattr(transport, "kill_tree", cleanup)
    try:
        if cancel:
            with pytest.raises(KeyboardInterrupt) as caught:
                native.observe(GROK, tmp_path, now=NOW)
            assert caught.value is failure
            if cleanup_fails:
                assert "cleanup is unconfirmed" in " ".join(failure.__notes__)
        else:
            result = native.observe(GROK, tmp_path, now=NOW)
            assert result and not result.known and not result.available
            assert "ProcessHostError" in result.reason
            assert "private" not in result.reason
            if cleanup_fails:
                assert "cleanup is unconfirmed" in result.reason
        assert children and cleanup_attempts == [children[0].proc.pid]
        if not cleanup_fails:
            assert children[0].proc.poll() is not None
            assert pid_gone(children[0].proc.pid)
    finally:
        # Even an ablated cleanup guard must not leave this fake owned child.
        for child in children:
            original_kill(child.proc, known=child.known)
            for thread in child.threads:
                thread.join(timeout=2)
            for stream in (child.proc.stdout, child.proc.stderr):
                if stream and not any(thread.is_alive() for thread in child.threads):
                    stream.close()


def test_unsupported_profile_does_not_spawn(tmp_path, monkeypatch):
    monkeypatch.setattr(
        native, "get_profile", lambda *_: pytest.fail("unsupported native observer")
    )
    assert native.observe(RoleCandidate("claude", "claude"), tmp_path) is None


@pytest.mark.parametrize("fault", ["kill", "liveness", "reader"])
def test_cleanup_uncertainty_survives_sanitized_observation(tmp_path, monkeypatch, fault):
    fixture(tmp_path, monkeypatch, "grok")
    original_kill = transport.kill_tree
    children = []

    class UnconfirmedReader:
        def join(self, timeout=None):
            pass

        def is_alive(self):
            return True

    class UnconfirmedHost:
        def __init__(self, host):
            self.host = host

        def liveness_of(self, pid, identity):
            return "unknown"

        def __getattr__(self, name):
            return getattr(self.host, name)

    class FaultAfterResponse(transport.StatusProcess):
        def request(self, identifier, method, params):
            result = super().request(identifier, method, params)
            if method == "_x.ai/billing":
                children.append(self)
                if fault == "liveness":
                    self.known[self.proc.pid] = self.host.identity(self.proc.pid)
                    self.host = UnconfirmedHost(self.host)
                if fault == "reader":
                    self.threads.append(UnconfirmedReader())
                if fault != "kill":
                    raise ProbeError("initial status fault")
            return result

    def failed_cleanup(*args, **kwargs):
        raise ProcessHostError("private-cleanup-detail")

    monkeypatch.setattr(native, "StatusProcess", FaultAfterResponse)
    if fault == "kill":
        monkeypatch.setattr(transport, "kill_tree", failed_cleanup)
    try:
        result = native.observe(GROK, tmp_path, now=NOW)
        assert result and not result.known and not result.available
        assert "cleanup is unconfirmed" in result.reason
        assert "private" not in result.reason
        assert ("ProcessHostError" if fault == "kill" else "initial status fault") in result.reason
    finally:
        for child in children:
            original_kill(child.proc, known=child.known)
            actual_readers = [t for t in child.threads if not isinstance(t, UnconfirmedReader)]
            for thread in actual_readers:
                thread.join(timeout=2)
            for stream in (child.proc.stdout, child.proc.stderr):
                if stream and not any(t.is_alive() for t in actual_readers):
                    stream.close()


def test_unsafe_launch_override_refuses_before_spawn(tmp_path, monkeypatch):
    profile = dataclasses.replace(get_profile("grok"), launch_args=("--single", "model prompt"))
    monkeypatch.setattr(native, "get_profile", lambda *_: profile)
    monkeypatch.setattr(native, "StatusProcess", lambda *_: pytest.fail("must not spawn inference"))
    observed = native.observe(GROK, tmp_path)
    assert observed and not observed.known and "custom capacity command" in observed.reason
