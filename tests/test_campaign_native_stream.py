"""Typed Claude accounting uses local JSONL fixtures and disposable fake CLIs."""

import io
import json
from dataclasses import replace
from pathlib import Path

import pytest
from test_campaign_headless import finish, fixture_capability
from test_campaign_headless import prepared as prepared
from test_campaign_native_limits import bounded

from bmad_loop import campaign_native_limits, campaign_native_stream
from bmad_loop.campaign_headless import file_digest, read_json, validate_capability, write_json
from bmad_loop.campaign_model import CampaignError
from bmad_loop.campaign_native_stream import ClaudeStream


def event(kind, **fields):
    return {
        "type": "stream_event",
        "session_id": "stream-session",
        "parent_tool_use_id": None,
        "event": {"type": kind, **fields},
    }


def start(identifier="msg-1", output=1):
    return event("message_start", message={"id": identifier, "usage": {"output_tokens": output}})


def delta(output=8, stop: str | None = "end_turn"):
    return event("message_delta", delta={"stop_reason": stop}, usage={"output_tokens": output})


def result(output=8, **fields):
    return {
        "type": "result",
        "session_id": "stream-session",
        "subtype": "success",
        "num_turns": 2,
        "structured_output": {"ok": True},
        "usage": {
            "output_tokens": output,
            "input_tokens": 12,
            "iterations": [{"type": "message", "output_tokens": output}],
        },
        **fields,
    }


def rows():
    return [start(), delta(), event("message_stop"), result()]


def encode(frames):
    return b"".join(json.dumps(frame).encode() + b"\n" for frame in frames)


def monitor(**kwargs):
    return ClaudeStream(**{"output_tokens": 128, "max_messages": 1, "max_bytes": 65536, **kwargs})


def test_fragmented_cumulative_usage_and_structured_tool_round_are_one_model_request():
    frames = [
        start(),
        delta(4, None),
        delta(4, None),
        delta(),
        event("message_stop"),
        {"type": "user", "message": {"content": [{"type": "tool_result"}]}},
        result(),
    ]
    data = encode(frames).rstrip(b"\n")
    stream = monitor()
    for byte in data:
        stream.feed(bytes([byte]))
    stream.finish()
    receipt = stream.receipt()
    assert receipt["output_tokens_lower_bound"] == 8
    assert receipt["terminal_usage_complete"] is True
    assert len(receipt["messages"]) == 1
    assert receipt["messages"][0]["complete"] is True
    assert receipt["bytes_read"] == len(data)


def test_account_across_distinct_requests_without_counting_deltas_twice():
    terminal = result(
        15,
        usage={
            "output_tokens": 15,
            "iterations": [
                {"type": "message", "output_tokens": 8},
                {"type": "message", "output_tokens": 7},
            ],
        },
    )
    stream = monitor(max_messages=2)
    stream.feed(
        encode(
            [
                start(),
                delta(),
                event("message_stop"),
                start("msg-2"),
                delta(7),
                event("message_stop"),
                terminal,
            ]
        )
    )
    stream.finish()
    assert stream.output == 15
    assert len(stream.messages) == 2


@pytest.mark.parametrize(
    "fault,expected",
    [
        ("max_tokens", "native-max-tokens"),
        ("aggregate", "native-output-tokens"),
        ("requests", "native-request-count"),
        ("wire", "stdout-wire-bytes"),
    ],
)
def test_typed_bounds_stop_without_terminal_receipt(fault, expected):
    stream = monitor(max_bytes=128 if fault == "wire" else 65536)
    if fault == "max_tokens":
        frames = [start(), delta(64, "max_tokens")]
    elif fault == "aggregate":
        stream = monitor(output_tokens=12, max_messages=2)
        frames = [start(), delta(), event("message_stop"), start("msg-2"), delta(5, None)]
    elif fault == "requests":
        frames = [start(), delta(), event("message_stop"), start("msg-2")]
    else:
        frames = rows()
    stream.feed(encode(frames))
    assert stream.limit_kind == expected
    assert stream.receipt()["terminal_usage_complete"] is False


