"""Audited liveness gaps exercise production proposals, effects and receipts."""

from dataclasses import asdict, replace
from types import SimpleNamespace

import pytest
from test_campaign_core import make_snapshot
from test_campaign_publication import publishable as publishable
from test_campaign_resolution_flow import candidate, state

from bmad_loop.campaign_model import (
    CampaignSnapshot,
    CampaignSpec,
    CampaignUnit,
    OutboxAction,
    RoleSpec,
    UnitSpec,
    UnitState,
)
from bmad_loop.campaign_runtime import CampaignRuntime
from bmad_loop.campaign_store import CampaignStore


def runtime(tmp_path, snapshot):
    snapshot.bindings.update(
        inventory={"complete": True, "observed_at": 100}, intake={"observed_at": 100}
    )
    return CampaignRuntime(tmp_path, CampaignStore(tmp_path / "state"), clock=lambda: 100)


@pytest.mark.parametrize("epics", [["a", "a", "b", "b"], ["a", "a", "a", "a"]])
def test_workspace_reservations_skip_overlapping_siblings(tmp_path, epics):
    snapshot = make_snapshot(
        scopes=[("src/shared",), ("src/shared",), ("src/disjoint",), ("src/more",)], epics=epics
    )
    snapshot.spec = replace(snapshot.spec, max_in_flight_units=2)
    for unit in snapshot.units.values():
        unit.state = UnitState.PENDING
    driver = runtime(tmp_path, snapshot)
    proposals = [p for p in driver.proposals(snapshot) if p.kind == "prepare-unit"]
    assert [p.unit for p in proposals] == ["source:a:0", f"source:{epics[2]}:2"]
    # An already allocated writer still reserves scope on the next pass.
    first = snapshot.units[proposals[0].unit]
    first.state = UnitState.DRAFT
    first.bindings["workspace"] = "first"
    proposals = [p for p in driver.proposals(snapshot) if p.kind == "prepare-unit"]
    assert [p.unit for p in proposals] == [f"source:{epics[2]}:2"]


def test_launch_claim_persists_epic_fairness(tmp_path):
    snapshot = make_snapshot(workers=1)
    driver = runtime(tmp_path, snapshot)
    first = snapshot.units["source:a:0"]
    first.bindings.update(workspace=str(tmp_path), branch="test")
    action = OutboxAction("launch", "launch", first.id, {"kind": "verify"}, 0)
    snapshot.outbox.append(action)
    driver.control(snapshot, {"kind": "prepare-effect", "action": asdict(action)})
    assert snapshot.last_epic == "source::a"
    first.claim.quiescent = True
    from bmad_loop.campaign_scheduler import admit

    assert admit(snapshot).units[0] == "source:b:2"


@pytest.mark.parametrize("provider_timestamp", [None, 10])
def test_cached_capacity_expires_before_runtime_admission(tmp_path, provider_timestamp):
    snapshot = make_snapshot()
    snapshot.spec = replace(
        snapshot.spec, roles=(("planning", RoleSpec(profile="codex", model="gpt-astra")),)
    )
    snapshot.bindings["capacity"] = {
        "observed_at": 10,
        "observations": [
            {
                "profile": "codex",
                "model": "gpt-astra",
                "capacity": {
                    "available": True,
                    "slots": 8,
                    "model_access": True,
                    "observed_at": provider_timestamp,
                    "expires_at": 130 if provider_timestamp else None,
                },
            }
        ],
    }
    driver = runtime(tmp_path, snapshot)
    assert driver._role(snapshot, "planning") is not None
    driver.clock = lambda: 131
    assert driver._role(snapshot, "planning") is None
    snapshot.bindings["capacity"]["observed_at"] = 131
    row = snapshot.bindings["capacity"]["observations"][0]["capacity"]
    row.update(observed_at=131, expires_at=251)
    assert driver._role(snapshot, "planning") is not None


