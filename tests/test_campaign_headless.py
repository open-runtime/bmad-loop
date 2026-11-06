"""Zero-token native-job transport contracts with real disposable children."""

from __future__ import annotations

import json
import shlex
import shutil
import subprocess
import sys
import time
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


@pytest.fixture
def prepared(tmp_path):
    source = tmp_path / "source.txt"
    source.write_text("owner obligation\n" * 2000)
    evidence = tmp_path / "qualification.json"
    evidence.write_text('{"fixture":"zero-token protocol"}')
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
        "qualification": {
            "status": "passed",
            "path": str(evidence),
            "sha256": file_digest(evidence),
            "input_tokens": 100000,
            "output_tokens": 4096,
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
        JobLimits(100000, 4096, timeout_s=15),
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


def test_headless_adapter_handoff_and_grants_reach_native_stdin(prepared, tmp_path):
    from bmad_loop.adapters.base import SessionSpec
    from bmad_loop.adapters.headless import HeadlessAdapter
    from bmad_loop.adapters.profile import get_profile
    from bmad_loop.policy import Policy

    runner, _, request, candidate, original = prepared
    cap = {
        **original,
        "permissions": "workspace-tools",
        "argv": [*original["argv"], "{grants_path}"],
        "qualification": {
            **original["qualification"],
            "checks": [
                "scoped_tools" if value == "no_tools" else value
                for value in original["qualification"]["checks"]
            ],
        },
    }
    grants = {
        "schema_version": 1,
        "workspace": str(tmp_path),
        "scopes": ["src"],
        "config_digest": "frozen",
        "generation": 2,
    }
    adapter = HeadlessAdapter(
        run_dir=tmp_path / "adapter", profile=get_profile("codex"), policy=Policy(), mux=runner.mux
    )
    adapter.set_capability(cap, grants, request.coordinates, request.inputs[0])
    spec = SessionSpec(
        "execution-task",
        "dev",
        "Continue exactly",
        tmp_path,
        model=candidate.model,
        effort=candidate.effort,
        timeout_s=15,
    )
    result = adapter.run(spec)
    assert result.status == "completed"
    delivery = result.native_binding["input_delivery"]
    assert delivery["sources"][0]["sha256"] == request.inputs[0]["sha256"]
    assert result.result_json["digest"] == delivery["sha256"]
    assert adapter.read_usage(result).models == (candidate.model,)
    path = adapter.requests[spec.task_id]
    assert read_json(path.parent / "grants.json") == grants


def test_execution_capability_must_consume_scope_grants(prepared):
    _, _, _, candidate, cap = prepared
    cap = {**cap, "permissions": "workspace-tools"}
    with pytest.raises(CampaignError, match="consume the frozen grants"):
        validate_capability(cap, candidate, permissions="workspace-tools")


def test_headless_profiles_are_explicitly_qualified():
    from bmad_loop.adapters.profile import get_profile
    from bmad_loop.adapters.registry import get_adapter_kind

    for name in ("grok", "muse"):
        profile = get_profile(name)
        assert profile.adapter == "bounded-headless"
        assert get_adapter_kind(profile.adapter).load().plain.__name__ == "HeadlessAdapter"


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
