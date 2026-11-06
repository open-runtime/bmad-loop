"""Runtime intake reserves native actors and gates work on reviewed plan delivery."""

from __future__ import annotations

import copy
import dataclasses
import hashlib
import json
from pathlib import Path

import pytest
from test_campaign_runtime import diagnose
from test_campaign_runtime import production_case as production_case
from test_campaign_runtime import run_case

from bmad_loop import campaign_intake_flow as flow
from bmad_loop import campaign_runtime_intake as intake
from bmad_loop.campaign_github import ApiResponse, GithubGateway, managed_marker
from bmad_loop.campaign_intake import IntakeBatch, IntakeObservation
from bmad_loop.campaign_model import (
    CampaignError,
    CampaignSnapshot,
    CampaignSpec,
    CampaignUnit,
    IntakeSpec,
    OutboxAction,
    RoleCandidate,
    RoleSpec,
    SourceSelector,
    UnitSpec,
    UnitState,
    WorkerClaim,
)
from bmad_loop.campaign_store import CampaignStore

ROLE = RoleCandidate("codex", "gpt-6-astra", "high", "openai")


def observed(number=7, *, body="Add scoped incoming behavior", kind="issue"):
    payload = {
        "id": 1000 + number,
        "number": number,
        "title": "New incoming work",
        "body": body,
        "updated_at": "2026-11-05T10:00:00Z",
        "state": "open",
        "labels": [],
        "user": {"id": 5, "login": "owner"},
    }
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return IntakeObservation(
        "a/b",
        str(payload["id"]),
        kind,
        number,
        payload["updated_at"],
        digest,
        "owner",
        f"https://github.com/a/b/issues/{number}",
        payload,
    )


@pytest.fixture
def snapshot():
    unit = UnitSpec(
        "source:project:existing",
        "source",
        "epic",
        "Existing",
        scopes=("src/existing.txt",),
        acceptance=("Keep existing",),
    )
    spec = CampaignSpec(
        "intake",
        sources=(SourceSelector("stories", path="specs", id="source"),),
        intake=IntakeSpec(
            issues="all-open-and-new",
            allow_new_units=True,
            max_new_units=2,
            allowed_scopes=("src",),
        ),
        roles=(("triage", RoleSpec(profile=ROLE.profile, model=ROLE.model, effort=ROLE.effort)),),
    )
    state = CampaignSnapshot(
        "runtime-intake", "a/b", spec, {unit.id: CampaignUnit(unit, UnitState.READY)}
    )
    state.bindings["inventory"] = {"complete": True, "issues": [], "pulls": []}
    observation = observed()
    intake.apply_poll(
        state,
        {
            "batch": dataclasses.asdict(
                IntakeBatch((observation,), observation.updated_at, True, 0)
            ),
            "observed_at": 100,
        },
    )
    return state


def start(snapshot):
    proposal = intake.proposals(snapshot, ROLE)[0]
    unit_id = intake.create_control(snapshot, proposal.payload)
    unit = snapshot.units[unit_id]
    unit.state = UnitState.READY
    unit.bindings.update(
        branch="bmad/intake",
        head="head",
        pr={"number": 20, "url": "https://github.com/a/b/pull/20"},
        issue={"number": 19, "database_id": 2019, "url": "https://github.com/a/b/issues/19"},
    )
    stage = intake.stage_proposal(snapshot, unit_id, ROLE)
    assert stage.payload["result_contract"] == intake.CONTRACT
    intake.prepare_stage(snapshot, unit_id, stage.payload, launch_id="triage-launch")
    unit.state = UnitState.RUNNING
    unit.claim = WorkerClaim(unit_id, 1, 0, "triage-launch", quiescent=True)
    return unit_id