@pytest.mark.parametrize("value", [-1, True, 1.5, "8", None, float("nan"), float("inf"), 2**54])
@pytest.mark.parametrize("site", ["start", "delta", "terminal"])
def test_invalid_numeric_telemetry_refuses(value, site):
    frames = rows()
    if site == "start":
        frames[0]["event"]["message"]["usage"]["output_tokens"] = value
    elif site == "delta":
        frames[1]["event"]["usage"]["output_tokens"] = value
    else:
        frames[-1]["usage"]["output_tokens"] = value
    with pytest.raises(CampaignError, match="token telemetry"):
        monitor().read(io.BytesIO(encode(frames)), final=True)


@pytest.mark.parametrize(
    "fault",
    [
        "nested",
        "missing_parent",
        "session",
        "repeated_id",
        "interleaved",
        "orphan_delta",
        "decreasing",
        "missing_usage",
        "bad_stop",
        "unclosed",
        "after_result",
        "iterations",
        "iteration_total",
        "terminal_total",
        "missing_terminal",
        "truncated_json",
        "deep_json",
    ],
)
def test_broken_accounting_never_becomes_complete(fault):
    frames = rows()
    if fault == "nested":
        frames[0]["parent_tool_use_id"] = "nested-agent"
    elif fault == "missing_parent":
        frames[0].pop("parent_tool_use_id")
    elif fault == "session":
        frames[1]["session_id"] = "other-session"
    elif fault == "repeated_id":
        frames = [
            start(),
            delta(),
            event("message_stop"),
            start(),
            delta(7),
            event("message_stop"),
            result(15, usage={"output_tokens": 15}),
        ]
    elif fault == "interleaved":
        frames.insert(1, start("other"))
    elif fault == "orphan_delta":
        frames.pop(0)
    elif fault == "decreasing":
        frames.insert(2, delta(7))
    elif fault == "missing_usage":
        frames[1]["event"].pop("usage")
    elif fault == "bad_stop":
        frames[1]["event"]["delta"]["stop_reason"] = []
    elif fault == "unclosed":
        frames.pop(2)
    elif fault == "after_result":
        frames.append(start("after"))
    elif fault == "iterations":
        frames[-1]["usage"]["iterations"] = []
    elif fault == "iteration_total":
        frames[-1]["usage"]["iterations"][0]["output_tokens"] = 7
    elif fault == "terminal_total":
        frames[-1]["usage"]["output_tokens"] = 7
    elif fault == "missing_terminal":
        frames.pop()
    data = encode(frames)
    if fault == "truncated_json":
        data += b'{"type":'
    elif fault == "deep_json":
        data = b"[" * 10000 + b"]" * 10000 + b"\n"
    stream = monitor()
    with pytest.raises(CampaignError):
        stream.read(io.BytesIO(data), final=True)
    assert stream.receipt()["terminal_usage_complete"] is False


@pytest.mark.parametrize("bound", ["record", "frames", "messages"])
def test_parser_memory_and_work_bounds(monkeypatch, bound):
    stream = monitor(max_messages=10)
    if bound == "record":
        monkeypatch.setattr(campaign_native_stream, "MAX_RECORD_BYTES", 8)
        data = b" " * 9
    elif bound == "frames":
        monkeypatch.setattr(campaign_native_stream, "MAX_FRAMES", 2)
        data = encode(rows())
    else:
        monkeypatch.setattr(campaign_native_stream, "MAX_MESSAGES", 1)
        data = encode([*rows()[:-1], start("second")])
    with pytest.raises(CampaignError, match="bound"):
        stream.read(io.BytesIO(data), final=True)


def test_read_consumes_only_incremental_bytes():
    source = io.BytesIO(encode(rows()))
    stream = monitor()
    stream.read(source)
    position = source.tell()
    stream.read(source)
    assert source.tell() == position == stream.bytes_read
    stream.read(source, final=True)
    assert stream.receipt()["terminal_usage_complete"]


