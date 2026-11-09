"""Incoming graph work cannot strand an already reviewed default-role candidate."""

import copy
import dataclasses
import json

import pytest
from test_campaign_intake_flow import ingest, observation
from test_campaign_native_review_flow import configured, receipt
from test_campaign_runtime import diagnose
from test_campaign_runtime import production_case as production_case
from test_campaign_runtime import run_case

from bmad_loop.campaign_intake_flow import next as next_intake
from bmad_loop.campaign_model import OutboxAction, UnitState
from bmad_loop.campaign_runtime_intake import create_control
from bmad_loop.campaign_supervisor import Observation


@pytest.mark.parametrize("revision", ["graph_revision", "config_revision"])
def test_default_review_refreshes_on_resume_but_issued_lineage_survives(
    project, tmp_path, revision
):
    runtime, snapshot, unit = configured(project, tmp_path)
    snapshot.spec = dataclasses.replace(snapshot.spec, roles=())
    old = receipt(snapshot, unit, stage="publication_review")
    unit.evidence["review"] = old
    unit.state = UnitState.READY_TO_PUBLISH
    setattr(snapshot, revision, getattr(snapshot, revision) + 1)
    action = OutboxAction("merge", "github", unit.id, {"purpose": "merge"}, 1, state="sent_unknown")
    snapshot.outbox.append(action)
    assert not runtime.prepare_resume(snapshot)
    assert unit.evidence["review"] == old
    assert not runtime._publication_evidence(snapshot, unit).review_approved
    assert unit.state == UnitState.READY_TO_PUBLISH
    action.state = "rejected"
    assert runtime.prepare_resume(snapshot)
    assert unit.state == UnitState.SYNCING
    assert "review" not in unit.evidence


@pytest.mark.parametrize("kind", ["prepare-ready", "prepare-merge", "github"])
def test_stale_gate_observation_retires_wait_and_returns_to_verification(project, tmp_path, kind):
    runtime, snapshot, unit = configured(project, tmp_path)
    snapshot.spec = dataclasses.replace(snapshot.spec, roles=())
    unit.evidence["review"] = receipt(snapshot, unit, stage="publication_review")
    unit.state = UnitState.READY_TO_PUBLISH
    snapshot.graph_revision += 1
    action = OutboxAction("stale", kind, unit.id, {}, snapshot.generation)
    runtime._apply_effect(
        snapshot,
        action,
        Observation(
            "confirmed",
            {
                "acceptance_stale": True,
                **({"canceled_before_write": True} if kind == "github" else {}),
            },
        ),
    )
    assert unit.state == UnitState.SYNCING
    assert "review" not in unit.evidence
    if kind != "github":
        assert unit.evidence["verification"]["outcome"] == "verified"


def test_actual_intake_after_review_reverifies_rereviews_and_merges(production_case, monkeypatch):
    case = production_case
    assert "native_review_pair" not in dict(case.snapshot.spec.roles)
    script = case.events.parent / "scripted-coding-cli.py"
    text = script.read_text(encoding="utf-8")
    text = text.replace(
        '"decomposition","planning_review"}', '"decomposition","planning_review","triage"}'
    )
    text = text.replace(
        '    if name in {"requirements","architecture","decomposition"}:',
        """    if name=="triage":
        contract=context["triage_result_contract"]
        target=next(row for row in context["existing_units"] if row["id"]=="source:project:first")
        artifact.update(intake_coordinates=contract["intake_coordinates"],proposal={"schema_version":1,"disposition":"duplicate","input_versions":contract["intake_coordinates"]["input_versions"],"unit_id":target["id"],"rationale":"Existing first story covers this unrelated intake request","acceptance_criteria":target["acceptance"],"dependencies":target["after"],"related_issue":900,"related_pr":None,"within_scope":True,"graph_revision":request["coordinates"]["graph_revision"]},units=[])
    if name in {"requirements","architecture","decomposition"}:""",
    )
    script.write_text(text, encoding="utf-8")
    original = case.runtime.control
    injected = {}

    def control(snapshot, command):
        original(snapshot, command)
        action = command.get("action", {})
        if command["kind"] != "prepare-effect" or action.get("kind") != "prepare-merge" or injected:
            return
        unit = snapshot.units[action["unit"]]
        # A sibling merge would independently move main and mask graph-only
        # starvation. Inject after every other candidate is fully delivered.
        if any(
            other.id != unit.id and other.state != UnitState.DELIVERED
            for other in snapshot.units.values()
        ):
            return
        assert unit.state == UnitState.READY_TO_PUBLISH
        injected.update(
            unit=unit.id,
            review=copy.deepcopy(unit.evidence["review"]),
            graph=snapshot.graph_revision,
        )
        incoming = observation(
            number=900, body="Confirm existing first behavior without new scope."
        )
        with case.service.lock:
            case.service.issues[900] = {
                **incoming.payload,
                "node_id": "owner-900",
                "title": "Unrelated intake",
                "state": "open",
                "labels": [],
                "assignees": [],
                "milestone": None,
                "html_url": incoming.url,
            }
            case.service.counter = max(case.service.counter, 900)
            case.service._save()
        ingest(snapshot, incoming)
        request = next_intake(snapshot)
        assert request is not None
        injected["control"] = create_control(snapshot, request.payload)
        assert snapshot.graph_revision > injected["graph"]

    monkeypatch.setattr(case.runtime, "control", control)
    result = run_case(case)
    assert injected
    assert result.phase == "delivered", diagnose(result)
    from bmad_loop.campaign_action_history import actions

    assert any(
        action.kind == "prepare-merge"
        and action.state == "confirmed"
        and action.receipt.get("acceptance_stale")
        for action in actions(result, unit=injected["unit"])
    ), "the obsolete merge wait must retire before the fresh review can publish"
    unit = result.units[injected["unit"]]
    assert unit.state == UnitState.DELIVERED
    assert case.service.pulls[unit.bindings["pr"]["number"]]["merged"]
    assert unit.evidence["review"]["launch_id"] != injected["review"]["launch_id"]
    assert unit.evidence["review"]["validated"]["coordinates"]["graph_revision"] > injected["graph"]
    assert (
        unit.evidence["verification"]["launch_id"]
        != injected["review"]["artifact"]["acceptance_evidence"][0]["verification_launch_id"]
    )
    reviews = [json.loads(line) for line in case.events.read_text(encoding="utf-8").splitlines()]
    assert (
        sum(row["event"] == "publication-review" and row["unit"] == unit.id for row in reviews) >= 2
    )
    assert result.units[injected["control"]].state == UnitState.CANCELED
    assert case.service.issues[900]["state"] == "open"
