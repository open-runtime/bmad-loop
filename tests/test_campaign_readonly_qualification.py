"""Read-only routes earn distinct evidence through real zero-token confinement."""

import json
import sys
from pathlib import Path

import pytest
from test_campaign_execution_qualification import writer as writer
from test_campaign_headless import ChildMux
from test_campaign_headless import prepared as prepared

from bmad_loop.campaign_execution_qualification import qualify
from bmad_loop.campaign_execution_sandbox import write_paths
from bmad_loop.campaign_headless import (
    HeadlessRunner,
    capability_for,
    file_digest,
    load_capabilities,
    read_json,
    readonly_grants,
    validate_capability,
)
from bmad_loop.campaign_model import CampaignError


@pytest.fixture
def readonly(writer, tmp_path):
    proposal, candidate, cap = writer
    script = tmp_path / "writer.py"
    script.write_text("""import json,re,sys
from pathlib import Path
text=Path(sys.argv[1]).read_text()
nonce=re.search(r'QUALIFICATION NONCE ([a-f0-9]+)',text)[1]
markers=re.findall(r'CANARY [0-9]+: ([a-f0-9]+)',text)
readback=Path('src/owned.txt').read_text()
try: Path('src/owned.txt').write_text('escape')
except PermissionError: pass
else: raise AssertionError('native read tool escaped confinement')
print(json.dumps({'type':'system','model':'gpt-test','session_id':'fixture-native'}))
print(json.dumps({'type':'result','structured_output':{'nonce':nonce,'markers':markers,'workspace_readback':readback},'usage':{'input_tokens':100,'output_tokens':80}}))
""")
    cap["permissions"] = "read-only-tools"
    cap["dependencies"][0]["sha256"] = file_digest(script)
    proposal.write_text(json.dumps(cap))
    return proposal, candidate, cap


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS OS sandbox is required")
def test_readonly_route_earns_readback_and_product_git_descendant_write_denial(readonly, tmp_path):
    proposal, candidate, _ = readonly
    report = qualify(
        proposal,
        tmp_path / "qualification",
        input_bytes=4096,
        output_tokens=4096,
        timeout_s=20,
        runner=HeadlessRunner(ChildMux()),
    )
    assert report["ok"], report
    assert {"owned", "new_product", "worker_ref", "git_index", "descendant", "readable"} <= report[
        "checks"
    ].keys()
    assert all(report["checks"].values())
    cap = read_json(Path(report["capability_path"]))
    validate_capability(cap, candidate, permissions="read-only-tools")
    with pytest.raises(CampaignError, match="exact versioned permission"):
        validate_capability(cap, candidate, permissions="no-tools")
    with pytest.raises(CampaignError, match="exact versioned permission"):
        validate_capability(cap, candidate, permissions="workspace-tools")
    manifest = tmp_path / "capabilities.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "routes": {
                    candidate.profile + ":" + candidate.model: {"reasoning": cap},
                },
            }
        )
    )
    routes = load_capabilities(manifest)
    assert (
        capability_for(
            routes, candidate.profile, candidate.model, "reasoning", effort=candidate.effort
        )
        == cap
    )
    with pytest.raises(CampaignError, match="No qualified no-tools"):
        capability_for(routes, candidate.profile, candidate.model, effort=candidate.effort)


def test_readonly_grants_discard_worker_authority_and_cannot_write_product_git_or_lifecycle(
    project, tmp_path
):
    cap = {"permissions": "read-only-tools"}
    grants = readonly_grants(project.project, {"config_revision": 1}, cap)
    directory = tmp_path / "controller" / "job"
    directory.mkdir(parents=True)
    workspace, paths, _ = write_paths(grants, directory)
    assert workspace == project.project.resolve()
    assert paths == ((directory / "sandbox-runtime").resolve(),)
    # A disjoint approved native cache still works. This isolates overlap
    # rejection from a blanket refusal of every runtime grant.
    runtime = tmp_path / "native-cache"
    runtime.mkdir()
    assert write_paths({**grants, "runtime_write_paths": [str(runtime)]}, directory)[1] == (
        *paths,
        runtime.resolve(),
    )
    for key, value in (
        ("scopes", ["src"]),
        ("write_paths", ["test.txt"]),
        ("git_branch", "worker"),
    ):
        with pytest.raises(CampaignError, match="cannot grant"):
            write_paths({**grants, key: value}, directory)
    for path in (
        project.project,
        project.project / "src",
        project.project / ".git",
        project.project.parent,
        directory,
        directory / "request.json",
        directory.parent,
    ):
        with pytest.raises(CampaignError, match="overlaps product, Git or lifecycle authority"):
            write_paths({**grants, "runtime_write_paths": [str(path)]}, directory)


def test_readonly_adapter_minimizes_frozen_grants(prepared, project, tmp_path):
    from bmad_loop.adapters.headless import HeadlessAdapter
    from bmad_loop.adapters.profile import get_profile
    from bmad_loop.policy import Policy

    _, _, _, _, cap = prepared
    adapter = HeadlessAdapter(
        run_dir=tmp_path / "child", profile=get_profile("codex", project.project), policy=Policy()
    )
    cap = {**cap, "permissions": "read-only-tools"}
    adapter.set_capability(
        cap,
        {
            "workspace": str(project.project),
            "scopes": ["."],
            "git_branch": "main",
            "write_paths": ["secrets"],
        },
        {"config_revision": 1},
    )
    assert adapter.grants["permissions"] == "read-only-tools"
    assert adapter.grants["scopes"] == []
    assert not {"git_branch", "write_paths"} & adapter.grants.keys()