def result(snapshot, unit_id, *, disposition="new_story"):
    context = intake.stage_context(snapshot, unit_id)
    raw = copy.deepcopy(context["triage_result_contract"])
    target_id = (
        "source:project:incoming" if disposition == "new_story" else "source:project:existing"
    )
    raw["proposal"].update(
        disposition=disposition,
        unit_id=target_id,
        rationale="Exact evidence adds bounded behavior within src",
        acceptance_criteria=(
            ["Accept incoming"] if disposition == "new_story" else ["Keep existing"]
        ),
        related_issue=7,
        within_scope=True,
    )
    raw["units"] = (
        [
            dataclasses.asdict(
                UnitSpec(
                    target_id,
                    "source",
                    "epic",
                    "Incoming",
                    selector="incoming",
                    scopes=("src/incoming.txt",),
                    acceptance=("Accept incoming",),
                )
            )
        ]
        if disposition == "new_story"
        else []
    )
    return {"artifact": raw, "observed_model": ROLE.model, "provider_family": ROLE.provider_family}


def test_bootstrap_is_durable_idempotent_and_triage_reserves_exact_actor(snapshot, tmp_path):
    unit_id = start(snapshot)
    assert len(snapshot.units) == 2
    assert snapshot.graph_revision == 2
    assert intake.owns_stage(snapshot.units[unit_id])
    assert intake.proposals(snapshot, ROLE) == ()
    binding = intake.control(snapshot.units[unit_id])
    versions = binding["input_versions"]
    assert all(
        snapshot.bindings[flow.INBOX]["records"][version]["state"] == "triaging"
        for version in versions
    )
    store = CampaignStore(tmp_path / "state")
    store.create(snapshot)
    resumed = store.load(snapshot.id)
    assert intake.control(resumed.units[unit_id])["attempt_id"] == "triage-launch"
    assert (
        intake.create_control(
            resumed, {"input_versions": versions, "packet_id": binding["packet_id"]}
        )
        == unit_id
    )
    assert len(resumed.units) == 2


def test_new_intent_is_not_executable_until_same_planning_pr_delivers_and_activates(snapshot):
    unit_id = start(snapshot)
    receipt = result(snapshot, unit_id)
    proposal_id = intake.apply_triage(snapshot, unit_id, receipt, launch_id="triage-launch")
    control = snapshot.units[unit_id]
    assert not intake.owns_stage(control)
    assert "source:project:incoming" not in snapshot.units
    proposed = snapshot.bindings[flow.INBOX]["proposals"][proposal_id]
    intake.validate_decomposition(snapshot, unit_id, {"units": proposed["units"]})
    bad = copy.deepcopy(proposed["units"])
    bad[0]["scopes"] = ["outside/owner-scope"]
    with pytest.raises(CampaignError, match="bounded accepted intake graph"):
        intake.validate_decomposition(snapshot, unit_id, {"units": bad})
    with pytest.raises(CampaignError, match="not delivered"):
        intake.complete_delivery(snapshot, unit_id)
    control.state = UnitState.DELIVERED
    control.evidence["planning_flow"] = {"state": "activated"}
    spec = UnitSpec(**proposed["units"][0])
    # Actual planning-flow activation owns adding units and graph revision.
    snapshot.units[spec.id] = CampaignUnit(spec)
    revision = snapshot.graph_revision
    intake.complete_delivery(snapshot, unit_id)
    assert snapshot.graph_revision == revision
    assert (
        snapshot.bindings["source_observations"][spec.id]["tracker_id"]
        == "https://github.com/a/b/issues/7"
    )
    assert proposed["state"] == "activated"
    assert intake.settled(snapshot)
    intake.complete_delivery(snapshot, unit_id)
    assert snapshot.graph_revision == revision


def test_receipt_validation_cannot_release_real_actor_or_mutate_authority(snapshot):
    unit_id = start(snapshot)
    snapshot.units[unit_id].claim.quiescent = False
    before = snapshot.clone()
    intake.validate_receipt(snapshot, unit_id, result(snapshot, unit_id), launch_id="triage-launch")
    assert snapshot == before
    with pytest.raises(CampaignError, match="quiescent owned actor"):
        intake.apply_triage(snapshot, unit_id, result(snapshot, unit_id), launch_id="triage-launch")


