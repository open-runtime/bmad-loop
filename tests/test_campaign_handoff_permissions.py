"""Quota continuation retains the frozen stage's read-only or writer authority."""

import dataclasses
import hashlib
import json
import sys
from pathlib import Path

import pytest
from conftest import install_bmad_config
from test_campaign_handoff_flow import campaign as campaign
from test_campaign_handoff_flow import (
    complete_audits,
    complete_maps,
    step,
)
from test_campaign_headless import fixture_capability
from test_campaign_headless import prepared as prepared
from test_campaign_workers import envelope

from bmad_loop import campaign_handoff_flow as flow
from bmad_loop import campaign_workers as workers
from bmad_loop.adapters import multiplexer
from bmad_loop.adapters.base import SessionSpec
from bmad_loop.campaign_headless import read_json
from bmad_loop.campaign_model import CampaignError, RoleCandidate, RoleSpec
from bmad_loop.policy import AdapterPolicy, Policy


def readonly(campaign, role="publication_review", *, actual_native=False):
    runtime, snapshot, unit, receipt = campaign
    first, second = snapshot.spec.candidates("execution")
    snapshot.spec = dataclasses.replace(
        snapshot.spec,
        roles=(*snapshot.spec.roles, (role, RoleSpec(pool="writers"))),
    )
    unit.claim.role = role
    unit.claim.writes = False
    snapshot.outbox[0].payload.update(kind="stage", role=role, stage=role, read_only=True)
    routes = snapshot.config_snapshot["reasoning_capabilities"]
    del routes[second.profile + ":" + second.model]["execution"]
    if actual_native:
        auditor = second
        second = RoleCandidate("claude", "claude-successor", "high", "anthropic")
        snapshot.spec = dataclasses.replace(
            snapshot.spec,
            roles=tuple(
                (name, RoleSpec(pool="readers") if name == role else spec)
                for name, spec in snapshot.spec.roles
            ),
            role_pools=(*snapshot.spec.role_pools, ("readers", (first, second))),
        )
        directory = runtime.store.root.parent
        script = directory / "readonly-successor.py"
        script.write_text(
            "import json,sys\n"
            "payload=sys.stdin.buffer.read()\n"
            f'print(json.dumps({{"type":"system","model":{second.model!r},"session_id":"readonly-successor"}}))\n'
            'print(json.dumps({"type":"result","structured_output":{"status":"done","accepted":True},"usage":{"input_tokens":12,"output_tokens":8}}))\n',
            encoding="utf-8",
        )
        basecap = routes[auditor.profile + ":" + auditor.model]["reasoning"]
        cap = {
            **basecap,
            "model": second.model,
            "argv": [str(Path(sys.executable).resolve()), str(script)],
            "dependencies": [
                {"path": str(script), "sha256": hashlib.sha256(script.read_bytes()).hexdigest()}
            ],
        }
        cap.pop("qualification")
        cap.pop("semantic_qualification")
        # Keep the independently qualified historical audit route separate.
        routes[second.profile + ":" + second.model] = {
            "reasoning": fixture_capability(directory, cap, second)
        }
    return runtime, snapshot, unit, receipt, second


def ready(runtime, snapshot, unit):
    for phase in ("handoff-capture", "handoff-plan"):
        step(runtime, snapshot, unit, phase)
    complete_maps(runtime, snapshot, unit)
    step(runtime, snapshot, unit, "handoff-audit-plan")
    complete_audits(runtime, snapshot, unit)
    step(runtime, snapshot, unit, "handoff-assemble")
    step(runtime, snapshot, unit, "handoff-release")


@pytest.mark.parametrize(
    "role", ["publication_review", "planning_review", "triage", "decision_review"]
)
def test_readonly_quota_handoff_builds_readonly_successor_without_writer_cap(campaign, role):
    runtime, snapshot, unit, receipt, target = readonly(campaign, role)
    assert flow.intercept_terminal(runtime, snapshot, unit, receipt)
    assert unit.evidence["handoff"]["candidate"] == dataclasses.asdict(target)
    ready(runtime, snapshot, unit)
    proposal = flow.resume_proposal(snapshot, unit)
    assert proposal.payload["read_only"] is True
    request = dict(proposal.payload)
    flow.decorate_launch(snapshot, unit, request)
    packet = read_json(Path(request["handoff_packet"]["path"]))
    assert request["handoff_packet"]["read_only"] is True and packet["read_only"] is True
    assert request["role"] == role and request["stage"] == role
    assert unit.bindings["pr"] == 17
    successor = dataclasses.replace(unit.claim, launch_id="successor", writes=False)
    flow.adopt_launch(snapshot, unit, proposal.payload, successor)
    assert unit.evidence["handoff"]["successor"]["writes"] is False


