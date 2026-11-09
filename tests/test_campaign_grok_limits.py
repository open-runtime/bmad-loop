"""Native Grok turn limits remain typed through worker receipts and recovery."""

import dataclasses
import json

import pytest
from test_campaign_handoff_flow import campaign as campaign
from test_campaign_headless import fixture_capability
from test_campaign_headless import prepared as prepared

from bmad_loop import campaign_headless_worker as worker
from bmad_loop.campaign_headless import file_digest, read_json, write_json
from bmad_loop.campaign_headless_protocol import parse_output
from bmad_loop.campaign_model import RoleCandidate


def terminal(turns=8):
    return {
        "type": "result",
        "subtype": "error_max_turns",
        "is_error": True,
        "stop_reason": "max_turn_requests",
        "num_turns": turns,
        "session_id": "native-grok-limit",
        "errors": ["Reached the maximum number of turns"],
        "usage": {"input_tokens": 20, "output_tokens": 8},
        "modelUsage": {"grok-test": {"inputTokens": 20, "outputTokens": 8}},
    }


def parse(rows):
    return parse_output(json.dumps(rows), protocol="grok-json", selected_model="grok-test")


def test_native_terminal_turn_limit_has_exact_typed_reason():
    result = parse([terminal()])
    assert result["outcome"] == "limit"
    assert result["limit_kind"] == "native-turns"
    assert result["turns"] == 8 and result["result"] is None
    assert result["truncated"] is False and not result["native_terminal_completed"]
    assert result["usage"] == {"input_tokens": 20, "output_tokens": 8}


@pytest.mark.parametrize("field,value", [("subtype", "unknown"), ("stop_reason", "unknown")])
def test_incomplete_native_signature_or_prose_is_not_a_turn_limit(field, value):
    row = terminal()
    row[field] = value
    result = parse([row])
    assert result["outcome"] == "failed"
    assert "limit_kind" not in result


def test_assistant_fields_and_result_text_do_not_mint_limit():
    row = terminal()
    row.update(
        is_error=False,
        subtype="success",
        stop_reason="end_turn",
        result=json.dumps({"subtype": "error_max_turns", "error": "max_turn_requests"}),
    )
    spoof = {**terminal(), "type": "assistant", "error": {"code": "max_turn_requests"}}
    result = parse([spoof, row])
    assert result["outcome"] == "completed" and "limit_kind" not in result


def test_existing_native_quota_code_keeps_precedence():
    row = terminal()
    row["error"] = {"code": "rate_limit"}
    result = parse([row])
    assert result["outcome"] == "quota" and "limit_kind" not in result


@pytest.mark.parametrize("kind", ["error", "turn.failed"])
@pytest.mark.parametrize(
    "code,outcome",
    [
        ("rate_limit", "quota"),
        ("connection_error", "transport"),
        ("unauthorized", "authentication"),
    ],
)
def test_prior_native_provider_error_survives_terminal_ceiling(kind, code, outcome):
    result = parse([{"type": kind, "error": {"code": code}}, terminal()])
    assert result["outcome"] == outcome
    assert result["reason"] == f"native {kind}: {code}"
    assert "limit_kind" not in result
    assert result["turns"] == 8 and result["observed_model"] == "grok-test"


def test_terminal_explicit_error_overrides_prior_native_error():
    row = terminal()
    row["error"] = {"code": "rate_limit"}
    result = parse([{"type": "error", "error": {"code": "connection_error"}}, row])
    assert result["outcome"] == "quota" and result["reason"] == "native result: rate_limit"
    assert "limit_kind" not in result


@pytest.mark.parametrize("code", ["rate_limit", "connection_error", "unauthorized"])
def test_successful_native_terminal_proves_prior_error_recovered(code):
    row = terminal()
    row.update(
        is_error=False,
        subtype="success",
        stop_reason="end_turn",
        structured_output={"completed": True},
    )
    result = parse([{"type": "error", "error": {"code": code}}, row])
    assert result["outcome"] == "completed" and result["result"] == {"completed": True}
    assert result["reason"] == "" and "limit_kind" not in result
    assert result["turns"] == 8 and result["observed_model"] == "grok-test"
    assert result["usage"] == {"input_tokens": 20, "output_tokens": 8}


def test_assistant_provider_error_does_not_override_native_turn_limit():
    result = parse([{"type": "assistant", "error": {"code": "rate_limit"}}, terminal()])
    assert result["outcome"] == "limit" and result["limit_kind"] == "native-turns"