def test_observed_owner_reply_reopens_attempted_context(project):
    from bmad_loop.campaign_intake import IntakeBatch, IntakeObservation
    from bmad_loop.campaign_intake_flow import _digest, apply_batch
    from bmad_loop.campaign_resolution_flow import (
        apply_context,
        collect_context,
        prepare,
        proposals,
    )

    snapshot = state(project, hold="product_choice")
    unit = next(iter(snapshot.units.values()))
    unit.bindings["issue"] = {"number": 12}
    context = collect_context(project.project, snapshot, unit.id, "decision")
    apply_context(snapshot, unit.id, context)
    launch = proposals(snapshot, project.project, candidate(), now=10)[0]
    prepare(snapshot, unit.id, launch.payload)
    assert proposals(snapshot, project.project, candidate(), now=10) == ()
    observed = IntakeObservation(
        snapshot.repository,
        "99",
        "comment",
        12,
        "2026-01-01T00:00:00Z",
        _digest({"id": 99, "body": "Use the documented behavior"}),
        "owner",
        "https://github.com/org/repo/issues/12#issuecomment-99",
        {"id": 99, "body": "Use the documented behavior"},
    )
    batch = IntakeBatch((observed,), "2026-01-01T00:00:00Z", True, 0)
    apply_batch(snapshot, batch, observed_at=20)
    assert proposals(snapshot, project.project, candidate(), now=20)[0].kind == "resolution-context"
    context = collect_context(project.project, snapshot, unit.id, "decision")
    apply_context(snapshot, unit.id, context)
    fresh = proposals(snapshot, project.project, candidate(), now=20)[0]
    assert fresh.kind == "launch"
    prepare(snapshot, unit.id, fresh.payload)
    apply_batch(snapshot, batch, observed_at=30)
    assert proposals(snapshot, project.project, candidate(), now=30) == ()


@pytest.mark.parametrize("veto", ["check", "review", "label"])
def test_queue_wait_reobserves_and_reenters_full_acceptance(
    tmp_path, monkeypatch, publishable, veto
):
    from bmad_loop import campaign_runtime_queue as queue
    from bmad_loop.campaign_publication import PublicationController

    old, candidate, evidence, observed = publishable
    unit = CampaignUnit(UnitSpec("unit", "s", "e", "Unit"), state=UnitState.MERGE_REQUESTED)
    unit.bindings.update(head=candidate.head, pr={"number": 1}, workspace=str(tmp_path))
    unit.evidence.update(
        integrated={"head": candidate.head, "base": candidate.base}, queue={"gate_reasons": [veto]}
    )
    snapshot = CampaignSnapshot("wait", "a/b", CampaignSpec("wait"), {unit.id: unit})
    driver = runtime(tmp_path, snapshot)
    driver._gateway = old.gateway
    monkeypatch.setattr(driver, "_candidate", lambda *args: candidate)
    queue.apply_effect(
        driver,
        snapshot,
        unit,
        OutboxAction("dequeue", "queue-observe", unit.id, {}, 0),
        {"dequeued": True, "candidate": asdict(candidate)},
    )
    assert unit.hold.kind == "remote_wait"
    waiting = (
        replace(observed, checks=())
        if veto == "check"
        else (
            replace(observed, requested_reviewers=("reviewer",))
            if veto == "review"
            else replace(observed, issue={"labels": [{"name": "bmad:hold"}]})
        )
    )
    current = [waiting]
    monkeypatch.setattr(PublicationController, "inspect", lambda *args: current[0])
    proposal = next(p for p in driver.proposals(snapshot) if p.kind == "remote-wait")
    action = OutboxAction("wait", proposal.kind, unit.id, proposal.payload, 0)
    result = driver._perform_remote_wait(snapshot, action)
    driver.apply(snapshot, action, result)
    assert unit.hold and not result.data["ready_to_reaccept"]
    current[0] = observed
    result = driver._perform_remote_wait(snapshot, action)
    driver.apply(snapshot, action, result)
    assert unit.hold is None
    assert unit.state == UnitState.SYNCING
    assert "verification" not in unit.evidence
    assert any(
        p.kind == "launch" and p.payload["kind"] == "integrate" for p in driver.proposals(snapshot)
    )


