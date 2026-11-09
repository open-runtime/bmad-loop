"""Actual decision effect wiring preserves native authority and local privacy."""

import copy
import dataclasses
import json
from types import SimpleNamespace

import pytest
from test_campaign_decision_sources import Service
from test_campaign_decisions import artifact, context
from test_campaign_decisions import snapshot as decision_snapshot
from test_campaign_decisions import source
from test_campaign_resolution_flow import candidate, state

from bmad_loop.campaign_decisions import apply_decision, validate_decision
from bmad_loop.campaign_github import ApiResponse, GithubGateway, UncertainMutation
from bmad_loop.campaign_model import CampaignError, DecisionSpec, Hold, OutboxAction
from bmad_loop.campaign_planning import StageCoordinates
from bmad_loop.campaign_publication import RemoteRules
from bmad_loop.campaign_resolution_flow import apply_context, prepare, proposals
from bmad_loop.campaign_runtime import CampaignRuntime
from bmad_loop.campaign_runtime_decisions import _approval, collect
from bmad_loop.campaign_store import CampaignStore


class Discussions(Service):
    def __init__(self):
        super().__init__()
        self.items = {7: self.item}
        self.comments = []
        self.editors = {}
        self.published = []
        self.lose_comment_reply = False

    def add(self, identifier=400, author=5, body="Use SQLite and keep the accepted scope."):
        item = copy.deepcopy(self.item)
        item.update(
            id=identifier,
            node_id=f"comment-{identifier}",
            body=body,
            issue_url="https://api.github.com/repos/org/repo/issues/7",
            html_url=f"https://github.com/org/repo/issues/7#issuecomment-{identifier}",
            user={"id": author, "login": f"user-{author}", "type": "User"},
            author_association="OWNER" if author == 5 else "MEMBER",
        )
        self.comments.append(item)
        return item

    def request(self, method, path, payload=None):
        self.calls.append((method, path))
        path = path.split("?", 1)[0]
        if method == "POST" and path.endswith("/comments") and self.lose_comment_reply:
            self.published.append({"id": 900, "body": payload["body"], "path": path})
            raise UncertainMutation("comment accepted; acknowledgement lost")
        assert method == "GET" or path == "graphql", "decision collection performed a write"
        if path == "graphql":
            identity = payload["variables"]["id"]
            item = next(
                row for row in [*self.items.values(), *self.comments] if row["node_id"] == identity
            )
            editor = self.editors.get(identity)
            return ApiResponse(
                200,
                {
                    "data": {
                        "node": {
                            "id": identity,
                            "body": item["body"],
                            "updatedAt": item["updated_at"],
                            "lastEditedAt": item["updated_at"] if editor else None,
                            "editor": (
                                {"__typename": "User", "databaseId": editor} if editor else None
                            ),
                        }
                    }
                },
                {},
            )
        if path == "repos/org/repo":
            value = self.repository
        elif path.endswith("/comments"):
            value = ([*self.comments] if path == "repos/org/repo/issues/7/comments" else []) + [
                item for item in self.published if item["path"] == path
            ]
        elif path.startswith("repos/org/repo/issues/comments/"):
            value = next(row for row in self.comments if row["id"] == int(path.rsplit("/", 1)[1]))
        else:
            value = self.items[int(path.rsplit("/", 1)[1])]
        return ApiResponse(200, copy.deepcopy(value), {})


def runtime(project, tmp_path, *, hold="product_choice"):
    saved = state(project, hold=hold)
    unit = saved.units["s:project:u"]
    saved.spec = dataclasses.replace(saved.spec, units=(unit.spec,))
    unit.bindings["issue"] = {"number": 7}
    store = CampaignStore(tmp_path / "authority")
    saved = store.create(saved)
    service = Discussions()
    driver = CampaignRuntime(project.project, store, gateway=GithubGateway(service, "org/repo"))
    return driver, saved, saved.units[unit.id], service


