"""Trusted pre-model failure transcripts use scripted identities, never providers."""

import json
import os
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_campaign_headless import prepared as prepared

from bmad_loop import campaign_headless_worker as worker
from bmad_loop import campaign_prelaunch_failure as failure
from bmad_loop.campaign_headless import file_digest, read_json, write_json
from bmad_loop.campaign_jobs import digest
from bmad_loop.campaign_model import CampaignError


@pytest.fixture(autouse=True)
def scripted_recipe(monkeypatch):
    # These unit fixtures are not native qualifications. Recipe exactness has
    # its own maintained tests; the new validator's call is asserted below.
    from bmad_loop import campaign_codex_appserver

    monkeypatch.setattr(campaign_codex_appserver, "validate_recipe", lambda cap: None)


def capability(tmp_path):
    pins = {}
    for name in failure.PINS:
        path = tmp_path / name
        path.write_text(name)
        pins[name] = {"path": str(path), "sha256": file_digest(path)}
    paths = [Path(failure.__file__).resolve(), Path(worker.__file__).resolve()]
    return {
        "platform": "darwin",
        "protocol": "codex-app-server",
        "argv": ["launcher", "--native", pins["producer"]["path"]],
        "prelaunch_failure": {"contract": failure.CONTRACT, **pins},
        "dependencies": [
            *pins.values(),
            *({"path": str(p), "sha256": file_digest(p)} for p in paths),
        ],
    }


@pytest.fixture
def channel(tmp_path, monkeypatch):
    if os.name != "posix":
        pytest.skip(
            "opt-in producer uses POSIX private-file ownership; legacy routes remain portable"
        )
    cap = capability(tmp_path)
    envelope = {
        "capability": cap,
        "capability_digest": digest(cap),
        "launch_id": "launch",
        "generation": 2,
        "request_digest": "a" * 64,
        "request": {"id": "job", "limits": {"timeout_s": 30}},
    }
    clock = [1000.0]
    item = failure.Channel(tmp_path, envelope, -1234.5, clock=lambda: clock[0])
    actors = [
        {
            "pid": 101,
            "identity": -100.5,
            "ppid": 102,
            "executable": "producer",
            "argv_sha256": "1" * 64,
        }
    ]
    monkeypatch.setattr(failure, "observe_chain", lambda *args: actors)
    host = SimpleNamespace(liveness_of=lambda *args: "alive")
    return item, clock, host


def refusal(item, **changes):
    return {
        **failure.REFUSAL_FIXED,
        **{key: item.context[key] for key in failure.BINDING},
        "context_sha256": item.context_sha,
        "producer_pid": 101,
        "producer_ppid": 102,
        "issued_at_unix_ms": 1000000,
        "expires_at_unix_ms": 1002000,
        **changes,
    }


def begin(item, host, value=None, persist=lambda: None):
    failure._publish(item.directory / "refusal.json", value or refusal(item))
    known = {}
    item.poll(104, -104.5, host, known, persist)
    if item.proof_started:
        assert item.proof_done.wait(2)
        item.poll(104, -104.5, host, known, persist)
    return known


def confirm(item, host, **changes):
    accepted = item.accepted
    assert accepted is not None, item.fault
    failure._publish(
        item.directory / "confirmed.json",
        {
            "schema_version": 1,
            "context_sha256": item.context_sha,
            "refusal_sha256": accepted["refusal_sha256"],
            "ack_sha256": accepted["ack_sha256"],
            "producer_pid": 101,
            **changes,
        },
    )
    item.poll(104, -104.5, host, {}, lambda: None)


def finish(item, **changes):
    receipt = {
        "outcome": "failed",
        "reason": "Native headless output has no object frames",
        "usage": {},
        "input_complete": False,
        "processes_quiescent": True,
    }
    args = {"returncode": 1, "stdout_bytes": 0, "ordinary_terminal": True, **changes}
    item.finish(receipt, **args)
    return receipt


