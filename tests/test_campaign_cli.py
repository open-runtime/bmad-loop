"""Campaign command wiring and the single-document/observer contracts."""

import json
import sys
import types
from dataclasses import asdict

import pytest
from conftest import machine_json

from bmad_loop import cli
from bmad_loop.campaign_cli import inbox_document, status_document
from bmad_loop.campaign_model import (
    CampaignSnapshot,
    CampaignUnit,
    Hold,
    OutboxAction,
    UnitSpec,
    UnitState,
    spec_from_dict,
)
from bmad_loop.campaign_store import CampaignStore


@pytest.fixture
def campaign_spec(tmp_path):
    path = tmp_path / "campaign.toml"
    path.write_text('schema_version = 1\nname = "offline"\n[[sources]]\nkind = "stories"\n')
    return path


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    import bmad_loop
    from bmad_loop import campaign_preflight
    from bmad_loop.campaign_supervisor import Observation, Proposal

    store = CampaignStore(tmp_path / "campaign-state")
    spec = spec_from_dict(
        {"schema_version": 1, "name": "test", "sources": [{"kind": "stories", "path": "specs"}]}
    )
    unit = CampaignUnit(
        UnitSpec("s:e:u", "s", "e", "One unit"),
        UnitState.READY,
        bindings={"head": "head", "branch": "branch", "pr_url": "https://example/pr/1"},
    )
    snapshot = store.create(
        CampaignSnapshot(
            "cli-campaign",
            "org/repo",
            spec,
            {unit.id: unit},
            config_snapshot={"credentials": "private-policy-secret"},
        )
    )
    calls = []

    class Runtime:
        def __init__(self, project, saved_store):
            calls.append(("runtime", project, saved_store.root))

        def authorize_backup_resume(self, saved_snapshot):
            calls.append(("backup-resume", saved_snapshot.id))

        def attach(self, campaign_id, unit_id, role="execution"):
            calls.append(("attach", campaign_id, unit_id, role))
            return 0

    class Supervisor:
        def __init__(self, saved_store, driver):
            self.store = saved_store

        def run(self, campaign_id):
            calls.append(("run", campaign_id))
            return self.store.load(campaign_id)

        def reconcile(self, campaign_id):
            calls.append(("reconcile", campaign_id))
            return self.store.load(campaign_id)

    def create(project, spec, *, prepare_only=False):
        calls.append(("create", project, spec))
        if prepare_only:
            snapshot.phase = "stopped"
            return store, store.save(snapshot, snapshot.revision)
        return store, snapshot

    runtime_module = types.ModuleType("bmad_loop.campaign_runtime")
    runtime_module.CampaignRuntime = Runtime
    runtime_module.create = create
    runtime_module.store_for = lambda project: store
    supervisor_module = types.ModuleType("bmad_loop.campaign_supervisor")
    supervisor_module.CampaignSupervisor = Supervisor
    supervisor_module.Observation = Observation
    supervisor_module.Proposal = Proposal
    monkeypatch.setitem(sys.modules, "bmad_loop.campaign_runtime", runtime_module)
    monkeypatch.setitem(sys.modules, "bmad_loop.campaign_supervisor", supervisor_module)
    monkeypatch.setattr(bmad_loop, "campaign_runtime", runtime_module, raising=False)
    monkeypatch.setattr(bmad_loop, "campaign_supervisor", supervisor_module, raising=False)
    monkeypatch.setattr(
        campaign_preflight,
        "inspect",
        lambda project, spec: {"schema_version": 1, "ok": True, "capabilities": {}},
    )
    return store, snapshot, calls


def test_validate_is_offline_and_skips_project_mux_and_policy(
    campaign_spec, tmp_path, monkeypatch, capsys
):
    def forbidden(*args, **kwargs):
        raise AssertionError("offline validate reached a project/configuration seam")

    monkeypatch.setattr(cli, "_project", forbidden)
    monkeypatch.setattr(cli, "_configure_mux", forbidden)
    monkeypatch.setattr(cli.policy_mod, "load", forbidden)
    before = set(tmp_path.rglob("*"))
    assert (
        cli.main(
            ["campaign", "validate", str(campaign_spec), "--project", "/does/not/exist", "--json"]
        )
        == 0
    )
    output = capsys.readouterr()
    doc = json.loads(output.out)
    assert doc["schema_version"] == 1
    assert doc["command"] == "campaign.validate"
    assert doc["ok"] is True
    assert output.err == ""
    assert set(tmp_path.rglob("*")) == before


def test_invalid_spec_is_a_complete_failing_verdict(campaign_spec, capsys):
    campaign_spec.write_text('schema_version = 999\nname = "future"\n')
    assert cli.main(["campaign", "validate", str(campaign_spec), "--json"]) == 1
    output = capsys.readouterr()
    doc = json.loads(output.out)
    assert doc["ok"] is False
    assert "schema_version" in doc["problems"][0]
    assert output.err == ""