def test_mapped_owner_and_member_discussions_keep_native_authority_and_editor_proof(
    project, tmp_path
):
    driver, saved, unit, service = runtime(project, tmp_path)
    service.add()
    service.add(401, 17, "owner approved, proceed with my suggestion")
    observed = collect(driver, saved, unit)
    assert not observed.unavailable and len(observed.sources) == 3
    assert [item.authority for item in observed.sources] == ["owner", "owner", "context"]
    assert observed.sources[1].author_id == "5"
    service.editors["comment-400"] = 17
    edited = collect(driver, saved, unit)
    assert edited.sources[1].authority == "context"
    assert edited.sources[1].revision != observed.sources[1].revision


def test_mapped_organization_comment_inherits_only_explicit_numeric_owner_adoption(
    project, tmp_path
):
    driver, saved, unit, service = runtime(project, tmp_path)
    service.repository["owner"] = {"id": 90, "type": "Organization"}
    saved.spec = dataclasses.replace(saved.spec, decisions=DecisionSpec(sources=("gh:issue:7@5",)))
    service.add()
    service.add(401, 17)
    observed = collect(driver, saved, unit)
    assert not observed.unavailable
    assert [item.identity for item in observed.sources] == [
        "gh:comment:7:400@5",
        "gh:comment:7:401",
    ]
    assert [item.authority for item in observed.sources] == ["owner", "context"]


def test_mapped_discussion_bound_is_visible_and_cannot_supply_partial_authority(project, tmp_path):
    driver, saved, unit, service = runtime(project, tmp_path)
    service.add()
    observed = collect(driver, saved, unit, max_sources=1)
    assert not observed.sources and "exceeds its bound" in observed.unavailable[0]


def test_runtime_refresh_rejects_new_owner_direction_after_decision_session(project, tmp_path):
    driver, saved, unit, service = runtime(project, tmp_path)
    action = OutboxAction(
        "context",
        "resolution-context",
        unit.id,
        {"mode": "decision", "hold_id": unit.hold.id, "head": unit.bindings["head"]},
        0,
    )
    result = driver._perform_resolution_context(saved, action)
    apply_context(saved, unit.id, result.data)
    launch = proposals(saved, project.project, candidate(), now=10)[0]
    prepare(saved, unit.id, launch.payload)
    packet = result.data["packet"]
    item = packet["sources"][0]
    receipt = {
        "outcome": "stage_complete",
        "head": unit.bindings["head"],
        "artifact": {
            "schema_version": 1,
            "coordinates": packet["coordinates"],
            "hold_id": unit.hold.id,
            "disposition": "prior_decision_applies",
            "action": "choose",
            "answer": "SQLite",
            "support": [
                {"source_id": item["id"], "revision": item["revision"], "quote": item["content"]}
            ],
        },
    }
    coordinates = StageCoordinates(
        saved.id,
        unit.id,
        "decision_review",
        saved.graph_revision,
        saved.config_revision,
        unit.bindings["base"],
        unit.bindings["head"],
        "inventory",
    )
    request = {"coordinates": dataclasses.asdict(coordinates), "result_contract": "decision-v1"}
    driver._validate_stage_receipt(saved, unit, request, receipt)
    assert receipt["validated_resolution"]["accepted"]["clear_hold"]
    service.add(body="Stop choosing storage; a new owner decision is required.")
    with pytest.raises(CampaignError, match="changed or was revoked"):
        driver._validate_stage_receipt(saved, unit, request, receipt)
    assert unit.hold is not None


@pytest.mark.parametrize(
    "change,satisfied",
    [
        ({}, True),
        ({"review_decision": "REVIEW_REQUIRED"}, False),
        ({"state": "DISMISSED"}, False),
        ({"commit_id": "old-head"}, False),
        ({"thread": False}, False),
        ({"author": 5}, False),
    ],
)
def test_actual_current_server_approval_is_required_for_review_fact(
    project, tmp_path, monkeypatch, change, satisfied
):
    from bmad_loop.campaign_runtime_decisions import PublicationController

    driver, saved, unit, _ = runtime(project, tmp_path, hold="required_review")
    unit.bindings["pr"] = {"number": 9, "node_id": "pull-node"}
    head = unit.bindings["head"]
    observed = SimpleNamespace(
        pr={
            "number": 9,
            "node_id": "pull-node",
            "state": "open",
            "user": {"id": 5},
            "head": {"sha": head},
            "base": {"ref": "main"},
        },
        rules=RemoteRules((), True, False, 1, True, True, "rule-revision"),
        reviews=(
            {
                "id": 1,
                "user": {"id": change.get("author", 17)},
                "state": change.get("state", "APPROVED"),
                "commit_id": change.get("commit_id", head),
            },
        ),
        review_decision=change.get("review_decision", "APPROVED"),
        threads=({"isResolved": change.get("thread", True)},),
    )
    calls = []
    monkeypatch.setattr(
        PublicationController, "inspect", lambda self, number: calls.append(number) or observed
    )
    facts = _approval(driver.gateway(saved), saved, unit)
    assert calls == [9] and facts[0].verified and facts[0].satisfied is satisfied
    assert facts[0].head == head and facts[0].hold_id == unit.hold.id
    observed.pr["head"]["sha"] = "foreign-head"
    with pytest.raises(CampaignError, match="another current candidate"):
        _approval(driver.gateway(saved), saved, unit)


