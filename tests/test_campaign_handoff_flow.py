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
        from bmad_loop.campaign_headless import file_digest, write_json
        from bmad_loop.campaign_summary import (
            EXTRACT_PROMPT_VERSION,
            REDUCE_PROMPT_VERSION,
            summary_schema,
        )
        from bmad_loop.campaign_transcripts import READER_VERSIONS, digest

        corpus = [
            {
                "source_path": basecap["qualification"]["path"],
                "source_sha256": basecap["qualification"]["sha256"],
                "critical_ids": ["scripted-fixture"],
            }
        ]
        from bmad_loop.campaign_handoff_constraints import AUDIT_SCHEMA as CONSTRAINT_AUDIT_SCHEMA
        from bmad_loop.campaign_handoff_constraints import REDUCTION_SCHEMA
        from bmad_loop.campaign_handoff_recovery import AUDIT_SCHEMA as RECOVERY_AUDIT_SCHEMA
        from bmad_loop.campaign_handoff_recovery import SCHEMA as RECOVERY_SCHEMA

        native_path = tmp_path / (candidate.profile + str(execution) + "-native-fidelity.json")
        write_json(
            native_path,
            {
                "outcome": "completed",
                "quiescent": True,
                "input_complete": True,
                "session_id": "scripted-zero-token",
                "observed_model": (
                    "gpt-independent" if candidate.profile == "claude" else "claude-independent"
                ),
                "result": {
                    "verdict": "passed",
                    "contract_schema_digests": {
                        "constraints-v1": digest([REDUCTION_SCHEMA, CONSTRAINT_AUDIT_SCHEMA]),
                        "recovery-v1": digest([RECOVERY_SCHEMA, RECOVERY_AUDIT_SCHEMA]),
                    },
                    "corpus_digest": digest(corpus),
                    "missing_critical_ids": [],
                    "invented_authority": [],
                    "unresolved_conflicts": [],
                },
            },
        )
        qualification = {
            "schema_version": 1,
            "route": {
                key: value[key] for key in ("profile", "model", "cli_version", "binary_sha256")
            },
            "reader_versions": READER_VERSIONS,
            "summary_schema_digest": digest(summary_schema()),
            "audit_schema_digest": digest(flow.AUDIT_SCHEMA),
            "constraint_schemas_digest": digest([REDUCTION_SCHEMA, CONSTRAINT_AUDIT_SCHEMA]),
            "recovery_schemas_digest": digest([RECOVERY_SCHEMA, RECOVERY_AUDIT_SCHEMA]),
            "prompt_versions": [EXTRACT_PROMPT_VERSION, REDUCE_PROMPT_VERSION],
            "structural_pass": True,
            "corpus": corpus,
            "independent_review": {
                "native_receipt_path": str(native_path),
                "sha256": file_digest(native_path),
            },
        }
        qualification_path = tmp_path / (candidate.profile + str(execution) + "-fidelity.json")
        write_json(qualification_path, qualification)
        value["semantic_qualification"] = {
            "schema_version": 1,
            "path": str(qualification_path),
            "sha256": file_digest(qualification_path),
        }
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
    spec = dataclasses.replace(spec, units=(unit.spec,))
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
    snapshot = runtime.store.create(snapshot)
    return runtime, snapshot, snapshot.units[unit.id], receipt


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


def test_map_survives_graph_edit_and_owner_answer_refresh_reuses_cold_results(campaign):
    from bmad_loop.campaign_jobs import current, request_from_dict

    runtime, snapshot, unit, receipt = campaign
    flow.intercept_terminal(runtime, snapshot, unit, receipt)
    step(runtime, snapshot, unit, "handoff-capture")
    step(runtime, snapshot, unit, "handoff-plan")
    key = unit.evidence["handoff"]["jobs"][0]
    request = request_from_dict(snapshot.jobs[key]["request"])
    snapshot.graph_revision += 1
    assert current(snapshot, request)
    complete_maps(runtime, snapshot, unit)
    snapshot = runtime.store.save(snapshot, snapshot.revision)
    snapshot = runtime.store.load(snapshot.id)
    unit = snapshot.units[unit.id]
    assert key not in snapshot.jobs
    unit.evidence["decision_input_revision"] = "new-owner-answer"
    assert [row.kind for row in flow.proposals(runtime, snapshot)] == ["handoff-refresh"]
    step(runtime, snapshot, unit, "handoff-refresh")
    step(runtime, snapshot, unit, "handoff-plan")
    assert unit.evidence["handoff"]["jobs"] == [key]
    assert key not in snapshot.jobs  # immutable receipt stays archived
    step(runtime, snapshot, unit, "handoff-audit-plan")
    assert unit.evidence["handoff"]["audits"]


