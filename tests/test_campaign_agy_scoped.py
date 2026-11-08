"""AGY native argument rewriting keeps all model commands inside scoped tools."""

import json
import shlex
import sys
from pathlib import Path

import pytest
from test_campaign_headless import ChildMux
from test_campaign_headless import prepared as prepared

from bmad_loop import (
    campaign_agy_launcher,
    campaign_agy_scoped,
    campaign_execution_sandbox,
    campaign_native_git,
    campaign_object_promotion,
    campaign_scoped_tools,
)
from bmad_loop.campaign_execution_qualification import qualify
from bmad_loop.campaign_headless import HeadlessRunner, file_digest, read_json, validate_capability
from bmad_loop.campaign_model import RoleCandidate


def test_hook_rewrites_exact_shell_input_and_rejects_other_tools_or_changed_call(tmp_path):
    (tmp_path / "invocations.json").write_text(json.dumps({"session": "native", "count": 1}))
    command = "printf '%s' 'literal $HOME `id`'"
    payload = {
        "conversationId": "native",
        "stepIdx": 1,
        "toolCall": {"name": "run_command", "args": {"CommandLine": command}},
    }
    kwargs = dict(
        launcher=tmp_path / "launcher",
        grants=tmp_path / "grants.json",
        directory=tmp_path,
        schema={"type": "object"},
    )
    result = campaign_agy_scoped.tool_hook(payload, **kwargs)
    assert result["decision"] == "allow"
    assert result["overwrite"]["CommandLine"] != command
    argv = shlex.split(result["overwrite"]["CommandLine"])
    assert argv[0] == str(tmp_path / "launcher") and "--execute" in argv
    assert campaign_agy_scoped.tool_hook(payload, **kwargs) == result
    payload["toolCall"]["args"]["CommandLine"] = "other"
    assert campaign_agy_scoped.tool_hook(payload, **kwargs)["decision"] == "deny"
    payload["stepIdx"] = 2
    for name in ("write_to_file", "mcp.run_command", "view_file", "send_message"):
        payload["toolCall"]["name"] = name
        assert campaign_agy_scoped.tool_hook(payload, **kwargs)["decision"] == "deny"
    payload["toolCall"]["name"] = "run_command"
    payload["conversationId"] = "other"
    assert campaign_agy_scoped.tool_hook(payload, **kwargs)["decision"] == "deny"


@pytest.mark.skipif(sys.platform != "darwin", reason="actual macOS scoped command boundary")
def test_agy_native_hook_recipe_commits_through_bounded_tool_and_qualifies(prepared, tmp_path):
    _, _, _, original, cap = prepared
    native = tmp_path / "native-agy"
    native.write_text(f"#!{sys.executable}\n" + r"""import json,re,shlex,subprocess,sys
from pathlib import Path
def hook(command,payload):
 completed=subprocess.run(shlex.split(command),input=json.dumps(payload),capture_output=True,text=True,check=True)
 return json.loads(completed.stdout)
hooks=json.loads(Path('.agents/hooks.json').read_text())['bmad-scoped-writer']
invocation=hooks['PreInvocation'][0]['command'];tool=hooks['PreToolUse'][0]['hooks'][0]['command']
assert 'injectSteps' in hook(invocation,{'conversationId':'native','invocationNum':0})
text=json.load(sys.stdin)['message']['content'];nonce=re.search(r'QUALIFICATION NONCE ([a-f0-9]+)',text)[1]
code="from pathlib import Path; import subprocess; Path('src/owned.txt').write_text('native writer qualification: "+nonce+"\\n'); subprocess.run(['git','add','src/owned.txt'],check=True); subprocess.run(['git','commit','-qm','native agy proof'],check=True)"
payload={'conversationId':'native','stepIdx':1,'toolCall':{'name':'run_command','args':{'CommandLine':shlex.join([sys.executable,'-c',code])}}}
rewritten=hook(tool,payload);assert rewritten['decision']=='allow'
completed=subprocess.run(shlex.split(rewritten['overwrite']['CommandLine']),capture_output=True,text=True)
assert completed.returncode==0,completed.stderr+completed.stdout
assert json.loads(completed.stdout)['success']
result={'nonce':nonce,'markers':re.findall(r'CANARY [0-9]+: ([a-f0-9]+)',text)}
assert hook(tool,{'toolCall':{'name':'finish','args':result}})['decision']=='allow'
args=sys.argv[1:]
print(json.dumps({'event':'init','conversation_id':'native','init':{'model':args[args.index('--model')+1]}}))
print(json.dumps({'event':'result','result':{'conversation_id':'native','status':'SUCCESS','num_turns':1,'structured_output':result,'usage':{'input_tokens':2000,'output_tokens':512}}}))
""")
    native.chmod(0o700)
    launcher = tmp_path / "agy-launcher"
    launcher.write_text(campaign_agy_scoped.launcher_text())
    launcher.chmod(0o700)
    cap = {
        **cap,
        "protocol": "agy-json",
        "permissions": "workspace-tools",
        "efforts": ["high"],
        "confinement": campaign_agy_scoped.CONFINEMENT,
        "binary": str(launcher),
        "binary_sha256": file_digest(launcher),
        "input_mode": "prompt-file",
        "max_turns": 8,
        "argv": [
            str(launcher),
            "--native",
            str(native),
            "--grants",
            "{grants_path}",
            "--model",
            "{model}",
            "--effort",
            "{effort}",
            "--input",
            "{input_path}",
            "--schema",
            "{schema_path}",
            "--max-invocations",
            "{max_turns}",
        ],
        "dependencies": [
            {"path": str(p), "sha256": file_digest(p)}
            for p in (
                native,
                Path(sys.executable).resolve(),
                Path(campaign_agy_scoped.__file__).resolve(),
                Path(campaign_agy_launcher.__file__).resolve(),
                Path(campaign_scoped_tools.__file__).resolve(),
                Path(campaign_execution_sandbox.__file__).resolve(),
                Path(campaign_native_git.__file__).resolve(),
                Path(campaign_object_promotion.__file__).resolve(),
            )
        ],
    }
    cap.pop("qualification")
    proposal = tmp_path / "proposal.json"
    proposal.write_text(json.dumps(cap))
    report = qualify(
        proposal,
        tmp_path / "qualification",
        input_bytes=4096,
        output_tokens=4096,
        timeout_s=30,
        runner=HeadlessRunner(ChildMux()),
    )
    assert report["ok"], report
    assert report["checks"]["scoped_native_calls"]
    earned = read_json(Path(report["capability_path"]))
    validate_capability(
        earned,
        RoleCandidate(original.profile, original.model, "high"),
        permissions="workspace-tools",
    )
