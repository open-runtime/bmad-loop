"""Zero-token native-job transport contracts with real disposable children."""

from __future__ import annotations

import json
import shlex
import shutil
import subprocess
import sys
import time
import uuid
from dataclasses import replace
from pathlib import Path

import pytest
from conftest import real_mux_e2e

from bmad_loop.campaign_headless import (
    HeadlessRunner,
    capability_for,
    file_digest,
    load_capabilities,
    read_json,
    render_input,
    validate_capability,
    write_json,
)
from bmad_loop.campaign_headless_protocol import parse_output
from bmad_loop.campaign_jobs import JobLimits, JobRequest
from bmad_loop.campaign_model import CampaignError, RoleCandidate


class ChildMux:
    """Test transport: each window is an actual owned child, never an LLM."""

    def __init__(self):
        self.processes = {}
        self.names = {}
        self.drop_ack = False

    def has_session(self, session):
        return True

    def new_window(self, session, name, cwd, env, command):
        identifier = str(len(self.processes) + 1)
        argv = shlex.split(command)
        log = (cwd / "window.log").open("wb")
        try:
            self.processes[identifier] = subprocess.Popen(
                argv, cwd=cwd, stdout=log, stderr=subprocess.STDOUT
            )
        finally:
            log.close()
        self.names[identifier] = name
        if self.drop_ack:
            self.drop_ack = False
            raise OSError("lost window acknowledgement")
        return identifier

    def list_windows(self, session, fields):
        return [
            (key, self.names[key])
            for key, process in self.processes.items()
            if process.poll() is None
        ]

    def window_alive(self, session, identifier):
        return self.processes[identifier].poll() is None


def fixture_capability(tmp_path, capability, candidate=None):
    """Construct full synthetic canary evidence, never a production proof bypass."""
    from bmad_loop.campaign_qualification import result

    proposal = {key: value for key, value in capability.items() if key != "qualification"}
    candidate = candidate or RoleCandidate(
        proposal["profile"], proposal["model"], proposal["efforts"][0]
    )
    runner = HeadlessRunner(ChildMux())
    path = runner.prepare_probe(
        tmp_path / ("fixture-proof-" + uuid.uuid4().hex),
        candidate,
        proposal,
        input_bytes=min(90000, proposal["input_tokens"] - 4096),
        output_tokens=proposal["output_tokens"],
    )
    envelope = read_json(path)
    receipt = {
        "schema_version": 1,
        "job_id": envelope["request"]["id"],
        "launch_id": envelope["launch_id"],
        "generation": envelope["generation"],
        "request_digest": envelope["request_digest"],
        "outcome": "completed",
        "quiescent": True,
        "processes_quiescent": True,
        "completion_evidence": "owned-window-death-and-process-readback",
        "input_complete": True,
        "input_delivery": envelope["input"],
        "selected_model": candidate.model,
        "observed_model": candidate.model,
        "observed_models": [candidate.model],
        "session_id": "synthetic-canary",
        "turns": 1,
        "usage": {"input_tokens": 1000, "output_tokens": 8},
        "result": envelope["probe"]["expected"],
    }
    write_json(path.parent / "receipt.json", receipt)
    report = result(path, {"state": "finished", "receipt": receipt})
    return {
        **proposal,
        "input_tokens": report["coverage"]["input_tokens"],
        "max_turns": 1,
        "qualification": {
            "status": "passed",
            "path": report["evidence_path"],
            "sha256": file_digest(Path(report["evidence_path"])),
            "input_tokens": report["coverage"]["input_tokens"],
            "output_tokens": report["coverage"]["configured_output_tokens"],
            "observed_output_tokens": report["coverage"]["output_tokens"],
            "native_output_limit_enforced": False,
            "served_models": [candidate.model],
            "checks": [
                "full_input",
                "no_tools",
                "window_completion",
                "quiescence",
                "model_attribution",
                "structured_output",
                "turn_bound",
                "output_bound",
            ],
        },
    }


