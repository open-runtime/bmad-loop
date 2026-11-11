"""GitHub boundaries bind real object versions without retaining model slots."""

import copy
import dataclasses
import json
from pathlib import Path

import pytest
from conftest import git
from test_campaign_runtime import production_case as production_case
from test_campaign_runtime import run_case
from test_campaign_takeover import configured

from bmad_loop import campaign_boundary_sync as boundary
from bmad_loop.campaign_github import ApiResponse, GithubError, MutationIntent
from bmad_loop.campaign_intake_flow import INBOX
from bmad_loop.campaign_model import CampaignError, OutboxAction, UnitState, WorkerClaim
from bmad_loop.campaign_supervisor import Proposal


class Gateway:
    repository, root = "org/repo", "repos/org/repo"

    def __init__(self, head):
        self.item = {
            "id": 1,
            "number": 1,
            "title": "Story",
            "body": "Acceptance",
            "state": "open",
            "updated_at": "2026-11-06T00:00:00Z",
            "user": {"id": 10, "login": "owner"},
            "labels": [],
            "assignees": [],
        }
        self.pr = {
            "id": 2,
            "node_id": "P2",
            "number": 2,
            "title": "Work",
            "body": "Story #1",
            "state": "open",
            "draft": True,
            "head": {"sha": head},
            "base": {"ref": "main", "repo": {"full_name": self.repository}},
        }
        self.notes, self.review_rows, self.threads, self.fail = [], [], [], False

    def issues(self, **kwargs):
        return [copy.deepcopy(self.item)]

    def issue(self, number):
        return copy.deepcopy(self.item)

    def pulls(self):
        return [copy.deepcopy(self.pr)]

    def pull(self, number):
        return copy.deepcopy(self.pr)

    def comments(self, number):
        return copy.deepcopy(self.notes)

    def reviews(self, number):
        return copy.deepcopy(self.review_rows)

    def review_threads(self, number):
        if self.fail:
            raise GithubError("review thread readback unavailable")
        return copy.deepcopy(self.threads)

    def prepare_comment(self, number, *, action_id, campaign_id, unit_id, text, generation=0):
        return MutationIntent(
            action_id,
            "comment",
            "POST",
            f"{self.root}/issues/{number}/comments",
            {"body": text},
            generation=generation,
        )


def setup(project, tmp_path):
    git(project.project, "clone", "--bare", str(project.project), str(tmp_path / "remote.git"))
    git(project.project, "remote", "add", "origin", str(tmp_path / "remote.git"))
    runtime, snapshot, unit = configured(project, tmp_path)
    git(project.project, "switch", "-c", "campaign-boundary")
    git(project.project, "push", "origin", "campaign-boundary")
    snapshot.remote_url = str(tmp_path / "remote.git")
    unit.spec = dataclasses.replace(unit.spec, kind="planning")
    unit.bindings.update(
        pr={"number": 2, "node_id": "P2", "url": "https://github.com/org/repo/pull/2"},
        issue={"number": 1, "url": "https://github.com/org/repo/issues/1"},
        branch="campaign-boundary",
        remote_head_anchor=unit.bindings["head"],
    )
    runtime._gateway = Gateway(unit.bindings["head"])
    runtime._gateway.pr["merged"] = False
    runtime._gateway.pr["head"].update(ref="campaign-boundary", repo={"full_name": "org/repo"})
    runtime.template = lambda *args: type(
        "Template",
        (),
        {
            "policy": type(
                "Policy", (), {"limits": type("Limits", (), {"max_review_cycles": 3})()}
            )()
        },
    )()
    return runtime, snapshot, unit


def prepared(runtime, snapshot, unit):
    payload = {
        "kind": "stage",
        "stage": "requirements",
        "role": "planning",
        "candidate": {"profile": "codex"},
    }
    proposal = boundary.before_proposal(runtime, snapshot, Proposal("launch", unit.id, payload))
    action = OutboxAction("before", proposal.kind, unit.id, proposal.payload, snapshot.generation)
    data = boundary.before_effect(runtime, snapshot, action).data
    boundary.before_apply(runtime, snapshot, action, data)
    return payload, data["inputs"]


