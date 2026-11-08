"""Real qualified headless transport for scripted production campaign services.

Only model behavior is scripted. The child engine, adapter selection, scoped
writer/reader sandbox, native JSON, owned subprocess completion, checkpoints and
publication remain production paths. Qualification executes real canaries with
zero LLM tokens; it does not inject successful qualification receipts.
"""

import json
import site
import sys
from pathlib import Path

from test_campaign_headless import ChildMux
from test_campaign_runtime import SCRIPT

from bmad_loop import campaign_execution_sandbox, campaign_native_git, campaign_object_promotion
from bmad_loop.campaign_execution_qualification import launcher_text, qualify
from bmad_loop.campaign_headless import HeadlessRunner, file_digest, read_json

NATIVE = r"""
import re
text = Path(sys.argv[1]).read_text()
model, permission = sys.argv[2:4]
nonce = re.search(r'QUALIFICATION NONCE ([a-f0-9]+)', text)
if nonce:
    markers = re.findall(r'CANARY [0-9]+: ([a-f0-9]+)', text)
    artifact = {'nonce': nonce[1], 'markers': markers}
    if permission == 'workspace-tools':
        Path('src/owned.txt').write_text('native writer qualification: ' + nonce[1] + '\n')
        verify.commit_paths(Path.cwd(), 'native writer qualification', [Path('src/owned.txt')])
    else:
        artifact['workspace_readback'] = Path('src/owned.txt').read_text()
    session = 'qualified-scripted-native'
else:
    run_dir = Path(os.environ['BMAD_LOOP_RUN_DIR'])
    marker = run_dir / 'campaign-worker.json'
    request_path = (Path(json.loads(marker.read_text())['execution']).parent / 'request.json'
                    if marker.exists() else run_dir.parent / 'request.json')
    request = json.loads(request_path.read_text())
    result = execution(request, None) if request['kind'] == 'execution' else stage(request, None)
    artifact = result.result_json
    session = result.session_id
print(json.dumps({'type':'system', 'model':model, 'session_id':session}))
print(json.dumps({'type':'result','structured_output':artifact,'usage':{'input_tokens':1000,'output_tokens':512}}))
"""


def qualify_case(case, prepared, tmp_path):
    if sys.platform != "darwin":
        import pytest

        pytest.skip("The scripted writer and reader qualify the macOS scoped sandbox")
    root = tmp_path / "scripted-native"
    root.mkdir()
    script = root / "native.py"
    # Keep the existing strict model behavior (early draft, source ancestry,
    # parallel start barrier, acceptance proof) while changing its transport.
    body = SCRIPT[: SCRIPT.index("def adapters(request,envelope")]
    body = (
        body.replace("__GH_FILE__", repr(str(case.service.state_file)))
        .replace("__EVENTS__", repr(str(case.events)))
        .replace("__MARKERS__", repr(str(tmp_path / "parallel-markers")))
    )
    source_root = str(Path(campaign_execution_sandbox.__file__).resolve().parents[1])
    script.write_text(
        "import sys\nsys.path[:0] = "
        + repr([source_root, *site.getsitepackages()])
        + "\n"
        + body
        + NATIVE
    )
    binary = root / "sandbox-launcher"
    binary.write_text(launcher_text())
    binary.chmod(0o700)
    interpreter = Path(sys.executable).resolve()
    sandbox = Path(campaign_execution_sandbox.__file__).resolve()
    git_dependencies = (
        Path(campaign_native_git.__file__).resolve(),
        Path(campaign_object_promotion.__file__).resolve(),
    )
    original = prepared[4]
    case.events.touch()
    (tmp_path / "parallel-markers").mkdir()
    routes = {}
    for profile, model, effort, permissions in (
        ("codex", "gpt-6-sol", "", "workspace-tools"),
        ("claude", "claude-fable-5-1", "high", "workspace-tools"),
        ("claude", "claude-fable-5-1", "high", "read-only-tools"),
        ("codex", "gpt-6-astra", "high", "read-only-tools"),
    ):
        cap = {
            **{key: value for key, value in original.items() if key != "qualification"},
            "profile": profile,
            "model": model,
            "efforts": [effort],
            "binary": str(binary),
            "binary_sha256": file_digest(binary),
            "dependencies": [
                {"path": str(path), "sha256": file_digest(path)}
                for path in (script, sandbox, interpreter, *git_dependencies)
            ],
            "permissions": permissions,
            "runtime_write_paths": [str(case.events), str(tmp_path / "parallel-markers")],
            "context_tokens": 1100000,
            "input_tokens": 1000000,
            "argv": [
                str(binary),
                "--grants",
                "{grants_path}",
                "--",
                str(interpreter),
                str(script),
                "{input_path}",
                model,
                permissions,
            ],
        }
        key = model + "-" + permissions
        proposal = root / (key + ".json")
        proposal.write_text(json.dumps(cap))
        report = qualify(
            proposal,
            root / key,
            input_bytes=900000,
            output_tokens=4096,
            timeout_s=30,
            runner=HeadlessRunner(ChildMux()),
        )
        assert report["ok"], report
        earned = read_json(Path(report["capability_path"]))
        mode = "execution" if permissions == "workspace-tools" else "reasoning"
        routes.setdefault(profile + ":" + model, {})[mode] = earned
    manifest = root / "capabilities.json"
    manifest.write_text(json.dumps({"schema_version": 1, "routes": routes}))
    # The production fixture still owns actual engine PIDs and reads their real
    # receipts. This bootstrap changes only the zero-token terminal backend.
    bootstrap = """import sys
from pathlib import Path
from bmad_loop import campaign_workers as workers, campaign_headless
from bmad_loop.adapters import multiplexer
sys.path.insert(0, str(Path(workers.__file__).resolve().parents[2] / "tests"))
from test_campaign_headless import ChildMux
mux = ChildMux()
mux.name = "scripted-native"
mux.available = lambda: True
mux.version = lambda: "scripted-v1"
mux.list_window_ids = lambda session: [key for key, process in mux.processes.items() if process.poll() is None]
multiplexer.configure_multiplexer = lambda *args, **kwargs: None
multiplexer.get_multiplexer = lambda: mux
multiplexer.get_multiplexer.cache_clear = lambda: None
campaign_headless.get_multiplexer = lambda: mux
raise SystemExit(workers.child_main(Path(sys.argv[1])))
"""
    (tmp_path / "scripted-coding-cli.py").write_text(bootstrap)
    return manifest