@pytest.fixture
def prepared(tmp_path):
    source = tmp_path / "source.txt"
    source.write_text("owner obligation\n" * 2000)
    script = tmp_path / "native.py"
    script.write_text("""import hashlib,json,sys,time
payload=sys.stdin.buffer.read()
time.sleep(0.3)
print(json.dumps({"type":"system","model":"gpt-test","session_id":"native-test"}))
print(json.dumps({"type":"result","structured_output":{"digest":hashlib.sha256(payload).hexdigest(),"bytes":len(payload)},"usage":{"input_tokens":12,"output_tokens":8}}))
""")
    binary = Path(sys.executable).resolve()
    candidate = RoleCandidate("codex", "gpt-test", "high", "openai")
    cap = {
        "schema_version": 1,
        "platform": sys.platform,
        "profile": "codex",
        "model": "gpt-test",
        "protocol": "bmad-test-v1",
        "cli_version": "fixture-v1",
        "binary": str(binary),
        "binary_sha256": file_digest(binary),
        "dependencies": [{"path": str(script), "sha256": file_digest(script)}],
        "argv": [str(binary), str(script)],
        "efforts": ["high"],
        "permissions": "no-tools",
        "process_containment": "foreground-contained",
        "input_mode": "text-stdin",
        "tokenizer": "utf8-byte-upper-bound",
        "context_tokens": 200000,
        "input_tokens": 100000,
        "output_tokens": 4096,
        "reserved_tokens": 4096,
        "max_turns": 4,
    }
    cap = fixture_capability(tmp_path, cap, candidate)
    request = JobRequest(
        "job-1",
        "handoff_map",
        "summary",
        {"head": "", "base": "", "graph_revision": 1, "config_revision": 1},
        (
            {
                "source_id": "owner",
                "path": str(source),
                "sha256": file_digest(source),
                "tokens": 10000,
            },
        ),
        "Extract obligations",
        {
            "type": "object",
            "properties": {"digest": {"type": "string"}, "bytes": {"type": "integer"}},
            "required": ["digest", "bytes"],
        },
        JobLimits(100000, 4096, max_turns=1, timeout_s=15),
        candidates=(candidate,),
    )
    mux = ChildMux()
    runner = HeadlessRunner(mux)
    path = runner.prepare(
        tmp_path / "job", request, candidate, cap, launch_id="launch-1", generation=1
    )
    yield runner, path, request, candidate, cap
    for process in mux.processes.values():
        if process.poll() is None:
            process.terminate()
        process.wait(timeout=10)


def finish(runner, path):
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        result = runner.observe(path)
        if result["state"] == "finished":
            return result["receipt"]
        time.sleep(0.03)
    pytest.fail(str(result) + (path.parent / "window.log").read_text())


def test_complete_input_arrives_on_stdin_and_completion_requires_quiescence(prepared):
    runner, path, request, candidate, cap = prepared
    assert runner.launch(path)["state"] == "running"
    receipt = finish(runner, path)
    expected = read_json(path)["input"]
    assert receipt["outcome"] == "completed", receipt
    assert receipt["quiescent"] is True
    assert receipt["result"] == {"digest": expected["sha256"], "bytes": expected["bytes"]}
    assert receipt["observed_model"] == candidate.model
    assert receipt["input_complete"] is True
    assert len(runner.mux.processes) == 1
    assert runner.launch(path)["state"] == "finished"
    assert len(runner.mux.processes) == 1


@pytest.mark.parametrize("configured,managed", [(False, False), (True, False), (True, True)])
def test_headless_room_is_explicit_and_inherited_actor_identity_is_removed(
    prepared, tmp_path, monkeypatch, configured, managed
):
    runner, _, request, candidate, cap = prepared
    script = tmp_path / "native-environment.py"
    script.write_text("""import hashlib,json,os,sys
payload=sys.stdin.buffer.read()
keys=('HCOM_DIR','HCOM_INSTANCE_NAME','HCOM_PROCESS_ID','HCOM_NAME','HCOM_LAUNCHED','HCOM_NOTES','SUPERSET_TERMINAL_ID','BMAD_LOOP_TASK_ID')
print(json.dumps({'type':'system','model':'gpt-test','session_id':'room-native'}))
print(json.dumps({'type':'result','structured_output':{'digest':hashlib.sha256(payload).hexdigest(),'bytes':len(payload),'environment':{key:os.environ[key] for key in keys if key in os.environ}},'usage':{'input_tokens':12,'output_tokens':8}}))
""")
    cap = fixture_capability(
        tmp_path,
        {
            **cap,
            "argv": [cap["binary"], str(script)],
            "dependencies": [{"path": str(script), "sha256": file_digest(script)}],
        },
    )
    room = str(tmp_path / "campaign-room") if configured else ""
    for key in (
        "HCOM_DIR",
        "HCOM_INSTANCE_NAME",
        "HCOM_PROCESS_ID",
        "HCOM_NAME",
        "HCOM_LAUNCHED",
        "HCOM_NOTES",
    ):
        monkeypatch.setenv(key, "inherited-parent")
    monkeypatch.setenv("SUPERSET_TERMINAL_ID", "parent-terminal")
    path = runner.prepare(
        tmp_path / "room-job",
        request,
        candidate,
        cap,
        launch_id="room-launch",
        generation=1,
        communication_room=room,
        managed_binding=(
            {
                "kind": "bridge",
                "name": "nava",
                "room": room,
                "target": {"launch_id": "parent-claim"},
            }
            if managed
            else None
        ),
        environment={"BMAD_LOOP_TASK_ID": "owned-task"},
    )
    runner.launch(path)
    receipt = finish(runner, path)
    assert receipt["outcome"] == "completed", receipt
    assert receipt["result"]["environment"] == {
        "BMAD_LOOP_TASK_ID": "owned-task",
        **(
            {
                "HCOM_DIR": (
                    str(path.parent / "sandbox-runtime" / "hcom")
                    if managed
                    else str(Path(room).resolve())
                )
            }
            if configured
            else {}
        ),
    }
    # Empty rooms retain the preexisting wire shape and do not infer a parent room.
    assert ("communication_room" in read_json(path)) is configured
    if managed:
        assert receipt["managed_communication"]["name"] == "nava"
        assert receipt["native_communication_room"] != room


