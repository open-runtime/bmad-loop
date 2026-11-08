"""Native Codex callbacks keep builtins read-only and writing commands scoped."""

import json
import sys
from pathlib import Path

import pytest
from test_campaign_execution_qualification import writer as writer
from test_campaign_headless import ChildMux
from test_campaign_headless import prepared as prepared

from bmad_loop import (
    campaign_codex_appserver,
    campaign_codex_review,
    campaign_codex_review_evidence,
    campaign_execution_sandbox,
    campaign_readonly_qualification,
    campaign_scoped_tools,
)
from bmad_loop.campaign_execution_qualification import qualify, scoped_tool_checks
from bmad_loop.campaign_headless import HeadlessRunner, file_digest, read_json, validate_capability
from bmad_loop.campaign_model import CampaignError


@pytest.fixture
def scoped_writer(writer, tmp_path):
    proposal, candidate, cap = writer
    native = tmp_path / "native-codex"
    native.write_text(f"#!{sys.executable}\n" + r"""import json,re,sys,subprocess
from pathlib import Path
def emit(value):print(json.dumps(value),flush=True)
prompt=''; responses=0
for line in sys.stdin:
 r=json.loads(line);method=r.get('method');p=r.get('params',{})
 if method=='initialize':emit({'id':r['id'],'result':{}})
 elif method=='command/exec':
  from bmad_loop.campaign_execution_sandbox import command
  assert p['sandboxPolicy']=={'type':'readOnly','networkAccess':False}
  grant=Path(p['cwd']).parent/'native-readonly-grants.json'
  grant.write_text(json.dumps({'schema_version':1,'workspace':p['cwd'],'permissions':'read-only-tools','scopes':[],'config_digest':'readonly'}))
  argv,cwd,env=command(grant,p['command'])
  result=subprocess.run(argv,cwd=cwd,env=env,capture_output=True,text=True,timeout=20)
  emit({'id':r['id'],'result':{'exitCode':result.returncode,'stdout':result.stdout,'stderr':result.stderr}})
 elif method=='thread/start':
  assert p['sandbox']=='read-only'
  assert p['dynamicTools'][0]['name']=='bmad_shell'
  emit({'id':r['id'],'result':{'thread':{'id':'native'},'model':p['model'],'reasoningEffort':p['config']['model_reasoning_effort'],'sandbox':{'type':'readOnly'}}})
 elif method=='turn/start':
  schema=p['outputSchema']
  assert schema['additionalProperties'] is False, 'native strict schema requires additionalProperties=false'
  assert set(schema['required'])==set(schema['properties']), 'native strict schema requires every property'
  prompt=p['input'][0]['text'];nonce=re.search(r'QUALIFICATION NONCE ([a-f0-9]+)',prompt)[1]
  emit({'id':r['id'],'result':{'turn':{'id':'turn'}}})
  code="from pathlib import Path; import subprocess; Path('src/owned.txt').write_text('native writer qualification: "+nonce+"\\n'); subprocess.run(['git','add','src/owned.txt'],check=True); subprocess.run(['git','commit','-qm','native tool qualification'],check=True)"
  call={'id':'call-rpc','method':'item/tool/call','params':{'threadId':'native','turnId':'turn','tool':'bmad_shell','namespace':None,'callId':'write-once','arguments':{'command':[sys.executable,'-c',code]}}}
  emit(call)
 elif r.get('id')=='call-rpc':
  assert r['result']['success'],r
  responses+=1
  if responses==1:emit(call);continue
  result={'nonce':re.search(r'QUALIFICATION NONCE ([a-f0-9]+)',prompt)[1],'markers':re.findall(r'CANARY [0-9]+: ([a-f0-9]+)',prompt)}
  if 'bmad_result_json' in schema['properties']:result={'bmad_result_json':json.dumps(result)}
  emit({'method':'thread/tokenUsage/updated','params':{'threadId':'native','turnId':'turn','tokenUsage':{'total':{'inputTokens':100,'outputTokens':80,'cachedInputTokens':0,'reasoningOutputTokens':1}}}})
  emit({'method':'turn/completed','params':{'threadId':'native','turn':{'id':'turn','status':'completed','items':[{'id':'result','type':'agentMessage','text':json.dumps(result)}]}}})
""")
    native.chmod(0o700)
    launcher = tmp_path / "appserver-launcher"
    launcher.write_text(campaign_codex_appserver.launcher_text())
    launcher.chmod(0o700)
    cap.update(
        protocol="codex-app-server",
        max_turns=1,
        input_mode="prompt-file",
        confinement="codex-scoped-tools-v1",
        binary=str(launcher),
        binary_sha256=file_digest(launcher),
    )
    cap["argv"] = [
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
        "--scoped-tools",
        "--grants",
        "{grants_path}",
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
            Path(campaign_scoped_tools.__file__).resolve(),
            Path(campaign_execution_sandbox.__file__).resolve(),
        )
    ]
    proposal.write_text(json.dumps(cap))
    return proposal, candidate, cap


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS scoped process-session backend")
def test_scoped_native_writer_earns_both_boundaries_and_replays_no_commit(scoped_writer, tmp_path):
    proposal, candidate, _ = scoped_writer
    report = qualify(
        proposal,
        tmp_path / "qualification",
        input_bytes=4096,
        output_tokens=4096,
        timeout_s=30,
        runner=HeadlessRunner(ChildMux()),
    )
    assert report["ok"], report
    assert report["checks"]["native_builtin_confinement"]
    assert report["checks"]["scoped_native_calls"]
    cap = read_json(Path(report["capability_path"]))
    validate_capability(cap, candidate, permissions="workspace-tools")
    envelope = read_json(tmp_path / "qualification/job/request.json")
    directory = tmp_path / "qualification/job/scoped-tools"
    budget = read_json(directory / "budget.json")
    assert len(budget["calls"]) == 1
    record_path = directory / (budget["calls"][0] + ".json")
    record = read_json(record_path)
    record["receipt"]["processes_quiescent"] = False
    record_path.write_text(json.dumps(record))
    with pytest.raises(CampaignError, match="unfinished processes"):
        scoped_tool_checks(envelope)