def test_authenticated_handshake_preserves_unknown_usage_and_observed_birth(channel):
    item, clock, host = channel
    persisted = []
    known = begin(
        item, host, persist=lambda: persisted.append(not (item.directory / "ack.json").exists())
    )
    assert persisted == [True]
    assert known == {101: -100.5}
    assert read_json(item.directory / "ack.json")["producer_identity"] == -100.5
    confirm(item, host)
    clock[0] += 10  # Already observed confirmation survives later normal cleanup.
    result = finish(item)
    assert result["outcome"] == "authentication"
    assert "CANONICAL_REFRESH_QUARANTINED" in result["reason"]
    assert "sign-in required" in result["reason"]
    assert result["usage"] == {} and result["input_complete"] is False
    assert not {"session_id", "observed_model", "turns", "result"} & result.keys()
    assert result["prelaunch_failure"]["actors"][0]["identity"] == -100.5


@pytest.mark.parametrize(
    "change",
    [
        {"generation": 1},
        {"generation": True},
        {"request_digest": "b" * 64},
        {"launch_id": "other"},
        {"job_id": "other"},
        {"capability_digest": "b" * 64},
        {"nonce": "b" * 64},
        {"producer_pid": True},
        {"producer_ppid": 0},
        {"native_worker_started": 0},
        {"exit_code": True},
        {"schema_version": True},
        {"code": "MODEL_SAID_AUTH"},
        {"mode": "session"},
        {"unexpected": 1},
        {"expires_at_unix_ms": 1003000},
        {"issued_at_unix_ms": 1001000},
    ],
)
def test_wrong_attempt_or_nonprelaunch_record_never_acknowledged(channel, change):
    item, _, host = channel
    begin(item, host, refusal(item, **change))
    assert not (item.directory / "ack.json").exists()
    assert finish(item)["outcome"] == "failed"
    assert item.fault


@pytest.mark.parametrize("gate", ["returncode", "stdout_bytes", "ordinary_terminal", "quiescence"])
def test_terminal_state_cannot_be_overridden_by_committed_auth(channel, gate):
    item, _, host = channel
    begin(item, host)
    confirm(item, host)
    if gate == "quiescence":
        result = {"outcome": "failed", "processes_quiescent": False}
        item.finish(result, returncode=1, stdout_bytes=0, ordinary_terminal=True)
    else:
        result = finish(
            item, **{gate: {"returncode": 3, "stdout_bytes": 1, "ordinary_terminal": False}[gate]}
        )
    assert result["outcome"] == "failed"
    assert "terminal evidence" in result["prelaunch_failure_error"]


@pytest.mark.parametrize(
    "phase", ["ack-only", "late-confirm", "wrong-confirm", "mutated-ack", "mutated-context"]
)
def test_handshake_races_or_mutation_remain_generic(channel, phase):
    item, clock, host = channel
    begin(item, host)
    if phase == "late-confirm":
        clock[0] = 1002.001
        confirm(item, host)
    elif phase == "wrong-confirm":
        confirm(item, host, ack_sha256="0" * 64)
    elif phase == "mutated-ack":
        (item.directory / "ack.json").write_text("{}")
        confirm(item, host)
    elif phase == "mutated-context":
        confirm(item, host)
        (item.directory / "context.json").write_text("{}")
    assert finish(item)["outcome"] == "failed"


def test_lost_or_unreadable_live_identity_never_acknowledged(channel, monkeypatch):
    item, _, host = channel
    monkeypatch.setattr(
        failure, "observe_chain", lambda *args: (_ for _ in ()).throw(CampaignError("unowned"))
    )
    begin(item, host)
    assert item.fault and not (item.directory / "ack.json").exists()


def test_producer_dying_during_identity_observation_refuses_ack(channel):
    item, _, host = channel
    host.liveness_of = lambda *args: "dead"
    begin(item, host)
    assert not (item.directory / "ack.json").exists()
    assert "ended during observation" in item.fault


def test_late_durable_actor_record_cannot_ack(channel):
    item, clock, host = channel
    begin(item, host, persist=lambda: clock.__setitem__(0, 1002.001))
    assert not (item.directory / "ack.json").exists()
    assert "deadline" in item.fault