@pytest.mark.parametrize("models,hook", [(("gpt-astra", "claude-helper"), ""), ((), "gpt-astra")])
def test_stage_keeps_primary_attribution_with_auxiliary_models(
    project, tmp_path, monkeypatch, models, hook
):
    from test_campaign_workers import envelope, worker_request

    from bmad_loop import campaign_workers as workers
    from bmad_loop.adapters.base import SessionResult
    from bmad_loop.model import TokenUsage

    context = envelope(project.project, tmp_path / "execution")
    adapter = SimpleNamespace(journal=None)

    def run(spec):
        if hook:
            adapter.journal.append("session-model", task_id="foreign", model="wrong")
            adapter.journal.append("session-model", task_id=spec.task_id, model=hook)
        return SessionResult("completed", result_json={"status": "done"}, session_id="native")

    adapter.run = run
    adapter.read_usage = lambda _: TokenUsage(models=models)
    monkeypatch.setattr(workers, "_adapters", lambda *args, **kwargs: {"triage": adapter})
    receipt = workers._stage(
        worker_request(
            project.project,
            context,
            kind="stage",
            role="planning_review",
            candidate={"profile": "codex", "model": "gpt-astra", "provider_family": "openai"},
            profile_key="triage",
            prompt="Review",
            read_only=True,
        ),
        context,
    )
    assert receipt["outcome"] == "stage_complete"
    assert receipt["observed_model"] == "gpt-astra"
    assert receipt["observed_models"] == list(models)
    assert receipt["observed_model_source"] == (
        "native_session_hook" if hook else "transcript_primary"
    )


def test_custom_capacity_receipt_cannot_refresh_its_stale_source(tmp_path, monkeypatch):
    import json

    from bmad_loop import campaign_capacity
    from bmad_loop.campaign_model import RoleCandidate

    route = RoleCandidate("codex", "gpt-astra", capacity_command=("observer",))
    payload = {
        "schema_version": 1,
        "model": route.model,
        "quota_known": True,
        "available": True,
        "model_access": True,
        "slots": 2,
        "observed_at": 1,
        "expires_at": 121,
    }
    monkeypatch.setattr(
        campaign_capacity,
        "run_argv",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0, timed_out=False, interrupted=False, stdout=json.dumps(payload)
        ),
    )
    observed = campaign_capacity.observe(route, tmp_path, now=122)
    assert not observed.known
    assert "expired" in observed.reason
    payload.update(observed_at=122, expires_at=242)
    observed = campaign_capacity.observe(route, tmp_path, now=122)
    assert observed.available
    assert observed.expires_at == 242


def test_decision_refresh_retrieves_new_documents_without_repeating_identical_review(project):
    from bmad_loop.campaign_resolution_flow import (
        apply_context,
        collect_context,
        prepare,
        proposals,
    )

    snapshot = state(project, hold="product_choice")
    unit = next(iter(snapshot.units.values()))
    context = collect_context(project.project, snapshot, unit.id, "decision")
    context["observed_at"] = 10
    apply_context(snapshot, unit.id, context)
    launch = proposals(snapshot, project.project, candidate(), now=10)[0]
    prepare(snapshot, unit.id, launch.payload)
    assert proposals(snapshot, project.project, candidate(), now=69) == ()
    assert proposals(snapshot, project.project, candidate(), now=70)[0].kind == "resolution-context"
    context["observed_at"] = 70
    apply_context(snapshot, unit.id, context)
    assert proposals(snapshot, project.project, candidate(), now=70) == ()


def test_resume_upgrades_only_authenticated_legacy_queue_waits(tmp_path):
    from bmad_loop.campaign_model import Hold

    snapshot = make_snapshot()
    unit = next(iter(snapshot.units.values()))
    unit.hold = Hold(
        "approval",
        "pending checks",
        id="old",
        actor="campaign",
        evidence={"dequeue_confirmed": True, "queue": {"candidate": {"head": "old-head"}}},
    )
    driver = runtime(tmp_path, snapshot)
    assert driver.prepare_resume(snapshot)
    assert unit.hold.kind == "remote_wait"
    assert unit.hold.evidence["candidate"]["head"] == "old-head"
    unit.hold = Hold("approval", "owner authorization", id="explicit", actor="owner")
    assert not driver.prepare_resume(snapshot)
    assert unit.hold.kind == "approval"