def completed(runtime, snapshot, unit):
    payload, before = prepared(runtime, snapshot, unit)
    unit.state = UnitState.RUNNING
    unit.claim = WorkerClaim(unit.id, 1, snapshot.generation, "model", quiescent=True)
    receipt = {
        "outcome": "stage_complete",
        "launch_id": "model",
        "stage": "requirements",
        "head": unit.bindings["head"],
    }
    unit.evidence["stage_sync_debt"] = {
        "receipt": receipt,
        "request": {
            "stage_boundary": {"inputs": before},
            "coordinates": {"head": unit.bindings["head"]},
        },
    }
    return OutboxAction("after", "stage-boundary-after", unit.id, {}, snapshot.generation)


def advance_owned_remote(project, tmp_path, runtime):
    """Create a real remote-only descendant without moving the worker checkout."""
    remote = tmp_path / "remote.git"
    before = runtime._gateway.pr["head"]["sha"]
    head = git(
        project.project,
        "--git-dir=" + str(remote),
        "-c",
        "user.name=Scripted Bot",
        "-c",
        "user.email=bot@example.test",
        "commit-tree",
        before + "^{tree}",
        "-p",
        before,
        "-m",
        "outside contributor",
    ).strip()
    git(
        project.project,
        "--git-dir=" + str(remote),
        "update-ref",
        "refs/heads/campaign-boundary",
        head,
    )
    runtime._gateway.pr["head"]["sha"] = head
    return head


def test_before_boundary_records_external_head_before_another_writing_stage(project, tmp_path):
    runtime, snapshot, unit = setup(project, tmp_path)
    unit.state = UnitState.READY
    local = unit.bindings["head"]
    remote = advance_owned_remote(project, tmp_path, runtime)
    payload, inputs = prepared(runtime, snapshot, unit)
    debt = unit.evidence["remote_head_sync"]
    assert debt["head"] == remote and debt["local_head"] == debt["provenance_before"] == local
    assert debt["pr_node_id"] == "P2" and debt["branch"] == "campaign-boundary"
    assert inputs["github"]["pulls"]["2"]["pr"]["head"]["sha"] == remote
    assert "stage_boundary_ready" not in unit.evidence
    assert (
        boundary.before_proposal(runtime, snapshot, Proposal("launch", unit.id, payload)).kind
        != "launch"
    )
    assert git(project.project, "rev-parse", "HEAD").strip() == local
    assert unit.state == UnitState.READY and unit.hold is None


def test_after_boundary_retains_completed_receipt_and_rearms_stage_after_external_head(
    project, tmp_path
):
    runtime, snapshot, unit = setup(project, tmp_path)
    action = completed(runtime, snapshot, unit)
    receipt = copy.deepcopy(unit.evidence["stage_sync_debt"]["receipt"])
    unit.evidence["planning_flow"] = {
        "history": [{"launch_id": "model", "stage": "requirements", "state": "prepared"}],
        "attempts": {"requirements": 1},
        "stages": {},
        "state": "planning",
    }
    runtime._validate_stage_receipt = lambda *args: pytest.fail("accepted stale stage evidence")
    runtime._apply_stage_result = lambda *args: pytest.fail("advanced an obsolete stage")
    remote = advance_owned_remote(project, tmp_path, runtime)
    result = boundary.after_effect(runtime, snapshot, action)
    assert result.data["changed"] and not result.data["validated"]
    boundary.after_apply(runtime, snapshot, action, result.data)
    assert unit.evidence["remote_head_sync"]["head"] == remote
    assert unit.evidence["stage_input_invalidations"][-1]["receipt"] == receipt
    assert "stage_sync_debt" not in unit.evidence and "stage_boundary_ready" not in unit.evidence
    assert unit.claim.quiescent and unit.state == UnitState.RUNNING and unit.hold is None
    assert unit.evidence["planning_flow"]["attempts"]["requirements"] == 0


