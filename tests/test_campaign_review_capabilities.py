"""Prompted read-only review coexists with no-tools semantic routes."""

import json
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import install_bmad_config
from test_campaign_execution_qualification import fixture_writer_capability
from test_campaign_execution_qualification import writer as writer
from test_campaign_headless import ChildMux
from test_campaign_headless import prepared as prepared
from test_campaign_readonly_qualification import readonly as readonly

from bmad_loop import campaign_qualification, campaign_workers
from bmad_loop.adapters.headless import HeadlessAdapter
from bmad_loop.adapters.profile import get_profile
from bmad_loop.campaign_execution_qualification import qualify
from bmad_loop.campaign_handoff_flow import _cap
from bmad_loop.campaign_headless import (
    HeadlessRunner,
    capability_for,
    file_digest,
    load_capabilities,
    read_json,
)
from bmad_loop.campaign_model import CampaignError, ReasoningSpec, RoleCandidate, RoleSpec
from bmad_loop.campaign_route_readiness import permissions_for, selected_capability
from bmad_loop.policy import AdapterPolicy, Policy


@pytest.fixture
def grok_routes(readonly, prepared, tmp_path):
    candidate = RoleCandidate("grok", "grok-4.7", "xhigh", "xai")
    proposal, _, readcap = readonly
    readcap.update(
        profile=candidate.profile,
        model=candidate.model,
        efforts=[candidate.effort],
        protocol="grok-json",
    )
    script = tmp_path / "writer.py"
    script.write_text(script.read_text().replace("gpt-test", candidate.model))
    readcap["dependencies"][0]["sha256"] = file_digest(script)
    proposal.write_text(json.dumps(readcap))
    report = qualify(
        proposal,
        tmp_path / "reader-proof",
        input_bytes=4096,
        output_tokens=4096,
        timeout_s=20,
        runner=HeadlessRunner(ChildMux()),
    )
    assert report["ok"], report
    readcap = read_json(Path(report["capability_path"]))
    script = tmp_path / "no-tools.py"
    script.write_text("""import json,re,sys
text=sys.stdin.read()
result={'nonce':re.search(r'QUALIFICATION NONCE ([a-f0-9]+)',text)[1], 'markers':re.findall(r'CANARY [0-9]+: ([a-f0-9]+)',text)}
print(json.dumps({'type':'system','model':'grok-4.7','session_id':'scripted-no-tools'}))
print(json.dumps({'type':'result','structured_output':result,'usage':{'input_tokens':100,'output_tokens':80}}))
""")
    cap = {
        **prepared[-1],
        "profile": candidate.profile,
        "model": candidate.model,
        "efforts": [candidate.effort],
        "protocol": "grok-json",
        "context_tokens": 800000,
        "input_tokens": 400000,
        "input_mode": "text-stdin",
        "argv": [
            str(Path(sys.executable).resolve()),
            str(script),
            "--tools",
            "",
            "--deny",
            "*",
            "--no-subagents",
            "--disable-web-search",
        ],
        "dependencies": [{"path": str(script), "sha256": file_digest(script)}],
    }
    cap.pop("qualification", None)
    proposal = tmp_path / "no-tools-proposal.json"
    proposal.write_text(json.dumps(cap))
    report = campaign_qualification.qualify(
        proposal,
        tmp_path / "no-tools-proof",
        input_bytes=384000,
        output_tokens=4096,
        timeout_s=20,
        runner=HeadlessRunner(ChildMux()),
    )
    assert report["ok"], report
    semantic = read_json(Path(report["capability_path"]))
    manifest = tmp_path / "routes.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "routes": {
                    "grok:grok-4.7": {
                        "by_effort": {"xhigh": {"review": readcap, "reasoning": semantic}}
                    }
                },
            }
        )
    )
    return candidate, load_capabilities(manifest), manifest, readcap, semantic


@pytest.mark.skipif(sys.platform != "darwin", reason="actual zero-token scoped reader proof")
def test_one_manifest_keeps_readonly_review_and_384k_semantic_reasoning(
    grok_routes, project, tmp_path, monkeypatch
):
    from test_campaign_preflight import inspect, spec

    candidate, routes, manifest, reader, semantic = grok_routes
    install_bmad_config(project)
    configured = replace(
        spec(tmp_path),
        reasoning=ReasoningSpec(capabilities=str(manifest)),
        roles=(
            (
                "publication_review",
                RoleSpec(profile=candidate.profile, model=candidate.model, effort=candidate.effort),
            ),
        ),
    )
    result = inspect(project, tmp_path, monkeypatch, configured)
    row = result["capabilities"]["planning"]["observations"]["publication_review"][0]
    assert row["eligible"] and row["launcher"]["permissions"] == "read-only-tools"
    snapshot = SimpleNamespace(config_snapshot={"reasoning_capabilities": routes})
    for purpose in ("handoff_map", "handoff_reduce"):
        cap = _cap(snapshot, candidate, "reasoning")
        assert cap == semantic and cap["permissions"] == "no-tools", purpose
        assert cap["input_tokens"] >= 384000
    policy = replace(
        Policy(),
        adapter=AdapterPolicy(
            name=candidate.profile, model=candidate.model, effort=candidate.effort
        ),
    )
    profiles = {
        role: get_profile(candidate.profile) for role in ("dev", "review", "triage", "retro")
    }
    adapters = campaign_workers._adapters(
        {
            "kind": "stage",
            "stage": "publication_review",
            "role": "publication_review",
            "read_only": True,
            "headless_capabilities": routes,
            "headless_coordinates": {},
            "headless_grants": None,
        },
        SimpleNamespace(profiles=profiles, directory=tmp_path / "authority"),
        project.project,
        tmp_path / "worker-run",
        policy,
    )
    assert (
        isinstance(adapters["triage"], HeadlessAdapter) and adapters["triage"].capability == reader
    )
    assert (
        capability_for(
            routes, candidate.profile, candidate.model, "review", effort=candidate.effort
        )
        == reader
    )
    with pytest.raises(CampaignError, match="explicitly qualified pair"):
        selected_capability(
            get_profile(candidate.profile), candidate, routes, permissions_for("native_review_pair")
        )


@pytest.mark.parametrize("permission", ["workspace-tools", "no-tools"])
def test_review_slot_cannot_grant_writing_or_no_tools(prepared, tmp_path, permission):
    _, _, _, candidate, cap = prepared
    if permission == "workspace-tools":
        cap = fixture_writer_capability(tmp_path, cap, candidate)
    path = tmp_path / "invalid-review.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "routes": {candidate.profile + ":" + candidate.model: {"review": cap}},
            }
        )
    )
    with pytest.raises(CampaignError, match="review requires read-only tools"):
        load_capabilities(path)