def test_late_handoff_result_preserves_new_operator_hold(campaign):
    runtime, snapshot, unit, receipt = campaign
    flow.intercept_terminal(runtime, snapshot, unit, receipt)
    value = unit.evidence["handoff"]
    action = OutboxAction("late", "handoff-capture", unit.id, {"handoff_id": value["id"]}, 0)
    observed = flow.perform(runtime, snapshot, action)
    unit.hold = Hold("operator", "Stop here", id="owner")
    flow.apply(runtime, snapshot, action, observed)
    assert unit.hold.id == "owner" and value["phase"] == "drain"
    assert snapshot.events[-1]["kind"] == "handoff-stale-result"


def test_native_review_uses_explicit_review_pool_and_preserves_dev_candidate(campaign):
    runtime, snapshot, unit, receipt = campaign
    original = dict(snapshot.outbox[0].payload["candidate"])
    receipt = {**receipt, "interruption": {"kind": "quota", "role": "review"}}
    flow.intercept_terminal(runtime, snapshot, unit, receipt)
    value = unit.evidence["handoff"]
    assert value["route_role"] == "execution_review" and value["candidate"] is None
    snapshot.spec = dataclasses.replace(
        snapshot.spec, roles=(*snapshot.spec.roles, ("execution_review", RoleSpec(pool="auditors")))
    )
    proposal = flow.proposals(runtime, snapshot)[0]
    assert proposal.kind == "handoff-route"
    action = OutboxAction("route", proposal.kind, unit.id, proposal.payload, 0)
    flow.apply(runtime, snapshot, action, flow.perform(runtime, snapshot, action))
    value["capture"] = {"audit_context": flow._audit_context(snapshot, unit)}
    value["phase"] = "ready"
    proposal = flow.resume_proposal(snapshot, unit)
    assert proposal.payload["candidate"] == original
    assert proposal.payload["review_candidate"]["profile"] == "claude"
    assert proposal.payload["kind"] == "execution" and proposal.payload["role"] == "execution"


def test_cli_selector_is_resolved_in_frozen_pool_not_ignored(campaign):
    runtime, snapshot, unit, _ = campaign
    with pytest.raises(CampaignError, match="approved role pool"):
        flow.control(
            runtime,
            snapshot,
            {"unit": unit.id, "payload": {"profile": "muse", "model": "not-approved"}},
        )
    flow.control(
        runtime,
        snapshot,
        {
            "unit": unit.id,
            "reason": "quota swap",
            "payload": {"profile": "claude", "model": "claude-test"},
        },
    )
    assert unit.evidence["handoff"]["candidate"]["effort"] == "high"
    assert unit.evidence["handoff"]["reason"] == "quota swap"


def test_missing_fidelity_proof_waits_without_authorizing_writer(campaign):
    runtime, snapshot, unit, receipt = campaign
    snapshot.config_snapshot["reasoning_capabilities"]["claude:claude-test"]["reasoning"].pop(
        "semantic_qualification"
    )
    flow.intercept_terminal(runtime, snapshot, unit, receipt)
    step(runtime, snapshot, unit, "handoff-capture")
    step(runtime, snapshot, unit, "handoff-plan")
    complete_maps(runtime, snapshot, unit)
    step(runtime, snapshot, unit, "handoff-audit-plan")
    assert unit.evidence["handoff"]["phase"] == "summarize"
    assert "fidelity qualification is missing" in unit.hold.reason
    assert flow.resume_proposal(snapshot, unit) is None