def test_retained_writer_is_attributed_before_boundary_acceptance(project, tmp_path):
    runtime, snapshot, unit = setup(project, tmp_path)
    _, before = prepared(runtime, snapshot, unit)
    unit.state = UnitState.RUNNING
    unit.evidence.update(developer_family="openai", developer_families=["openai"])
    unit.claim = WorkerClaim(
        unit.id, 1, snapshot.generation, "writer", role="planning", writes=True
    )
    directory = runtime.store.directory(snapshot.id) / "workers" / "writer"
    directory.mkdir(parents=True)
    request = {"stage_boundary": {"inputs": before}, "coordinates": {"head": unit.bindings["head"]}}
    (directory / "request.json").write_text(json.dumps(request), encoding="utf-8")
    receipt = {
        "outcome": "stage_complete",
        "launch_id": "writer",
        "stage": "requirements",
        "head": unit.bindings["head"],
        "provider_family": "anthropic",
        "observed_model": "claude-fable-5-1",
        "observed_models": ["claude-fable-5-1"],
    }
    action = OutboxAction(
        "observe", "observe-worker", unit.id, {"launch_id": "writer"}, snapshot.generation
    )
    runtime._apply_worker(snapshot, unit, action, {"state": "finished", "receipt": receipt})
    assert unit.evidence["developer_families"] == ["anthropic", "openai"]
    assert unit.evidence["stage_sync_debt"]["receipt"] == receipt
    assert "planning_results" not in unit.evidence
    runtime._validate_stage_receipt = lambda *args: pytest.fail("accepted stale writer result")
    runtime._apply_stage_result = lambda *args: pytest.fail("granted stale stage completion")
    advance_owned_remote(project, tmp_path, runtime)
    after = OutboxAction("after-writer", "stage-boundary-after", unit.id, {}, snapshot.generation)
    result = boundary.after_effect(runtime, snapshot, after)
    boundary.after_apply(runtime, snapshot, after, result.data)
    assert unit.evidence["stage_input_invalidations"][-1]["receipt"] == receipt
    assert unit.evidence["developer_families"] == ["anthropic", "openai"]
    assert unit.state == UnitState.RUNNING


def test_unpushed_local_descendant_is_not_external_debt(project, tmp_path):
    runtime, snapshot, unit = setup(project, tmp_path)
    git(project.project, "commit", "--allow-empty", "-m", "owned unpublished work")
    unit.bindings["head"] = git(project.project, "rev-parse", "HEAD").strip()
    payload, _ = prepared(runtime, snapshot, unit)
    assert "remote_head_sync" not in unit.evidence and unit.hold is None
    assert (
        boundary.before_proposal(runtime, snapshot, Proposal("launch", unit.id, payload)).kind
        == "launch"
    )


@pytest.mark.parametrize(
    "fault",
    [None, "ordinary-stage", "state", "canonical", "merge", "unmerged", "node", "missing-node"],
)
def test_landing_boundary_preserves_confirmed_postmerge_review_only(project, tmp_path, fault):
    runtime, snapshot, unit = setup(project, tmp_path)
    head = unit.bindings["head"]
    unit.state = UnitState.MERGED_PENDING_SYNC
    unit.evidence["merge"] = {
        "state": "merged_pending_sync",
        "merge_commit": head,
        "canonical_head": head,
    }
    unit.evidence["landing_acceptance"] = {"merge_commit": head, "state": "reviewing"}
    runtime._gateway.pr.update(merged=True, state="closed", merge_commit_sha=head)
    payload = {
        "kind": "stage",
        "stage": "publication_review",
        "role": "publication_review",
        "landing": True,
        "read_only": True,
    }
    if fault == "ordinary-stage":
        payload.pop("landing")
    elif fault == "state":
        unit.state = UnitState.READY_TO_PUBLISH
    elif fault == "canonical":
        unit.evidence["merge"].pop("canonical_head")
    elif fault == "merge":
        runtime._gateway.pr["merge_commit_sha"] = "f" * 40
    elif fault == "unmerged":
        runtime._gateway.pr["merged"] = False
    elif fault == "node":
        runtime._gateway.pr["node_id"] = "different-pr"
    elif fault == "missing-node":
        unit.bindings["pr"].pop("node_id")
        runtime._gateway.pr.pop("node_id")
    proposal = boundary.before_proposal(runtime, snapshot, Proposal("launch", unit.id, payload))
    action = OutboxAction(
        "landing-before", proposal.kind, unit.id, proposal.payload, snapshot.generation
    )
    result = boundary.before_effect(runtime, snapshot, action)
    boundary.before_apply(runtime, snapshot, action, result.data)
    if fault:
        assert result.data["remote_head_error"] and unit.hold.kind == "ownership_uncertain"
        assert "stage_boundary_ready" not in unit.evidence
        return
    assert unit.hold is None and "remote_head_sync" not in unit.evidence
    assert (
        boundary.before_proposal(runtime, snapshot, Proposal("launch", unit.id, payload)).kind
        == "launch"
    )
    unit.claim = WorkerClaim(unit.id, 1, snapshot.generation, "landing-review", quiescent=True)
    unit.evidence["stage_sync_debt"] = {
        "request": {
            **payload,
            "stage_boundary": {"inputs": result.data["inputs"]},
            "coordinates": {"head": head},
        },
        "receipt": {
            "outcome": "stage_complete",
            "stage": "publication_review",
            "landing": True,
            "head": head,
            "launch_id": "landing-review",
        },
    }
    calls = []
    runtime._validate_stage_receipt = lambda *args: calls.append("validate-landing")
    runtime._apply_stage_result = lambda *args: calls.append("accept-landing")
    after = OutboxAction("landing-after", "stage-boundary-after", unit.id, {}, snapshot.generation)
    observed = boundary.after_effect(runtime, snapshot, after)
    boundary.after_apply(runtime, snapshot, after, observed.data)
    assert calls == ["validate-landing", "accept-landing"]
    assert unit.hold is None and unit.state == UnitState.MERGED_PENDING_SYNC
    assert "stage_sync_debt" not in unit.evidence