def test_nonwork_control_needs_disposition_comment_and_closed_unmerged_pr(snapshot):
    unit_id = start(snapshot)
    intake.apply_triage(
        snapshot,
        unit_id,
        result(snapshot, unit_id, disposition="duplicate"),
        launch_id="triage-launch",
    )
    unit = snapshot.units[unit_id]
    assert intake.proposals(snapshot, ROLE)[0].kind == "intake-note"

    class Transport:
        def request(self, method, path, payload=None):
            if path.endswith("/issues/19"):
                return ApiResponse(
                    200,
                    {
                        "id": 2019,
                        "number": 19,
                        "state": "open",
                        "updated_at": "t1",
                        "body": managed_marker(snapshot.id, "story:" + unit_id, "issue"),
                    },
                    {},
                )
            return ApiResponse(
                200,
                {
                    "id": 2020,
                    "node_id": "pr",
                    "number": 20,
                    "state": "open",
                    "draft": True,
                    "merged": False,
                    "head": {"sha": "head", "ref": "bmad/intake"},
                    "updated_at": "t1",
                    "body": managed_marker(snapshot.id, unit_id, "pr"),
                },
                {},
            )

    gateway = GithubGateway(Transport(), "a/b")
    with pytest.raises(CampaignError, match="validated nonwork"):
        intake.cancellation_intent(snapshot, unit_id, gateway, action_id="cancel")
    note = intake.disposition_intent(snapshot, unit_id, gateway, action_id="note")
    assert note.path.endswith("/issues/7/comments")
    assert "https://github.com/a/b/pull/20" in note.payload["body"]
    intake.apply_note(snapshot, unit_id, {"state": "confirmed", "receipt": {"id": 1}})
    assert intake.proposals(snapshot, ROLE)[0].kind == "intake-cancel-issue"
    snapshot.outbox.append(
        OutboxAction("pending-github", "github", unit_id, {}, 0, state="sent_unknown")
    )
    assert intake.proposals(snapshot, ROLE) == ()
    snapshot.outbox.pop()
    issue_intent = intake.issue_cancellation_intent(
        snapshot, unit_id, gateway, action_id="cancel-issue"
    )
    assert issue_intent.payload == {"state": "closed", "state_reason": "not_planned"}
    confirmed_issue = dataclasses.asdict(issue_intent)
    confirmed_issue.update(
        state="confirmed", receipt={"id": 2019, "state": "closed", "state_reason": "not_planned"}
    )
    intake.apply_issue_cancellation(snapshot, unit_id, confirmed_issue)
    assert intake.proposals(snapshot, ROLE)[0].kind == "intake-cancel"
    cancellation = intake.cancellation_intent(snapshot, unit_id, gateway, action_id="cancel")
    assert cancellation.payload == {"state": "closed"}
    assert not intake.optional_canceled(unit)
    confirmed = dataclasses.asdict(cancellation)
    confirmed.update(
        state="confirmed",
        receipt={
            "id": 2020,
            "number": 20,
            "state": "closed",
            "merged": False,
            "head": {"sha": "head"},
        },
    )
    intake.apply_cancellation(snapshot, unit_id, confirmed)
    assert unit.state == UnitState.CANCELED
    assert intake.optional_canceled(unit)
    before = snapshot.clone()
    intake.apply_cancellation(snapshot, unit_id, confirmed)
    assert snapshot == before
    assert intake.settled(snapshot)
    assert snapshot.units["source:project:existing"].state == UnitState.READY


def test_ordinary_cancellation_never_becomes_optional_delivery(snapshot):
    unit = snapshot.units["source:project:existing"]
    unit.state = UnitState.CANCELED
    assert not intake.optional_canceled(unit)
    unit.bindings["intake_control"] = {
        "schema_version": 1,
        "state": "canceled",
        "optional": True,
        "cancellation": {"confirmed": False},
    }
    assert not intake.optional_canceled(unit)
    assert not intake.settled(snapshot)