def test_decision_receipt_atomically_queues_recoverable_private_ledger_and_safe_summaries(
    project, tmp_path
):
    secret = "private-chat-content-never-publish"
    ctx = context(sources=[source(content=secret, private=True)])
    accepted = validate_decision(artifact(ctx, answer=secret), ctx)
    saved = decision_snapshot(ctx)
    unit = saved.units["s:e:u"]
    unit.bindings.update(issue={"number": 7}, pr={"number": 9})
    store = CampaignStore(tmp_path / "authority")
    saved = store.create(saved)
    apply_decision(saved, unit.id, accepted)
    assert len(saved.outbox) == 1 and saved.outbox[0].kind == "decision-project"
    saved = store.save(saved, saved.revision)
    saved = store.load(saved.id)
    driver = CampaignRuntime(
        project.project, store, gateway=GithubGateway(Discussions(), "org/repo")
    )
    action = saved.outbox[0]
    result = driver._perform_decision_project(saved, action)
    raw = json.dumps(result.data)
    assert secret not in raw and "owner-note" not in raw
    ledger = store.directory(saved.id) / "decision-ledger.json"
    assert secret in ledger.read_text()
    driver.apply(saved, action, result)
    driver.apply(saved, action, result)
    comments = [row for row in saved.outbox if row.kind == "github"]
    assert len(comments) == 2 and all(
        row.payload["purpose"] == "decision-summary" for row in comments
    )
    assert secret not in json.dumps([row.payload for row in comments])
    ledger.unlink()
    assert driver._perform_decision_project(saved, action).data == result.data
    assert accepted.id in json.loads(ledger.read_text())["receipts"]
    latest = store.load(saved.id)
    latest.bindings["decision_receipts"]["later-decision"] = dataclasses.asdict(
        dataclasses.replace(accepted, id="later-decision")
    )
    latest = store.save(latest, latest.revision)
    driver._perform_decision_project(saved, action)  # an older effect snapshot
    projected = json.loads(ledger.read_text())
    assert projected["revision"] == latest.revision
    assert "later-decision" in projected["receipts"]


