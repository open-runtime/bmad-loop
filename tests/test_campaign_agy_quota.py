"""Public AGY quota envelope regressions; scripted native processes only."""

import json
import sys
import time
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_campaign_agy_protocol import frames, native_proposal, parse
from test_campaign_headless import prepared as prepared

from bmad_loop import campaign_headless_worker as worker
from bmad_loop.adapters.base import SessionHandle, SessionResult, SessionSpec
from bmad_loop.adapters.headless import HeadlessAdapter
from bmad_loop.adapters.profile import get_profile
from bmad_loop.campaign_headless import file_digest, read_json, write_json
from bmad_loop.campaign_headless_protocol import AGY_QUOTA_SOURCE, parse_output
from bmad_loop.campaign_interruptions import execution_interruption, session_interruption
from bmad_loop.campaign_model import CampaignError, RoleCandidate
from bmad_loop.policy import Policy

ERROR = (
    "Individual quota reached. Please upgrade your subscription to increase your limits. "
    "Resets in 55h12m36s."
)
RESET_S = 198756


def quota_frames(**changes):
    # Public terminal shape, with synthetic session/model; no private transcript.
    return frames(
        status="ERROR",
        finished_reason="error",
        error=ERROR,
        num_turns=1,
        structured_output=None,
        response="",
        usage={"input_tokens": 47860, "output_tokens": 1159, "thinking_tokens": 1114},
        **changes,
    )


def test_actual_agy_quota_shape_is_typed_without_replay_clock(monkeypatch):
    monkeypatch.setattr(
        worker, "time", SimpleNamespace(time=lambda: pytest.fail("parser must not read the clock"))
    )
    result = parse(quota_frames())
    assert result["outcome"] == "quota"
    assert result["reason"] == "AGY native failure: " + ERROR
    assert result["quota_source"] == AGY_QUOTA_SOURCE
    assert result["quota_retry_after_s"] == RESET_S
    assert "quota_retry_at" not in result
    assert result["usage"] == {
        "input_tokens": 47860,
        "output_tokens": 1159,
        "reasoning_tokens": 1114,
    }
    assert result["turns"] == 1 and result["result"] is None
    assert parse(quota_frames()) == result


@pytest.mark.parametrize("status", ["SUCCESS", "CANCELED", "INTERRUPTED", "INVALID"])
def test_quota_words_require_native_error_status(status):
    rows = quota_frames()
    rows[-1]["result"].update(status=status, structured_output={"ok": True})
    result = parse(rows)
    assert result["outcome"] != "quota"
    assert "quota_source" not in result and "quota_retry_after_s" not in result


@pytest.mark.parametrize("location", ["response", "structured_output", "tool", "init"])
@pytest.mark.parametrize("status", ["SUCCESS", "ERROR"])
def test_quota_words_in_model_or_tool_content_do_not_classify(location, status):
    rows = frames(status=status, finished_reason="error")
    if location == "tool":
        rows[1]["step_update"]["tool_info"] = {"error": ERROR}
    elif location == "init":
        rows[0]["init"]["error"] = ERROR
    else:
        rows[-1]["result"][location] = ERROR if location == "response" else {"error": ERROR}
    result = parse(rows)
    assert result["outcome"] == ("completed" if status == "SUCCESS" else "failed")
    assert "quota_source" not in result


@pytest.mark.parametrize(
    "replacement",
    [
        "quota exhausted",
        "quoted: " + ERROR,
        ERROR + " extra",
        ERROR + "\n",
        ERROR.replace("55h12m36s", "0h0m0s"),
        ERROR.replace("55h12m36s", "169h0m0s"),
        ERROR.replace("55h12m36s", "999999h0m0s"),
        ERROR.replace("55h12m36s", "-1h12m36s"),
        ERROR.replace("55h12m36s", "55h60m36s"),
        ERROR.replace("55h12m36s", "55h12m60s"),
        ERROR.replace("55h12m36s", "55h12m36.5s"),
        ERROR.replace("55h12m36s", "12m36s"),
        ERROR.replace("55h12m36s", "５５h12m36s"),
        ERROR.replace("Resets in 55h12m36s.", ""),
    ],
)
def test_unknown_or_malformed_quota_error_stays_failed(replacement):
    rows = quota_frames()
    rows[-1]["result"]["error"] = replacement
    result = parse(rows)
    assert result["outcome"] == "failed"
    assert "quota_retry_after_s" not in result and "quota_source" not in result