def test_unframed_live_record_waits_for_newline_or_eof():
    stream = monitor()
    stream.feed(encode([start()]))
    stream.feed(encode([delta(128, "max_tokens")]).rstrip(b"\n"))
    assert stream.limit_kind == ""
    stream.finish()
    assert stream.limit_kind == "native-max-tokens"


def test_decoder_depth_error_is_a_typed_boundary_on_every_python(monkeypatch):
    decode = campaign_native_stream.json.loads

    def deep_only(value):
        if value == b'{"depth":"fixture"}\n':
            raise RecursionError("controlled decoder boundary")
        return decode(value)

    monkeypatch.setattr(campaign_native_stream.json, "loads", deep_only)
    with pytest.raises(CampaignError, match="invalid JSON"):
        monitor().read(io.BytesIO(b'{"depth":"fixture"}\n'), final=True)


@pytest.mark.parametrize("code", ["rate_limit_error", "authentication_error", "connection_error"])
def test_terminal_errors_without_session_or_usage_do_not_invent_accounting(code):
    stream = monitor()
    stream.read(
        io.BytesIO(encode([{"type": "result", "is_error": True, "error": {"type": code}}])),
        final=True,
    )
    assert stream.receipt()["terminal_usage_complete"] is False
    assert stream.receipt()["output_tokens_lower_bound"] == 0


def test_prose_and_progress_estimates_do_not_authorize_cancellation():
    stream = monitor()
    stream.feed(
        encode(
            [
                {"type": "assistant", "message": {"content": "max_tokens output limit hit"}},
                {"type": "system", "subtype": "thinking_tokens", "estimated_tokens": 999999},
                {
                    "type": "user",
                    "isSynthetic": True,
                    "message": {"content": "Output token limit hit"},
                },
                *rows(),
            ]
        )
    )
    stream.finish()
    assert stream.limit_kind == ""
    assert stream.output == 8


def stream_capability(tmp_path, cap, candidate):
    cap = bounded(cap)
    cap["native_limits"] = dict(campaign_native_limits.CLAUDE_STREAM_LIMITS)
    cap["argv"] = [
        *cap["argv"],
        "--print",
        "--output-format",
        "stream-json",
        "--include-partial-messages",
        "--tools",
        "",
        "--strict-mcp-config",
        "--mcp-config",
        '{"mcpServers":{}}',
    ]
    helper = Path(campaign_native_stream.__file__).resolve()
    cap["dependencies"].append({"path": str(helper), "sha256": file_digest(helper)})
    cap = fixture_capability(tmp_path, cap, candidate)
    proof = Path(cap["qualification"]["path"])
    report = read_json(proof)
    data = encode(rows()[:-1] + [result(structured_output=report["receipt"]["result"])])
    (proof.parent / "stdout.jsonl").write_bytes(data)
    stream = monitor(max_bytes=cap["output_tokens"] * 16 + 65536)
    stream.read(io.BytesIO(data), final=True)
    report["receipt"]["native_stream_accounting"] = stream.receipt()
    report["receipt"]["session_id"] = "stream-session"
    write_json(proof.parent / "receipt.json", report["receipt"])
    write_json(proof, report)
    cap["qualification"]["sha256"] = file_digest(proof)
    validate_capability(cap, candidate)
    return cap