def test_amendment_preserves_live_contract_and_waits_actor_and_publishers(snapshot):
    target = snapshot.units["source:project:existing"]
    target.bindings["pr"] = {"number": 21}
    target.state = UnitState.RUNNING
    target.claim = WorkerClaim(target.id, 1, 0, "old-worker", quiescent=False)
    unit_id = start(snapshot)
    receipt = result(snapshot, unit_id, disposition="attach")
    receipt["artifact"]["proposal"]["acceptance_criteria"] = [
        "Keep existing and add scoped acceptance"
    ]
    original = target.spec
    proposal = intake.apply_triage(snapshot, unit_id, receipt, launch_id="triage-launch")
    plan = snapshot.units[unit_id]
    assert target.spec == original and target.hold.kind == "intake_plan_change"
    assert not intake.ready_for_planning(snapshot, plan)
    target.claim.quiescent = True
    snapshot.outbox.append(
        OutboxAction("checkpoint", "checkpoint", target.id, {}, 0, state="sent_unknown")
    )
    assert not intake.ready_for_planning(snapshot, plan)
    with pytest.raises(CampaignError, match="publishers to drain"):
        intake.validate_activation(snapshot, unit_id)
    snapshot.outbox[-1].state = "confirmed"
    assert intake.ready_for_planning(snapshot, plan)
    intake.validate_activation(snapshot, unit_id)
    assert (
        snapshot.bindings[flow.INBOX]["proposals"][proposal]["preimages"][target.id]["acceptance"]
        == original.acceptance
    )


def test_delivered_acceptance_cannot_be_changed_by_attach(snapshot):
    target = snapshot.units["source:project:existing"]
    target.bindings["pr"] = {"number": 21}
    target.state = UnitState.DELIVERED
    unit_id = start(snapshot)
    receipt = result(snapshot, unit_id, disposition="attach")
    receipt["artifact"]["proposal"]["acceptance_criteria"] = [
        "New requirement must be a follow-up story"
    ]
    before = snapshot.clone()
    with pytest.raises(CampaignError, match="follow-up story"):
        intake.apply_triage(snapshot, unit_id, receipt, launch_id="triage-launch")
    assert snapshot == before


def test_invalid_triage_retries_same_draft_with_lifetime_budget(snapshot):
    unit_id = start(snapshot)
    unit = snapshot.units[unit_id]
    intake.fail_stage(
        snapshot,
        unit_id,
        {"outcome": "invalid_stage_result", "error": "Use a fresh follow-up ID"},
        launch_id="triage-launch",
    )
    assert unit.state == UnitState.READY and unit.bindings["pr"]["number"] == 20
    assert intake.control(unit)["failures"] == ["Use a fresh follow-up ID"]
    proposal = intake.stage_proposal(snapshot, unit_id, ROLE)
    intake.prepare_stage(snapshot, unit_id, proposal.payload, launch_id="retry")
    unit.state = UnitState.RUNNING
    unit.claim = WorkerClaim(unit_id, 2, 0, "retry", quiescent=True)
    intake.fail_stage(snapshot, unit_id, {"error": "Still invalid"}, launch_id="retry")
    assert unit.hold.kind == "triage_attempts_exhausted"
    assert intake.stage_proposal(snapshot, unit_id, ROLE) is None
    assert len(snapshot.units) == 2
    assert not intake.settled(snapshot)


def test_owner_edits_to_bot_comments_are_preserved_as_new_input(snapshot):
    original = observed(kind="comment", body="Managed content <!-- marker -->")
    intent = {
        "kind": "comment",
        "state": "confirmed",
        "payload": {"body": original.payload["body"]},
        "receipt": {"id": int(original.object_id), "user": {"id": 5}},
    }
    snapshot.outbox.append(
        OutboxAction("comment", "github", "", {}, 0, state="confirmed", receipt={"intent": intent})
    )
    assert intake._owned_projection(snapshot, original.to_dict())
    edited = observed(kind="comment", body="Owner updated acceptance <!-- marker -->")
    assert not intake._owned_projection(snapshot, edited.to_dict())
    quotation = edited.to_dict()
    quotation["object_id"] = "another-comment"
    assert not intake._owned_projection(snapshot, quotation)


