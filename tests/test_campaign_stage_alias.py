"""Earned served-model aliases survive the actual campaign stage boundary."""

import dataclasses
import json
from pathlib import Path

import pytest
from test_campaign_headless import finish
from test_campaign_headless import prepared as prepared
from test_campaign_workers import envelope, worker_request

from bmad_loop import campaign_workers as workers
from bmad_loop.adapters.headless import HeadlessAdapter
from bmad_loop.campaign_headless import file_digest, read_json
from bmad_loop.campaign_model import CampaignError
from bmad_loop.campaign_qualification import qualify


@pytest.mark.parametrize("mutation", ["", "effort", "other-task", "unearned-alias"])
def test_native_alias_is_bound_to_qualified_route_and_current_stage(
    project, tmp_path, prepared, monkeypatch, mutation
):
    runner, _, _, candidate, original = prepared
    script = tmp_path / "stage-native.py"
    script.write_text("""import json,re,sys
text=sys.stdin.read()
nonce=re.search(r"QUALIFICATION NONCE ([a-f0-9]+)",text)
value=({"nonce":nonce.group(1),"markers":re.findall(r"CANARY [0-9]+: ([a-f0-9]+)",text)} if nonce else {"status":"done"})
print(json.dumps({"type":"system","model":"gpt-test-build","session_id":"stage-native"}))
print(json.dumps({"type":"result","structured_output":value,"usage":{"input_tokens":1000,"output_tokens":512}}))
""")
    proposal = {
        **{key: value for key, value in original.items() if key != "qualification"},
        "dependencies": [{"path": str(script), "sha256": file_digest(script)}],
        "argv": [original["binary"], str(script)],
    }
    first = runner.prepare_probe(
        tmp_path / "alias-observation", candidate, proposal, input_bytes=4096
    )
    runner.launch(first)
    assert finish(runner, first)["outcome"] != "completed"
    proposal.update(
        served_model_aliases=["gpt-test-build"],
        alias_evidence={
            "request_path": str(first),
            "request_sha256": file_digest(first),
            "stdout_sha256": file_digest(first.parent / "stdout.jsonl"),
        },
    )
    proposal_path = tmp_path / "alias-proposal.json"
    proposal_path.write_text(json.dumps(proposal))
    earned = qualify(
        proposal_path,
        tmp_path / "alias-qualified",
        input_bytes=8192,
        output_tokens=1024,
        timeout_s=30,
        runner=runner,
    )
    assert earned["ok"], earned
    cap = read_json(Path(earned["capability_path"]))
    context = envelope(project.project, tmp_path / "execution")
    adapter = HeadlessAdapter(
        run_dir=tmp_path / "native-stage",
        profile=dataclasses.replace(context.profiles["triage"], name=candidate.profile),
        policy=context.policy,
        mux=runner.mux,
    )
    adapter.set_capability(
        cap, None, {"head": "", "base": "", "graph_revision": 1, "config_revision": 1}
    )
    run = adapter.run

    def run_then_tamper(spec):
        result = run(spec)
        if mutation == "effort":
            adapter.capability["efforts"] = ["low"]
        elif mutation == "other-task":
            adapter.receipts["prior-task"] = adapter.receipts.pop(spec.task_id)
        elif mutation == "unearned-alias":
            adapter.capability["served_model_aliases"] = []
        return result

    monkeypatch.setattr(adapter, "run", run_then_tamper)
    monkeypatch.setattr(workers, "_adapters", lambda *a, **k: {"triage": adapter})
    request = worker_request(
        project.project,
        context,
        kind="stage",
        role="publication_review",
        candidate=dataclasses.asdict(candidate),
        profile_key="triage",
        prompt="Review",
        read_only=True,
    )
    if mutation:
        with pytest.raises(CampaignError, match="observed model"):
            workers._stage(request, context)
    else:
        result = workers._stage(request, context)
        assert result["outcome"] == "stage_complete"
        assert result["selected_model"] == "gpt-test"
        assert result["observed_model"] == "gpt-test-build"
        assert result["observed_models"] == ["gpt-test-build"]
        assert result["session_id"] == "stage-native"
        assert Path(result["transcript_path"]).is_file()