def test_stalled_identity_reader_never_blocks_poll_or_publishes_late_ack(channel, monkeypatch):
    item, clock, host = channel
    gate = threading.Event()
    entered = threading.Event()
    original = failure.observe_chain

    def stalled(*args):
        entered.set()
        gate.wait(5)
        return original(*args)

    monkeypatch.setattr(failure, "observe_chain", stalled)
    failure._publish(item.directory / "refusal.json", refusal(item))
    try:
        item.poll(104, -104.5, host, {}, lambda: pytest.fail("no actor persistence before proof"))
        assert entered.wait(2)
        assert not item.proof_done.is_set()  # poll returned while read is still blocked.
        clock[0] = 1002.001
        item.poll(104, -104.5, host, {}, lambda: pytest.fail("late proof cannot persist actors"))
        assert "deadline" in item.fault
    finally:
        gate.set()
        assert item.proof_done.wait(2)
    item.poll(104, -104.5, host, {}, lambda: pytest.fail("no late writes"))
    assert not (item.directory / "ack.json").exists()
    assert finish(item)["outcome"] == "failed"


@pytest.mark.parametrize("kind", ["mode", "symlink", "oversize", "duplicate", "array"])
def test_channel_files_must_be_bounded_private_single_objects(channel, kind, tmp_path):
    item, _, host = channel
    path = item.directory / "refusal.json"
    if kind == "symlink":
        target = tmp_path / "elsewhere"
        target.write_text(json.dumps(refusal(item)))
        path.symlink_to(target)
    else:
        failure._publish(path, refusal(item))
        if kind == "mode":
            path.chmod(0o644)
        elif kind == "oversize":
            path.write_bytes(b" " * 4097)
        elif kind == "duplicate":
            path.write_text('{"schema_version":1,"schema_version":1}')
        else:
            path.write_text("[]")
    item.poll(104, -104.5, host, {}, lambda: None)
    assert item.fault and not (item.directory / "ack.json").exists()


def test_publish_cannot_replace_preexisting_record(channel):
    item, _, _ = channel
    before = (item.directory / "context.json").read_bytes()
    with pytest.raises(FileExistsError):
        failure._publish(item.directory / "context.json", {})
    assert (item.directory / "context.json").read_bytes() == before


@pytest.mark.parametrize(
    "mutation", ["none", "missing", "stale", "protocol", "helper", "producer", "platform"]
)
def test_optin_requires_exact_new_carrier_and_consumer_dependencies(tmp_path, mutation):
    cap = capability(tmp_path)
    if mutation == "none":
        failure.validate(cap)
        return
    if mutation == "missing":
        cap["dependencies"].pop()
    elif mutation == "stale":
        Path(cap["prelaunch_failure"]["carrier"]["path"]).write_text("changed")
    elif mutation == "protocol":
        cap["protocol"] = "bmad-test-v1"
    elif mutation == "helper":
        del cap["prelaunch_failure"]["producer_helper"]
    elif mutation == "producer":
        cap["argv"][-1] = "/different"
    else:
        cap["platform"] = "win32"
    with pytest.raises(CampaignError, match="Prelaunch authentication"):
        failure.validate(cap)


def test_legacy_capability_is_unchanged_and_cannot_borrow_new_optin(prepared, tmp_path):
    from bmad_loop.campaign_capability_evidence import validate

    _, _, _, candidate, cap = prepared
    failure.validate(cap)
    changed = {**cap, "prelaunch_failure": capability(tmp_path)["prelaunch_failure"]}
    with pytest.raises(CampaignError, match="route, executable or permissions changed"):
        validate(changed, candidate)


def test_optin_cannot_bypass_maintained_codex_recipe(tmp_path, monkeypatch):
    from bmad_loop import campaign_codex_appserver

    monkeypatch.setattr(
        campaign_codex_appserver,
        "validate_recipe",
        lambda cap: (_ for _ in ()).throw(CampaignError("recipe changed")),
    )
    with pytest.raises(CampaignError, match="recipe changed"):
        failure.validate(capability(tmp_path))


