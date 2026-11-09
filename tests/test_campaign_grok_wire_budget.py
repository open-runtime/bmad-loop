"""Grok tool telemetry has a finite allowance independent of model output."""

import json
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest
from test_campaign_execution_qualification import writer as writer
from test_campaign_headless import ChildMux, fixture_capability
from test_campaign_headless import prepared as prepared
from test_campaign_readonly_qualification import readonly as readonly

from bmad_loop import campaign_headless_worker as worker
from bmad_loop.campaign_execution_qualification import qualify
from bmad_loop.campaign_headless import (
    HeadlessRunner,
    file_digest,
    read_json,
    readonly_grants,
    write_json,
)
from bmad_loop.campaign_jobs import JobLimits, JobRequest
from bmad_loop.campaign_model import RoleCandidate


def messages(kind):
    output = 4097 if kind == "output" else 8
    turns = 3 if kind == "turns" else 2
    return [
        {"type": "system", "subtype": "init", "model": "grok-test", "session_id": "native"},
        {
            "type": "assistant",
            "session_id": "native",
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "thinking",
                        "thinking": "Inspect the source",
                        "signature": "s" * 30000,
                    },
                    {
                        "type": "tool_use",
                        "id": "read",
                        "name": "read_file",
                        "input": {"path": "src/owned.txt"},
                    },
                ],
            },
        },
        {
            "type": "user",
            "session_id": "native",
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "read",
                        "content": "x" * (2000000 if kind == "wire" else 400000),
                    }
                ],
            },
        },
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "stop_reason": "end_turn",
            "num_turns": turns,
            "session_id": "native",
            "structured_output": {"answer": True},
            "usage": {"input_tokens": 20, "output_tokens": output},
            "modelUsage": {"grok-test": {"inputTokens": 20, "outputTokens": output}},
        },
    ]


def script_text(rows, *, hold_overflow=False):
    # Scripted native stream: no real harness or inference is invoked.
    if hold_overflow:
        return (
            "import json,time\nfor row in "
            + repr(rows[:-1])
            + ":\n print(json.dumps(row), flush=True)\nwhile True:\n time.sleep(1)\n"
        )
    return "import json\nfor row in " + repr(rows) + ":\n print(json.dumps(row), flush=True)\n"


@pytest.mark.parametrize(
    "permissions", ["read-only-tools", "workspace-tools", "no-tools", "review"]
)
def test_portable_grok_tool_stream_fits_only_tool_permissions(permissions):
    limits = JobLimits(131072, 16384, max_turns=8)
    wire_bytes = len("\n".join(json.dumps(row) for row in messages("normal")).encode())
    ceiling = worker._wire_output_limit("grok-json", permissions, 109338, limits)
    assert (wire_bytes < ceiling) is (permissions in {"read-only-tools", "workspace-tools"})
    assert ceiling < 5 * 1024 * 1024


@pytest.mark.parametrize("permissions", ["read-only-tools", "workspace-tools", "no-tools"])
@pytest.mark.parametrize(
    "protocol,expected",
    [
        ("claude-stream", 66560),
        ("agy-json", 66560),
        ("bmad-test-v1", 66560),
        ("codex-jsonl", 66560),
        ("muse-json", 86016),
        ("codex-app-server", 25288704),
        ("codex-native-review", 25288704),
    ],
)
def test_portable_other_protocol_transport_budgets_unchanged(protocol, expected, permissions):
    limits = JobLimits(4096, 64, max_turns=3)
    assert worker._wire_output_limit(protocol, permissions, 4096, limits) == expected


@pytest.mark.parametrize("turns,expected", [(1, 851968), (8, 4521984), (16, 8716288)])
def test_portable_grok_allowance_scales_with_authorized_turns_and_stays_finite(turns, expected):
    limits = JobLimits(131072, 16384, max_turns=turns)
    assert worker._wire_output_limit("grok-json", "read-only-tools", 109338, limits) == expected
    assert worker._wire_output_limit("grok-json", "no-tools", 109338, limits) == 327680


