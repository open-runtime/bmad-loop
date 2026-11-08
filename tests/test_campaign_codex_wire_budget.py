"""Native RPC framing has a finite budget distinct from model output tokens."""

import json
import sys
from pathlib import Path

import pytest
from test_campaign_codex_scoped_tools import scoped_writer as scoped_writer
from test_campaign_execution_qualification import writer as writer
from test_campaign_headless import ChildMux
from test_campaign_headless import prepared as prepared

from bmad_loop.campaign_execution_qualification import qualify
from bmad_loop.campaign_headless import HeadlessRunner, file_digest, read_json


@pytest.mark.skipif(sys.platform != "darwin", reason="actual scoped native transport")
@pytest.mark.parametrize("overflow", [False, True], ids=["native-framing", "finite-ceiling"])
def test_large_rpc_echo_and_delta_framing_has_its_own_finite_bound(
    scoped_writer, tmp_path, overflow
):
    proposal, _, cap = scoped_writer
    native = Path(cap["argv"][2])
    body = native.read_text()
    needle = '  code="from pathlib import Path;'
    assert needle in body
    framing = """  for kind in ('started','completed'):
   emit({'method':'item/'+kind,'params':{'threadId':'native','turnId':'turn','item':{'type':'userMessage','content':[{'text':prompt}]}}})
  for index in range(800):
   emit({'method':'item/agentMessage/delta','params':{'threadId':'native','turnId':'turn','itemId':'progress','delta':'x','padding':'x'*100}})
"""
    if overflow:
        framing += """  for index in range(32):
   emit({'method':'item/agentMessage/delta','params':{'threadId':'native','turnId':'turn','itemId':'progress','delta':'x'*2097152}})
"""
    native.write_text(body.replace(needle, framing + needle, 1))
    cap.update(input_tokens=150000, context_tokens=200000, output_tokens=16384)
    for row in cap["dependencies"]:
        if row["path"] == str(native):
            row["sha256"] = file_digest(native)
    proposal.write_text(json.dumps(cap))
    report = qualify(
        proposal,
        tmp_path / "qualification",
        input_bytes=131072,
        output_tokens=16384,
        timeout_s=30,
        runner=HeadlessRunner(ChildMux()),
    )
    job = tmp_path / "qualification" / "job"
    receipt = read_json(job / "receipt.json")
    stdout_bytes = (job / "stdout.jsonl").stat().st_size
    assert 0 < receipt["wire_output_limit_bytes"] < 40 * 1024 * 1024
    assert receipt["processes_quiescent"]
    if overflow:
        assert not report["ok"]
        assert stdout_bytes > receipt["wire_output_limit_bytes"]
        assert receipt["outcome"] == "limit" and receipt["limit_kind"] == "stdout-wire-bytes"
    else:
        # The real failing native run crossed327680 bytes on input echoes and
        # per-token frames before its final result, with its token budget intact.
        assert stdout_bytes > 327680
        assert report["ok"], report
        assert receipt["usage"]["output_tokens"] == 80
        assert receipt["input_complete"] and report["checks"]["scoped_commit"]
