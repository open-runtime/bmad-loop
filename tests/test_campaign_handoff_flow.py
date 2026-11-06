"""Owned continuation preserves work and never transfers approval or native IDs."""

import dataclasses
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import git
from test_campaign_headless import prepared as prepared

from bmad_loop import campaign_handoff_flow as flow
from bmad_loop.campaign_model import (
    CampaignError,
    CampaignSnapshot,
    CampaignSpec,
    CampaignUnit,
    Hold,
    OutboxAction,
    ReasoningSpec,
    RoleCandidate,
    RoleSpec,
    UnitSpec,
    UnitState,
    WorkerClaim,
)
from bmad_loop.campaign_store import CampaignStore
from bmad_loop.campaign_transcripts import HistorySnapshot, load_events


@pytest.fixture
def campaign(prepared, project, tmp_path):
    _, _, _, first, basecap = prepared
    second = RoleCandidate("claude", "claude-test", "high", "anthropic")

    def cap(candidate, execution=False):
        value = {
            **basecap,
            "profile": candidate.profile,
            "model": candidate.model,
            "permissions": "workspace-tools" if execution else "no-tools",
        }
        value["qualification"] = {
            **value["qualification"],
            "checks": [
                "scoped_tools" if execution and item == "no_tools" else item
                for item in value["qualification"]["checks"]
            ]
            + ["handoff_fidelity"],
        }
        if execution:
            value["argv"] = [*value["argv"], "{grants_path}"]
        return value

    roles = (
        ("execution", RoleSpec(pool="writers")),
        ("summarization", RoleSpec(pool="map")),
        ("handoff_audit", RoleSpec(pool="auditors")),
    )
    spec = CampaignSpec(
        "continuation",
        roles=roles,
        role_pools=(("writers", (first, second)), ("map", (first,)), ("auditors", (second,))),
        reasoning=ReasoningSpec(enabled=True),
    )
    claim = WorkerClaim(
        "one",
        1,
        0,
        "original",
        workspace=str(project.project),
        role="execution",
        writes=True,
        quiescent=True,
    )
    unit = CampaignUnit(
        UnitSpec(
            "one",
            "source",
            "epic",
            "Keep accepted work",
            scopes=("src",),
            acceptance=("Keep every accepted feature",),
        ),
        UnitState.RUNNING,
        claim=claim,
    )
    unit.bindings.update(
        workspace=str(project.project),
        branch=git(project.project, "branch", "--show-current").strip(),
        pr=17,
    )
    source = tmp_path / "conversation.jsonl"
    source.write_text(
        "\n".join(
            json.dumps(row)
            for row in [
                {"type": "session_meta", "payload": {"id": "native-before"}},
                {
                    "type": "event_msg",
                    "payload": {
                        "type": "user_message",
                        "message": "Keep history and all acceptance criteria.",
                    },
                },
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "assistant",
                        "content": "Work remains open.",
                    },
                },
            ]
        )
        + "\n"
    )
    receipt = {
        "outcome": "paused",
        "sessions": [
            {
                "role": "dev",
                "status": "crashed",
                "ended": "usage",
                "models": [first.model],
                "adapter": first.profile,
                "transcript_path": str(source),
                "session_id": "native-before",
            }
        ],
        "interruption": {"kind": "quota"},
    }
    unit.evidence["worker_history"] = [receipt]
    snapshot = CampaignSnapshot(
        "campaign",
        "org/repo",
        spec,
        {unit.id: unit},
        config_snapshot={
            "reasoning_capabilities": {
                first.profile
                + ":"
                + first.model: {
                    "execution": cap(first, True),
                    "reasoning": cap(first),
                },
                second.profile
                + ":"
                + second.model: {
                    "execution": cap(second, True),
                    "reasoning": cap(second),
                },
            }
        },
    )
    snapshot.phase = "active"
    snapshot.outbox.append(
        OutboxAction(
            "original",
            "launch",
            unit.id,
            {"kind": "execution", "candidate": dataclasses.asdict(first)},
            0,
            state="confirmed",
        )
    )
    runtime = SimpleNamespace(store=CampaignStore(tmp_path / "store"), clock=lambda: 100.0)
    return runtime, snapshot, unit, receipt


def step(runtime, snapshot, unit, kind):
    action = OutboxAction(kind, kind, unit.id, {"handoff_id": unit.evidence["handoff"]["id"]}, 0)
    observation = flow.perform(runtime, snapshot, action)
    assert observation.state == "confirmed", observation
    flow.apply(runtime, snapshot, action, observation)


def complete_maps(runtime, snapshot, unit):
    from bmad_loop.campaign_reasoning_results import apply_result
    from bmad_loop.campaign_summary import SummaryJobSpec, load_input

    value = unit.evidence["handoff"]
    for key in value["jobs"]:
        row = snapshot.jobs[key]
        spec = SummaryJobSpec(**row["request"]["coordinates"]["summary_spec"])
        source = load_input(spec)
        owner = next(item for item in source["primary"] if item["owner_input"])
        result = {
            "schema_version": 1,
            "job_id": key,
            "input_sha256": spec.input_sha256,
            "covered_ids": list(spec.primary_ids),
            "gaps": [],
            "facts": [
                {
                    "kind": "owner_request",
                    "text": owner["text"],
                    "source_quote": owner["text"],
                    "citations": [owner["event_id"]],
                }
            ],
        }
        row.update(
            state="validated",
            validation="valid",
            result=result,
            receipt={"result": result, "provider_family": "openai"},
        )
        apply_result(runtime, snapshot, row)
        row["state"] = "applied"