@pytest.mark.parametrize("fault", ["identity", "lag"])
def test_boundary_never_converts_untrusted_or_disagreeing_remote_to_launch(
    project, tmp_path, fault
):
    from bmad_loop.campaign_remote_head import RemoteHeadPending

    runtime, snapshot, unit = setup(project, tmp_path)
    if fault == "identity":
        runtime._gateway.pr["head"]["repo"]["full_name"] = "other/repo"
        prepared(runtime, snapshot, unit)
        assert unit.hold.kind == "ownership_uncertain"
    else:
        runtime._gateway.pr["head"]["sha"] = "f" * 40
        with pytest.raises(RemoteHeadPending, match="disagree"):
            prepared(runtime, snapshot, unit)
        assert unit.hold is None
    assert "remote_head_sync" not in unit.evidence and "stage_boundary_ready" not in unit.evidence


def test_before_inputs_are_durable_before_any_model_launch_and_include_native_versions(
    project, tmp_path
):
    runtime, snapshot, unit = setup(project, tmp_path)
    payload, value = prepared(runtime, snapshot, unit)
    path = Path(value["receipt_path"])
    assert path.is_file() and not path.is_relative_to(project.project)
    assert boundary.digest(json.loads(path.read_text())) == value["receipt_digest"]
    assert value["github"]["issues"]["issue"]["issue"]["id"] == 1
    assert value["github"]["pulls"]["2"]["pr"]["node_id"] == "P2"
    assert (
        boundary.before_proposal(runtime, snapshot, Proposal("launch", unit.id, payload)).kind
        == "launch"
    )
    comments = [row.payload["intent"]["payload"]["body"] for row in snapshot.outbox]
    assert len(comments) == 2 and all(
        "/pull/2" in body and "/issues/1" in body for body in comments
    )
    assert "inputs" not in unit.evidence["stage_boundary_history"][-1]


def test_after_read_failure_retains_completed_receipt_and_quiescent_capacity_then_retries_without_model(
    project, tmp_path
):
    runtime, snapshot, unit = setup(project, tmp_path)
    action = completed(runtime, snapshot, unit)
    runtime._gateway.fail = True
    with pytest.raises(GithubError, match="thread readback"):
        boundary.after_effect(runtime, snapshot, action)
    assert (
        unit.claim.quiescent and unit.evidence["stage_sync_debt"]["receipt"]["launch_id"] == "model"
    )
    runtime._gateway.fail = False
    checked, accepted = [], []
    runtime._validate_stage_receipt = lambda *args: checked.append("validated")
    runtime._apply_stage_result = lambda *args: accepted.append("accepted")
    result = boundary.after_effect(runtime, snapshot, action)
    boundary.after_apply(runtime, snapshot, action, result.data)
    assert checked == ["validated"] and accepted == ["accepted"]
    assert unit.claim.launch_id == "model" and "stage_sync_debt" not in unit.evidence