def test_requalify_passes_explicit_revision_and_does_not_resume(
    runtime, tmp_path, monkeypatch, capsys
):
    from bmad_loop import campaign_requalification

    store, snapshot, calls = runtime
    manifest = tmp_path / "qualified.json"
    received = []

    def renew(saved_store, campaign_id, capabilities, *, expected_revision, reason):
        received.append((saved_store, campaign_id, capabilities, expected_revision, reason))
        snapshot.phase = "stopped"
        snapshot.config_revision += 1
        return snapshot

    monkeypatch.setattr(campaign_requalification, "requalify", renew)
    assert (
        cli.main(
            [
                "campaign",
                "requalify",
                snapshot.id,
                "--capabilities",
                str(manifest),
                "--expected-revision",
                str(snapshot.revision),
                "--reason",
                "installed route renewal",
                "--json",
            ]
        )
        == 0
    )
    output = capsys.readouterr()
    result = json.loads(output.out)
    assert result["command"] == "campaign.requalify"
    assert result["campaign"]["phase"] == "stopped"
    assert result["campaign"]["config_revision"] == 2
    assert received == [
        (store, snapshot.id, manifest, snapshot.revision, "installed route renewal")
    ]
    assert output.err == ""
    assert calls == []


def test_preflight_has_separate_capabilities_and_does_not_create_campaign(
    campaign_spec, runtime, capsys, monkeypatch
):
    from bmad_loop import campaign_preflight

    store, _, calls = runtime
    before = set(store.root.rglob("*"))
    observed = {
        "schema_version": 1,
        "ok": False,
        "capabilities": {
            "execution": {"ready": True, "problems": []},
            "publication": {"ready": False, "problems": ["strict checks required"]},
        },
    }
    monkeypatch.setattr(campaign_preflight, "inspect", lambda project, spec: observed)
    assert cli.main(["campaign", "preflight", str(campaign_spec), "--json"]) == 1
    doc = json.loads(capsys.readouterr().out)
    assert doc["capabilities"]["execution"]["ready"] is True
    assert doc["capabilities"]["publication"]["ready"] is False
    assert set(store.root.rglob("*")) == before
    assert calls == []


def test_run_persists_resolved_reflection_overrides_once(campaign_spec, runtime, capsys):
    _, _, calls = runtime
    assert (
        cli.main(
            [
                "campaign",
                "run",
                str(campaign_spec),
                "--paradev-reflect",
                "--paradev-project",
                "project-42",
                "--json",
            ]
        )
        == 0
    )
    doc = json.loads(capsys.readouterr().out)
    assert doc["campaign"]["id"] == "cli-campaign"
    creation = next(call for call in calls if call[0] == "create")
    assert creation[2].paradev.reflect is True
    assert creation[2].paradev.project_id == "project-42"
    assert ("run", "cli-campaign") in calls


def test_failed_preflight_prevents_run_creation(campaign_spec, runtime, monkeypatch, capsys):
    from bmad_loop import campaign_preflight

    _, _, calls = runtime
    monkeypatch.setattr(
        campaign_preflight,
        "inspect",
        lambda project, spec: {"schema_version": 1, "ok": False, "capabilities": {}},
    )
    assert cli.main(["campaign", "run", str(campaign_spec), "--json"]) == 1
    assert json.loads(capsys.readouterr().out)["ok"] is False
    assert not any(call[0] in {"create", "run"} for call in calls)


