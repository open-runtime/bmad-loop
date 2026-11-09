"""Native writer qualification uses real scripts and Git, never inference in tests."""

import json
import sys
import uuid
from pathlib import Path

import pytest
from test_campaign_headless import ChildMux
from test_campaign_headless import prepared as prepared

from bmad_loop import campaign_execution_sandbox, campaign_native_git, campaign_object_promotion
from bmad_loop.campaign_execution_qualification import launcher_text, qualify, validate_proposal
from bmad_loop.campaign_headless import HeadlessRunner, file_digest, read_json, validate_capability
from bmad_loop.campaign_model import CampaignError


def fixture_writer_capability(tmp_path, basecap, candidate, *, input_bytes=80000):
    """Full synthetic writer proof for portable controller tests.

    Real Git readback and exact scoped-wrapper bindings remain in the proof.
    Only the OS denial probe is injected here; the separate macOS scripted
    qualification test exercises actual containment and native execution.
    """
    from conftest import git

    from bmad_loop import campaign_execution_qualification as qualification
    from bmad_loop.campaign_headless import write_json

    directory = (tmp_path / ("fixture-writer-" + uuid.uuid4().hex)).resolve()
    directory.mkdir()
    original = {
        **basecap,
        "profile": candidate.profile,
        "model": candidate.model,
        "efforts": [candidate.effort],
        "input_tokens": max(basecap["input_tokens"], input_bytes + 4096),
    }
    _, _, proposal = writer.__wrapped__((None, None, None, candidate, original), directory)
    script = directory / "writer.py"
    script.write_text(script.read_text().replace("gpt-test", candidate.model))
    proposal["dependencies"][0]["sha256"] = file_digest(script)
    runner = HeadlessRunner(ChildMux())
    root = directory / "canary"
    request, probe, grants = qualification.build(
        root,
        candidate,
        proposal,
        input_bytes=input_bytes,
        output_tokens=proposal["output_tokens"],
    )
    path = runner._prepare(
        root / "job",
        request,
        candidate,
        proposal,
        launch_id="writer-probe-" + probe["nonce"],
        generation=1,
        permissions="workspace-tools",
        grants=grants,
        probe=probe,
    )
    envelope = read_json(path)
    workspace = Path(grants["workspace"])
    (workspace / "src/owned.txt").write_text(qualification.CONTENT + probe["nonce"] + "\n")
    git(workspace, "add", "src/owned.txt")
    git(workspace, "commit", "-qm", "scripted fixture evidence")
    receipt = {
        "schema_version": 1,
        "job_id": request.id,
        "request_digest": envelope["request_digest"],
        "launch_id": envelope["launch_id"],
        "generation": envelope["generation"],
        "outcome": "completed",
        "quiescent": True,
        "processes_quiescent": True,
        "completion_evidence": "owned-window-death-and-process-readback",
        "input_complete": True,
        "input_delivery": envelope["input"],
        "session_id": "synthetic-writer",
        "selected_model": candidate.model,
        "observed_model": candidate.model,
        "observed_models": [candidate.model],
        "turns": 1,
        "usage_known": True,
        "usage": {"input_tokens": 1000, "output_tokens": 80},
        "result": {"nonce": probe["nonce"], "markers": probe["markers"]},
    }
    write_json(path.parent / "receipt.json", receipt)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(
            qualification,
            "containment",
            lambda _: {
                "target_ref": True,
                "sibling": True,
                "outside": True,
                "object_unlink": True,
                "object_overwrite": True,
                "object_hardlink": True,
                "packed_object_shadow": True,
                "bank_shadow_readback": True,
            },
        )
        report = qualification.result(path, {"state": "finished", "receipt": receipt})
    assert report["ok"], report
    cap = read_json(Path(report["capability_path"]))
    validate_capability(cap, candidate, permissions="workspace-tools")
    return cap


