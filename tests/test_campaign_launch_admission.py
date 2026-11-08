"""Control acknowledgements and native intent share one local admission boundary."""

import contextlib
import hashlib
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest
from test_campaign_headless import prepared as prepared
from test_campaign_reasoning_controls import finish_job, queued, release

from bmad_loop import campaign_headless as headless
from bmad_loop import campaign_hold
from bmad_loop import campaign_reasoning_flow as flow
from bmad_loop import campaign_summary
from bmad_loop.adapters.base import SessionSpec
from bmad_loop.adapters.headless import HeadlessAdapter
from bmad_loop.adapters.multiplexer import MultiplexerError
from bmad_loop.adapters.profile import get_profile
from bmad_loop.childrun import install_stop_probe, reset_stop_probe
from bmad_loop.platform_util import LockUnavailableError, file_lock
from bmad_loop.policy import Policy


@pytest.mark.parametrize("control", ["hold", "stop"])
@pytest.mark.parametrize("boundary", ["launch-lock", "mux-lock", "session-setup"])
def test_control_ack_before_intent_suppresses_locked_launch_then_recovers(
    tmp_path, prepared, monkeypatch, control, boundary
):
    snapshot, runtime, supervisor, clock, action = queued(tmp_path, prepared)
    directory = Path(snapshot.jobs["job-0"]["claim"]["run_dir"])
    directory.mkdir(parents=True)
    entered, proceed = threading.Event(), threading.Event()
    blocked = (
        directory / "launch.lock"
        if boundary == "launch-lock"
        else directory.parent / "mux-session.lock"
    )

    @contextlib.contextmanager
    def observed_lock(path, **kwargs):
        if path == blocked:
            entered.set()
        with file_lock(path, **kwargs):
            yield

    def session(*args):
        entered.set()
        assert proceed.wait(20), "session preparation was never released"

    if boundary == "session-setup":
        monkeypatch.setattr(prepared[0].mux, "has_session", lambda _: False)
        monkeypatch.setattr(prepared[0].mux, "new_session", session, raising=False)
    else:
        monkeypatch.setattr(headless, "file_lock", observed_lock)
    with ThreadPoolExecutor(max_workers=1) as executor:
        try:
            with file_lock(blocked) if boundary != "session-setup" else contextlib.nullcontext():
                snapshot = supervisor._dispatch_ready(snapshot, executor)
                assert entered.wait(20)
                assert not (directory / "launch-intent.json").exists()
                latest = runtime.store.load(snapshot.id)
                runtime.control(
                    latest, {"kind": control, "unit": action.unit, "id": "owner-control"}
                )
                latest = runtime.store.save(latest, latest.revision)
                assert not prepared[0].mux.processes
        finally:
            proceed.set()
        observation = supervisor._active[action.id].result(timeout=20)
        assert observation.data["state"] == "deferred"
        latest = supervisor._harvest(latest)
    assert not (directory / "launch-intent.json").exists()
    assert not prepared[0].mux.processes
    assert latest.jobs["job-0"]["claim"]["quiescent"]
    assert latest.jobs["job-0"].get("failure_attempts", 0) == 0
    if control == "stop":
        latest.phase = "active"
        latest = runtime.store.save(latest, latest.revision)
    else:
        latest = release(runtime, latest, action.unit)
    latest = finish_job(latest, runtime, supervisor, clock)
    assert latest.jobs["job-0"]["state"] == "validated"
    assert latest.jobs["job-0"].get("failure_attempts", 0) == 0
    assert len(prepared[0].mux.processes) == 1


@pytest.mark.parametrize("control", ["hold", "stop"])
def test_intent_before_control_ack_is_inflight_and_reconciles_once(
    tmp_path, prepared, monkeypatch, control
):
    snapshot, runtime, supervisor, _, action = queued(tmp_path, prepared)
    directory = Path(snapshot.jobs["job-0"]["claim"]["run_dir"])
    entered, proceed = threading.Event(), threading.Event()
    window = prepared[0].mux.new_window

    def blocked_window(*args, **kwargs):
        entered.set()
        assert proceed.wait(20)
        return window(*args, **kwargs)

    monkeypatch.setattr(prepared[0].mux, "new_window", blocked_window)
    with ThreadPoolExecutor(max_workers=1) as executor:
        try:
            snapshot = supervisor._dispatch_ready(snapshot, executor)
            assert entered.wait(20)
            assert headless.read_json(directory / "launch-intent.json")["phase"] == "window-pending"
            # No terminal RPC may hold the campaign state lock.
            with file_lock(runtime.store.root / ".state.lock", blocking=False):
                pass
            latest = runtime.store.load(snapshot.id)
            runtime.control(latest, {"kind": control, "unit": action.unit, "id": "owner-control"})
            latest = runtime.store.save(latest, latest.revision)
            assert not latest.jobs["job-0"]["claim"]["quiescent"]
        finally:
            proceed.set()
        supervisor._active[action.id].result(timeout=20)
        latest = supervisor._harvest(latest)
    for child in prepared[0].mux.processes.values():
        assert child.wait(timeout=20) == 0
    monkeypatch.setattr(prepared[0], "launch", lambda *a, **kw: pytest.fail("intent replay"))
    observed = flow.perform(runtime, latest, latest.outbox[0], reconcile=True)
    assert observed.data["state"] == "finished"
    flow.apply(runtime, latest, latest.outbox[0], observed)
    assert latest.jobs["job-0"]["state"] == "validated"
    assert len(prepared[0].mux.processes) == 1