def test_managed_identity_and_steering_are_sealed_before_native_launch(prepared, tmp_path):
    runner, _, request, candidate, cap = prepared
    room = str(tmp_path / "public-room")
    path = runner.prepare(
        tmp_path / "sealed-managed",
        request,
        candidate,
        cap,
        launch_id="managed-launch",
        generation=1,
        communication_room=room,
        managed_binding={
            "kind": "bridge",
            "name": "nava",
            "room": room,
            "target": {"launch_id": "parent-claim"},
        },
        steering=[{"id": "owner", "text": "Preserve acceptance", "authority": "advisory"}],
    )
    raw = read_json(path)
    assert b"Preserve acceptance" in (path.parent / "input.txt").read_bytes()
    raw["managed_binding"]["name"] = "other"
    write_json(path, raw)
    runner.launch(path)
    receipt = finish(runner, path)
    assert receipt["outcome"] == "failed"
    assert "managed communication binding changed" in receipt["reason"]
    assert not (path.parent / "stdout.jsonl").exists()


@pytest.mark.parametrize("tamper", ["replace", "remove"])
def test_headless_room_is_frozen_and_validated_before_native_launch(prepared, tmp_path, tamper):
    from bmad_loop.campaign_headless_worker import child_main

    runner, _, request, candidate, cap = prepared
    directory = tmp_path / "room-job"
    room = str(tmp_path / "room")
    with pytest.raises(CampaignError, match="absolute path"):
        runner.prepare(
            directory,
            request,
            candidate,
            cap,
            launch_id="room",
            generation=1,
            communication_room="relative-room",
        )
    path = runner.prepare(
        directory,
        request,
        candidate,
        cap,
        launch_id="room",
        generation=1,
        communication_room=room,
    )
    assert (
        runner.prepare(
            directory,
            request,
            candidate,
            cap,
            launch_id="room",
            generation=1,
            communication_room=room,
        )
        == path
    )
    with pytest.raises(CampaignError, match="immutable"):
        runner.prepare(
            directory,
            request,
            candidate,
            cap,
            launch_id="room",
            generation=1,
            communication_room=str(tmp_path / "other-room"),
        )
    raw = read_json(path)
    if tamper == "replace":
        raw["communication_room"] = str(tmp_path / "tampered-room")
    else:
        raw.pop("communication_room")
    write_json(path, raw)
    write_json(directory / "go.json", {"launch_id": "room", "generation": 1})
    assert child_main(path) == 1
    receipt = read_json(directory / "receipt.json")
    assert "communication room changed" in receipt["reason"]
    assert receipt["processes_quiescent"] and not receipt["input_complete"]


def test_lost_launch_ack_adopts_the_same_window(prepared):
    runner, path, *_ = prepared
    runner.mux.drop_ack = True
    with pytest.raises(OSError, match="lost"):
        runner.launch(path)
    assert runner.launch(path)["state"] == "running"
    assert finish(runner, path)["outcome"] == "completed"
    assert len(runner.mux.processes) == 1


def test_cancel_before_acknowledgement_does_not_start_native_cli(prepared):
    runner, path, *_ = prepared
    runner.terminate(path)
    runner.launch(path)
    receipt = finish(runner, path)
    assert receipt["outcome"] == "canceled"
    assert receipt["quiescent"] is True
    assert not (path.parent / "stdout.jsonl").exists()


def test_changed_input_or_binary_cannot_launch(prepared):
    runner, path, request, candidate, cap = prepared
    Path(request.inputs[0]["path"]).write_text("changed")
    with pytest.raises(CampaignError, match="changed"):
        render_input(request, cap)
    cap["binary_sha256"] = "0" * 64
    with pytest.raises(CampaignError, match="executable changed"):
        validate_capability(cap, candidate)