@pytest.mark.parametrize("finished_reason", [None, "completed", "ERROR"])
def test_quota_requires_exact_native_finished_reason(finished_reason):
    rows = quota_frames()
    rows[-1]["result"]["finished_reason"] = finished_reason
    result = parse(rows)
    assert result["outcome"] == "failed" and "quota_source" not in result


def test_quota_requires_complete_initialized_bound_native_stream():
    rows = quota_frames()
    partial = parse(rows, partial=True)
    assert partial["outcome"] == "failed" and "quota_source" not in partial
    bare = parse([{**rows[-1], "result": {**rows[-1]["result"], "model": "gemini-test"}}])
    assert bare["outcome"] == "failed" and "quota_source" not in bare
    with pytest.raises(CampaignError, match="primary model"):
        parse_output(json.dumps(rows), protocol="agy-json", selected_model="different-model")
    rows[-1]["result"]["conversation_id"] = "other-session"
    with pytest.raises(CampaignError, match="session identities"):
        parse(rows)


@pytest.mark.parametrize("duration", ["0h0m1s", "168h0m0s"])
def test_quota_accepts_positive_bounded_reset_edges(duration):
    rows = quota_frames()
    rows[-1]["result"]["error"] = ERROR.replace("55h12m36s", duration)
    result = parse(rows)
    assert result["outcome"] == "quota"
    assert result["quota_retry_after_s"] in {1, 604800}


@pytest.mark.parametrize("outcome", ["failed", "canceled", "limit"])
def test_other_worker_outcome_removes_quota_reset_metadata(outcome):
    receipt = {**parse(quota_frames()), "outcome": outcome, "finished_at": 1000}
    worker._stamp_quota_reset(receipt)
    assert receipt["outcome"] == outcome
    assert all(
        key not in receipt for key in ("quota_source", "quota_retry_after_s", "quota_retry_at")
    )


@pytest.mark.parametrize("value", [True, 0, -1, float("inf"), float("nan"), "123", 10**1000])
def test_invalid_interruption_deadline_is_rejected(value):
    result = SessionResult(
        "crashed",
        native_binding={
            "outcome": "quota",
            "quota_source": AGY_QUOTA_SOURCE,
            "quota_retry_at": value,
        },
    )
    with pytest.raises(CampaignError, match="invalid reset evidence"):
        session_interruption(result)
    with pytest.raises(CampaignError, match="invalid reset evidence"):
        execution_interruption([{"status": "crashed", "native_binding": result.native_binding}])


@pytest.mark.parametrize(
    "binding",
    [
        {"outcome": "quota", "quota_source": "model-prose", "quota_retry_at": 123},
        {"outcome": "quota", "quota_retry_at": 123},
        {"outcome": "quota", "quota_source": AGY_QUOTA_SOURCE},
        {"outcome": "failed", "quota_source": AGY_QUOTA_SOURCE, "quota_retry_at": 123},
    ],
)
def test_interruption_deadline_requires_native_quota_provenance(binding):
    with pytest.raises(CampaignError, match="invalid reset evidence"):
        session_interruption(SessionResult("crashed", native_binding=binding))


def test_old_quota_receipts_and_later_success_keep_existing_interruption_behavior():
    result = SessionResult("crashed", native_binding={"outcome": "quota"})
    assert "quota_retry_at" not in session_interruption(result)
    assert "quota_source" not in execution_interruption(
        [{"status": "crashed", "native_binding": result.native_binding}]
    )
    assert (
        execution_interruption(
            [
                {"status": "crashed", "native_binding": result.native_binding},
                {"status": "completed"},
            ]
        )
        is None
    )