def test_prior_campaign_native_projection_is_deduped_but_owner_edits_remain_input(
    snapshot, monkeypatch
):
    previous = snapshot.clone()
    previous.id = "prior-campaign"
    snapshot.common_dir = previous.common_dir = "/repository/.git"
    managed = observed(body="Managed epic source projection")
    raw = managed.payload
    intent = {
        "kind": "issue",
        "state": "confirmed",
        "payload": {"title": raw["title"], "body": raw["body"]},
        "receipt": {"id": int(managed.object_id), "labels": [], "assignees": []},
    }
    previous.outbox.append(
        OutboxAction("create", "github", "", {}, 0, state="confirmed", receipt={"intent": intent})
    )
    current = {"value": managed}
    monkeypatch.setattr(
        intake.GithubIntake,
        "poll",
        lambda *args, **kwargs: IntakeBatch((current["value"],), managed.updated_at, True, 0),
    )
    gateway = GithubGateway(type("Transport", (), {})(), "a/b")
    result = intake.collect(snapshot, gateway, now=102, prior_projections=(previous,))
    assert result["batch"]["observations"] == ()
    assert result["suppressed_versions"] == [managed.version_id]
    changed = {**raw, "body": raw["body"] + "\nOwner added new acceptance."}
    current["value"] = dataclasses.replace(managed, payload=changed, digest=intake._digest(changed))
    result = intake.collect(snapshot, gateway, now=103, prior_projections=(previous,))
    assert len(result["batch"]["observations"]) == 1
    assert result["suppressed_versions"] == []


def test_external_issue_closure_feedback_is_ignored_only_for_unchanged_content(snapshot):
    original = observed()
    own_close = {
        "kind": "close_issue",
        "state": "confirmed",
        "receipt": {"id": int(original.object_id)},
        "payload": {"state": "closed"},
    }
    snapshot.outbox.append(
        OutboxAction("close", "github", "", {}, 0, state="confirmed", receipt={"intent": own_close})
    )
    closure = original.to_dict()
    closure["payload"] = {**closure["payload"], "state": "closed"}
    assert intake._owned_projection(snapshot, closure)
    closure["payload"]["body"] = "Owner added a new requirement before closure"
    assert not intake._owned_projection(snapshot, closure)


# Reuse the conftest-backed actual Git/process fixture; only the scripted model's
# incoming-issue artifact and source-plan output differ in this scenario.
def test_intake_observes_pending_owned_write_before_lost_ack_without_retriage(snapshot):
    original = observed()
    intent = {
        "kind": "close_issue",
        "state": "prepared",
        "receipt": None,
        "coordinates": {"id": 1007, "source_confirmed": True},
        "payload": {"state": "closed", "state_reason": "completed"},
    }
    snapshot.outbox.append(
        OutboxAction("close", "github", "", {"intent": intent}, 0, state="sent_unknown")
    )
    closure = dataclasses.replace(original, payload={**original.payload, "state": "closed"})
    assert intake._owned_projection(snapshot, closure.to_dict())
    edited = dataclasses.replace(
        closure, payload={**closure.payload, "body": "New owner requirement"}
    )
    assert not intake._owned_projection(snapshot, edited.to_dict())


def test_poll_apply_rechecks_ack_arrived_after_frozen_poll_snapshot(snapshot):
    original = observed()
    closure = observed(body=original.payload["body"])
    payload = {**closure.payload, "state": "closed"}
    closure = dataclasses.replace(closure, payload=payload, digest=intake._digest(payload))
    intent = {
        "kind": "close_issue",
        "state": "confirmed",
        "receipt": {"id": 1007},
        "payload": {"state": "closed"},
    }
    snapshot.outbox.append(
        OutboxAction("close", "github", "", {}, 0, state="confirmed", receipt={"intent": intent})
    )
    intake.apply_poll(
        snapshot,
        {
            "batch": dataclasses.asdict(IntakeBatch((closure,), closure.updated_at, True, 0)),
            "observed_at": 101,
        },
    )
    assert closure.version_id not in snapshot.bindings[flow.INBOX]["records"]
    assert closure.version_id in snapshot.bindings["intake"]["seen_versions"]