def test_context_bound_includes_prompt_schema_and_output_reservation(prepared):
    _, _, request, _, cap = prepared
    small = replace(request, limits=JobLimits(10000, 4096))
    with pytest.raises(CampaignError, match="input token bound"):
        render_input(small, cap)
    cap = {**cap, "context_tokens": 33000}
    with pytest.raises(CampaignError, match="insufficient"):
        render_input(request, cap)


def test_capability_mode_never_upgrades_no_tools_to_execution(prepared, tmp_path):
    _, _, _, candidate, cap = prepared
    routes = {candidate.profile + ":" + candidate.model: {"reasoning": cap}}
    assert capability_for(routes, candidate.profile, candidate.model) == cap
    with pytest.raises(CampaignError, match="No qualified"):
        capability_for(routes, candidate.profile, candidate.model, "workspace-tools")
    path = tmp_path / "capabilities.json"
    write_json(path, {"schema_version": 1, "routes": routes})
    assert load_capabilities(path) == routes


@pytest.mark.parametrize(
    "model,family",
    [
        ("gpt-test", "openai"),
        ("claude-test", "anthropic"),
        ("gemini-test", "google"),
        ("grok-test", "xai"),
        ("muse-test", "meta"),
    ],
)
def test_model_families_and_auxiliary_telemetry(model, family):
    frames = [
        {"type": "system", "model": model, "session_id": "native"},
        {"type": "session", "model": "gpt-helper"},
        {"type": "result", "structured_output": {"done": True}},
    ]
    result = parse_output(
        "\n".join(json.dumps(row) for row in frames), protocol="claude-stream", selected_model=model
    )
    assert result["provider_family"] == family
    assert result["observed_model"] == model


def test_only_native_error_envelopes_can_claim_quota():
    raw = json.dumps({"type": "turn.failed", "error": {"code": "rate_limit_exceeded"}})
    assert (
        parse_output(raw, protocol="codex-jsonl", selected_model="gpt-test")["outcome"] == "quota"
    )
    text = "\n".join(
        json.dumps(row)
        for row in [
            {"type": "system", "model": "gpt-test", "session_id": "native"},
            {"type": "result", "structured_output": {"error": {"code": "rate_limit_exceeded"}}},
        ]
    )
    assert (
        parse_output(text, protocol="claude-stream", selected_model="gpt-test")["outcome"]
        == "completed"
    )


def test_native_model_mismatch_and_truncation_are_not_successful():
    text = json.dumps({"type": "result", "model": "claude-other", "session_id": "n", "result": {}})
    with pytest.raises(CampaignError, match="differs"):
        parse_output(text, protocol="claude-stream", selected_model="gpt-test")
    result = parse_output(
        json.dumps({"type": "result", "finish_reason": "length"}),
        protocol="claude-stream",
        selected_model="gpt-test",
    )
    assert result["outcome"] == "limit" and result["truncated"]


def test_native_grok_schema_rejection_preserves_telemetry_without_success():
    frame = {
        "sessionId": "native-schema-rejection",
        "stopReason": "end_turn",
        "modelUsage": {"grok-test": {"modelCalls": 1}},
        "num_turns": 1,
        "usage": {"input_tokens": 1234, "output_tokens": 789},
        "structuredOutput": None,
        "structuredOutputError": "citation is not in the enum " + "x" * 10000,
        "text": '{"citations":["invented"]}',
    }
    result = parse_output(json.dumps(frame), protocol="grok-json", selected_model="grok-test")
    assert result["outcome"] == "failed" and result["failure_kind"] == "schema"
    assert result["result"] is None and result["native_terminal_completed"]
    assert result["session_id"] == frame["sessionId"]
    assert result["usage"] == frame["usage"] and len(result["reason"]) < 600
    with pytest.raises(CampaignError, match="differs"):
        parse_output(json.dumps(frame), protocol="grok-json", selected_model="other")


def grok_messages():
    return [
        {"type": "system", "subtype": "init", "session_id": "native", "model": "grok-alias"},
        {
            "type": "assistant",
            "session_id": "native",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "Working on it"}],
                "usage": {"output_tokens": 999},
            },
        },
        {
            "type": "result",
            "session_id": "native",
            "is_error": False,
            "stop_reason": "end_turn",
            "result": '{"answer":true}',
            "num_turns": 4,
            "modelUsage": {"grok-served": {"inputTokens": 1234, "outputTokens": 789}},
            "usage": {"input_tokens": 1234, "output_tokens": 789},
        },
    ]


