"""Native review is independently qualified and cannot lose original findings."""

import json
import sys
from pathlib import Path

import pytest
from test_campaign_codex_appserver import frames
from test_campaign_execution_qualification import writer as writer
from test_campaign_headless import ChildMux
from test_campaign_headless import prepared as prepared
from test_campaign_readonly_qualification import readonly as readonly

from bmad_loop import (
    campaign_codex_appserver,
    campaign_codex_review,
    campaign_codex_review_evidence,
    campaign_codex_review_pair,
    campaign_readonly_qualification,
)
from bmad_loop.campaign_codex_review import fold_result
from bmad_loop.campaign_execution_qualification import qualify
from bmad_loop.campaign_headless import (
    HeadlessRunner,
    capability_for,
    file_digest,
    load_capabilities,
    read_json,
    validate_capability,
)
from bmad_loop.campaign_headless_protocol import parse_output
from bmad_loop.campaign_model import CampaignError


def review(findings=()):
    return {
        "findings": list(findings),
        "overall_correctness": "patch is incorrect" if findings else "patch is correct",
        "overall_explanation": "Native review proof",
        "overall_confidence_score": 0.9,
    }


def finding():
    return {
        "title": "[P1] Retain required validation",
        "body": "The final candidate bypasses required validation.",
        "confidence_score": 0.9,
        "priority": 1,
        "code_location": {
            "absolute_file_path": "/repo/file.py",
            "line_range": {"start": 1, "end": 2},
        },
    }


def combined():
    first = frames()
    first[-1]["params"]["turn"]["items"] = [
        {"id": "review", "type": "exitedReviewMode", "review": json.dumps(review([finding()]))}
    ]
    second = frames()[1:]
    second[0] = {"id": 4, "result": {"turn": {"id": "turn-two"}}}
    second[1]["params"]["turnId"] = "turn-two"
    second[2]["params"]["turn"]["id"] = "turn-two"
    return first + second


def test_native_review_and_mapping_keep_cumulative_usage_and_findings():
    raw = parse_output(
        json.dumps(combined()), protocol="codex-native-review", selected_model="gpt-test"
    )
    assert raw["turns"] == 2 and raw["usage"]["output_tokens"] == 20
    assert raw["native_review"] == review([finding()])
    mapped = fold_result(
        {"status": "done", "verdict": "approved", "findings": []}, raw["native_review"]
    )
    assert mapped["verdict"] == "changes_requested"
    assert mapped["findings"][0]["body"] == finding()["body"]
    assert mapped["findings"][0]["required"] is True and mapped["findings"][0]["resolved"] is False
    assert mapped["native_review"] == raw["native_review"]


def test_native_review_requires_actual_artifact_and_both_turns():
    rows = combined()
    rows[3]["params"]["turn"]["items"] = [
        {"type": "agentMessage", "id": "fake", "text": json.dumps(review())}
    ]
    with pytest.raises(CampaignError, match="artifact is missing"):
        parse_output(json.dumps(rows), protocol="codex-native-review", selected_model="gpt-test")
    with pytest.raises(CampaignError, match="mapping turn is missing"):
        parse_output(
            json.dumps(combined()[:4]), protocol="codex-native-review", selected_model="gpt-test"
        )
    assert (
        parse_output(
            json.dumps(combined()[:4]),
            protocol="codex-native-review",
            selected_model="gpt-test",
            partial=True,
        )["outcome"]
        == "failed"
    )