def test_administrative_job_replan_uses_smaller_route_and_preserves_completed_work(campaign):
    runtime, snapshot, unit, receipt = campaign
    source = Path(receipt["sessions"][0]["transcript_path"])
    with source.open("a") as stream:
        stream.write(
            json.dumps(
                {
                    "type": "event_msg",
                    "payload": {
                        "type": "user_message",
                        "message": "Do preserve this detail. " * 6000,
                    },
                }
            )
            + "\n"
        )
    first, second = snapshot.spec.candidates("execution")
    snapshot.spec = dataclasses.replace(
        snapshot.spec,
        role_pools=tuple(
            (name, (first, second) if name == "map" else pool)
            for name, pool in snapshot.spec.role_pools
        ),
    )
    smaller = snapshot.config_snapshot["reasoning_capabilities"]["claude:claude-test"]["reasoning"]
    smaller["input_tokens"] = 80000
    flow.intercept_terminal(runtime, snapshot, unit, receipt)
    step(runtime, snapshot, unit, "handoff-capture")
    step(runtime, snapshot, unit, "handoff-plan")
    value = unit.evidence["handoff"]
    original = list(value["jobs"])
    assert len(original) > 2
    value["jobs"] = original[:1]
    complete_maps(runtime, snapshot, unit)
    value["jobs"] = original
    for key in original[1:]:
        row = snapshot.jobs[key]
        row.update(state="capacity_wait", selection=row["request"]["candidates"][0])
        assert not flow.job_launch_allowed(snapshot, row)
    step(runtime, snapshot, unit, "handoff-replan")
    assert original[0] in value["jobs"]
    new_ids = [key for key in value["jobs"] if key not in original]
    assert new_ids and all(
        snapshot.jobs[key]["request"]["candidates"][0]["profile"] == "claude" for key in new_ids
    )
    assert all(snapshot.jobs[key]["state"] == "canceled" for key in original[1:])


def test_ready_handoff_refreshes_when_owner_constraints_change(campaign):
    runtime, snapshot, unit, receipt = campaign
    flow.intercept_terminal(runtime, snapshot, unit, receipt)
    step(runtime, snapshot, unit, "handoff-capture")
    step(runtime, snapshot, unit, "handoff-plan")
    complete_maps(runtime, snapshot, unit)
    step(runtime, snapshot, unit, "handoff-audit-plan")
    complete_audits(runtime, snapshot, unit)
    step(runtime, snapshot, unit, "handoff-assemble")
    step(runtime, snapshot, unit, "handoff-release")
    assert flow.resume_proposal(snapshot, unit)
    unit.evidence["decision_input_revision"] = "new-owner-constraints"
    assert flow.resume_proposal(snapshot, unit) is None
    assert [row.kind for row in flow.proposals(runtime, snapshot)] == ["handoff-refresh"]
    step(runtime, snapshot, unit, "handoff-refresh")
    assert unit.hold.kind == "handoff"
    assert unit.evidence["handoff"]["phase"] == "capture"


def test_effect_observation_cannot_apply_after_current_context_changes(campaign):
    runtime, snapshot, unit, receipt = campaign
    flow.intercept_terminal(runtime, snapshot, unit, receipt)
    value = unit.evidence["handoff"]
    action = OutboxAction("late", "handoff-capture", unit.id, {"handoff_id": value["id"]}, 0)
    observed = flow.perform(runtime, snapshot, action)
    snapshot.graph_revision += 1
    flow.apply(runtime, snapshot, action, observed)
    assert value["phase"] == "drain"
    assert snapshot.events[-1]["kind"] == "handoff-stale-result"


def test_semantic_failure_retries_same_job_without_resetting_failure_budget(campaign):
    runtime, snapshot, unit, receipt = campaign
    flow.intercept_terminal(runtime, snapshot, unit, receipt)
    step(runtime, snapshot, unit, "handoff-capture")
    step(runtime, snapshot, unit, "handoff-plan")
    row = snapshot.jobs[unit.evidence["handoff"]["jobs"][0]]
    row.update(state="failed", failure_attempts=1, receipt={"outcome": "completed"})
    assert not flow.proposals(runtime, snapshot)
    assert flow.job_launch_allowed(snapshot, row)
    row["failure_attempts"] = snapshot.spec.reasoning.max_attempts
    assert flow.proposals(runtime, snapshot)[0].kind == "handoff-failed"
    proposed = flow.proposals(runtime, snapshot)[0]
    action = OutboxAction("exhausted", proposed.kind, unit.id, proposed.payload, 0)
    flow.apply(runtime, snapshot, action, flow.perform(runtime, snapshot, action))
    assert not flow.proposals(runtime, snapshot)
    # The shared reasoning admission applies this unchanged terminal attempt
    # budget. No replacement identity can erase failed semantic attempts.
    assert len(snapshot.jobs) == 1