@pytest.mark.parametrize(
    "field,value",
    [
        ("threadId", "other"),
        ("turnId", "other"),
        ("tool", "unqualified"),
        ("namespace", "unqualified"),
    ],
)
def test_scoped_tool_requests_cannot_cross_native_identity(
    project, tmp_path, monkeypatch, field, value
):
    calls = []

    class FakeTools:
        def __init__(self, *args):
            pass

        def execute(self, *args):
            calls.append(args)
            return {"success": True, "contentItems": []}

    monkeypatch.setattr(campaign_scoped_tools, "ScopedTools", FakeTools)
    request = {
        "threadId": "native",
        "turnId": "turn",
        "tool": "bmad_shell",
        "namespace": None,
        "callId": "call",
        "arguments": {"command": ["true"]},
        field: value,
    }
    native = tmp_path / "native"
    native.write_text(f"#!{sys.executable}\n" + f"""import json,sys
for line in sys.stdin:
 r=json.loads(line);p=r.get('params',{{}});m=r.get('method')
 if m=='initialize':print(json.dumps({{'id':1,'result':{{}}}}),flush=True)
 elif m=='thread/start':print(json.dumps({{'id':2,'result':{{'thread':{{'id':'native'}},'model':p['model'],'reasoningEffort':p['config']['model_reasoning_effort'],'sandbox':{{'type':'readOnly'}}}}}}),flush=True)
 elif m=='turn/start':
  print(json.dumps({{'id':3,'result':{{'turn':{{'id':'turn'}}}}}}),flush=True)
  print(json.dumps({{'id':'callback','method':'item/tool/call','params':{request!r}}}),flush=True)
 elif r.get('id')=='callback':
  print(json.dumps({{'method':'turn/completed','params':{{'threadId':'native','turn':{{'id':'turn','status':'completed','items':[]}}}}}}),flush=True)
""")
    native.chmod(0o700)
    grants = tmp_path / "grants.json"
    grants.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "workspace": str(project.project),
                "scopes": ["src"],
                "config_digest": "fixed",
            }
        )
    )
    with pytest.raises(CampaignError, match="invalid identity or authority"):
        campaign_codex_appserver.run(
            native,
            workspace=project.project,
            model="gpt-test",
            effort="high",
            prompt="test",
            schema={},
            scoped_tools=True,
            grants_path=grants,
        )
    assert not calls