def test_grok_messages_use_native_final_result_and_actual_cumulative_model_telemetry():
    frames = grok_messages()
    result = parse_output(
        "\n".join(json.dumps(row) for row in frames),
        protocol="grok-json",
        selected_model="grok-alias",
        served_model_aliases=("grok-served",),
    )
    assert result["outcome"] == "completed" and result["result"] == {"answer": True}
    assert result["observed_model"] == "grok-served" and result["turns"] == 4
    assert result["usage"] == frames[-1]["usage"]
    frames[-1]["result"] = 'Working on it {"answer":true}'
    failed = parse_output(json.dumps(frames), protocol="grok-json", selected_model="grok-served")
    assert failed["outcome"] == "failed" and failed["result"] is None


@pytest.mark.parametrize(
    "invalid", ["session", "after-terminal", "missing-terminal", "turns", "model"]
)
def test_grok_messages_refuse_missing_or_ambiguous_native_evidence(invalid):
    frames = grok_messages()
    if invalid == "session":
        frames[-1]["session_id"] = "other"
    elif invalid == "after-terminal":
        frames.append(frames[-1])
    elif invalid == "missing-terminal":
        frames.pop()
    elif invalid == "turns":
        frames[-1].pop("num_turns")
    else:
        frames[-1]["modelUsage"] = {}
    with pytest.raises(CampaignError):
        parse_output(json.dumps(frames), protocol="grok-json", selected_model="grok-served")


@pytest.mark.parametrize("bound", ["stdout-wire-bytes", "stderr-bytes", "wall-time"])
def test_finished_native_child_cannot_bypass_final_whole_job_bounds(
    prepared, tmp_path, monkeypatch, bound
):
    from types import SimpleNamespace

    from bmad_loop import campaign_headless_worker as worker

    runner, _, request, candidate, cap = prepared
    script = tmp_path / "fast-native.py"
    script.write_text(
        """import hashlib,json,sys,time
payload=sys.stdin.buffer.read()
time.sleep(.35)
print(json.dumps({'type':'system','model':'gpt-test','session_id':'fast-native'}))
print(json.dumps({'type':'result','structured_output':{'digest':hashlib.sha256(payload).hexdigest(),'bytes':len(payload)},'usage':{'input_tokens':12,'output_tokens':8},'padding':'x'*STDOUT_BYTES}))
sys.stderr.write('x'*STDERR_BYTES)
""".replace("STDOUT_BYTES", "500000" if bound == "stdout-wire-bytes" else "0").replace(
            "STDERR_BYTES", "1100000" if bound == "stderr-bytes" else "0"
        )
    )
    cap = fixture_capability(
        tmp_path,
        {
            **cap,
            "argv": [cap["binary"], str(script)],
            "dependencies": [{"path": str(script), "sha256": file_digest(script)}],
        },
    )
    path = runner.prepare(
        tmp_path / "fast-job", request, candidate, cap, launch_id="fast", generation=1
    )
    write_json(path.parent / "go.json", {"launch_id": "fast", "generation": 1})
    original = subprocess.Popen

    def launched(*args, **kwargs):
        process = original(*args, **kwargs)
        if args and args[0] == cap["argv"]:
            poll = process.poll
            first = True

            def after_exit():
                nonlocal first
                if first:
                    first = False
                    process.wait(timeout=5)
                return poll()

            process.poll = after_exit
        return process

    monkeypatch.setattr(worker.subprocess, "Popen", launched)
    if bound == "wall-time":
        clock = iter((0, request.limits.timeout_s + 1))
        monkeypatch.setattr(
            worker,
            "time",
            SimpleNamespace(monotonic=lambda: next(clock), sleep=time.sleep, time=time.time),
        )
    assert worker.child_main(path) == 1
    receipt = read_json(path.parent / "receipt.json")
    assert receipt["outcome"] == "limit" and receipt["limit_kind"] == bound
    assert receipt["usage"] == {"input_tokens": 12, "output_tokens": 8}
    assert receipt["session_id"] == "fast-native" and receipt["processes_quiescent"]
    assert not receipt["input_complete"] and receipt.get("result") is None


def test_live_descendant_prevents_completion_even_after_window_death(prepared, monkeypatch):
    runner, path, *_ = prepared
    runner.launch(path)
    receipt = finish(runner, path)
    assert receipt["outcome"] == "completed"
    from bmad_loop import campaign_headless

    real_host = campaign_headless.get_process_host()

    class UnknownHost:
        def liveness_of(self, pid, identity):
            return "unknown"

    monkeypatch.setattr(campaign_headless, "get_process_host", lambda: UnknownHost())
    assert runner.observe(path)["state"] == "unknown"
    monkeypatch.setattr(campaign_headless, "get_process_host", lambda: real_host)
    assert runner.observe(path)["state"] == "finished"