@pytest.mark.parametrize("production_case", [{"intake": True}], indirect=True)
def test_github_issue_triage_planning_review_delivery_and_reused_issue_binding(production_case):
    case = production_case
    incoming = {
        "id": 1001,
        "node_id": "issue-1",
        "number": 1,
        "title": "Incoming story",
        "body": "Add the incoming scoped behavior and verify it.",
        "state": "open",
        "labels": [],
        "assignees": [],
        "milestone": None,
        "updated_at": "2026-11-05T10:00:00Z",
        "html_url": "https://github.com/a/b/issues/1",
        "user": {"id": 5, "login": "owner"},
    }
    case.service.issues[1] = incoming
    case.service.counter = 1
    case.service._save()
    script = case.events.parent / "scripted-coding-cli.py"
    text = script.read_text()
    text = text.replace(
        '"decomposition","planning_review"}', '"decomposition","planning_review","triage"}'
    )
    text = text.replace(
        """    if name in {"requirements","architecture","decomposition"}:""",
        """    if name=="triage":
        contract=context["triage_result_contract"]
        item=context["intake"]["observations"][0]
        new={"id":"source:project:incoming","source":"source","epic":"specs/epic","title":"Incoming","selector":"incoming","after":[],"scopes":["src/incoming.txt"],"kind":"story","acceptance":["Accept incoming"],"order":90,"parent":"","initiative":"","paths":[]}
        artifact.update(intake_coordinates=contract["intake_coordinates"],proposal={"schema_version":1,"disposition":"new_story","input_versions":contract["intake_coordinates"]["input_versions"],"unit_id":new["id"],"rationale":"Repository inventory shows no work covering incoming behavior","acceptance_criteria":new["acceptance"],"dependencies":[],"related_issue":item["number"],"related_pr":None,"within_scope":True,"graph_revision":request["coordinates"]["graph_revision"]},units=[new])
    if name in {"requirements","architecture","decomposition"}:""",
    )
    text = text.replace(
        """(workspace/"docs"/"brief.md").read_text()""",
        """(context["intake"]["observations"][0]["payload"]["body"] if context.get("intake") else (workspace/"docs"/"brief.md").read_text())""",
    )
    start = text.index(
        """            units=[row for row in context["existing_units"] if row["kind"]!="planning"]"""
    )
    end = text.index("""    if name=="planning_review":""", start)
    text = (
        text[:start]
        + """            units=[row for row in context["existing_units"] if row["kind"]!="planning"]
            added=context["proposed_intake_units"][0]
            units.append(added)
            spec=workspace/"specs"/"epic"/"stories"/"incoming-story.md"
            spec.write_text("---\\nstatus: ready-for-dev\\n---\\n# Incoming\\n\\nAccept incoming.\\n")
            manifest=workspace/"specs"/"epic"/"stories.yaml"
            manifest.write_text(manifest.read_text()+"- id: incoming\\n  title: Incoming\\n  description: Accept incoming\\n")
            artifact["artifacts"] += [str(spec.relative_to(workspace)),str(manifest.relative_to(workspace))]
            artifact.update(units=units,source_bindings={added["id"]:{"kind":"stories","path":str(spec.relative_to(workspace))}})
"""
        + text[end:]
    )
    script.write_text(text)
    result = run_case(case, ticks=500)
    if result.phase != "delivered":
        pytest.fail(json.dumps(diagnose(result), indent=2))
    assert "source:project:incoming" in result.units
    unit = result.units["source:project:incoming"]
    assert unit.state == UnitState.DELIVERED
    assert unit.bindings["issue"]["number"] == 1
    assert case.service.issues[1]["state"] == "closed"
    controls = [value for value in result.units.values() if intake.control(value)]
    assert len(controls) == 1
    assert intake.control(controls[0])["state"] == "completed"
    assert controls[0].evidence["planning_flow"]["state"] == "activated"
    assert len(case.service.pulls) == 5
    assert any(
        method == "POST" and "/issues/1/comments" in path for method, path, _ in case.service.calls
    )
    assert intake.settled(result)


