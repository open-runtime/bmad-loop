"""Scripted Codex app-server routing, native attribution and review semantics."""

import json
import sys

import pytest

from bmad_loop.campaign_codex_appserver import envelopes, run
from bmad_loop.campaign_headless_protocol import parse_output
from bmad_loop.campaign_model import CampaignError
from bmad_loop.campaign_qualification import _no_tools


def frames(status="completed", error=None):
    return [
        {"id": 2, "result": {"thread": {"id": "native"}, "model": "gpt-test"}},
        {"id": 3, "result": {"turn": {"id": "turn"}}},
        {
            "method": "thread/tokenUsage/updated",
            "params": {
                "threadId": "native",
                "turnId": "turn",
                "tokenUsage": {
                    "total": {
                        "inputTokens": 100,
                        "outputTokens": 20,
                        "cachedInputTokens": 30,
                        "reasoningOutputTokens": 10,
                    }
                },
            },
        },
        {
            "method": "turn/completed",
            "params": {
                "threadId": "native",
                "turn": {
                    "id": "turn",
                    "status": status,
                    "error": error,
                    "items": [{"type": "agentMessage", "id": "message", "text": '{"ok":true}'}],
                },
            },
        },
    ]


def parse(rows):
    return parse_output(json.dumps(rows), protocol="codex-app-server", selected_model="gpt-test")


def test_native_rpc_model_usage_and_result_are_bound_to_one_turn():
    rows = frames()
    rows.insert(3, rows[2])
    result = parse(rows)
    assert result["result"] == {"ok": True}
    assert result["session_id"] == "native" and result["observed_model"] == "gpt-test"
    assert result["usage"] == {
        "input_tokens": 100,
        "output_tokens": 20,
        "cache_read_tokens": 30,
        "reasoning_tokens": 10,
    }
    assert result["turns"] == 1


@pytest.mark.parametrize(
    "code,outcome",
    [
        ("usageLimitExceeded", "quota"),
        ("unauthorized", "authentication"),
        ("contextWindowExceeded", "limit"),
    ],
)
def test_native_rpc_error_code_is_typed(code, outcome):
    assert (
        parse(frames("failed", {"codexErrorInfo": code, "message": "opaque"}))["outcome"] == outcome
    )
    assert parse(frames("interrupted"))["outcome"] == "canceled"


def test_native_rpc_rejects_cross_thread_or_turn_observations():
    for key in ("threadId", "turnId"):
        rows = frames()
        rows[2]["params"][key] = "other"
        with pytest.raises(CampaignError, match="another thread|identity changed"):
            parse(rows)
    rows = frames()
    rows[0]["result"].pop("model")
    with pytest.raises(CampaignError, match="model identity"):
        parse(rows)
    with pytest.raises(CampaignError, match="multiple completed"):
        parse(frames() + [frames()[-1]])


def test_native_rpc_review_uses_actual_review_artifact():
    rows = frames()
    rows[-1]["params"]["turn"]["items"].append(
        {"type": "exitedReviewMode", "id": "review", "review": '{"findings":[]}'}
    )
    assert parse(rows)["result"] == {"findings": []}


def test_partial_rpc_is_never_completion():
    assert envelopes(frames()[:-1], partial=True) == [
        {"type": "system", "model": "gpt-test", "session_id": "native"}
    ]
    with pytest.raises(CampaignError, match="terminal turn is missing"):
        envelopes(frames()[:-1])


def test_tools_capable_codex_route_cannot_earn_no_tools():
    with pytest.raises(CampaignError, match="No qualified explicit no-tools"):
        _no_tools({"protocol": "codex-app-server", "argv": ["codex", "--tools", ""]})


@pytest.mark.parametrize("review", [False, True])
def test_client_runs_one_native_turn_or_review_and_preserves_frames(tmp_path, capsys, review):
    native = tmp_path / "codex"
    native.write_text(
        f"#!{sys.executable}\n" + """import json,sys
from pathlib import Path
def emit(value): print(json.dumps(value),flush=True)
thread='native';turn='turn';review=False;count=0
for line in sys.stdin:
 r=json.loads(line)
 with Path('requests.jsonl').open('a') as f:f.write(line)
 method=r.get('method')
 if method=='initialize':emit({'id':1,'result':{}})
 elif method=='thread/start':
  p=r['params'];emit({'id':2,'result':{'thread':{'id':thread},'model':p['model'],'reasoningEffort':p['config']['model_reasoning_effort'],'sandbox':{'type':'readOnly' if p['sandbox']=='read-only' else 'dangerFullAccess'}}})
 elif method in ('turn/start','review/start'):
  count+=1;turn='turn'+str(count);review=method=='review/start'
  emit({'id':r['id'],'result':{'turn':{'id':turn}}})
  emit({'id':'approval','method':'item/permissions/requestApproval','params':{'threadId':thread,'turnId':turn}})
 elif r.get('id')=='approval':
  assert r['error']['code']==-32601
  emit({'method':'thread/tokenUsage/updated','params':{'threadId':thread,'turnId':turn,'tokenUsage':{'total':{'inputTokens':100,'outputTokens':20,'cachedInputTokens':0,'reasoningOutputTokens':1}}}})
  artifact={'findings':[],'overall_correctness':'patch is correct','overall_explanation':'No defect','overall_confidence_score':0.9}
  item={'id':'review','type':'exitedReviewMode','review':json.dumps(artifact)} if review else {'id':'message','type':'agentMessage','text':'{"ok":true}'}
  emit({'method':'turn/completed','params':{'threadId':thread,'turn':{'id':turn,'status':'completed','items':[item]}}})
""",
        encoding="utf-8",
    )
    native.chmod(0o700)
    assert (
        run(
            native,
            workspace=tmp_path,
            model="gpt-test",
            effort="high",
            prompt="exact input",
            schema={"type": "object"},
            review=review,
        )
        == 0
    )
    native_output = capsys.readouterr().out
    result = parse_output(
        native_output,
        protocol="codex-native-review" if review else "codex-app-server",
        selected_model="gpt-test",
    )
    assert result["result"] == {"ok": True}
    requests = [json.loads(line) for line in (tmp_path / "requests.jsonl").read_text().splitlines()]
    assert [r.get("method") for r in requests] == [
        "initialize",
        "initialized",
        "thread/start",
        "review/start" if review else "turn/start",
        None,
    ] + (["turn/start", None] if review else [])
    start = requests[2]["params"]
    assert start["allowProviderModelFallback"] is False and start["approvalPolicy"] == "never"
    if review:
        assert requests[3]["params"]["target"] == {"type": "custom", "instructions": "exact input"}
        assert start["sandbox"] == "read-only"
    else:
        assert requests[3]["params"]["outputSchema"] == {"type": "object"}