def test_fresh_guard_and_intent_are_inside_control_save_lock(tmp_path, prepared, monkeypatch):
    snapshot, runtime, supervisor, _, action = queued(tmp_path, prepared)
    actual = headless.write_json
    serialized = []

    def write(path, value):
        if path.name == "launch-intent.json":
            with pytest.raises(LockUnavailableError):
                with file_lock(runtime.store.root / ".state.lock", blocking=False):
                    pass
            serialized.append(True)
        return actual(path, value)

    monkeypatch.setattr(headless, "write_json", write)
    observed = flow.perform(runtime, snapshot, action)
    assert observed.data["state"] == "running"
    assert serialized == [True]


@pytest.mark.parametrize("change", ["configuration", "summary-contract"])
def test_final_admission_rechecks_current_request_after_lock_wait(
    tmp_path, prepared, monkeypatch, change
):
    runner, path, request, candidate, cap = prepared
    request = replace(
        request,
        coordinates={
            **request.coordinates,
            "summary_spec": {"prompt_version": campaign_summary.EXTRACT_PROMPT_VERSION},
        },
    )
    snapshot, runtime, supervisor, _, action = queued(
        tmp_path, (runner, path, request, candidate, cap)
    )
    directory = Path(snapshot.jobs["job-0"]["claim"]["run_dir"])
    directory.mkdir(parents=True)
    entered = threading.Event()

    @contextlib.contextmanager
    def observed_lock(path, **kwargs):
        if path == directory / "launch.lock":
            entered.set()
        with file_lock(path, **kwargs):
            yield

    monkeypatch.setattr(headless, "file_lock", observed_lock)
    with ThreadPoolExecutor(max_workers=1) as executor:
        with file_lock(directory / "launch.lock"):
            snapshot = supervisor._dispatch_ready(snapshot, executor)
            assert entered.wait(20)
            latest = runtime.store.load(snapshot.id)
            if change == "configuration":
                latest.config_revision += 1
                latest = runtime.store.save(latest, latest.revision)
            else:
                monkeypatch.setattr(campaign_summary, "EXTRACT_PROMPT_VERSION", "next-contract")
        observation = supervisor._active[action.id].result(timeout=20)
        assert observation.data["state"] == "deferred"
        assert "no longer current" in observation.data["reason"]
        latest = supervisor._harvest(latest)
    assert not (directory / "launch-intent.json").exists()
    assert not runner.mux.processes
    assert latest.jobs["job-0"]["claim"]["quiescent"]
    assert latest.jobs["job-0"].get("failure_attempts", 0) == 0
    latest = supervisor._queue(latest, flow.proposals(runtime, latest))
    stale = next(action for action in latest.outbox if action.kind == "reasoning-stale")
    flow.apply(runtime, latest, stale, flow.perform(runtime, latest, stale))
    assert latest.jobs["job-0"]["state"] == "stale"


@pytest.mark.parametrize("control", ["channel", "stored", "hard-stop"])
def test_direct_adapter_rechecks_final_scoped_control_under_shared_lock(
    tmp_path, prepared, monkeypatch, control
):
    runner, _, request, candidate, capability = prepared
    adapter = HeadlessAdapter(
        run_dir=tmp_path / "adapter", profile=get_profile("codex"), policy=Policy(), mux=runner.mux
    )
    adapter.set_capability(capability, None, request.coordinates)
    spec = SessionSpec(
        "held-stage",
        "planning",
        "Return JSON",
        tmp_path,
        model=candidate.model,
        effort=candidate.effort,
        timeout_s=15,
    )
    directory = (
        adapter.native_job_root
        / "headless"
        / hashlib.sha256(spec.task_id.encode()).hexdigest()[:24]
    )
    directory.mkdir(parents=True)
    lock = tmp_path / "campaigns/.state.lock"
    changed = [False]
    entered = threading.Event()

    @contextlib.contextmanager
    def observed_lock(path, **kwargs):
        if path == directory / "launch.lock":
            entered.set()
        with file_lock(path, **kwargs):
            yield

    def start():
        hold_token = campaign_hold.install(
            lambda: changed[0] and control == "channel",
            admission_lock=lock,
            admission_probe=lambda: changed[0] and control == "stored",
        )
        stop_token = install_stop_probe(lambda: changed[0] and control == "hard-stop")
        try:
            return adapter.start_session(spec)
        finally:
            reset_stop_probe(stop_token)
            campaign_hold.reset(hold_token)

    monkeypatch.setattr(headless, "file_lock", observed_lock)
    with ThreadPoolExecutor(max_workers=1) as executor:
        with file_lock(directory / "launch.lock"):
            future = executor.submit(start)
            assert entered.wait(20)
            changed[0] = True
        with pytest.raises(MultiplexerError, match="campaign (operator hold|hard stop)"):
            future.result(timeout=20)
    assert not (directory / "launch-intent.json").exists()
    assert not runner.mux.processes
    changed[0] = False
    handle = start()
    assert adapter.wait_for_completion(handle, spec).status == "completed"
    assert len(runner.mux.processes) == 1


def test_failed_empty_session_setup_has_no_native_intent_and_can_retry(prepared, monkeypatch):
    runner, path, *_ = prepared
    monkeypatch.setattr(runner.mux, "has_session", lambda _: False)

    def fail(*args):
        raise MultiplexerError("session setup unavailable")

    monkeypatch.setattr(runner.mux, "new_session", fail, raising=False)
    with pytest.raises(MultiplexerError, match="session setup unavailable"):
        runner.launch(path)
    assert not (path.parent / "launch-intent.json").exists()
    assert headless.read_json(path.parent / "launch-preparation-error.json")["pre_window_error"]
    monkeypatch.setattr(runner.mux, "has_session", lambda _: True)
    assert runner.launch(path)["state"] == "running"
    assert len(runner.mux.processes) == 1