@pytest.mark.parametrize(
    "mutation", ["none", "unowned", "reused", "image", "script", "carrier", "parent", "argv"]
)
def test_live_chain_authentication_uses_kernel_identity_and_exact_images(tmp_path, mutation):
    spec = capability(tmp_path)["prelaunch_failure"]
    pids = [101, 102, 103, 104]
    stamps = {pid: -float(pid) for pid in pids}
    rows = {
        101: (
            102,
            spec["producer_executable"]["path"],
            [
                "node",
                spec["producer"]["path"],
                "-c",
                "features.hooks=true",
                "app-server",
                "--listen",
                "stdio://",
            ],
        ),
        102: (
            103,
            spec["carrier"]["path"],
            [
                "hcom",
                "codex",
                "--stdio",
                "--run-here",
                "--go",
                "--",
                "app-server",
                "--listen",
                "stdio://",
            ],
        ),
        103: (
            104,
            spec["producer_executable"]["path"],
            ["node", spec["producer"]["path"], "app-server", "--listen", "stdio://"],
        ),
        104: (100, sys.executable, [sys.executable, "launcher"]),
    }
    if mutation == "unowned":
        stamps.pop(101)
    elif mutation == "image":
        rows[101] = (102, "/wrong-node", rows[101][2])
    elif mutation == "script":
        rows[101][2][1] = "/wrong-script"
    elif mutation == "carrier":
        rows[102] = (103, "/wrong-hcom", rows[102][2])
    elif mutation == "parent":
        rows[101] = (104, rows[101][1], rows[101][2])
    elif mutation == "argv":
        rows[102][2][2] = "--background"
    host = SimpleNamespace(
        descendants=lambda _: stamps,
        liveness_of=lambda *args: "alive",
        identity=lambda pid: 999 if mutation == "reused" and pid == 101 else stamps[pid],
    )
    psutil = SimpleNamespace(
        Process=lambda pid: SimpleNamespace(
            ppid=lambda: rows[pid][0], exe=lambda: rows[pid][1], cmdline=lambda: rows[pid][2]
        )
    )
    if mutation == "none":
        observed = failure._observe_chain(101, 104, -104.0, spec, host, psutil)
        assert [row["pid"] for row in observed] == pids
    else:
        with pytest.raises(CampaignError):
            failure._observe_chain(101, 104, -104.0, spec, host, psutil)