@pytest.mark.parametrize("production_case", [{"intake": True}], indirect=True)
def test_live_story_acceptance_amendment_drains_retains_same_pr_and_reexecutes_reviewed_source(
    production_case, monkeypatch
):
    from conftest import git

    from bmad_loop import verify

    case = production_case
    # Exercise a private native story source: no old completed plan is rewritten
    # or regressed, and both actual source publication transactions are required.
    ignore = case.repo / ".gitignore"
    ignore.write_text(ignore.read_text() + "\n/specs/\n")
    git(case.repo, "rm", "-r", "--cached", "specs")
    git(case.repo, "add", ".gitignore")
    git(case.repo, "commit", "-qm", "isolate private native stories")
    git(case.repo, "push", "origin", "HEAD:main")
    case.service.counter = 1
    released = case.events.parent / "amendment-release"
    script = case.events.parent / "scripted-coding-cli.py"
    text = script.read_text()
    text = text.replace(
        '"decomposition","planning_review"}', '"decomposition","planning_review","triage"}'
    )
    text = text.replace(
        '    workspace=Path(request["workspace"])\n    product=',
        """    if suffix=="first" and "amended" not in request["unit"]["acceptance"][0]:
        deadline=time.monotonic()+15
        while not Path("""
        + repr(str(released))
        + """).exists():
            if time.monotonic()>deadline: raise RuntimeError("intake did not triage while original actor remained live")
            threading.Event().wait(.01)
    workspace=Path(request["workspace"])
    product=""",
    )
    text = text.replace(
        'product.write_text("accepted "+suffix+"\\n")',
        'product.write_text("accepted "+suffix+"\\n"+("amended acceptance\\n" if "amended" in request["unit"]["acceptance"][0] else ""))',
    )
    text = text.replace(
        'planned_marker="planning-revision-marker:first" if suffix=="first" and remote.get("planning_required") else ""',
        'planned_marker="amendment-marker:first" if "amended" in request["unit"]["acceptance"][0] else ""',
    )
    text = text.replace(
        'verify.commit_paths(workspace,"feat: implement "+suffix,[product,spec])',
        'verify.commit_paths(workspace,"feat: implement "+suffix,[product]+([] if verify.git_bytes(workspace,"check-ignore",str(spec.relative_to(workspace))).returncode==0 else [spec]))',
    )
    text = text.replace(
        """    if name in {"requirements","architecture","decomposition"}:""",
        """    if name=="triage":
        contract=context["triage_result_contract"]
        target=next(row for row in context["existing_units"] if row["id"]=="source:project:first")
        target["acceptance"]=["Accept first with amended acceptance"]
        artifact.update(intake_coordinates=contract["intake_coordinates"],proposal={"schema_version":1,"disposition":"attach","input_versions":contract["intake_coordinates"]["input_versions"],"unit_id":target["id"],"rationale":"Owner feedback amends the existing unmerged first story without expanding ownership","acceptance_criteria":target["acceptance"],"dependencies":target["after"],"related_issue":1,"related_pr":None,"within_scope":True,"graph_revision":request["coordinates"]["graph_revision"]},units=[target])
    if name in {"requirements","architecture","decomposition"}:""",
    )
    text = text.replace(
        """(workspace/"docs"/"brief.md").read_text()""",
        """context["intake"]["observations"][0]["payload"]["body"]""",
    )
    start = text.index(
        """            units=[row for row in context["existing_units"] if row["kind"]!="planning"]"""
    )
    end = text.index("""    if name=="planning_review":""", start)
    text = (
        text[:start]
        + """            units=[row for row in context["existing_units"] if row["kind"]!="planning"]
            target=context["proposed_intake_units"][0]
            units=[target if row["id"]==target["id"] else row for row in units]
            spec=workspace/"specs"/"epic"/"stories"/"first-story.md"
            spec.write_text(spec.read_text()+"\\namendment-marker:first\\n")
            manifest=workspace/"specs"/"epic"/"stories.yaml"
            manifest.write_text(manifest.read_text().replace("description: Accept first\\n","description: Accept first with amended acceptance\\n"))
            artifact["artifacts"] += [str(spec.relative_to(workspace)),str(manifest.relative_to(workspace))]
            artifact.update(units=units,source_bindings={target["id"]:{"kind":"stories","path":str(spec.relative_to(workspace))}})
"""
        + text[end:]
    )
    script.write_text(text)
    original_poll = case.runtime._perform_intake
    injected = False

    def poll(snapshot, action):
        nonlocal injected
        if not injected and (case.events.parent / "parallel-markers" / "first").exists():
            case.service.issues[1] = {
                "id": 1001,
                "node_id": "issue-1",
                "number": 1,
                "title": "Amend first acceptance",
                "body": "Add the amended first behavior while preserving the current PR and ownership.",
                "state": "open",
                "labels": [],
                "assignees": [],
                "milestone": None,
                "updated_at": "2026-11-05T10:00:00Z",
                "html_url": "https://github.com/a/b/issues/1",
                "user": {"id": 5, "login": "owner"},
            }
            injected = True
        return original_poll(snapshot, action)

    monkeypatch.setattr(case.runtime, "_perform_intake", poll)
    original_apply = case.runtime.apply
    live_amendments = []

    def apply(snapshot, action, observation):
        original_apply(snapshot, action, observation)
        controls = [
            unit
            for unit in snapshot.units.values()
            if intake.control(unit) and intake.control(unit).get("amendments")
        ]
        if controls and not released.exists():
            target = snapshot.units["source:project:first"]
            live_amendments.append(bool(target.claim and not target.claim.quiescent))
            assert target.hold.kind == "intake_plan_change"
            released.write_text("triage accepted; original actor may finish")

    monkeypatch.setattr(case.runtime, "apply", apply)
    result = run_case(case, ticks=600)
    if result.phase != "delivered":
        pytest.fail(json.dumps(diagnose(result), indent=2))
    assert injected and live_amendments == [True]
    first = result.units["source:project:first"]
    assert first.spec.acceptance == ("Accept first with amended acceptance",)
    assert first.state == UnitState.DELIVERED and len(first.evidence["amendment_history"]) == 1
    history = first.evidence["amendment_history"][0]
    assert verify.is_ancestor(
        case.remote, history["receipt"]["old_head"], first.evidence["merge"]["merge_commit"]
    )
    assert len([row for row in case.service.pulls.values() if row["title"] == "First"]) == 1
    assert len(case.service.pulls) == 4
    assert (
        "amended acceptance"
        in verify.file_bytes_at_revision(case.remote, "refs/heads/main", "src/first.txt").decode()
    )
    old_source = history["invalidated"]["source_artifacts"][0]
    assert (case.events.parent / "workers").exists()
    assert (Path(first.bindings["workspace"]) / old_source["path"]).is_file()
    assert first.bindings["effective_source"]["path"].startswith(".bmad-loop/campaign-amendments/")


