"""Owned continuation preserves work and never transfers approval or native IDs."""

import copy
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


@pytest.fixture
def campaign(prepared, project, tmp_path):
    _, _, _, first, basecap = prepared
    second = RoleCandidate("claude", "claude-test", "high", "anthropic")

    def cap(candidate, execution=False, **limits):
        from test_campaign_execution_qualification import fixture_writer_capability
        from test_campaign_headless import fixture_capability

        value = {
            **basecap,
            "profile": candidate.profile,
            "model": candidate.model,
            "efforts": [candidate.effort],
            "permissions": "workspace-tools" if execution else "no-tools",
            **limits,
        }
        if execution:
            value = fixture_writer_capability(tmp_path, value, candidate, input_bytes=100000)
        else:
            value = fixture_capability(tmp_path, value, candidate)
        from bmad_loop.campaign_fidelity import (
            FINAL_AUDIT_PROMPT_VERSION,
            HISTORY_AUDIT_PROMPT_VERSION,
            PURPOSES,
            route_binding,
            semantic_contract,
        )
        from bmad_loop.campaign_headless import file_digest, read_json, write_json
        from bmad_loop.campaign_summary import (
            EXTRACT_PROMPT_VERSION,
            REDUCE_PROMPT_VERSION,
            summary_schema,
            summary_schema_template,
        )
        from bmad_loop.campaign_transcripts import READER_VERSIONS, digest

        route_bindings = [route_binding(value, purpose, candidate) for purpose in sorted(PURPOSES)]
        evidence_name = (
            candidate.profile + "-" + digest(dataclasses.asdict(candidate))[:12] + str(execution)
        )

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

        native_path = tmp_path / (evidence_name + "-native-fidelity.json")
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
                    "audit_prompt_version": HISTORY_AUDIT_PROMPT_VERSION,
                    "route_bindings_digest": digest(route_bindings),
                    "semantic_contract_digests": {
                        purpose: digest(semantic_contract(purpose, flow.AUDIT_SCHEMA))
                        for purpose in (
                            "handoff_map",
                            "handoff_reduce",
                            "handoff_final",
                            "handoff_final_audit",
                            "handoff_audit",
                        )
                    },
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
            "schema_version": 2,
            "route_bindings": route_bindings,
            "reader_versions": READER_VERSIONS,
            "summary_schema_digest": digest(summary_schema()),
            "summary_job_schema_digest": digest(summary_schema_template()),
            "audit_schema_digest": digest(flow.AUDIT_SCHEMA),
            "constraint_schemas_digest": digest([REDUCTION_SCHEMA, CONSTRAINT_AUDIT_SCHEMA]),
            "recovery_schemas_digest": digest([RECOVERY_SCHEMA, RECOVERY_AUDIT_SCHEMA]),
            "prompt_versions": [EXTRACT_PROMPT_VERSION, REDUCE_PROMPT_VERSION],
            "audit_prompt_version": HISTORY_AUDIT_PROMPT_VERSION,
            "structural_pass": True,
            "corpus": corpus,
            "independent_review": {
                "native_receipt_path": str(native_path),
                "sha256": file_digest(native_path),
            },
        }
        qualification_path = tmp_path / (evidence_name + "-fidelity.json")
        write_json(qualification_path, qualification)
        primary = {
            "schema_version": 2,
            "path": str(qualification_path),
            "sha256": file_digest(qualification_path),
        }
        # Final-synthesis auditing earns a different prompt header. Keep its
        # receipt/proof separate so the legacy history audit remains qualified.
        final_bindings = [route_binding(value, "handoff_final_audit", candidate)]
        final_native = read_json(native_path)
        final_native["result"].update(
            audit_prompt_version=FINAL_AUDIT_PROMPT_VERSION,
            route_bindings_digest=digest(final_bindings),
        )
        final_native_path = tmp_path / (evidence_name + "-native-final-audit.json")
        write_json(final_native_path, final_native)
        final_proof = copy.deepcopy(qualification)
        final_proof.update(
            route_bindings=final_bindings,
            audit_prompt_version=FINAL_AUDIT_PROMPT_VERSION,
            independent_review={
                "native_receipt_path": str(final_native_path),
                "sha256": file_digest(final_native_path),
            },
        )
        final_path = tmp_path / (evidence_name + "-final-audit-fidelity.json")
        write_json(final_path, final_proof)
        value["semantic_qualification"] = {
            **primary,
            "proofs": [
                primary,
                {"path": str(final_path), "sha256": file_digest(final_path)},
            ],
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
    runtime = SimpleNamespace(
        store=CampaignStore(tmp_path / "store"), clock=lambda: 100.0, fixture_cap=cap
    )
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
            receipt={"job_id": key, "result": result, "provider_family": "openai"},
        )
        apply_result(runtime, snapshot, row)
        row["state"] = "applied"