@pytest.fixture
def writer(prepared, tmp_path):
    _, _, _, candidate, capability = prepared
    script = tmp_path / "writer.py"
    script.write_text("""import json,re,subprocess,sys
from pathlib import Path
text=Path(sys.argv[1]).read_text()
nonce=re.search(r'QUALIFICATION NONCE ([a-f0-9]+)',text)[1]
markers=re.findall(r'CANARY [0-9]+: ([a-f0-9]+)',text)
Path('src/owned.txt').write_text('native writer qualification: '+nonce+'\\n')
subprocess.run(['git','add','src/owned.txt'],check=True,stdout=subprocess.DEVNULL)
subprocess.run(['git','commit','-qm','native writer qualification'],check=True,stdout=subprocess.DEVNULL)
print(json.dumps({'type':'system','model':'gpt-test','session_id':'fixture-native'}))
print(json.dumps({'type':'result','structured_output':{'nonce':nonce,'markers':markers},'usage':{'input_tokens':100,'output_tokens':80}}))
""")
    binary = str(tmp_path / "sandbox-launcher")
    Path(binary).write_text(launcher_text())
    Path(binary).chmod(0o700)
    wrapper = str(Path(campaign_execution_sandbox.__file__).resolve())
    capability = {
        **capability,
        "permissions": "workspace-tools",
        "binary": binary,
        "binary_sha256": file_digest(Path(binary)),
        "dependencies": [
            {"path": str(script), "sha256": file_digest(script)},
            {"path": wrapper, "sha256": file_digest(Path(wrapper))},
            {
                "path": str(Path(sys.executable).resolve()),
                "sha256": file_digest(Path(sys.executable).resolve()),
            },
            *(
                {
                    "path": str(Path(module.__file__).resolve()),
                    "sha256": file_digest(Path(module.__file__).resolve()),
                }
                for module in (campaign_native_git, campaign_object_promotion)
            ),
        ],
        "argv": [
            binary,
            "--grants",
            "{grants_path}",
            "--",
            str(Path(sys.executable).resolve()),
            str(script),
            "{input_path}",
        ],
    }
    capability.pop("qualification")
    proposal = tmp_path / "proposal.json"
    proposal.write_text(json.dumps(capability))
    return proposal, candidate, capability


@pytest.mark.parametrize("module", [campaign_native_git, campaign_object_promotion])
def test_old_writer_proofs_require_current_private_git_dependency_binding(writer, module):
    _, candidate, cap = writer
    cap["dependencies"] = [
        row for row in cap["dependencies"] if row["path"] != str(Path(module.__file__).resolve())
    ]
    with pytest.raises(CampaignError, match="private Git publication.*requalification"):
        validate_proposal(cap, candidate)


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS OS sandbox is required")
def test_writer_qualifies_commit_protection_lifecycle_and_immutable_resume(writer, tmp_path):
    proposal, candidate, _ = writer
    runner = HeadlessRunner(ChildMux())
    root = tmp_path / "qualification"
    report = qualify(
        proposal, root, input_bytes=4096, output_tokens=4096, timeout_s=20, runner=runner
    )
    assert report["ok"], report
    assert all(report["checks"].values())
    assert (
        report["checks"]["target_ref"]
        and report["checks"]["sibling"]
        and report["checks"]["outside"]
    )
    validate_capability(
        read_json(Path(report["capability_path"])), candidate, permissions="workspace-tools"
    )
    assert qualify(
        proposal, root, input_bytes=4096, output_tokens=4096, timeout_s=20, runner=runner
    )["ok"]
    assert len(runner.mux.processes) == 1
    (root / "outside.txt").write_text("owner changed protected content")
    assert not qualify(
        proposal, root, input_bytes=4096, output_tokens=4096, timeout_s=20, runner=runner
    )["ok"]


def test_writer_requires_exact_wrapper_and_fresh_directory(writer, tmp_path):
    proposal, candidate, cap = writer
    with pytest.raises(CampaignError, match="exact scoped sandbox"):
        validate_proposal({**cap, "argv": [cap["binary"], "ignored", "{grants_path}"]}, candidate)
    occupied = tmp_path / "occupied"
    occupied.mkdir()
    (occupied / "retained.txt").write_text("owner work")
    with pytest.raises(CampaignError, match="new empty directory"):
        qualify(proposal, occupied, runner=HeadlessRunner(ChildMux()))
    assert (occupied / "retained.txt").read_text() == "owner work"


@pytest.mark.parametrize(
    "field,value",
    [
        ("scopes", ["src/owned.txt", "src/sibling.txt"]),
        ("git_branch", "main"),
        ("protected_branches", []),
        ("config_digest", "changed"),
    ],
)
def test_writer_canary_refuses_changed_frozen_grants(writer, tmp_path, field, value):
    from bmad_loop.campaign_execution_qualification import build, validate_request

    _, candidate, capability = writer
    request, probe, grants = build(tmp_path / "fixture", candidate, capability, output_tokens=4096)
    validate_request(request, probe)
    grants[field] = value
    with pytest.raises(CampaignError, match="fixed fixture"):
        validate_request(request, probe)


def test_writer_cli_has_separate_explicit_operator_route():
    import argparse

    from bmad_loop.campaign_cli import register

    parser = argparse.ArgumentParser()
    register(parser.add_subparsers(dest="command"))
    args = parser.parse_args(
        ["campaign", "qualify-writer", "proposal.json", "--directory", "fixture", "--json"]
    )
    assert args.campaign_command == "qualify-writer"
    assert args.input_bytes == 16384
