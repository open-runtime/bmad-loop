"""Exact original input reaches the mapper without replacing native review passes."""

import dataclasses
import hashlib
import json
import sys

import pytest
from conftest import git
from test_campaign_codex_review import finding, review
from test_campaign_headless import prepared as prepared

from bmad_loop import campaign_codex_appserver
from bmad_loop.campaign_codex_review import fold_result
from bmad_loop.campaign_headless import render_input
from bmad_loop.campaign_headless_protocol import parse_output
from bmad_loop.campaign_model import CampaignError


@pytest.fixture
def native(project, tmp_path, prepared):
    workspace = project.project
    head = git(workspace, "rev-parse", "HEAD").strip()
    grants = tmp_path / "mapping-authority" / "grants.json"
    grants.parent.mkdir()
    grants.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "workspace": str(workspace),
                "permissions": "read-only-tools",
                "scopes": [],
                "config_digest": "frozen",
                "native_review_target": {"head": head, "base": head},
            }
        ),
        encoding="utf-8",
    )
    source = tmp_path / "immutable-source.txt"
    source.write_text(
        "CANARY 0: aabbcc\nAcceptance: preserve café and 日本語.\nLast line remains exact.\n",
        encoding="utf-8",
    )
    _, _, request, _, cap = prepared
    request = dataclasses.replace(
        request,
        output_schema={
            "type": "object",
            "properties": {
                "nonce": {"type": "string"},
                "markers": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["nonce", "markers"],
            "additionalProperties": False,
        },
        prompt="QUALIFICATION NONCE abcdef\nReview the exact accepted scope; never waive it.",
        inputs=(
            {
                "source_id": "immutable-canary",
                "path": str(source),
                "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                "tokens": len(source.read_bytes()),
            },
        ),
    )
    cap = {**cap, "input_mode": "prompt-file"}
    rendered, delivery = render_input(request, cap)
    input_path = tmp_path / "original-input.txt"
    input_path.write_bytes(rendered)
    executable = tmp_path / "native-codex"
    body = r"""import json,re,sys
from pathlib import Path
root=Path(__file__).parent
store=root/'rollouts';store.mkdir(exist_ok=True)
parent=store/'parent.jsonl';parent.write_text(json.dumps({'type':'session_meta','payload':{'id':'native'}})+'\n')
config=json.loads((root/'config.json').read_text())
def emit(value):print(json.dumps(value),flush=True)
count=0;pending=None
for line in sys.stdin:
 r=json.loads(line)
 with (root/'requests.jsonl').open('a') as stream:stream.write(line)
 method=r.get('method');p=r.get('params',{})
 if method=='initialize':emit({'id':r['id'],'result':{}})
 elif method=='thread/start':
  model=p['model'];effort=p['config']['model_reasoning_effort']
  emit({'id':r['id'],'result':{'thread':{'id':'native','path':str(parent)},'model':model,'reasoningEffort':effort,'sandbox':{'type':'readOnly'}}})
 elif method in ('review/start','turn/start'):
  count+=1;turn='t'+str(count);pending=(method,p,turn)
  emit({'id':r['id'],'result':{'turn':{'id':turn}}})
  emit({'id':'approval'+str(count),'method':'item/permissions/requestApproval','params':{'threadId':'native','turnId':turn}})
 elif str(r.get('id','')).startswith('approval'):
  assert r['error']['code']==-32601
  method,p,turn=pending
  failed=config['fail_turn']==count
  if method=='review/start':
   artifact=config['reviews'][count-1]
   item={'id':'review'+str(count),'type':'exitedReviewMode','review':json.dumps(artifact)}
   emit({'method':'turn/started','params':{'threadId':'native','turn':{'id':'child'+str(count),'rootTurnId':turn}}})
   with parent.open('a') as out:out.write(json.dumps({'type':'event_msg','payload':{'type':'item_completed','thread_id':'native','turn_id':turn,'item':{'type':'ExitedReviewMode','id':'review'+str(count),'review_output':artifact}}})+'\n')
   rows=[{'type':'session_meta','payload':{'id':'review-child'+str(count),'parent_thread_id':'native','source':{'subagent':'review'}}},
         {'type':'event_msg','payload':{'type':'task_started','turn_id':'child'+str(count),'root_turn_id':turn,'turn_attribution':{'parent_turn_id':turn}}},
         {'type':'turn_context','payload':{'turn_id':'child'+str(count),'root_turn_id':turn,'model':model,'effort':effort}},
         {'type':'event_msg','payload':{'type':'token_count','info':{'total_token_usage':{'input_tokens':10,'output_tokens':10,'cached_input_tokens':0,'reasoning_output_tokens':0}}}},
         {'type':'event_msg','payload':{'type':'task_complete','turn_id':'child'+str(count),'root_turn_id':turn}}]
   (store/('child'+str(count)+'.jsonl')).write_text(''.join(json.dumps(row)+'\n' for row in rows))
   item['review']='Native formatted review explanation; original input is not copied here.'
  else:
   # Model only its actual current-turn inputs. Do not retain a delegate prompt.
   text='\n'.join(part['text'] for part in p['input'])
   nonce=re.search(r'QUALIFICATION NONCE ([a-f0-9]+)',text)
   result={'nonce':nonce[1] if nonce else '', 'markers':re.findall(r'CANARY [0-9]+: ([a-f0-9]+)',text)}
   item={'id':'output','type':'agentMessage','text':json.dumps(result)}
  emit({'method':'thread/tokenUsage/updated','params':{'threadId':'native','turnId':turn,'tokenUsage':{'total':{'inputTokens':100*count,'outputTokens':80*count,'cachedInputTokens':0,'reasoningOutputTokens':count}}}})
  emit({'method':'turn/completed','params':{'threadId':'native','turn':{'id':turn,'status':'failed' if failed else 'completed','items':[item]}}})
"""
    executable.write_text(f"#!{sys.executable}\n" + body, encoding="utf-8")
    executable.chmod(0o700)
    return workspace, head, grants, source, request, rendered, delivery, executable


def perform(native, tmp_path, capsys, pair, fail_turn=0):
    workspace, head, grants, source, request, rendered, delivery, executable = native
    primary = finding()
    primary["title"] = "[P1] Main finding remains required"
    custom = {**finding(), "title": "[P2] Custom finding remains required", "priority": 2}
    (tmp_path / "config.json").write_text(
        json.dumps({"fail_turn": fail_turn, "reviews": [review([primary]), review([custom])]}),
        encoding="utf-8",
    )
    before = {p: p.read_bytes() for p in (source, grants, tmp_path / "original-input.txt")}
    assert (
        campaign_codex_appserver.run(
            executable,
            workspace=workspace,
            model="gpt-test",
            effort="high",
            prompt=rendered.decode("utf-8"),
            schema=request.output_schema,
            review=True,
            review_pair=pair,
            native_sandbox=True,
            grants_path=grants,
        )
        == 0
    )
    output = capsys.readouterr().out
    calls = [
        json.loads(line)
        for line in (tmp_path / "requests.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert all(path.read_bytes() == content for path, content in before.items())
    assert (
        git(workspace, "rev-parse", "HEAD").strip() == head
        and not git(workspace, "status", "--porcelain").strip()
    )
    assert delivery["sources"][0]["sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()
    start = next(row["params"] for row in calls if row.get("method") == "thread/start")
    assert start["approvalPolicy"] == "never" and start["sandbox"] == "read-only"
    assert start["allowProviderModelFallback"] is False and "dynamicTools" not in start
    assert start["config"] == {"model_reasoning_effort": "high", "review_model": "gpt-test"}
    approvals = [row for row in calls if str(row.get("id", "")).startswith("approval")]
    assert approvals and all(row["error"]["code"] == -32601 for row in approvals)
    assert not any(row.get("method") == "command/exec" for row in calls)
    return calls, output, primary, custom


@pytest.mark.parametrize("pair", [False, True], ids=["single", "main-custom-pair"])
def test_mapping_receives_exact_task_sources_and_keeps_all_native_findings(
    native, tmp_path, capsys, pair
):
    calls, output, primary, custom = perform(native, tmp_path, capsys, pair)
    workspace, head, grants, source, request, rendered, delivery, executable = native
    reviews = [row for row in calls if row.get("method") == "review/start"]
    mappings = [row for row in calls if row.get("method") == "turn/start"]
    assert len(reviews) == (2 if pair else 1) and len(mappings) == 1
    if pair:
        assert reviews[0]["params"]["target"] == {"type": "baseBranch", "branch": head}
        assert reviews[1]["params"]["target"]["type"] == "custom"
        assert reviews[1]["params"]["target"]["instructions"].endswith(rendered.decode("utf-8"))
    else:
        assert reviews[0]["params"]["target"] == {
            "type": "custom",
            "instructions": rendered.decode("utf-8"),
        }
    mapping = mappings[0]["params"]
    assert mapping["input"][1:] == [{"type": "text", "text": rendered.decode("utf-8")}]
    assert mapping["outputSchema"] == request.output_schema and mapping["effort"] == "high"
    assert "Retain every unresolved finding" in mapping["input"][0]["text"]
    parsed = parse_output(output, protocol="codex-native-review", selected_model="gpt-test")
    assert parsed["result"]["nonce"] == "abcdef" and parsed["result"]["markers"] == ["aabbcc"]
    assert parsed["turns"] == (3 if pair else 2)
    folded = fold_result(parsed["result"], parsed["native_review"])
    assert folded["verdict"] == "changes_requested"
    assert [f["title"] for f in folded["findings"]] == [primary["title"]] + (
        [custom["title"]] if pair else []
    )
    assert all(f["required"] and not f["resolved"] for f in folded["findings"])


@pytest.mark.parametrize("pair,failed", [(False, 1), (True, 1), (True, 2)])
def test_failed_native_review_cannot_start_mapping(native, tmp_path, capsys, pair, failed):
    calls, output, _, _ = perform(native, tmp_path, capsys, pair, fail_turn=failed)
    assert not any(row.get("method") == "turn/start" for row in calls)
    assert len([row for row in calls if row.get("method") == "review/start"]) == failed


def test_changed_original_source_cannot_be_rendered(native):
    _, _, _, source, request, _, _, _ = native
    source.write_text("mutated owner input", encoding="utf-8")
    with pytest.raises(CampaignError, match="source changed"):
        render_input(
            request,
            {
                "input_tokens": 100000,
                "reserved_tokens": 0,
                "context_tokens": 200000,
                "input_mode": "prompt-file",
            },
        )