@pytest.mark.skipif(sys.platform != "darwin", reason="actual qualified read-only sandbox")
@pytest.mark.parametrize("kind", ["normal", "wire", "output", "turns"])
@pytest.mark.parametrize(
    "fast_exit", [False, True], ids=["ordinary-observation", "exited-before-poll"]
)
def test_tool_heavy_grok_keeps_transport_token_and_turn_bounds(
    readonly, project, tmp_path, monkeypatch, kind, fast_exit
):
    proposal, _, cap = readonly
    candidate = RoleCandidate("grok", "grok-test", "high", "xai")
    script = tmp_path / "writer.py"
    canary = script.read_text().replace("gpt-test", candidate.model)
    script.write_text(
        "from pathlib import Path\nimport sys\ntext=Path(sys.argv[1]).read_text()\n"
        + "if 'QUALIFICATION NONCE ' in text:\n exec("
        + repr(canary)
        + ")\n"
        + "else:\n exec("
        + repr(script_text(messages(kind), hold_overflow=kind == "wire" and not fast_exit))
        + ")\n"
    )
    cap.update(profile=candidate.profile, model=candidate.model, protocol="grok-json", max_turns=2)
    cap["dependencies"][0]["sha256"] = file_digest(script)
    proposal.write_text(json.dumps(cap))
    report = qualify(
        proposal,
        tmp_path / "reader-proof",
        input_bytes=4096,
        output_tokens=4096,
        timeout_s=20,
        runner=HeadlessRunner(ChildMux()),
    )
    assert report["ok"], report
    cap = read_json(Path(report["capability_path"]))
    request = JobRequest(
        "grok-tool-review",
        "graph_review",
        "publication_review",
        {"head": "", "base": "", "graph_revision": 1, "config_revision": 1},
        (),
        "Inspect the selected source",
        {"type": "object", "properties": {"answer": {"type": "boolean"}}, "required": ["answer"]},
        JobLimits(4096, 4096, max_turns=2, timeout_s=20),
        candidates=(candidate,),
    )
    grants = readonly_grants(project.project, request.coordinates, cap)
    runner = HeadlessRunner(ChildMux())
    path = runner.prepare(
        tmp_path / "review",
        request,
        candidate,
        cap,
        launch_id="review",
        generation=1,
        permissions="read-only-tools",
        grants=grants,
    )
    write_json(path.parent / "go.json", {"launch_id": "review", "generation": 1})
    if fast_exit:
        original = subprocess.Popen

        def launched(*args, **kwargs):
            process = original(*args, **kwargs)
            if args and args[0][0] == cap["binary"]:
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
    result = worker.child_main(path)
    receipt = read_json(path.parent / "receipt.json")
    assert receipt["processes_quiescent"]
    wire_bytes = (path.parent / "stdout.jsonl").stat().st_size
    assert 327680 < wire_bytes
    assert 327680 < receipt["wire_output_limit_bytes"] < 2 * 1024 * 1024
    if kind == "normal":
        assert result == 0 and receipt["outcome"] == "completed"
        assert receipt["result"] == {"answer": True}
        assert receipt["usage"]["output_tokens"] == 8 and receipt["turns"] == 2
        assert receipt["input_complete"]
    else:
        assert result == 1 and receipt["outcome"] == "limit"
        assert (
            receipt["limit_kind"]
            == {
                "wire": "stdout-wire-bytes",
                "output": "native-output-tokens",
                "turns": "native-turns",
            }[kind]
        )
        if kind == "wire":
            assert wire_bytes > receipt["wire_output_limit_bytes"]
            assert not receipt["input_complete"] and receipt.get("result") is None
            frames = [
                json.loads(line) for line in (path.parent / "stdout.jsonl").read_text().splitlines()
            ]
            assert any(row.get("type") == "result" for row in frames) is fast_exit


@pytest.mark.skipif(sys.platform != "darwin", reason="actual qualified writer sandbox")
def test_grok_writer_keeps_tool_telemetry_and_scoped_commit_proof(writer, tmp_path):
    proposal, _, cap = writer
    script = tmp_path / "writer.py"
    prefix = script.read_text().split("print(json.dumps({'type':'system'", 1)[0]
    rows = messages("normal")
    script.write_text(
        prefix
        + "\nrows="
        + repr(rows)
        + "\nrows[-1]['structured_output']={'nonce':nonce,'markers':markers}\n"
        + "for row in rows:\n print(json.dumps(row), flush=True)\n"
    )
    cap.update(profile="grok", model="grok-test", protocol="grok-json", max_turns=2)
    cap["dependencies"][0]["sha256"] = file_digest(script)
    proposal.write_text(json.dumps(cap))
    report = qualify(
        proposal,
        tmp_path / "writer-proof",
        input_bytes=4096,
        output_tokens=4096,
        timeout_s=20,
        runner=HeadlessRunner(ChildMux()),
    )
    assert report["ok"], report
    assert report["checks"]["scoped_commit"]
    receipt = read_json(tmp_path / "writer-proof/job/receipt.json")
    assert receipt["processes_quiescent"] and receipt["usage"]["output_tokens"] == 8
    assert (
        327680
        < (tmp_path / "writer-proof/job/stdout.jsonl").stat().st_size
        < receipt["wire_output_limit_bytes"]
    )


def test_no_tools_grok_retains_existing_wire_bound(prepared, tmp_path):
    runner, _, request, _, cap = prepared
    candidate = RoleCandidate("grok", "grok-test", "high", "xai")
    script = tmp_path / "grok-no-tools.py"
    script.write_text("import sys\nsys.stdin.buffer.read()\n" + script_text(messages("normal")))
    cap = fixture_capability(
        tmp_path,
        {
            **cap,
            "profile": candidate.profile,
            "model": candidate.model,
            "protocol": "grok-json",
            "argv": [
                cap["binary"],
                str(script),
                "--tools",
                "",
                "--deny",
                "*",
                "--no-subagents",
                "--disable-web-search",
            ],
            "dependencies": [{"path": str(script), "sha256": file_digest(script)}],
        },
        candidate,
    )
    request = replace(request, candidates=(candidate,))
    path = runner.prepare(
        tmp_path / "no-tools", request, candidate, cap, launch_id="no-tools", generation=1
    )
    write_json(path.parent / "go.json", {"launch_id": "no-tools", "generation": 1})
    assert worker.child_main(path) == 1
    receipt = read_json(path.parent / "receipt.json")
    assert receipt["wire_output_limit_bytes"] == 131072
    assert receipt["outcome"] == "limit" and receipt["limit_kind"] == "stdout-wire-bytes"
    assert receipt["processes_quiescent"] and not receipt["input_complete"]