@pytest.mark.parametrize("over_output_bound", [False, True])
def test_scripted_worker_adapter_and_interruptions_keep_one_absolute_reset(
    prepared, tmp_path, monkeypatch, over_output_bound
):
    runner, cap, _ = native_proposal(tmp_path, prepared)
    native = Path(cap["argv"][2])
    rows = quota_frames()
    rows[0]["init"]["model"] = cap["model"]
    wire = "\n".join(json.dumps(row) for row in rows) + "\n"
    # No tool invocation is needed for a native quota failure. The separate
    # recipe tests exercise hook subprocesses; this seam uses one fixed emitter.
    native.write_text(
        f"#!{sys.executable}\nimport sys\nsys.stdin.read()\nsys.stdout.write({wire!r})\n"
    )
    for dependency in cap["dependencies"]:
        if dependency["path"] == str(native):
            dependency["sha256"] = file_digest(native)
    candidate = RoleCandidate(cap["profile"], cap["model"], "high")
    path = runner.prepare_probe(
        tmp_path / "quota-job",
        candidate,
        cap,
        input_bytes=4096,
        output_tokens=1024 if over_output_bound else 4096,
        timeout_s=60,
    )
    envelope = read_json(path)
    write_json(
        path.parent / "go.json",
        {"launch_id": envelope["launch_id"], "generation": envelope["generation"]},
    )
    monkeypatch.setattr(
        worker,
        "time",
        SimpleNamespace(time=lambda: 1700000000.0, monotonic=time.monotonic, sleep=time.sleep),
    )
    assert worker.child_main(path) == 1
    raw = read_json(path.parent / "receipt.json")
    if over_output_bound:
        assert raw["outcome"] == "limit" and raw["native_outcome"] == "quota"
        assert raw["limit_kind"] == "native-output-tokens"
        assert all(
            key not in raw for key in ("quota_source", "quota_retry_after_s", "quota_retry_at")
        )
        return
    assert raw["outcome"] == "quota" and raw["processes_quiescent"] is True
    assert raw["finished_at"] == 1700000000.0
    assert raw["quota_retry_at"] == raw["finished_at"] + RESET_S
    assert raw["quota_source"] == AGY_QUOTA_SOURCE
    # Adapter seam consumes the independently settled observation; no model or
    # real mux is involved in this lower-layer fixture.
    adapter = HeadlessAdapter(
        run_dir=tmp_path / "adapter", profile=get_profile("codex"), policy=Policy(), mux=runner.mux
    )
    adapter.requests["quota-task"] = path
    adapter.runner = SimpleNamespace(
        observe=lambda _: {"state": "finished", "receipt": {**raw, "quiescent": True}}
    )
    spec = SessionSpec("quota-task", "dev", "fixture", tmp_path, model=cap["model"], effort="high")
    result = adapter.wait_for_completion(SessionHandle("quota-task", "fixture-window"), spec)
    assert result.transport_fault == "usage" and result.usage_limited == raw["reason"]
    evidence = {"quota_retry_at": raw["quota_retry_at"], "quota_source": AGY_QUOTA_SOURCE}
    assert {key: result.native_binding[key] for key in evidence} == evidence
    single = session_interruption(result)
    execution = execution_interruption([{**asdict(result), "role": "dev"}])
    for interruption in (single, execution):
        assert interruption["kind"] == "quota"
        assert {key: interruption[key] for key in evidence} == evidence
    monkeypatch.setattr(worker.time, "time", lambda: 1800000000.0)
    replayed = parse_output(
        (path.parent / "stdout.jsonl").read_text(), protocol="agy-json", selected_model=cap["model"]
    )
    assert replayed["quota_retry_after_s"] == RESET_S and "quota_retry_at" not in replayed
    assert read_json(path.parent / "receipt.json")["quota_retry_at"] == evidence["quota_retry_at"]