@pytest.mark.parametrize(
    "mode", ["handshake", "stderr", "frames", "exit3", "missingcommit", "legacy"]
)
@pytest.mark.skipif(os.name != "posix", reason="scripted opt-in producer uses POSIX private files")
def test_actual_worker_consumes_scripted_handshake_and_preserves_receipt_paths(
    prepared, tmp_path, monkeypatch, mode
):
    """Actual worker/child I/O; chain verification is separately tested above."""
    _, path, _, _, _ = prepared
    script = tmp_path / "scripted-prelaunch.py"
    script.write_text("""import hashlib,json,os,pathlib,time,sys
mode=sys.argv[1]
if mode=='legacy':
 assert 'TEAMROUTER_BMAD_PREFLIGHT_CONTEXT' not in os.environ
 print(json.dumps({'type':'system','model':'gpt-test','session_id':'scripted-legacy'}))
 print(json.dumps({'type':'result','structured_output':{'legacy':True},'usage':{'input_tokens':1,'output_tokens':1}}))
 raise SystemExit(0)
if mode=='stderr':
 print('CANONICAL_REFRESH_QUARANTINED',file=sys.stderr);raise SystemExit(1)
p=pathlib.Path(os.environ['TEAMROUTER_BMAD_PREFLIGHT_CONTEXT']); d=p.parent
c=json.loads(p.read_bytes()); sha=lambda b:hashlib.sha256(b).hexdigest()
def put(name,value):
 data=json.dumps(value,sort_keys=True,separators=(',',':')).encode(); t=d/('.'+name)
 fd=os.open(t,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
 with os.fdopen(fd,'wb') as f:f.write(data)
 os.link(t,d/name);t.unlink()
now=int(time.time()*1000)
r={k:c[k] for k in ['job_id','launch_id','generation','request_digest','capability_digest','nonce']}
r.update(schema_version=1,context_sha256=sha(p.read_bytes()),outcome='authentication',code='CANONICAL_REFRESH_QUARANTINED',stage='canonical-access-before-native',mode='hcom-hook-inspection',producer_pid=os.getpid(),producer_ppid=os.getppid(),exit_code=3,native_worker_started=False,issued_at_unix_ms=now,expires_at_unix_ms=now+2000)
put('refusal.json',r)
while time.time()*1000<now+2000:
 if (d/'ack.json').exists():
  if mode!='missingcommit':put('confirmed.json',dict(schema_version=1,context_sha256=r['context_sha256'],refusal_sha256=sha((d/'refusal.json').read_bytes()),ack_sha256=sha((d/'ack.json').read_bytes()),producer_pid=os.getpid()))
  break
 time.sleep(.01)
if mode=='frames':print(json.dumps({'type':'result','structured_output':{},'usage':{'input_tokens':1,'output_tokens':1}}))
raise SystemExit(3 if mode=='exit3' else 1)
""")
    envelope = read_json(path)
    cap = envelope["capability"]
    cap["argv"] = [sys.executable, str(script), mode]
    if mode != "legacy":
        cap["prelaunch_failure"] = capability(tmp_path)["prelaunch_failure"]
    monkeypatch.setenv("TEAMROUTER_BMAD_PREFLIGHT_CONTEXT", "/untrusted/inherited/path")
    envelope["capability_digest"] = digest(cap)
    write_json(path, envelope)
    write_json(
        path.parent / "go.json",
        {"launch_id": envelope["launch_id"], "generation": envelope["generation"]},
    )
    monkeypatch.setattr(worker, "validate_capability", lambda *args, **kwargs: None)
    monkeypatch.setattr(failure, "validate", lambda *args, **kwargs: None)

    def observe(pid, root_pid, root_stamp, spec, host):
        stamp = host.identity(pid)
        assert stamp is not None and pid == root_pid
        return [
            {
                "pid": pid,
                "identity": stamp,
                "ppid": os.getpid(),
                "executable": sys.executable,
                "argv_sha256": file_digest(script),
            }
        ]

    monkeypatch.setattr(failure, "observe_chain", observe)
    assert worker.child_main(path) == (0 if mode == "legacy" else 1)
    result = read_json(path.parent / "receipt.json")
    if mode != "handshake":
        assert result["outcome"] != "authentication", result
        if mode == "legacy":
            assert result["outcome"] == "completed" and result["result"] == {"legacy": True}
            assert not (path.parent / "prelaunch-failure").exists()
        return
    assert result["outcome"] == "authentication", result
    assert result["processes_quiescent"] is True
    assert result["usage"] == {} and result["input_complete"] is False
    assert result["request_digest"] == envelope["request_digest"]
    assert result["launch_id"] == envelope["launch_id"]
    assert result["generation"] == envelope["generation"]
    from bmad_loop.adapters.base import SessionHandle
    from bmad_loop.adapters.headless import HeadlessAdapter
    from bmad_loop.adapters.profile import get_profile
    from bmad_loop.campaign_interruptions import execution_interruption, session_interruption
    from bmad_loop.policy import Policy

    adapter = HeadlessAdapter(
        run_dir=tmp_path, profile=get_profile("codex"), policy=Policy(), mux=prepared[0].mux
    )
    adapter.requests["job"] = path
    adapter.runner = SimpleNamespace(observe=lambda _: {"state": "finished", "receipt": result})
    session = adapter.wait_for_completion(
        SessionHandle("job", "owned-window"), SimpleNamespace(timeout_s=1)
    )
    assert session.transport_fault == "environment"
    assert "sign-in required" in session.transport_evidence
    assert session.native_binding["headless_receipt_sha256"] == file_digest(
        path.parent / "receipt.json"
    )
    assert session_interruption(session)["kind"] == "authentication"
    assert (
        execution_interruption(
            [
                {
                    "status": session.status,
                    "native_binding": session.native_binding,
                    "ended": "environment",
                }
            ]
        )["kind"]
        == "authentication"
    )


def test_fixed_authentication_reaches_both_existing_interruption_paths():
    from bmad_loop.adapters.base import SessionResult
    from bmad_loop.campaign_interruptions import execution_interruption, session_interruption

    binding = {"outcome": "authentication", "headless_receipt_sha256": "a" * 64}
    result = SessionResult(
        "crashed",
        transport_fault="environment",
        transport_evidence="CANONICAL_REFRESH_QUARANTINED: owner sign-in required",
        native_binding=binding,
    )
    stage = session_interruption(result)
    execution = execution_interruption(
        [{"status": "crashed", "native_binding": binding, "ended": "environment"}]
    )
    assert stage["kind"] == execution["kind"] == "authentication"
    assert stage["native_binding"] == execution["native_binding"] == binding
    assert "CANONICAL_REFRESH_QUARANTINED" in stage["reason"]
