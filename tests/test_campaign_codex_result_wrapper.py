"""Codex transport envelopes preserve stage results and native review findings."""

import json
from types import SimpleNamespace

import pytest
from test_campaign_codex_review import finding, review

from bmad_loop.adapters.base import SessionSpec
from bmad_loop.adapters.headless import HeadlessAdapter
from bmad_loop.campaign_headless import write_json
from bmad_loop.campaign_model import CampaignError


def run_result(tmp_path, protocol, result, *, outcome="completed", native_review=None):
    """Keep native completion at the runner seam; exercise actual adapter logic."""
    adapter = HeadlessAdapter(
        run_dir=tmp_path / "adapter", profile=SimpleNamespace(name="codex"), policy=None
    )
    adapter.set_capability(
        {
            "protocol": protocol,
            "permissions": "no-tools",
            "input_tokens": 8192,
            "output_tokens": 4096,
            "max_turns": 2,
        },
        None,
        {"head": "candidate"},
    )
    path = tmp_path / "native/request.json"
    receipt = {
        "outcome": outcome,
        "result": result,
        "observed_model": "gpt-test",
        "native_review": native_review,
    }
    captured = []

    def prepare(directory, request, *args, **kwargs):
        captured.append(request)
        path.parent.mkdir(parents=True)
        write_json(path, {"job": request.id})
        write_json(path.parent / "receipt.json", receipt)
        return path

    def launch(request, *, admission):
        with admission():
            return {"native_id": "native"}

    adapter.runner = SimpleNamespace(
        prepare=prepare,
        launch=launch,
        observe=lambda request: {"state": "finished", "receipt": receipt},
    )
    spec = SessionSpec(
        "stage", "triage", "Return the full stage result", tmp_path, model="gpt-test", effort="high"
    )
    result = adapter.run(spec)
    return captured[0], result


@pytest.mark.parametrize("protocol", ["codex-app-server", "codex-native-review"])
def test_closed_native_wrapper_preserves_arbitrary_stage_fields(tmp_path, protocol):
    inner = {
        "status": "done",
        "spec_file": "stories/1.md",
        "verdict": "approved",
        "findings": [],
        "unknown_stage_data": {"nested": [None, "unicode Ω", 1]},
    }
    request, result = run_result(
        tmp_path,
        protocol,
        {"bmad_result_json": json.dumps(inner)},
        native_review=review([finding()]),
    )
    assert request.output_schema == {
        "type": "object",
        "properties": {"bmad_result_json": {"type": "string"}},
        "required": ["bmad_result_json"],
        "additionalProperties": False,
    }
    assert "complete JSON result object" in request.prompt
    assert result.status == "completed"
    if protocol == "codex-native-review":
        assert result.result_json["verdict"] == "changes_requested"
        assert result.result_json["findings"][0]["body"] == finding()["body"]
        assert result.result_json["findings"][0]["required"] is True
        assert result.result_json["native_review"] == review([finding()])
    else:
        assert result.result_json == inner
    assert result.result_json["unknown_stage_data"] == inner["unknown_stage_data"]
    assert result.result_json["spec_file"] == "stories/1.md"


@pytest.mark.parametrize("protocol", ["codex-app-server", "codex-native-review"])
@pytest.mark.parametrize(
    "result",
    [
        {"status": "done"},
        {"bmad_result_json": {}},
        {"bmad_result_json": "{}", "extra": "unvalidated"},
        {"bmad_result_json": "not JSON"},
        {"bmad_result_json": "[]"},
        {"bmad_result_json": "null"},
        {"bmad_result_json": "true"},
        {"bmad_result_json": "1"},
    ],
)
def test_invalid_inner_result_is_rejected_before_stage_or_review(tmp_path, protocol, result):
    with pytest.raises(CampaignError, match="Codex result wrapper"):
        run_result(tmp_path, protocol, result, native_review=review())


@pytest.mark.parametrize("protocol", ["claude-json", "grok-json", "muse-json", "agy-json"])
def test_other_protocols_keep_original_result_contract(tmp_path, protocol):
    inner = {"status": "done", "bmad_result_json": "a legitimate stage field"}
    request, result = run_result(tmp_path, protocol, inner)
    assert request.output_schema["additionalProperties"] is True
    assert "Native transport result" not in request.prompt
    assert result.result_json == inner


def test_failed_native_session_needs_no_completed_wrapper(tmp_path):
    _, result = run_result(tmp_path, "codex-app-server", None, outcome="failed")
    assert result.status == "crashed"
    assert result.result_json is None