def test_new_human_comment_invalidates_planning_but_refunds_only_administrative_attempt(
    project, tmp_path
):
    runtime, snapshot, unit = setup(project, tmp_path)
    action = completed(runtime, snapshot, unit)
    unit.evidence["planning_flow"] = {
        "history": [{"launch_id": "model", "stage": "requirements", "state": "prepared"}],
        "attempts": {"requirements": 1},
        "stages": {},
        "state": "planning",
    }
    unit.evidence["usage_charged"] = {"tokens": 42}
    runtime._gateway.notes.append(
        {
            "id": 55,
            "body": "Owner acceptance changed",
            "updated_at": "2026-11-06T00:01:00Z",
            "user": {"id": 10},
        }
    )
    result = boundary.after_effect(runtime, snapshot, action)
    assert result.data["changed"]
    boundary.after_apply(runtime, snapshot, action, result.data)
    assert unit.evidence["planning_flow"]["attempts"]["requirements"] == 0
    assert unit.evidence["usage_charged"]["tokens"] == 42
    assert unit.evidence["stage_input_invalidations"][-1]["receipt"]["launch_id"] == "model"


def test_own_exact_ack_comment_and_checkpoint_do_not_hide_owner_edits(project, tmp_path):
    runtime, snapshot, unit = setup(project, tmp_path)
    _, before = prepared(runtime, snapshot, unit)
    intent = MutationIntent(
        "note",
        "comment",
        "POST",
        "/comment",
        {"body": "own transition"},
        state="confirmed",
        receipt={"id": 99, "user": {"id": 20}},
    )
    snapshot.outbox.append(
        OutboxAction(
            "note",
            "github",
            unit.id,
            {"intent": intent.to_dict()},
            snapshot.generation,
            state="confirmed",
            receipt={"intent": intent.to_dict()},
        )
    )
    runtime._gateway.notes.append(
        {
            "id": 99,
            "body": "own transition",
            "updated_at": "2026-11-06T00:01:00Z",
            "user": {"id": 20},
        }
    )
    runtime._gateway.pr["head"]["sha"] = "owned-new-head"
    after = boundary.collect(runtime, snapshot, unit, action_id="own-update")
    allowed = {unit.bindings["head"], "owned-new-head"}
    assert boundary.semantic(snapshot, unit, before, allowed_heads=allowed) == boundary.semantic(
        snapshot, unit, after, allowed_heads=allowed
    )
    runtime._gateway.notes[0]["body"] = "owner edited the bot comment"
    after = boundary.collect(runtime, snapshot, unit, action_id="owner-edit")
    assert boundary.semantic(snapshot, unit, before, allowed_heads=allowed) != boundary.semantic(
        snapshot, unit, after, allowed_heads=allowed
    )


def test_unknown_foreign_head_and_required_review_are_new_inputs(project, tmp_path):
    runtime, snapshot, unit = setup(project, tmp_path)
    _, before = prepared(runtime, snapshot, unit)
    runtime._gateway.pr["head"]["sha"] = "foreign"
    runtime._gateway.review_rows.append(
        {"id": 71, "state": "CHANGES_REQUESTED", "body": "required repair"}
    )
    after = boundary.collect(runtime, snapshot, unit, action_id="foreign")
    allowed = {unit.bindings["head"]}
    assert boundary.semantic(snapshot, unit, before, allowed_heads=allowed) != boundary.semantic(
        snapshot, unit, after, allowed_heads=allowed
    )