@pytest.mark.skipif(sys.platform != "darwin", reason="qualified writer requires the macOS sandbox")
def test_headless_adapter_handoff_and_grants_reach_native_stdin(prepared, tmp_path):
    from test_campaign_execution_qualification import writer

    from bmad_loop.adapters.base import SessionSpec
    from bmad_loop.adapters.headless import HeadlessAdapter
    from bmad_loop.adapters.profile import get_profile
    from bmad_loop.campaign_execution_qualification import qualify
    from bmad_loop.policy import Policy

    runner, _, request, candidate, _ = prepared
    proposal, _, cap = writer.__wrapped__(prepared, tmp_path)
    script = Path(cap["dependencies"][0]["path"])
    body = script.read_text(encoding="utf-8")
    body = body.replace("import json,re,subprocess,sys", "import hashlib,json,re,subprocess,sys")
    body = body.replace(
        "nonce=re.search(r'QUALIFICATION NONCE ([a-f0-9]+)',text)[1]",
        """match=re.search(r'QUALIFICATION NONCE ([a-f0-9]+)',text)
if match is None:
    payload=Path(sys.argv[1]).read_bytes()
    print(json.dumps({'type':'system','model':'gpt-test','session_id':'fixture-successor'}))
    print(json.dumps({'type':'result','structured_output':{'digest':hashlib.sha256(payload).hexdigest(),'bytes':len(payload)},'usage':{'input_tokens':100,'output_tokens':80}}))
    sys.exit(0)
nonce=match[1]""",
    )
    script.write_text(body, encoding="utf-8")
    cap["dependencies"][0]["sha256"] = file_digest(script)
    proposal.write_text(json.dumps(cap), encoding="utf-8")
    report = qualify(
        proposal,
        tmp_path / "writer-proof",
        input_bytes=65536,
        output_tokens=4096,
        timeout_s=30,
        runner=runner,
    )
    assert report["ok"], report
    cap = read_json(Path(report["capability_path"]))
    grants = read_json(Path(report["capability_path"]).parent / "grants.json")
    grants["generation"] = 2
    workspace = Path(grants["workspace"])
    adapter = HeadlessAdapter(
        run_dir=tmp_path / "adapter", profile=get_profile("codex"), policy=Policy(), mux=runner.mux
    )
    adapter.set_capability(cap, grants, request.coordinates, request.inputs[0])
    spec = SessionSpec(
        "execution-task",
        "dev",
        "Continue exactly",
        workspace,
        model=candidate.model,
        effort=candidate.effort,
        timeout_s=15,
        env={"HCOM_DIR": str(tmp_path / "execution-room"), "HCOM_INSTANCE_NAME": "parent"},
    )
    result = adapter.run(spec)
    assert result.status == "completed"
    delivery = result.native_binding["input_delivery"]
    assert delivery["sources"][0]["sha256"] == request.inputs[0]["sha256"]
    assert result.result_json["digest"] == delivery["sha256"]
    assert adapter.read_usage(result).models == (candidate.model,)
    path = adapter.requests[spec.task_id]
    assert read_json(path.parent / "grants.json") == grants
    envelope = read_json(path)
    assert envelope["communication_room"] == str((tmp_path / "execution-room").resolve())
    assert "HCOM_INSTANCE_NAME" not in envelope["environment"]


def test_execution_capability_must_consume_scope_grants(prepared):
    _, _, _, candidate, cap = prepared
    cap = {**cap, "permissions": "workspace-tools"}
    with pytest.raises(CampaignError, match="consume the frozen grants"):
        validate_capability(cap, candidate, permissions="workspace-tools")


def test_headless_hold_prevents_preparation_and_release_allows_one_launch(prepared, tmp_path):
    from bmad_loop import campaign_hold
    from bmad_loop.adapters.base import SessionSpec
    from bmad_loop.adapters.headless import HeadlessAdapter
    from bmad_loop.adapters.multiplexer import MultiplexerError
    from bmad_loop.adapters.profile import get_profile
    from bmad_loop.policy import Policy

    runner, _, request, candidate, cap = prepared
    adapter = HeadlessAdapter(
        run_dir=tmp_path / "held-adapter",
        profile=get_profile("codex"),
        policy=Policy(),
        mux=runner.mux,
    )
    adapter.set_capability(cap, None, request.coordinates)
    spec = SessionSpec(
        "held-stage",
        "planning",
        "Return a JSON result",
        tmp_path,
        model=candidate.model,
        effort=candidate.effort,
        timeout_s=15,
    )
    held = [True]
    token = campaign_hold.install(lambda: held[0])
    try:
        with pytest.raises(MultiplexerError, match="campaign operator hold"):
            adapter.start_session(spec)
        assert not adapter.run_dir.exists()
        assert not runner.mux.processes
        held[0] = False
        handle = adapter.start_session(spec)
        held[0] = True  # A running session keeps its normal completion path.
        result = adapter.wait_for_completion(handle, spec)
        assert result.status == "completed"
        assert len(runner.mux.processes) == 1
    finally:
        campaign_hold.reset(token)