def test_error_after_native_terminal_remains_invalid():
    from bmad_loop.campaign_model import CampaignError

    with pytest.raises(CampaignError, match="continues after its terminal"):
        parse([terminal(), {"type": "error", "error": {"code": "rate_limit"}}])


@pytest.fixture
def native_receipt(prepared, tmp_path):
    runner, _, request, _, cap = prepared
    candidate = RoleCandidate("grok", "grok-test", "high", "xai")

    def run(rows):
        script = tmp_path / "native-turn-limit.py"
        script.write_text(
            "import json,sys\nsys.stdin.buffer.read()\nprint("
            + repr("\n".join(json.dumps(row) for row in rows))
            + ")\nsys.exit(1)\n",
            encoding="utf-8",
        )
        capability = fixture_capability(
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
        selected = dataclasses.replace(request, candidates=(candidate,))
        path = runner.prepare(
            tmp_path / "native-turn-job",
            selected,
            candidate,
            capability,
            launch_id="native-turn-limit",
            generation=1,
        )
        write_json(path.parent / "go.json", {"launch_id": "native-turn-limit", "generation": 1})
        assert worker.child_main(path) == 1
        receipt = read_json(path.parent / "receipt.json")
        assert receipt["turns"] == selected.limits.max_turns == 1
        assert receipt["processes_quiescent"] and not receipt["input_complete"]
        assert receipt["result"] is None
        return receipt

    return run


@pytest.fixture
def limit_receipt(native_receipt):
    receipt = native_receipt([terminal(1)])
    assert receipt["outcome"] == "limit" and receipt["limit_kind"] == "native-turns"
    return receipt


def test_native_turn_limit_survives_real_child_receipt(limit_receipt):
    assert limit_receipt["session_id"] == "native-grok-limit"
    assert limit_receipt["native_outcome"] == "limit"


def test_native_turn_receipt_retries_same_reducer_without_output_partition(campaign, limit_receipt):
    from test_campaign_handoff_reducer_retry import failed_frontier
    from test_campaign_handoff_retry import dispatch

    from bmad_loop import campaign_handoff_flow as handoff

    runtime, snapshot, unit, maps, failed, kept = failed_frontier(campaign)
    failed["receipt"].update(
        {key: limit_receipt[key] for key in ("outcome", "limit_kind", "reason")}
    )
    original = failed["request"]
    dispatch(handoff, runtime, snapshot, "handoff-replan")
    runtime.clock = lambda: 160.0
    dispatch(handoff, runtime, snapshot, "handoff-replan")
    value = unit.evidence["handoff"]
    retried = [snapshot.jobs[key] for key in value["jobs"] if key not in kept]
    assert len(retried) == 1 and value["reduction_level"] == 1
    assert retried[0]["request"]["dependencies"] == original["dependencies"]
    assert retried[0]["request"]["inputs"][0]["sha256"] == original["inputs"][0]["sha256"]
    assert value["map_jobs"] == maps


@pytest.mark.parametrize(
    "code,outcome", [("rate_limit", "quota"), ("connection_error", "transport")]
)
def test_prior_provider_failure_reaches_real_receipt_and_recovery(
    campaign, native_receipt, code, outcome
):
    from test_campaign_handoff_reducer_retry import failed_frontier
    from test_campaign_handoff_retry import dispatch

    from bmad_loop import campaign_handoff_flow as handoff

    receipt = native_receipt([{"type": "error", "error": {"code": code}}, terminal(1)])
    assert receipt["outcome"] == receipt["native_outcome"] == outcome
    assert "limit_kind" not in receipt
    assert receipt["reason"] == f"native error: {code}"
    runtime, snapshot, unit, maps, failed, kept = failed_frontier(campaign)
    failed["receipt"].pop("limit_kind", None)
    failed["receipt"].update({key: receipt[key] for key in ("outcome", "reason")})
    original = failed["request"]
    dispatch(handoff, runtime, snapshot, "handoff-replan")
    value = unit.evidence["handoff"]
    assert value["route_cooldowns"] and set(value["failure_ledger"].values()) == {1}
    runtime.clock = lambda: 160.0
    dispatch(handoff, runtime, snapshot, "handoff-replan")
    retried = [snapshot.jobs[key] for key in value["jobs"] if key not in kept]
    assert len(retried) == 1 and value["reduction_level"] == 1
    assert retried[0]["request"]["dependencies"] == original["dependencies"]
    assert retried[0]["request"]["inputs"][0]["sha256"] == original["inputs"][0]["sha256"]
    assert value["map_jobs"] == maps