def qualify_recipe(readonly, tmp_path, *, native_sandbox=True, review_pair=True, input_bytes=4096):
    """Run the production zero-token qualification recipe for stage seam tests."""
    proposal, candidate, cap = readonly
    native = tmp_path / "native-codex"
    native.write_text(
        f"#!{sys.executable}\n"
        + ("""import json,re,sys
from pathlib import Path
def emit(value):print(json.dumps(value),flush=True)
prompt='';count=0
use_snapshot=USE_NATIVE_SNAPSHOT
pair_mode=USE_NATIVE_PAIR
if use_snapshot:
 store=Path.cwd().parent/'rollouts';store.mkdir(exist_ok=True)
 parent=store/'parent.jsonl';parent.write_text(json.dumps({'type':'session_meta','payload':{'id':'native'}})+'\\n')
for line in sys.stdin:
 r=json.loads(line);method=r.get('method');p=r.get('params',{})
 if method=='initialize':emit({'id':r['id'],'result':{}})
 elif method=='command/exec':
  import subprocess
  from bmad_loop.campaign_execution_sandbox import command
  assert p['sandboxPolicy']=={'type':'readOnly','networkAccess':False}
  argv,cwd,env=command(Path(p['cwd']).parent/'job/grants.json',p['command'])
  completed=subprocess.run(argv,cwd=cwd,env=env,capture_output=True,text=True,timeout=20)
  emit({'id':r['id'],'result':{'exitCode':completed.returncode,'stdout':completed.stdout,'stderr':completed.stderr}})
 elif method=='thread/start':
  assert p['sandbox']=='read-only' and p['config']['review_model']==p['model']
  model=p['model'];effort=p['config']['model_reasoning_effort']
  emit({'id':r['id'],'result':{'thread':{'id':'native',**({'path':str(parent)} if use_snapshot else {})},'model':model,'reasoningEffort':effort,'sandbox':{'type':'readOnly'}}})
 elif method in ('review/start','turn/start'):
  count+=1;turn='t'+str(count)
  if method=='review/start':
   if pair_mode:assert p['target']['type']==('baseBranch' if count==1 else 'custom')
   if p['target']['type']=='baseBranch':
    import subprocess
    assert count==1
    subprocess.run(['git','merge-base','--is-ancestor',p['target']['branch'],'HEAD'],check=True)
   else:prompt=p['target']['instructions']
   item={'id':'review'+str(count),'type':'exitedReviewMode','review':json.dumps({'findings':[],'overall_correctness':'patch is correct','overall_explanation':'No defect','overall_confidence_score':0.9})}
  else:
   schema=p['outputSchema'];assert schema['additionalProperties'] is False
   if 'bmad_result_json' in schema['properties']:
    packet=json.JSONDecoder().raw_decode(prompt[prompt.index('{'):])[0]
    result={'bmad_result_json':json.dumps({'status':'done','schema_version':1,'coordinates':packet['coordinates'],'artifacts':[],'verdict':'approved','findings':[],'source_artifact_digest':packet['coordinates']['source_revision']})}
   else:result={'nonce':re.search(r'QUALIFICATION NONCE ([a-f0-9]+)',prompt)[1],'markers':re.findall(r'CANARY [0-9]+: ([a-f0-9]+)',prompt),'workspace_readback':Path('src/owned.txt').read_text()}
   item={'id':'output','type':'agentMessage','text':json.dumps(result)}
  emit({'id':r['id'],'result':{'turn':{'id':turn}}})
  if use_snapshot and method=='review/start':
   emit({'method':'turn/started','params':{'threadId':'native','turn':{'id':'child'+str(count),'rootTurnId':turn}}})
   with parent.open('a') as out:out.write(json.dumps({'type':'event_msg','payload':{'type':'item_completed','thread_id':'native','turn_id':turn,'item':{'type':'ExitedReviewMode','id':'review'+str(count),'review_output':json.loads(item['review'])}}})+'\\n')
   rows=[{'type':'session_meta','payload':{'id':'review-child'+str(count),'parent_thread_id':'native','source':{'subagent':'review'}}},
         {'type':'event_msg','payload':{'type':'task_started','turn_id':'child'+str(count),'root_turn_id':turn,'turn_attribution':{'parent_turn_id':turn}}},
         {'type':'turn_context','payload':{'turn_id':'child'+str(count),'root_turn_id':turn,'model':model,'effort':effort}},
         {'type':'event_msg','payload':{'type':'token_count','info':{'total_token_usage':{'input_tokens':10,'output_tokens':10,'cached_input_tokens':0,'reasoning_output_tokens':0}}}},
         {'type':'event_msg','payload':{'type':'task_complete','turn_id':'child'+str(count),'root_turn_id':turn}}]
   (store/('child'+str(count)+'.jsonl')).write_text(''.join(json.dumps(row)+'\\n' for row in rows))
   item['review']='Native formatted review explanation'
  emit({'method':'thread/tokenUsage/updated','params':{'threadId':'native','turnId':turn,'tokenUsage':{'total':{'inputTokens':100*count,'outputTokens':80*count,'cachedInputTokens':0,'reasoningOutputTokens':count}}}})
  emit({'method':'turn/completed','params':{'threadId':'native','turn':{'id':turn,'status':'completed','items':[item]}}})
""")
        .replace("USE_NATIVE_SNAPSHOT", str(native_sandbox))
        .replace("USE_NATIVE_PAIR", str(review_pair))
    )
    native.chmod(0o700)
    launcher = tmp_path / "appserver-launcher"
    launcher.write_text(campaign_codex_appserver.launcher_text())
    launcher.chmod(0o700)
    cap.update(protocol="codex-native-review", max_turns=2, input_mode="prompt-file")
    cap["argv"] = cap["argv"][:4] + [
        str(launcher),
        "--native",
        str(native),
        "--workspace",
        "{workspace}",
        "--model",
        "{model}",
        "--effort",
        "{effort}",
        "--input",
        "{input_path}",
        "--schema",
        "{schema_path}",
        "--review",
    ]
    cap["dependencies"] += [
        {"path": str(path), "sha256": file_digest(path)}
        for path in (
            launcher,
            native,
            Path(campaign_codex_appserver.__file__).resolve(),
            Path(campaign_codex_review.__file__).resolve(),
            Path(campaign_codex_review_evidence.__file__).resolve(),
            Path(campaign_readonly_qualification.__file__).resolve(),
        )
    ]
    if native_sandbox:
        cap.update(
            confinement="codex-native-read-only-v1",
            binary=str(launcher),
            binary_sha256=file_digest(launcher),
        )
        cap["argv"] = cap["argv"][4:] + ["--native-sandbox", "--grants", "{grants_path}"]
    if review_pair:
        cap["argv"].insert(cap["argv"].index("--review") + 1, "--review-pair")
        cap["max_turns"] = 3
        pair_source = Path(campaign_codex_review_pair.__file__).resolve()
        cap["dependencies"].append({"path": str(pair_source), "sha256": file_digest(pair_source)})
    proposal.write_text(json.dumps(cap))
    report = qualify(
        proposal,
        tmp_path / "qualification",
        input_bytes=input_bytes,
        output_tokens=4096,
        timeout_s=20,
        runner=HeadlessRunner(ChildMux()),
    )
    return report, candidate


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS OS sandbox is required")
@pytest.mark.parametrize(
    "native_sandbox,review_pair", [(False, False), (True, False), (True, True)]
)
def test_native_review_recipe_earns_separate_readonly_evidence_and_slot(
    readonly, tmp_path, native_sandbox, review_pair
):
    report, candidate = qualify_recipe(
        readonly, tmp_path, native_sandbox=native_sandbox, review_pair=review_pair
    )
    assert report["ok"] and report["checks"]["native_review"], report
    if review_pair:
        receipt = read_json(tmp_path / "qualification/job/receipt.json")
        assert receipt["turns"] == 3
        binding = receipt["native_review_evidence"]
        assert [row["kind"] for row in binding["reviews"]] == ["main", "custom"]
        assert len({row["binding"]["root_turn"] for row in binding["reviews"]}) == 2
    cap = read_json(Path(report["capability_path"]))
    validate_capability(cap, candidate, permissions="read-only-tools")
    manifest = tmp_path / "routes.json"
    key = candidate.profile + ":" + candidate.model
    manifest.write_text(json.dumps({"schema_version": 1, "routes": {key: {"review": cap}}}))
    routes = load_capabilities(manifest)
    assert (
        capability_for(
            routes, candidate.profile, candidate.model, "review", effort=candidate.effort
        )
        == cap
    )
    with pytest.raises(CampaignError, match="No qualified reasoning"):
        capability_for(
            routes, candidate.profile, candidate.model, "reasoning", effort=candidate.effort
        )