@pytest.mark.parametrize("production_case", [{"intake": True}], indirect=True)
def test_nonwork_intake_closes_only_owned_control_and_keeps_original_issue_open(production_case):
    case = production_case
    case.service.counter = 1
    case.service.issues[1] = {
        "id": 1001,
        "node_id": "owner-issue",
        "number": 1,
        "title": "Already covered",
        "body": "This incoming request repeats the existing first acceptance.",
        "state": "open",
        "labels": [],
        "assignees": [],
        "milestone": None,
        "updated_at": "2026-11-05T10:00:00Z",
        "html_url": "https://github.com/a/b/issues/1",
        "user": {"id": 5, "login": "owner"},
    }
    script = case.events.parent / "scripted-coding-cli.py"
    text = script.read_text(encoding="utf-8")
    text = text.replace(
        '"decomposition","planning_review"}', '"decomposition","planning_review","triage"}'
    )
    text = text.replace(
        """    if name in {"requirements","architecture","decomposition"}:""",
        """    if name=="triage":
        contract=context["triage_result_contract"]
        target=next(row for row in context["existing_units"] if row["id"]=="source:project:first")
        artifact.update(intake_coordinates=contract["intake_coordinates"],proposal={"schema_version":1,"disposition":"duplicate","input_versions":contract["intake_coordinates"]["input_versions"],"unit_id":target["id"],"rationale":"Existing first story already covers the entire incoming request","acceptance_criteria":target["acceptance"],"dependencies":target["after"],"related_issue":1,"related_pr":None,"within_scope":True,"graph_revision":request["coordinates"]["graph_revision"]},units=[])
    if name in {"requirements","architecture","decomposition"}:""",
    )
    script.write_text(text, encoding="utf-8")
    result = run_case(case, ticks=450)
    if result.phase != "delivered":
        pytest.fail(json.dumps(diagnose(result), indent=2))
    controls = [unit for unit in result.units.values() if intake.control(unit)]
    assert len(controls) == 1
    control = controls[0]
    assert intake.optional_canceled(control)
    assert case.service.issues[1]["state"] == "open"
    internal = case.service.issues[control.bindings["issue"]["number"]]
    assert internal["state"] == "closed" and internal["state_reason"] == "not_planned"
    pr = case.service.pulls[control.bindings["pr"]["number"]]
    assert pr["state"] == "closed" and pr["merged"] is False
    assert len(case.service.pulls) == 4
    assert intake.settled(result)