def test_hold_arriving_during_headless_preparation_prevents_native_launch(prepared, tmp_path):
    from bmad_loop import campaign_hold
    from bmad_loop.adapters.base import SessionSpec
    from bmad_loop.adapters.headless import HeadlessAdapter
    from bmad_loop.adapters.multiplexer import MultiplexerError
    from bmad_loop.adapters.profile import get_profile
    from bmad_loop.policy import Policy

    runner, _, request, candidate, cap = prepared
    adapter = HeadlessAdapter(
        run_dir=tmp_path / "held-late",
        profile=get_profile("codex"),
        policy=Policy(),
        mux=runner.mux,
    )
    adapter.set_capability(cap, None, request.coordinates)
    spec = SessionSpec(
        "late-stage",
        "planning",
        "Return a JSON result",
        tmp_path,
        model=candidate.model,
        effort=candidate.effort,
        timeout_s=15,
    )
    held = [False]
    prepare = adapter.runner.prepare

    def prepare_then_hold(*args, **kwargs):
        path = prepare(*args, **kwargs)
        held[0] = True
        return path

    adapter.runner.prepare = prepare_then_hold
    token = campaign_hold.install(lambda: held[0])
    try:
        with pytest.raises(MultiplexerError, match="campaign operator hold"):
            adapter.start_session(spec)
        path = adapter.requests[spec.task_id]
        assert path.is_file()
        assert not (path.parent / "launch-intent.json").exists()
        assert not runner.mux.processes
    finally:
        campaign_hold.reset(token)


def test_headless_profiles_are_explicitly_qualified():
    from bmad_loop.adapters.profile import get_profile
    from bmad_loop.adapters.registry import get_adapter_kind

    for name in ("grok", "muse"):
        profile = get_profile(name)
        assert profile.adapter == "bounded-headless"
        assert get_adapter_kind(profile.adapter).load().plain.__name__ == "HeadlessAdapter"


def test_child_stage_hold_after_startup_is_receipted_and_release_completes(
    prepared, project, tmp_path, monkeypatch
):
    from test_campaign_workers import envelope, worker_request

    from bmad_loop import campaign_hold, campaign_workers, runsetup
    from bmad_loop.adapters.headless import HeadlessAdapter
    from bmad_loop.adapters.profile import get_profile

    runner, _, job, candidate, original = prepared
    script = tmp_path / "completed-stage.py"
    script.write_text(
        "import json,sys\n"
        "sys.stdin.buffer.read()\n"
        "print(json.dumps({'type':'system','model':'gpt-test','session_id':'released-stage'}))\n"
        "print(json.dumps({'type':'result','structured_output':{'status':'done'},"
        "'usage':{'input_tokens':12,'output_tokens':8}}))\n"
    )
    cap = fixture_capability(
        tmp_path,
        {
            **original,
            "argv": [original["binary"], str(script)],
            "dependencies": [{"path": str(script), "sha256": file_digest(script)}],
        },
        candidate,
    )
    channel = tmp_path / "operator-hold.json"
    hold_during_construction = True
    contexts = []

    def make_adapters(workspace, run_dir, policy, *, profiles, **kwargs):
        context = contexts[-1]
        assert (context.directory / "startup.json").exists()
        if hold_during_construction:
            campaign_hold.write(
                channel, campaign=context.campaign_id, unit=context.unit_id, hold="operator"
            )
        return {
            "triage": HeadlessAdapter(
                run_dir=run_dir, profile=profiles["triage"], policy=policy, mux=runner.mux
            )
        }

    monkeypatch.setattr(runsetup, "make_adapters", make_adapters)
    for attempt, outcome in ((1, "operator_held"), (2, "stage_complete")):
        context = replace(
            envelope(project.project, tmp_path / f"stage-attempt-{attempt}"), attempt=attempt
        )
        context = replace(
            context, profiles={**context.profiles, "selected": get_profile(candidate.profile)}
        )
        contexts.append(context)
        path = campaign_workers.prepare_launch(
            context,
            worker_request(
                project.project,
                context,
                launch_id=f"stage-launch-{attempt}",
                kind="stage",
                role="planning_review",
                candidate={
                    "profile": candidate.profile,
                    "model": candidate.model,
                    "effort": candidate.effort,
                },
                profile_key="selected",
                prompt="Review the current planning artifact.",
                read_only=True,
                timeout_s=15,
                hold_channel=str(channel),
                headless_capabilities={
                    candidate.profile + ":" + candidate.model: {"reasoning": cap}
                },
                headless_coordinates=job.coordinates,
            ),
        )
        code = campaign_workers.child_main(path)
        receipt = read_json(context.directory / "receipt.json")
        assert receipt["outcome"] == outcome, receipt
        assert code == (1 if attempt == 1 else 0)
        if attempt == 1:
            assert not runner.mux.processes
            assert not receipt["accounting"]["session_observed"]
            campaign_hold.write(
                channel, campaign=context.campaign_id, unit=context.unit_id, hold=""
            )
            hold_during_construction = False
        else:
            assert len(runner.mux.processes) == 1
            assert receipt["accounting"]["session_observed"]
            assert receipt["observed_model"] == candidate.model