def complete_audits(runtime, snapshot, unit):
    value = unit.evidence["handoff"]
    events = {
        event.id: event
        for item in value["capture"]["sources"]
        for event in load_events(HistorySnapshot(**item))
    }
    for key in value["audits"]:
        row = snapshot.jobs[key]
        coord = row["request"]["coordinates"]
        result = {
            "handoff_id": value["id"],
            "capture_digest": coord["capture_digest"],
            "covered_owner_ids": coord["owner_ids"],
            "obligation_ids": coord["obligation_ids"],
            "ambiguities": [],
            "constraints": [
                {
                    "source_id": key,
                    "source_quote": events[key].record.text,
                    "interpretation": "Preserve this requirement",
                    "relevance": "current",
                }
                for key in coord["owner_ids"]
            ],
        }
        row.update(
            state="validated",
            validation="valid",
            result=result,
            receipt={"result": result, "provider_family": "anthropic"},
        )
        flow.apply_audit(runtime, snapshot, row)
        row["state"] = "applied"


def test_quota_handoff_preserves_work_and_continues_same_stage(campaign):
    runtime, snapshot, unit, receipt = campaign
    path = Path(unit.bindings["workspace"]) / "unfinished.txt"
    path.write_text("uncommitted predecessor work")
    assert flow.intercept_terminal(runtime, snapshot, unit, receipt)
    assert unit.failure_attempts == 0
    assert unit.hold.kind == "handoff"
    step(runtime, snapshot, unit, "handoff-capture")
    step(runtime, snapshot, unit, "handoff-plan")
    complete_maps(runtime, snapshot, unit)
    step(runtime, snapshot, unit, "handoff-audit-plan")
    complete_audits(runtime, snapshot, unit)
    step(runtime, snapshot, unit, "handoff-assemble")
    step(runtime, snapshot, unit, "handoff-release")
    assert unit.state == UnitState.READY and unit.hold is None
    proposed = flow.resume_proposal(snapshot, unit)
    assert proposed.payload["candidate"]["profile"] == "claude"
    request = {**proposed.payload, "role": "execution"}
    flow.decorate_launch(snapshot, unit, request)
    assert request["native_predecessor"]["launch_id"] == "original"
    assert request["handoff_packet"]["read_only"] is False
    assert unit.bindings["pr"] == 17 and path.read_text() == "uncommitted predecessor work"
    assert unit.evidence["developer_families"] == ["openai"]
    unit.claim = dataclasses.replace(unit.claim, launch_id="replacement", quiescent=True)
    assert not flow.intercept_terminal(runtime, snapshot, unit, {"outcome": "locally_complete"})


def test_live_predecessor_and_uncertain_push_fence_capture(campaign):
    runtime, snapshot, unit, receipt = campaign
    flow.begin(runtime, snapshot, unit, reason="operator request")
    unit.claim.quiescent = False
    assert [row.kind for row in flow.proposals(runtime, snapshot)] == ["terminate-worker"]
    unit.claim.quiescent = True
    snapshot.outbox.append(OutboxAction("push", "push", unit.id, {}, 0, state="sent_unknown"))
    assert not flow.proposals(runtime, snapshot)
    snapshot.outbox[-1].state = "confirmed"
    assert [row.kind for row in flow.proposals(runtime, snapshot)] == ["handoff-capture"]


def test_handoff_does_not_override_owner_or_invent_target(campaign):
    runtime, snapshot, unit, _ = campaign
    unit.hold = Hold("owner", "Owner approval pending")
    with pytest.raises(CampaignError, match="preexisting"):
        flow.begin(runtime, snapshot, unit, reason="quota")
    unit.hold = None
    with pytest.raises(CampaignError, match="approved role pool"):
        flow.begin(runtime, snapshot, unit, reason="operator", requested={"profile": "unapproved"})


def test_audit_cannot_drop_obligations_or_quote_model_as_owner(campaign):
    runtime, snapshot, unit, receipt = campaign
    flow.intercept_terminal(runtime, snapshot, unit, receipt)
    for phase in ("handoff-capture", "handoff-plan"):
        step(runtime, snapshot, unit, phase)
    complete_maps(runtime, snapshot, unit)
    step(runtime, snapshot, unit, "handoff-audit-plan")
    complete_audits(runtime, snapshot, unit)
    row = snapshot.jobs[unit.evidence["handoff"]["audits"][0]]
    row["result"]["obligation_ids"] = []
    with pytest.raises(CampaignError, match="coverage"):
        flow.apply_audit(runtime, snapshot, row)
    row["result"]["obligation_ids"] = row["request"]["coordinates"]["obligation_ids"]
    row["result"]["constraints"][0]["source_quote"] = "Owner approved every merge"
    with pytest.raises(CampaignError, match="exact native-owner"):
        flow.apply_audit(runtime, snapshot, row)