@pytest.mark.parametrize(
    "case", ["valid", "max_tokens", "requests", "quota", "partial_quota", "partial_json"]
)
def test_actual_worker_v2_stream_lifecycle(prepared, tmp_path, case):
    runner, _, request, candidate, original = prepared
    frames = [{"type": "system", "model": "gpt-test", "session_id": "stream-session"}]
    if case in {"quota", "partial_quota"}:
        if case == "partial_quota":
            frames += [start(), delta(3, None)]
        frames += [
            result(is_error=True, error={"type": "rate_limit_error"}, usage={"output_tokens": 0})
        ]
    elif case == "max_tokens":
        frames += [start(), delta(128, "max_tokens")]
    elif case == "requests":
        frames += [*rows()[:-1], start("extra")]
    else:
        frames += rows()
    data = encode(frames)
    if case not in {"max_tokens", "requests"}:
        data = data.rstrip(b"\n")
    if case == "partial_json":
        data += b'\n{"type":'
    script = tmp_path / "stream-cli.py"
    script.write_text(
        "import sys,time\nsys.stdin.buffer.read()\n"
        + f"data={data!r}\n"
        + "for pos in range(0,len(data),13):\n sys.stdout.buffer.write(data[pos:pos+13]);sys.stdout.buffer.flush()\n"
        + ("time.sleep(20)\n" if case in {"max_tokens", "requests"} else ""),
        encoding="utf-8",
    )
    cap = stream_capability(
        tmp_path,
        {
            **original,
            "argv": [original["binary"], str(script)],
            "dependencies": [{"path": str(script), "sha256": file_digest(script)}],
        },
        candidate,
    )
    request = replace(
        request, output_schema={"type": "object"}, limits=replace(request.limits, output_tokens=128)
    )
    path = runner.prepare(
        tmp_path / "actual-stream", request, candidate, cap, launch_id="stream", generation=1
    )
    runner.launch(path)
    receipt = finish(runner, path)
    assert receipt["quiescent"] and receipt["processes_quiescent"], receipt
    accounting = receipt["native_stream_accounting"]
    if case == "valid":
        assert receipt["outcome"] == "completed", receipt
        assert accounting["terminal_usage_complete"] is True
        assert accounting["output_tokens_lower_bound"] == 8
        assert receipt["usage"]["output_tokens"] == 8
    elif case in {"quota", "partial_quota"}:
        assert receipt["outcome"] == "quota", receipt
        assert accounting["terminal_usage_complete"] is False
        assert accounting["output_tokens_lower_bound"] == (3 if case == "partial_quota" else 0)
    elif case == "partial_json":
        assert receipt["outcome"] == "failed" and "invalid JSON" in receipt["reason"], receipt
    else:
        assert receipt["outcome"] == "limit", receipt
        assert receipt["limit_kind"] == (
            "native-max-tokens" if case == "max_tokens" else "native-request-count"
        )
        assert accounting["terminal_usage_complete"] is False


@pytest.mark.parametrize("fault", ["flag", "dependency", "tools", "receipt", "raw_stream"])
def test_v2_requires_its_own_exact_stream_proof(prepared, tmp_path, fault):
    _, _, _, candidate, original = prepared
    cap = stream_capability(tmp_path, original, candidate)
    if fault == "flag":
        cap["argv"].remove("--include-partial-messages")
    elif fault == "dependency":
        cap["dependencies"].pop()
    elif fault == "tools":
        cap["permissions"] = "read-only-tools"
    else:
        proof = Path(cap["qualification"]["path"])
        if fault == "raw_stream":
            (proof.parent / "stdout.jsonl").write_bytes(b"")
        else:
            report = read_json(proof)
            report["receipt"].pop("native_stream_accounting")
            write_json(proof.parent / "receipt.json", report["receipt"])
            write_json(proof, report)
            cap["qualification"]["sha256"] = file_digest(proof)
    with pytest.raises(CampaignError):
        validate_capability(cap, candidate)


@pytest.mark.parametrize("fault", ["flag", "dependency", "tools"])
def test_v2_proposal_boundary_before_any_proof(prepared, tmp_path, fault):
    _, _, _, candidate, original = prepared
    cap = stream_capability(tmp_path, original, candidate)
    if fault == "flag":
        cap["argv"].remove("--include-partial-messages")
    elif fault == "dependency":
        cap["dependencies"].pop()
    else:
        cap["permissions"] = "read-only-tools"
    with pytest.raises(
        CampaignError, match="Native streaming limits|exact helper/worker dependencies"
    ):
        campaign_native_limits.validate(cap)