def test_native_interruptions_survive_without_model_result():
    from bmad_loop.adapters.base import SessionResult
    from bmad_loop.campaign_interruptions import execution_interruption, session_interruption

    result = SessionResult(
        "crashed",
        session_id="before",
        usage_limited="provider quota",
        usage_limit_resets="tomorrow",
    )
    assert session_interruption(result)["kind"] == "quota"
    assert session_interruption(result)["retry_at"] == "tomorrow"
    assert (
        execution_interruption([{"status": "crashed", "ended": "pool", "session_id": "before"}])[
            "kind"
        ]
        == "quota"
    )
    assert (
        execution_interruption([{"status": "crashed", "ended": "usage"}, {"status": "completed"}])
        is None
    )
    assert (
        execution_interruption([{"status": "crashed", "ended": "parked"}])["kind"] == "owner_input"
    )


def test_attribution_preserves_predecessor_and_all_writer_families():
    from bmad_loop.campaign_attribution import development

    receipt = {
        "sessions": [
            {"role": "dev", "status": "crashed", "models": ["claude-writer"], "ended": "usage"},
            {
                "role": "dev",
                "status": "completed",
                "native_binding": {
                    "observed_model": "gpt-writer",
                    "observed_models": ["gpt-writer", "gemini-helper"],
                },
            },
            {"role": "review", "status": "completed", "models": ["muse-reviewer"]},
        ]
    }
    identity = development(receipt)
    assert identity["provider_family"] == "openai"
    assert identity["provider_families"] == ["anthropic", "google", "openai"]


@pytest.mark.skipif(
    sys.platform == "win32" or shutil.which("tmux") is None,
    reason="zero-token native window test requires tmux",
)
@real_mux_e2e
def test_real_mux_native_window_death_owns_completion(prepared, private_tmux_server):
    from bmad_loop.adapters.tmux_backend import TmuxMultiplexer

    _, path, *_ = prepared
    runner = HeadlessRunner(TmuxMultiplexer())
    assert runner.launch(path)["state"] == "running"
    receipt = finish(runner, path)
    assert receipt["outcome"] == "completed", receipt
    assert receipt["completion_evidence"] == "owned-window-death-and-process-readback"


def test_worker_selects_frozen_headless_route_and_keeps_legacy_other_roles(
    prepared, project, monkeypatch
):
    from test_campaign_workers import envelope

    from bmad_loop import campaign_workers, runsetup
    from bmad_loop.adapters.headless import HeadlessAdapter
    from bmad_loop.policy import AdapterPolicy, Policy, StageAdapterPolicy

    runner, _, request, candidate, cap = prepared
    policy = Policy(
        adapter=AdapterPolicy(
            triage=StageAdapterPolicy(name="codex", model=candidate.model, effort=candidate.effort)
        )
    )
    context = envelope(project.project, project.project / ".bmad-loop" / "worker", policy)
    captured = {}

    def create(project, run_dir, policy, **kwargs):
        captured.update(kwargs["profiles"])
        return {
            "triage": HeadlessAdapter(
                run_dir=run_dir, profile=kwargs["profiles"]["triage"], policy=policy, mux=runner.mux
            )
        }

    monkeypatch.setattr(runsetup, "make_adapters", create)
    campaign_workers._adapters(
        {
            "read_only": True,
            "headless_capabilities": {"codex:" + candidate.model: {"reasoning": cap}},
            "headless_coordinates": request.coordinates,
        },
        context,
        project.project,
        context.directory,
        policy,
        profiles=context.profiles,
    )
    assert captured["triage"].adapter == "bounded-headless"
    assert captured["dev"].adapter == context.profiles["dev"].adapter
    with pytest.raises(CampaignError, match="Handoff requires"):
        campaign_workers._adapters(
            {"handoff_packet": request.inputs[0]},
            context,
            project.project,
            context.directory,
            policy,
        )