def test_incomplete_boundary_retries_same_action_before_intake_mutation_and_keeps_post_actor_free(
    project, tmp_path
):
    runtime, snapshot, unit = setup(project, tmp_path)
    after = completed(runtime, snapshot, unit)
    original = boundary.collect

    def incomplete(*args, **kwargs):
        value = original(*args, **kwargs)
        value["complete"] = value["intake"]["batch"]["complete"] = False
        value["intake"]["batch"]["pending_count"] = 12
        return value

    from unittest.mock import patch

    intake_before = copy.deepcopy(snapshot.bindings.get(INBOX))
    with patch.object(boundary, "collect", incomplete):
        prior = boundary.before_effect(
            runtime,
            snapshot,
            OutboxAction(
                "before-incomplete",
                "stage-boundary-before",
                unit.id,
                {"key": "key", "launch": {}},
                snapshot.generation,
            ),
        )
        post = boundary.after_effect(runtime, snapshot, after)
    assert (
        prior.state == post.state == "waiting"
        and prior.retry_at == post.retry_at == runtime.clock() + 5
    )
    assert prior.data["coverage_pending"] == 12 and "incomplete" in post.reason
    assert snapshot.bindings.get(INBOX) == intake_before
    assert unit.claim.quiescent and "stage_sync_debt" in unit.evidence
    with pytest.raises(CampaignError, match="incomplete GitHub boundary"):
        boundary.before_apply(
            runtime,
            snapshot,
            after,
            {"inputs": incomplete(runtime, snapshot, unit, action_id="defensive")},
        )
    assert snapshot.bindings.get(INBOX) == intake_before


def test_changed_closure_inputs_return_quiescent_unit_to_ready_without_canonical_mutation(
    project, tmp_path
):
    runtime, snapshot, unit = setup(project, tmp_path)
    after = completed(runtime, snapshot, unit)
    unit.spec = dataclasses.replace(unit.spec, kind="closure")
    debt = unit.evidence["stage_sync_debt"]
    debt["receipt"]["stage"] = "closure"
    unit.bindings["canonical_source"] = {"native": "preserve"}
    unit.bindings["source_revision"] = "obsolete"
    unit.evidence.update(
        closure_manifest={"old": True},
        source_artifacts=[{"old": True}],
        actual_usage={"tokens": 99},
    )
    runtime._gateway.notes.append(
        {
            "id": 77,
            "body": "Owner changed closure scope",
            "updated_at": "2026-11-06T00:01:00Z",
            "user": {"id": 10},
        }
    )
    result = boundary.after_effect(runtime, snapshot, after)
    boundary.after_apply(runtime, snapshot, after, result.data)
    assert unit.state == UnitState.READY and unit.claim.quiescent
    assert "closure_manifest" not in unit.evidence and "source_artifacts" not in unit.evidence
    assert "source_revision" not in unit.bindings and unit.bindings["canonical_source"] == {
        "native": "preserve"
    }
    assert unit.evidence["actual_usage"]["tokens"] == 99