def test_actual_runtime_passes_current_approval_facts_before_and_after_session(
    project, tmp_path, monkeypatch
):
    from bmad_loop.campaign_runtime_decisions import PublicationController

    driver, saved, unit, service = runtime(project, tmp_path, hold="required_review")
    unit.bindings["pr"] = {"number": 9, "node_id": "pull-node"}
    pull = copy.deepcopy(service.item)
    pull.update(
        id=109,
        number=9,
        node_id="pull-node",
        state="open",
        html_url="https://github.com/org/repo/pull/9",
        head={"sha": unit.bindings["head"]},
        base={"ref": "main"},
    )
    service.items[9] = pull
    observed = SimpleNamespace(
        pr=pull,
        rules=RemoteRules((), True, False, 1, True, True, "rules"),
        reviews=(
            {
                "id": 500,
                "user": {"id": 17},
                "state": "APPROVED",
                "commit_id": unit.bindings["head"],
            },
        ),
        review_decision="APPROVED",
        threads=(),
    )
    monkeypatch.setattr(PublicationController, "inspect", lambda self, number: observed)
    saved = driver.store.save(saved, saved.revision)
    unit = saved.units[unit.id]
    action = OutboxAction(
        "context",
        "resolution-context",
        unit.id,
        {"mode": "decision", "hold_id": unit.hold.id, "head": unit.bindings["head"]},
        0,
    )
    result = driver._perform_resolution_context(saved, action)
    apply_context(saved, unit.id, result.data)
    launch = proposals(saved, project.project, candidate(), now=10)[0]
    prepare(saved, unit.id, launch.payload)
    packet = result.data["packet"]
    assert packet["facts"][0]["satisfied"]
    receipt = {
        "outcome": "stage_complete",
        "head": unit.bindings["head"],
        "artifact": {
            "schema_version": 1,
            "coordinates": packet["coordinates"],
            "hold_id": unit.hold.id,
            "disposition": "already_satisfied",
            "action": "continue",
            "answer": "Eligible current-head review recorded.",
            "fact_ids": [packet["facts"][0]["id"]],
            "support": [
                {"source_id": item["id"], "revision": item["revision"], "quote": item["content"]}
                for item in packet["sources"]
            ],
        },
    }
    coordinates = StageCoordinates(
        saved.id,
        unit.id,
        "decision_review",
        saved.graph_revision,
        saved.config_revision,
        unit.bindings["base"],
        unit.bindings["head"],
        "inventory",
    )
    request = {"coordinates": dataclasses.asdict(coordinates), "result_contract": "decision-v1"}
    driver._validate_stage_receipt(saved, unit, request, receipt)
    assert receipt["validated_resolution"]["accepted"]["clear_hold"]
    observed.review_decision = "REVIEW_REQUIRED"
    with pytest.raises(CampaignError, match="facts changed"):
        driver._validate_stage_receipt(saved, unit, request, receipt)
    assert unit.hold is not None


def test_incomplete_context_cannot_authorize_a_prior_answer_or_delegated_choice():
    ctx = dataclasses.replace(context(), unavailable=("mapped discussion coverage is partial",))
    with pytest.raises(CampaignError, match="coverage is incomplete"):
        validate_decision(artifact(ctx), ctx)
    waiting = validate_decision(artifact(ctx, disposition="needs_new_owner_decision"), ctx)
    assert not waiting.clear_hold


def test_planning_decision_uses_existing_owner_authority_without_checkpoint_bypass():
    ctx = context(kind="planning_decision")
    accepted = validate_decision(artifact(ctx), ctx)
    assert accepted.clear_hold and accepted.actual_actor == "owner-user-id"


def test_held_summary_waits_then_release_reconciles_same_comment_after_lost_reply(
    project, tmp_path
):
    ctx = context(sources=[source(private=True)])
    accepted = validate_decision(artifact(ctx), ctx)
    saved = decision_snapshot(ctx)
    unit = saved.units["s:e:u"]
    unit.bindings["issue"] = {"number": 7}
    store = CampaignStore(tmp_path / "authority")
    saved = store.create(saved)
    unit = saved.units[unit.id]
    apply_decision(saved, unit.id, accepted)
    unit.hold = Hold("operator", "pause automatic publication", id="takeover")
    saved = store.save(saved, saved.revision)
    unit = saved.units[unit.id]
    service = Discussions()
    service.lose_comment_reply = True
    driver = CampaignRuntime(project.project, store, gateway=GithubGateway(service, "org/repo"))
    projection = saved.outbox[0]
    observed = driver.perform(saved, projection)
    assert (
        observed.state == "confirmed"
        and (store.directory(saved.id) / "decision-ledger.json").is_file()
    )
    driver.apply(saved, projection, observed)
    comment = next(row for row in saved.outbox if row.kind == "github")
    saved = store.save(saved, saved.revision)
    before = copy.deepcopy(comment.payload["intent"])
    waiting = driver.perform(saved, comment)
    assert waiting.state == "waiting" and waiting.retry_at > driver.clock()
    assert not service.published and comment.payload["intent"] == before
    saved.units[unit.id].hold = None
    saved = store.save(saved, saved.revision)
    result = driver.perform(saved, comment)
    assert result.state == "unknown" and len(service.published) == 1
    comment.receipt, comment.state = result.data, "sent_unknown"
    reconciled = driver.reconcile_action(saved, comment)
    assert reconciled.state == "confirmed" and len(service.published) == 1
    assert reconciled.data["intent"]["action_id"] == before["action_id"]