def complete_audits(runtime, snapshot, unit):
    value = unit.evidence["handoff"]
    for key in value["audits"]:
        row = snapshot.jobs[key]
        coord = row["request"]["coordinates"]
        result = {
            "handoff_id": value["id"],
            "capture_digest": coord["capture_digest"],
            "covered_owner_ids": coord["owner_ids"],
            "covered_span_ids": [span["span_id"] for span in coord["owner_spans"]],
            "obligation_ids": coord["obligation_ids"],
            "ambiguities": [],
            "constraints": [
                {
                    "span_id": span["span_id"],
                    "interpretation": "Preserve this requirement",
                    "relevance": "current",
                    "exclusion_evidence_id": "",
                }
                for span in coord["owner_spans"]
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
    assert proposed is not None
    assert proposed.payload["candidate"]["profile"] == "claude"
    request = {**proposed.payload, "role": "execution"}
    flow.decorate_launch(snapshot, unit, request)
    assert request["native_predecessor"]["launch_id"] == "original"
    assert request["handoff_packet"]["read_only"] is False
    assert unit.bindings["pr"] == 17 and path.read_text() == "uncommitted predecessor work"
    assert unit.evidence["developer_families"] == ["openai"]
    unit.claim = dataclasses.replace(unit.claim, launch_id="replacement", quiescent=True)
    assert not flow.intercept_terminal(runtime, snapshot, unit, {"outcome": "locally_complete"})


def test_post_audit_context_fit_retains_omitted_summary_retrieval(campaign, monkeypatch):
    import copy

    from bmad_loop import campaign_handoff_context as context
    from bmad_loop.campaign_headless import read_json
    from bmad_loop.campaign_reasoning_results import apply_result
    from bmad_loop.campaign_summary import SummaryJobSpec, plan_reduction

    runtime, snapshot, unit, receipt = campaign
    assert flow.intercept_terminal(runtime, snapshot, unit, receipt)
    step(runtime, snapshot, unit, "handoff-capture")
    step(runtime, snapshot, unit, "handoff-plan")
    complete_maps(runtime, snapshot, unit)
    value = unit.evidence["handoff"]
    row = snapshot.jobs[value["jobs"][0]]
    row["result"]["facts"][0]["text"] = "Keep all accepted functionality. " * 35
    apply_result(runtime, snapshot, row)
    child = row
    child_spec = SummaryJobSpec(**child["request"]["coordinates"]["summary_spec"])
    candidate = flow._candidate(value["candidate"])
    reducer = plan_reduction(
        ((child_spec, flow._applied(child)),),
        flow._budget(candidate, flow._cap(snapshot, candidate)),
        runtime.store.root / "test-reduction",
    )[0]
    row = copy.deepcopy(child)
    row["id"] = reducer.id
    row["request"].update(id=reducer.id, purpose="handoff_reduce")
    row["request"]["coordinates"]["summary_spec"] = dataclasses.asdict(reducer)
    row["result"].update(
        job_id=reducer.id, input_sha256=reducer.input_sha256, covered_ids=list(reducer.primary_ids)
    )
    row["receipt"].update(job_id=reducer.id, result=row["result"])
    snapshot.jobs[row["id"]] = row
    apply_result(runtime, snapshot, row)
    value["all_jobs"] = [child["id"], row["id"]]
    value["jobs"] = [row["id"]]
    step(runtime, snapshot, unit, "handoff-audit-plan")
    complete_audits(runtime, snapshot, unit)
    original = context.assemble_packet
    initial = []

    def observe_initial(*args, **kwargs):
        packet = original(*args, **kwargs)
        initial.append(read_json(Path(packet.path))["summaries"])
        return packet

    monkeypatch.setattr(context, "assemble_packet", observe_initial)
    baseline = flow._assemble(runtime, snapshot, unit, value)
    payload = read_json(Path(baseline["packet"]["path"]))
    assert initial[-1] and not payload["summaries"] and payload["history_view"]
    assert baseline["packet"]["tokens"] <= snapshot.spec.reasoning.packet_token_limit
    assert context.retrieve_summary(payload, row["id"]) == flow._applied(row)
    parent = context.retrieve_summary(payload, row["id"])
    assert parent["covered_ids"] == [child["id"]]
    assert context.retrieve_summary(payload, parent["covered_ids"][0]) == flow._applied(child)
    omitted = read_json(Path(payload["omitted_summary_index"]["path"]))
    assert omitted["final_summary_ids"] == [row["id"]]


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
    receipt = {
        **receipt,
        "sessions": [{**row, "role": "review"} for row in receipt["sessions"]],
        "interruption": {"kind": "quota", "role": "review"},
    }
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
    step(runtime, snapshot, unit, "handoff-capture")
    step(runtime, snapshot, unit, "handoff-plan")
    complete_maps(runtime, snapshot, unit)
    step(runtime, snapshot, unit, "handoff-audit-plan")
    complete_audits(runtime, snapshot, unit)
    step(runtime, snapshot, unit, "handoff-assemble")
    step(runtime, snapshot, unit, "handoff-release")
    proposal = flow.resume_proposal(snapshot, unit)
    assert proposal is not None
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


def test_context_spans_survive_successor_and_missing_exact_span_blocks_packet(campaign):
    import hashlib

    from bmad_loop.campaign_transcripts import blob, encoded

    runtime, snapshot, unit, receipt = campaign
    flow.intercept_terminal(runtime, snapshot, unit, receipt)
    step(runtime, snapshot, unit, "handoff-capture")
    step(runtime, snapshot, unit, "handoff-plan")
    complete_maps(runtime, snapshot, unit)
    step(runtime, snapshot, unit, "handoff-audit-plan")
    complete_audits(runtime, snapshot, unit)
    value = unit.evidence["handoff"]
    row = snapshot.jobs[value["audits"][0]]
    row["result"]["constraints"][0]["relevance"] = "context"
    flow.apply_audit(runtime, snapshot, row)
    result = flow._assemble(runtime, snapshot, unit, value)
    packet = json.loads(Path(result["packet"]["path"]).read_text())
    assert packet["resolved_constraints"][0]["relevance"] == "context"
    assert (
        packet["resolved_constraints"][0]["source_quote"]
        == "Keep history and all acceptance criteria."
    )
    applied = flow._applied(row)
    applied["constraints"] = []
    data = encoded(applied)
    path = blob(flow._root(runtime, snapshot, value) / "legacy-omission-fixture", data)
    row["applied_result"] = {
        "artifact_path": str(path),
        "artifact_sha256": hashlib.sha256(data).hexdigest(),
    }
    with pytest.raises(CampaignError, match="complete native-owner span coverage"):
        flow._assemble(runtime, snapshot, unit, value)


@pytest.mark.parametrize("persisted_phase", ["audit", "ready"])
def test_persisted_legacy_audit_contract_refreshes_before_any_writer_admission(
    campaign, persisted_phase
):
    runtime, snapshot, unit, receipt = campaign
    flow.intercept_terminal(runtime, snapshot, unit, receipt)
    step(runtime, snapshot, unit, "handoff-capture")
    step(runtime, snapshot, unit, "handoff-plan")
    complete_maps(runtime, snapshot, unit)
    step(runtime, snapshot, unit, "handoff-audit-plan")
    complete_audits(runtime, snapshot, unit)
    if persisted_phase == "ready":
        step(runtime, snapshot, unit, "handoff-assemble")
        step(runtime, snapshot, unit, "handoff-release")
        assert flow.resume_proposal(snapshot, unit)
    value = unit.evidence["handoff"]
    old_audits = [flow._job(runtime, snapshot, key)["applied_result"] for key in value["audits"]]
    old_bytes = [Path(row["artifact_path"]).read_bytes() for row in old_audits]
    old_packet = value.get("packet")
    old_packet_bytes = Path(old_packet["path"]).read_bytes() if old_packet else None
    # Schema-2 captures used event-level audits and never bound their contract.
    value["capture"]["audit_context"].pop("history_audit_contract", None)
    snapshot = runtime.store.save(snapshot, snapshot.revision)
    snapshot = runtime.store.load(snapshot.id)
    unit = snapshot.units[unit.id]
    value = unit.evidence["handoff"]
    assert flow.resume_proposal(snapshot, unit) is None
    assert not flow.job_launch_allowed(snapshot, flow._job(runtime, snapshot, value["audits"][0]))
    if persisted_phase == "ready":
        request = {
            "handoff_id": value["id"],
            "role": value["role"],
            "candidate": value["candidate"],
        }
        with pytest.raises(CampaignError, match="constraints changed before launch"):
            flow.decorate_launch(snapshot, unit, request)
        replacement = dataclasses.replace(unit.claim, launch_id="successor-v3", quiescent=False)
        with pytest.raises(CampaignError, match="constraints changed before claim adoption"):
            flow.adopt_launch(snapshot, unit, request, replacement)
    assert [row.kind for row in flow.proposals(runtime, snapshot)] == ["handoff-refresh"]
    step(runtime, snapshot, unit, "handoff-refresh")
    assert value["phase"] == "capture" and unit.hold.kind == "handoff"
    assert (
        value["capture"]["audit_context"]["history_audit_contract"]
        == flow._audit_context(snapshot, unit)["history_audit_contract"]
    )
    assert old_bytes == [Path(row["artifact_path"]).read_bytes() for row in old_audits]
    if old_packet:
        assert Path(old_packet["path"]).read_bytes() == old_packet_bytes