def test_real_successor_composition_selects_no_tools_and_consumes_readonly_packet(
    campaign, prepared, project, monkeypatch
):
    runtime, snapshot, unit, receipt, target = readonly(campaign, actual_native=True)
    runner, *_ = prepared
    install_bmad_config(project)
    assert flow.intercept_terminal(runtime, snapshot, unit, receipt)
    ready(runtime, snapshot, unit)
    request = dict(flow.resume_proposal(snapshot, unit).payload)
    flow.decorate_launch(snapshot, unit, request)
    workspace = Path(unit.bindings["workspace"])
    before = workers.git(workspace, "status", "--porcelain=v1")
    policy = Policy(
        adapter=AdapterPolicy(name=target.profile, model=target.model, effort=target.effort)
    )
    context = envelope(workspace, runtime.store.root / "successor", policy)
    monkeypatch.setattr(multiplexer, "get_multiplexer", lambda: runner.mux)
    monkeypatch.setattr(multiplexer, "mux_usable", lambda _: True)
    request["headless_coordinates"] = {
        "campaign": snapshot.id,
        "unit": unit.id,
        "stage": request["stage"],
        "graph_revision": snapshot.graph_revision,
        "config_revision": snapshot.config_revision,
        "head": workers.git(workspace, "rev-parse", "HEAD"),
        "base": workers.git(workspace, "rev-parse", "HEAD"),
    }
    adapters = workers._adapters(
        request, context, workspace, workspace / ".bmad-loop/runs/successor", policy
    )
    adapter = adapters["triage"]
    assert adapter.capability["permissions"] == "no-tools"
    session = SessionSpec(
        "readonly-continuation",
        "triage",
        "Continue the exact publication review.",
        workspace,
        model=target.model,
        effort=target.effort,
        timeout_s=30,
    )
    result = adapter.run(session)
    assert result.status == "completed", result
    assert result.result_json == {"status": "done", "accepted": True}
    native = read_json(adapter.requests[session.task_id])
    assert native["permissions"] == "no-tools"
    assert unit.bindings["pr"] == 17
    assert workers.git(workspace, "status", "--porcelain=v1") == before


def test_writer_only_fallback_cannot_replace_readonly_stage(campaign):
    runtime, snapshot, unit, receipt, target = readonly(campaign)
    snapshot.config_snapshot["reasoning_capabilities"][target.profile + ":" + target.model] = {
        "execution": runtime.fixture_cap(target, True)
    }
    assert flow.intercept_terminal(runtime, snapshot, unit, receipt)
    assert unit.evidence["handoff"]["candidate"] is None
    assert "No qualified fallback" in unit.hold.reason


@pytest.mark.parametrize("mutation", ["request", "claim", "packet"])
def test_readonly_boundary_cannot_widen_during_successor_adoption(campaign, mutation):
    runtime, snapshot, unit, receipt, _ = readonly(campaign)
    flow.intercept_terminal(runtime, snapshot, unit, receipt)
    ready(runtime, snapshot, unit)
    request = dict(flow.resume_proposal(snapshot, unit).payload)
    if mutation == "request":
        request["read_only"] = False
    elif mutation == "packet":
        unit.evidence["handoff"]["packet"]["read_only"] = False
    if mutation == "claim":
        successor = dataclasses.replace(unit.claim, launch_id="successor", writes=True)
        with pytest.raises(CampaignError, match="permission boundary"):
            flow.adopt_launch(snapshot, unit, request, successor)
    else:
        with pytest.raises(CampaignError, match="permission boundary"):
            flow.decorate_launch(snapshot, unit, request)


def test_frozen_dynamic_planning_review_and_older_snapshot_keep_readonly_boundary(campaign):
    runtime, snapshot, unit, receipt, _ = readonly(campaign, "planning")
    # Generic plan launch resolves to a read-only review only in its immutable
    # child request. Preserve that resolved stage through continuation.
    unit.claim.writes = True
    snapshot.outbox[0].payload.pop("read_only")
    path = runtime.store.directory(snapshot.id) / "workers" / unit.claim.launch_id / "request.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "kind": "stage",
                "stage": "planning_review",
                "read_only": True,
                "campaign_id": snapshot.id,
                "unit_id": unit.id,
                "launch_id": unit.claim.launch_id,
                "generation": unit.claim.generation,
                "attempt": unit.claim.attempt,
            }
        ),
        encoding="utf-8",
    )
    flow.intercept_terminal(runtime, snapshot, unit, receipt)
    value = unit.evidence["handoff"]
    assert value["permissions"] == "review"
    value.pop("permissions")  # Compatibility derives from frozen original input.
    assert flow._permission(value) == "review"
    assert value["original_payload"]["stage"] == "planning_review"


def test_legacy_native_review_preserves_existing_writable_engine_contract(campaign):
    runtime, snapshot, unit, receipt = campaign
    snapshot.spec = dataclasses.replace(
        snapshot.spec, roles=(*snapshot.spec.roles, ("execution_review", RoleSpec(pool="writers")))
    )
    receipt["interruption"]["role"] = "review"
    snapshot.outbox[0].payload["review_candidate"] = snapshot.outbox[0].payload["candidate"]
    flow.intercept_terminal(runtime, snapshot, unit, receipt)
    value = unit.evidence["handoff"]
    assert value["route_role"] == "execution_review" and value["permissions"] == "workspace-tools"


def test_old_readonly_handoff_with_writable_packet_rebuilds_before_resuming(campaign):
    runtime, snapshot, unit, receipt, _ = readonly(campaign)
    flow.intercept_terminal(runtime, snapshot, unit, receipt)
    ready(runtime, snapshot, unit)
    unit.evidence["handoff"].pop("permissions")
    unit.evidence["handoff"]["packet"]["read_only"] = False
    assert flow.resume_proposal(snapshot, unit) is None
    assert [row.kind for row in flow.proposals(runtime, snapshot)] == ["handoff-refresh"]