@pytest.mark.parametrize("initial_owned", [False, True])
def test_owned_issue_close_is_metadata_but_owner_reopen_remains_new_intent(
    project, tmp_path, initial_owned
):
    runtime, snapshot, unit = setup(project, tmp_path)
    snapshot.revision = 10
    gateway = runtime._gateway
    if initial_owned:
        created = MutationIntent(
            "create",
            "issue",
            "POST",
            "/issues",
            {"title": gateway.item["title"], "body": gateway.item["body"]},
            state="confirmed",
            receipt=copy.deepcopy(gateway.item),
        )
        snapshot.outbox.append(
            OutboxAction(
                "create",
                "github",
                unit.id,
                {"intent": created.to_dict()},
                snapshot.generation,
                state="confirmed",
                receipt={"intent": created.to_dict()},
            )
        )
    _, before = prepared(runtime, snapshot, unit)
    if initial_owned:
        own_comment = {
            "id": 101,
            "body": "owned progress",
            "updated_at": "2026-11-06T00:01:00Z",
            "user": {"id": 20},
        }
        intent = MutationIntent(
            "progress",
            "comment",
            "POST",
            "/issues/1/comments",
            {"body": own_comment["body"]},
            state="confirmed",
            receipt=copy.deepcopy(own_comment),
        )
        snapshot.outbox.append(
            OutboxAction(
                "progress",
                "github",
                unit.id,
                {"intent": intent.to_dict()},
                snapshot.generation,
                state="confirmed",
                receipt={"intent": intent.to_dict()},
            )
        )
        gateway.notes.append(own_comment)
        gateway.item["updated_at"] = own_comment["updated_at"]
        projected = boundary.collect(runtime, snapshot, unit, action_id="own-comment")
        allowed = {unit.bindings["head"]}
        assert boundary.semantic(
            snapshot, unit, before, allowed_heads=allowed
        ) == boundary.semantic(snapshot, unit, projected, allowed_heads=allowed)
    prior = gateway.item["updated_at"]
    gateway.item.update(state="closed", state_reason="completed", updated_at="2026-11-06T00:02:00Z")
    closed = MutationIntent(
        "close",
        "close_issue",
        "PATCH",
        "/issues/1",
        {"state": "closed", "state_reason": "completed"},
        state="confirmed",
        receipt=copy.deepcopy(gateway.item),
        coordinates={"id": 1, "updated_at": prior},
    )
    snapshot.outbox.append(
        OutboxAction(
            "close",
            "github",
            unit.id,
            {"intent": closed.to_dict()},
            snapshot.generation,
            state="confirmed",
            receipt={"intent": closed.to_dict()},
        )
    )
    snapshot.events.append({"kind": "effect-confirmed", "action": "close", "revision": 20})
    snapshot.revision = 20
    after = boundary.collect(runtime, snapshot, unit, action_id="closed")
    allowed = {unit.bindings["head"]}
    assert boundary.semantic(snapshot, unit, before, allowed_heads=allowed) == boundary.semantic(
        snapshot, unit, after, allowed_heads=allowed
    )
    gateway.item.update(state="open", state_reason="reopened", updated_at="2026-11-06T00:03:00Z")
    reopened = boundary.collect(runtime, snapshot, unit, action_id="reopened")
    assert boundary.semantic(snapshot, unit, before, allowed_heads=allowed) != boundary.semantic(
        snapshot, unit, reopened, allowed_heads=allowed
    )


def test_settled_campaign_does_not_requeue_periodic_observations_forever(project, tmp_path):
    runtime, snapshot, unit = configured(project, tmp_path)
    unit.state = UnitState.DELIVERED
    snapshot.bindings.update(hierarchy_finalized_graph=snapshot.graph_revision, last_delivery_at=90)
    runtime.clock = lambda: 10**9
    assert runtime.proposals(snapshot) == ()


def test_custom_recovery_prompt_retains_one_json_packet_with_fresh_remote_evidence():
    original = "Trusted recovery instruction\n\n" + json.dumps(
        {"hold_id": "hold", "coordinates": {"head": "H"}}
    )
    result = boundary.custom_prompt(original, {"native_id": 55, "body": "untrusted note"})
    packet = json.loads(result.split("\n\n", 1)[1])
    assert packet["hold_id"] == "hold" and packet["github_stage_inputs"]["native_id"] == 55


def test_production_post_stage_api_failure_releases_actor_and_recovers_without_another_model(
    production_case,
):
    case, fault = production_case, {}
    original = case.service.request

    def request(method, path, payload=None):
        if path == "graphql" and "reviewThreads" in (payload or {}).get("query", "") and not fault:
            current = case.store.load(case.snapshot.id)
            for unit in current.units.values():
                debt = unit.evidence.get("stage_sync_debt")
                if debt and debt["receipt"]["stage"] == "admission_review":
                    fault.update(
                        unit=unit.id, launch=unit.claim.launch_id, quiescent=unit.claim.quiescent
                    )
                    return ApiResponse(
                        503, {}, {"message": "temporary required thread readback failure"}
                    )
        return original(method, path, payload)

    case.service.request = request
    result = run_case(case)
    assert result.phase == "delivered" and fault["quiescent"]
    from bmad_loop.campaign_action_history import actions

    launches = [
        row.id
        for row in actions(result)
        if row.kind == "launch"
        and row.unit == fault["unit"]
        and row.payload.get("stage") == "admission_review"
    ]
    assert launches == [fault["launch"]]
    assert any(
        event["kind"] == "effect-wait"
        and event.get("observation") == "unknown"
        and "thread observation failed" in event.get("reason", "")
        for event in result.events
    )
    assert any(
        row.kind == "github" and row.payload.get("purpose") == "transition"
        for row in actions(result)
    )