def test_prepare_only_returns_persisted_stopped_campaign_without_starting_runtime(
    campaign_spec, runtime, capsys
):
    store, snapshot, calls = runtime
    assert cli.main(["campaign", "run", str(campaign_spec), "--prepare-only", "--json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["campaign"]["phase"] == "stopped"
    saved = store.load(snapshot.id)
    assert saved.phase == "stopped" and not saved.outbox
    assert [call[0] for call in calls] == ["create"]


def test_status_and_list_are_observer_projections_without_private_config(runtime, capsys):
    store, snapshot, calls = runtime
    snapshot.bindings["decision_receipts"] = {"private": {"chat": "private-chat-source"}}
    snapshot.units["s:e:u"].evidence["diagnosis"] = "private-technical-context"
    snapshot = store.save(snapshot, snapshot.revision)
    assert cli.main(["campaign", "status", snapshot.id, "--json"]) == 0
    raw = capsys.readouterr().out
    doc = json.loads(raw)
    assert doc["campaign"]["units"][0]["links"]["head"] == "head"
    for secret in ("private-policy-secret", "private-chat-source", "private-technical-context"):
        assert secret not in raw
    assert cli.main(["campaign", "list", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["campaigns"][0]["id"] == snapshot.id
    assert store.load(snapshot.id).revision == snapshot.revision
    assert calls == []


def test_inbox_is_read_only_and_names_exact_hold_and_decision(runtime, monkeypatch, capsys):
    from bmad_loop import campaign_cli

    store, snapshot, calls = runtime
    unit = snapshot.units["s:e:u"]
    unit.hold = Hold("approval", "Owner signoff remains required", id="hold-1", actor="owner")
    unit.attempt = 2
    snapshot.bindings["decision_receipts"] = {
        "decision-1": {
            "coordinates": {"unit": unit.id},
            "hold_id": "hold-1",
            "answer": "private-decision-text",
        },
        "foreign": {"coordinates": {"unit": "another-unit"}, "hold_id": "hold-1"},
    }
    snapshot = store.save(snapshot, snapshot.revision)

    def forbidden(*args, **kwargs):
        raise AssertionError("read-only inbox reached mutation/runtime authority")

    monkeypatch.setattr(store, "enqueue", forbidden)
    monkeypatch.setattr(campaign_cli, "_runtime", forbidden)
    result = machine_json(["campaign", "inbox", snapshot.id, "--json"], capsys)
    assert result["schema_version"] == 1 and result["command"] == "campaign.inbox"
    item = next(row for row in result["inbox"]["items"] if row["kind"] == "hold")
    assert item["unit_id"] == unit.id and item["hold_id"] == "hold-1"
    assert item["decision_ids"] == ["decision-1"]
    assert item["reason"] == "Owner signoff remains required"
    assert item["operator_action_required"] is True
    assert item["pause_evidence_status"] == "unknown"
    raw = json.dumps(result)
    assert "private-decision-text" not in raw and "private-policy-secret" not in raw
    assert "campaign rearm" not in raw and "campaign answer" not in raw
    after = store.load(snapshot.id)
    assert after.revision == snapshot.revision
    assert asdict(after.units[unit.id].hold) == asdict(unit.hold)
    assert after.units[unit.id].attempt == 2
    assert store.commands(snapshot.id) == [] and calls == []


@pytest.mark.parametrize(
    "cause",
    [
        None,
        {"kind": "technical_failure"},
        {"schema_version": 1, "kind": "future_kind", "producer": "engine", "code": "future"},
    ],
)
def test_inbox_never_infers_missing_or_invalid_pause_cause_from_reason(runtime, cause):
    _, snapshot, _ = runtime
    unit = snapshot.units["s:e:u"]
    unit.hold = Hold(
        "paused",
        "technical failure: safe to retry and approved",
        id="legacy",
        evidence={"child_pause": {"schema_version": 1, "cause": cause}},
    )
    item = next(row for row in inbox_document(snapshot)["items"] if row["kind"] == "hold")
    assert item["pause_evidence_status"] == "unknown"
    assert item["pause_cause"] is None
    assert "No automatic continuation" in item["next_automatic_action"]


@pytest.fixture
def inbox_pause(runtime, request):
    from bmad_loop.campaign_child_pause import build
    from bmad_loop.model import PauseCause, PauseKind, Phase, RunState, StoryTask

    _, snapshot, _ = runtime
    unit = snapshot.units["s:e:u"]
    kind = getattr(request, "param", "technical_failure")
    cause = (
        PauseCause(PauseKind(kind), "engine.environment", "dispatch_probe")
        if kind is not None
        else None
    )
    state = RunState("child-run", "/synthetic/workspace", "2026-11-17T00:00:00Z")
    state.tasks["first"] = StoryTask("first", 1, phase=Phase.DEV_RUNNING, attempt=2)
    state.record_pause("Probe failed", "dispatch", "first", cause=cause)
    request = {
        "unit": {"selector": "first"},
        "source": {"kind": "stories", "id": "s", "path": "specs"},
        "source_revision": "source-1",
    }
    binding = {
        "campaign_id": snapshot.id,
        "unit_id": unit.id,
        "launch_id": "pause-hold",
        "attempt": 1,
        "generation": 1,
        "workspace": "/synthetic/workspace",
        "branch": "branch",
        "common_dir": "/synthetic/git",
        "head": "a" * 40,
        "run_id": state.run_id,
        "run_dir": "/synthetic/run",
    }
    sealed = build(
        state, request, types.SimpleNamespace(config_revision=snapshot.config_revision), binding
    )
    unit.hold = Hold("paused", "Probe failed", id="pause-hold", evidence={"child_pause": sealed})
    return snapshot, unit, cause


def test_inbox_projects_typed_pause_without_granting_continuation(inbox_pause):
    snapshot, unit, cause = inbox_pause
    item = next(row for row in inbox_document(snapshot)["items"] if row["kind"] == "hold")
    assert item["pause_evidence_status"] == "typed"
    assert item["pause_cause"] == cause.to_dict()
    assert "No automatic continuation" in item["next_automatic_action"]
    assert unit.hold.id == "pause-hold"


@pytest.mark.parametrize("inbox_pause", [None, "unknown"], indirect=True)
def test_complete_pause_receipt_retains_unknown_producer_evidence(inbox_pause):
    snapshot, _, cause = inbox_pause
    item = next(row for row in inbox_document(snapshot)["items"] if row["kind"] == "hold")
    assert item["pause_evidence_status"] == "unknown"
    assert item["pause_evidence_reason"]
    assert item["pause_cause"] == (cause.to_dict() if cause is not None else None)
    assert "No automatic continuation" in item["next_automatic_action"]


@pytest.mark.parametrize(
    "change",
    [
        "cause_only",
        "missing_binding",
        "partial_binding",
        "binding_list",
        "missing_task",
        "missing_source",
        "changed_digest",
        "foreign_unit",
        "foreign_campaign",
    ],
)
def test_inbox_rejects_incomplete_or_rehashed_malformed_pause_receipt(inbox_pause, change):
    import hashlib

    snapshot, unit, _ = inbox_pause
    baseline = next(row for row in inbox_document(snapshot)["items"] if row["kind"] == "hold")
    assert baseline["pause_evidence_status"] == "typed"
    pause = json.loads(json.dumps(unit.hold.evidence["child_pause"]))
    if change == "cause_only":
        pause = {"schema_version": 1, "cause": pause["cause"]}
    elif change == "missing_binding":
        pause.pop("binding")
    elif change == "partial_binding":
        pause["binding"].pop("run_dir")
    elif change == "binding_list":
        pause["binding"] = []
    elif change == "missing_task":
        pause.pop("task_digest")
    elif change == "missing_source":
        pause.pop("source")
    elif change == "foreign_unit":
        pause["binding"]["unit_id"] = "another-unit"
    elif change == "foreign_campaign":
        pause["binding"]["campaign_id"] = "another-campaign"
    body = {key: value for key, value in pause.items() if key != "id"}
    pause["id"] = (
        "0" * 64
        if change == "changed_digest"
        else hashlib.sha256(
            json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    )
    unit.hold = Hold(
        unit.hold.kind, unit.hold.reason, id=unit.hold.id, evidence={"child_pause": pause}
    )
    item = next(row for row in inbox_document(snapshot)["items"] if row["kind"] == "hold")
    assert item["pause_evidence_status"] == "unknown"
    assert item["pause_cause"] is None
    assert "invalid" in item["pause_evidence_reason"]
    assert "No automatic continuation" in item["next_automatic_action"]


def test_inbox_redacts_synthetic_credential_without_hiding_safe_reason(runtime):
    _, snapshot, _ = runtime
    secret = "ghp_" + "A" * 36
    snapshot.units["s:e:u"].hold = Hold(
        "paused", "Provider authentication failed: " + secret, id="hold"
    )
    item = next(row for row in inbox_document(snapshot)["items"] if row["kind"] == "hold")
    assert "Provider authentication failed" in item["reason"]
    assert secret not in item["reason"]


@pytest.mark.parametrize(
    "sequence", ["\x1b]0;forged-title\x07", "\x1b]52;c;clipboard-payload\x1b\\", "\x1b[31m"]
)
def test_inbox_remote_reason_cannot_emit_terminal_controls(runtime, capsys, sequence):
    store, snapshot, _ = runtime
    assert "\x1b" in sequence
    snapshot.units["s:e:u"].hold = Hold("transport", sequence + "waiting for capacity", id="hold")
    store.save(snapshot, snapshot.revision)
    assert cli.main(["campaign", "inbox", snapshot.id]) == 0
    output = capsys.readouterr().out
    assert "waiting for capacity" in output
    assert "\x1b" not in output and "\x07" not in output
    assert "forged-title" not in output and "clipboard-payload" not in output


def test_inbox_shows_unacknowledged_effect_without_fault_or_retry_time(runtime):
    _, snapshot, _ = runtime
    unit = snapshot.units["s:e:u"]
    unknown = OutboxAction(
        "merge-effect",
        "github",
        unit.id,
        {"intent": {"payload": {"body": "private-body"}}},
        1,
        state="sent_unknown",
    )
    refused = OutboxAction(
        "refused-effect", "github", unit.id, {}, 1, state="rejected", error="Permission denied"
    )
    delayed = OutboxAction(
        "delayed-effect",
        "github",
        unit.id,
        {},
        1,
        state="sent_unknown",
        error="Rate limit",
        next_retry_at=200,
    )
    delayed.receipt = {
        "github_fault": {
            "schema_version": 1,
            "category": "rate_limit",
            "certainty": "unknown",
            "retry_at": 200,
            "request_id": "request-17",
            "payload": "private-fault",
        }
    }
    snapshot.outbox.extend((unknown, refused, delayed))
    effects = {
        row["effect_id"]: row for row in inbox_document(snapshot)["items"] if "effect_id" in row
    }
    assert effects[unknown.id]["kind"] == "unknown_effect"
    assert effects[unknown.id]["retry_at"] is None
    assert "Reconcile the existing effect" in effects[unknown.id]["next_automatic_action"]
    assert effects[refused.id]["operator_action_required"]
    assert effects[delayed.id]["retry_at"] == 200
    assert effects[delayed.id]["github_fault"]["certainty"] == "unknown"
    assert effects[delayed.id]["github_fault"]["request_id"] == "request-17"
    assert "private-body" not in json.dumps(effects) and "private-fault" not in json.dumps(effects)


def test_inbox_preserves_safe_route_wait_reasons_and_existing_deadlines(runtime):
    _, snapshot, _ = runtime
    unit = snapshot.units["s:e:u"]
    unit.hold = Hold("handoff", "Waiting for a route", id="hold")
    unit.retry_at = 125
    unit.evidence["handoff"] = {
        "id": "handoff-1",
        "route_wait": "Quota unavailable; refresh capacity",
    }
    snapshot.bindings["capacity"] = {
        "observed_at": 100,
        "observations": [
            {
                "profile": "codex",
                "model": "model",
                "capacity": {
                    "available": False,
                    "known": False,
                    "reason": "Provider quota is unavailable",
                },
            },
            {
                "profile": "claude",
                "model": "healthy",
                "capacity": {
                    "available": True,
                    "known": True,
                    "model_access": True,
                    "slots": 2,
                    "observed_at": 100,
                    "expires_at": 220,
                },
            },
        ],
    }
    snapshot.jobs["waiting-job"] = {
        "id": "waiting-job",
        "state": "capacity_wait",
        "reason": "Account limit reached",
        "retry_at": 150,
        "request": {"unit": unit.id},
    }
    items = inbox_document(snapshot, now=110)["items"]
    handoff = next(row for row in items if row["kind"] == "route_wait")
    assert handoff["handoff_id"] == "handoff-1" and handoff["retry_at"] == 125
    route = next(row for row in items if row["kind"] == "provider_wait")
    assert route["profile"] == "codex" and "Provider quota is unavailable" in route["reason"]
    assert not route["capacity_known"]
    assert len([row for row in items if row["kind"] == "provider_wait"]) == 1
    job = next(row for row in items if row["kind"] == "job_wait")
    assert job["job_id"] == "waiting-job" and job["retry_at"] == 150
    expired = inbox_document(snapshot, now=230)["items"]
    assert any(row.get("model") == "healthy" and "expired" in row["reason"] for row in expired)


def test_inbox_pagination_is_bounded_complete_and_unit_filtered(runtime):
    _, snapshot, _ = runtime
    unit = snapshot.units["s:e:u"]
    unit.hold = Hold("operator", "Retain this hold", id="operator-hold")
    snapshot.outbox.extend(
        OutboxAction(f"effect-{n:03}", "github", unit.id, {}, 1, state="sent_unknown")
        for n in range(205)
    )
    snapshot.outbox.append(OutboxAction("global-effect", "github", "", {}, 1, state="sent_unknown"))
    seen = []
    after = ""
    while True:
        page = inbox_document(snapshot, unit=unit.id, after=after, limit=37)
        assert len(page["items"]) <= 37 and page["total"] == 206
        assert all(row["unit_id"] == unit.id for row in page["items"])
        seen.extend(row["id"] for row in page["items"])
        if not page["has_more"]:
            assert page["next_after"] is None
            break
        assert page["next_after"] > after
        after = page["next_after"]
    assert len(seen) == len(set(seen)) == 206
    assert seen == sorted(seen)


@pytest.mark.parametrize(
    "arguments",
    [["--limit", "0"], ["--limit", "201"], ["--unit", "missing"]],
    ids=["zero_limit", "excess_limit", "missing_unit"],
)
def test_inbox_invalid_filter_is_one_json_failure_without_mutation(runtime, capsys, arguments):
    store, snapshot, calls = runtime
    result = machine_json(["campaign", "inbox", snapshot.id, *arguments, "--json"], capsys, rc=1)
    assert not result["ok"] and result["problems"]
    assert store.load(snapshot.id).revision == snapshot.revision and calls == []


def test_inbox_remains_readable_for_retired_campaign_and_has_human_output(
    runtime, capsys, monkeypatch
):
    store, snapshot, calls = runtime
    snapshot.units["s:e:u"].hold = Hold("operator", "Preserved work", id="hold")
    snapshot.phase = "stopped"
    snapshot = store.save(snapshot, snapshot.revision)
    # Read a historical retired snapshot; this observer must not perform a transfer.
    snapshot.bindings["ownership_retirement"] = {"successor": "successor-campaign"}
    monkeypatch.setattr(store, "load", lambda identity: snapshot)
    result = machine_json(["campaign", "inbox", snapshot.id, "--json"], capsys)
    assert "retired" in result["inbox"]["items"][0]["next_automatic_action"]
    assert cli.main(["campaign", "inbox", snapshot.id]) == 0
    output = capsys.readouterr()
    assert "s:e:u" in output.out and "Preserved work" in output.out and "Operator:" in output.out
    assert "hold_id=hold" in output.out and "Pause evidence: unknown" in output.out
    assert "campaign release" not in output.out
    assert output.err == "" and calls == []


def test_status_reports_persisted_fault_wait_without_mutation_payload(runtime, capsys):
    from bmad_loop.campaign_store import add_action

    store, snapshot, calls = runtime
    action = add_action(snapshot, "github", "s:e:u", {"token": "private-mutation-token"})
    action.state, action.error, action.next_retry_at = "sent_unknown", "rate limit", 900
    action.receipt = {
        "github_fault": {
            "schema_version": 1,
            "category": "rate_limit",
            "certainty": "unknown",
            "status": 429,
            "retry_at": 900,
            "request_id": "request-7",
            "payload": "private-receipt",
        }
    }
    store.save(snapshot, snapshot.revision)
    assert cli.main(["campaign", "status", snapshot.id, "--json"]) == 0
    raw = capsys.readouterr().out
    wait = json.loads(raw)["campaign"]["waiting_effects"]
    assert wait["total"] == 1 and not wait["truncated"]
    assert wait["items"][0]["next_retry_at"] == 900
    assert wait["items"][0]["github_fault"]["category"] == "rate_limit"
    assert wait["items"][0]["github_fault"]["request_id"] == "request-7"
    assert "private-mutation-token" not in raw and "private-receipt" not in raw
    assert calls == []


@pytest.mark.parametrize("pending, attempts", [(True, 0), (False, 2)])
def test_status_exposes_current_final_repair_without_private_findings(
    runtime, capsys, pending, attempts
):
    store, snapshot, calls = runtime
    unit = snapshot.units["s:e:u"]
    unit.evidence["handoff"] = {
        "id": "handoff-current",
        "phase": "audit" if pending else "failed",
        "predecessor": {"launch_id": "current-launch"},
    }
    unit.evidence["handoff_final_repairs"] = {
        "older-launch": [{"private": "private-old-finding"}],
        "current-launch": [{"private": "private-repair-finding"}] * attempts,
    }
    if pending:
        unit.evidence["handoff"]["final_repair_pending"] = {
            "findings": [{"audit_job": "audit-rejected", "artifact_path": "private-findings"}]
        }
    snapshot = store.save(snapshot, snapshot.revision)
    assert cli.main(["campaign", "status", snapshot.id, "--json"]) == 0
    raw = capsys.readouterr().out
    repair = json.loads(raw)["campaign"]["units"][0]["handoff"]["final_repair"]
    assert repair == {
        "attempts": attempts,
        "max_attempts": 2,
        "pending": pending,
        "audit_jobs": ["audit-rejected"] if pending else [],
    }
    assert "private" not in raw and "older-launch" not in raw
    assert calls == [] and store.load(snapshot.id).revision == snapshot.revision


def test_history_reads_saved_events_without_starting_runtime(runtime, capsys):
    from bmad_loop.campaign_store import add_event

    store, snapshot, calls = runtime
    add_event(snapshot, "review-observed", unit="s:e:u", role="review", secret="private payload")
    snapshot = store.save(snapshot, snapshot.revision)
    assert (
        cli.main(
            [
                "campaign",
                "history",
                snapshot.id,
                "--unit",
                "s:e:u",
                "--role",
                "review",
                "--limit",
                "10",
                "--json",
            ]
        )
        == 0
    )
    output = capsys.readouterr().out
    result = json.loads(output)
    assert result["events"][-1]["kind"] == "review-observed"
    assert result["next_after"] > 0
    assert "private payload" not in output
    assert calls == []


def test_history_reads_archived_action_receipts_without_starting_runtime(runtime, capsys):
    from bmad_loop.campaign_store import add_action

    store, snapshot, calls = runtime
    for identity in ("old", "new"):
        row = add_action(snapshot, "github", "s:e:u", {}, action_id=identity)
        row.state = "confirmed"
        snapshot = store.save(snapshot, snapshot.revision)
    assert (
        cli.main(["campaign", "history", snapshot.id, "--actions", "--unit", "s:e:u", "--json"])
        == 0
    )
    result = json.loads(capsys.readouterr().out)
    assert result["scope"] == "archived-actions"
    assert [row["action"]["id"] for row in result["actions"]] == ["old"]
    assert result["next_after"] == 1
    assert calls == []


def test_normal_cli_runtime_binds_transcript_provider(runtime):
    from bmad_loop import campaign_runtime
    from bmad_loop.campaign_cli import _runtime
    from bmad_loop.campaign_history_sources import TranscriptEvidenceProvider

    store, _, _ = runtime
    driver = _runtime(campaign_runtime, store.root, store)
    assert isinstance(driver.evidence_providers["transcript"], TranscriptEvidenceProvider)


def test_jobs_reads_persisted_request_without_exposing_prompt(runtime, capsys):
    import dataclasses

    from bmad_loop.campaign_jobs import JobLimits, JobRequest, prepare

    store, snapshot, calls = runtime
    snapshot.spec = dataclasses.replace(
        snapshot.spec, reasoning=dataclasses.replace(snapshot.spec.reasoning, enabled=True)
    )
    snapshot.config_revision += 1
    prepare(
        snapshot,
        JobRequest(
            "job-one",
            "report",
            "reporting",
            {
                "head": "h",
                "base": "b",
                "graph_revision": 1,
                "config_revision": snapshot.config_revision,
            },
            (),
            "private owner conversation",
            {"type": "object", "properties": {}},
            JobLimits(1000, 100),
            unit="s:e:u",
        ),
    )
    store.save(snapshot, snapshot.revision)
    assert (
        cli.main(["campaign", "jobs", snapshot.id, "--job", "job-one", "--details", "--json"]) == 0
    )
    output = capsys.readouterr().out
    result = json.loads(output)
    assert result["jobs"][0]["id"] == "job-one"
    assert result["jobs"][0]["purpose"] == "report"
    assert "private owner conversation" not in output
    assert calls == []


def test_handoff_only_queues_and_preserves_state(runtime, capsys):
    store, snapshot, calls = runtime
    assert (
        cli.main(
            [
                "campaign",
                "handoff",
                snapshot.id,
                "--unit",
                "s:e:u",
                "--profile",
                "codex",
                "--model",
                "approved-model",
                "--reason",
                "quota",
                "--json",
            ]
        )
        == 0
    )
    result = json.loads(capsys.readouterr().out)
    request = result["request"]
    assert request["kind"] == "handoff"
    assert request["payload"]["profile"] == "codex"
    assert request["payload"]["model"] == "approved-model"
    assert store.load(snapshot.id).revision == snapshot.revision
    assert calls == []


def test_history_retrieves_role_evicted_from_hot_state(runtime, capsys):
    from bmad_loop.campaign_store import add_event

    store, snapshot, calls = runtime
    path = store.directory(snapshot.id) / "reflection-history" / "unit" / "projection.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "stage": {
                    "windows": [
                        {
                            "phase": "planning",
                            "terminal_id": "old",
                            "task_id": "old-plan",
                            "state": "stopped",
                            "log_path": "/retained/planning.log",
                        }
                    ]
                }
            }
        )
    )
    add_event(snapshot, "reflection-observed", unit="s:e:u", history_path=str(path))
    store.save(snapshot, snapshot.revision)
    assert cli.main(["campaign", "history", snapshot.id, "--role", "planning", "--json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["sessions"][0]["terminal_id"] == "old"
    assert result["events"][0]["history_path"] == str(path)
    assert not result["history_gaps"]
    assert calls == []


@pytest.mark.parametrize(
    "command,flags",
    [
        ("stop", []),
        ("stop", ["--terminate-workers"]),
        ("hold", ["--unit", "s:e:u", "--reason", "operator takeover"]),
        ("release", ["--unit", "s:e:u"]),
    ],
)
def test_controls_queue_requests_without_editing_state(runtime, capsys, command, flags):
    store, snapshot, calls = runtime
    assert cli.main(["campaign", command, snapshot.id, *flags, "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    request = doc["request"]
    assert request["kind"] == command
    assert request["payload"]["terminate_workers"] == ("--terminate-workers" in flags)
    assert store.commands(snapshot.id) == [request]
    assert store.load(snapshot.id).revision == snapshot.revision
    assert calls == []


def test_unknown_unit_error_leaves_stdout_and_inbox_empty(runtime, capsys):
    store, snapshot, _ = runtime
    assert (
        cli.main(
            ["campaign", "hold", snapshot.id, "--unit", "missing", "--reason", "hold", "--json"]
        )
        == 1
    )
    output = capsys.readouterr()
    assert output.out == ""
    assert "unknown campaign unit" in output.err
    assert store.commands(snapshot.id) == []


def test_resume_and_reconcile_use_distinct_no_launch_entrypoints(runtime, capsys):
    _, snapshot, calls = runtime
    assert cli.main(["campaign", "reconcile", snapshot.id, "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["command"] == "campaign.reconcile"
    assert ("reconcile", snapshot.id) in calls
    assert ("run", snapshot.id) not in calls
    assert ("backup-resume", snapshot.id) not in calls
    assert cli.main(["campaign", "resume", snapshot.id, "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["command"] == "campaign.resume"
    assert ("run", snapshot.id) in calls
    assert calls.index(("backup-resume", snapshot.id)) < calls.index(("run", snapshot.id))


def test_attach_uses_existing_runtime_identity_and_role(runtime, capsys):
    _, snapshot, calls = runtime
    assert (
        cli.main(
            ["campaign", "attach", snapshot.id, "--unit", "s:e:u", "--role", "publication_review"]
        )
        == 0
    )
    assert ("attach", snapshot.id, "s:e:u", "publication_review") in calls
    assert capsys.readouterr().out == ""
    with pytest.raises(SystemExit) as error:
        cli.main(["campaign", "attach", snapshot.id, "--unit", "s:e:u", "--json"])
    assert error.value.code == 2


def test_project_option_works_before_or_after_subcommand(runtime, capsys, tmp_path):
    _, snapshot, calls = runtime
    for arguments in (
        ["--project", str(tmp_path), "resume", snapshot.id],
        ["resume", snapshot.id, "--project", str(tmp_path)],
    ):
        assert cli.main(["campaign", *arguments, "--json"]) == 0
        capsys.readouterr()
    project_calls = [call for call in calls if call[0] == "runtime"]
    assert all(call[1] == tmp_path for call in project_calls)


def test_operational_failure_never_emits_partial_json(runtime, capsys):
    assert cli.main(["campaign", "status", "missing", "--json"]) == 1
    output = capsys.readouterr()
    assert output.out == ""
    assert "cannot read campaign" in output.err


def test_projection_preserves_actual_hold_and_delivery_distinctions(runtime):
    _, snapshot, _ = runtime
    unit = snapshot.units["s:e:u"]
    unit.state = UnitState.MERGED_PENDING_SYNC
    unit.hold = Hold("tracking", "GitHub unavailable", id="hold")
    view = status_document(snapshot)
    assert view["counts"] == {"merged_pending_sync": 1}
    assert view["units"][0]["hold"]["kind"] == "tracking"
    assert view["units"][0]["state"] != "delivered"


def test_projection_shows_native_reset_beside_positive_provider_metadata(runtime):
    from bmad_loop.campaign_capacity import record_unavailable

    _, snapshot, _ = runtime
    observation = {
        "profile": "antigravity",
        "model": "flash",
        "capacity": {"available": True, "observed_at": 150},
    }
    snapshot.bindings["capacity"] = {"observations": [observation]}
    record_unavailable(
        snapshot, "antigravity", "flash", now=100, reason="native quota", quota_retry_at=200
    )
    snapshot.bindings["capacity_faults"]["antigravity\0flash"]["private_payload"] = "hidden"
    view = status_document(snapshot)["resources"]
    assert view["provider_routes"] == [observation]
    assert view["provider_faults"] == [
        {
            "profile": "antigravity",
            "model": "flash",
            "observed_at": 100,
            "reason": "<redacted:str>",
            "quota_retry_at": 200,
            "quota_source": "agy-native-individual-quota-v1",
        }
    ]


def test_projection_explains_discovery_wait_and_clears_it_when_complete(runtime, capsys):
    from bmad_loop.campaign_cli import _emit

    _, snapshot, _ = runtime
    view = status_document(snapshot)
    assert view["discovery"]["state"] == "pending"
    assert view["units"][0]["admission_wait"] == "repository discovery has not completed"
    snapshot.bindings["inventory"] = {
        "complete": False,
        "observed_at": 100,
        "unavailable": ["github:pagination unavailable"],
        "truncated": ["git:branch detail"],
    }
    view = status_document(snapshot)
    assert view["discovery"]["state"] == "incomplete"
    assert view["discovery"]["unavailable"] == ["github:pagination unavailable"]
    assert view["discovery"]["truncated"] == ["git:branch detail"]
    assert view["units"][0]["admission_wait"] == "repository discovery is incomplete"
    _emit(types.SimpleNamespace(json=False), {"ok": True, "campaign": view})
    output = capsys.readouterr().out
    assert "discovery: incomplete" in output
    assert "github:pagination unavailable" in output
    assert "repository discovery is incomplete" in output
    snapshot.bindings["inventory"] = {"complete": True, "observed_at": 200}
    view = status_document(snapshot)
    assert view["discovery"]["state"] == "ready"
    assert view["discovery"]["unavailable"] == []
    assert view["units"][0]["admission_wait"] == ""
