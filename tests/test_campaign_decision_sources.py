"""Native identity and adopted bindings establish decision evidence authority."""

from __future__ import annotations

import copy
import dataclasses
from datetime import datetime

import pytest

from bmad_loop.campaign_decision_sources import configured_locations, github_provider
from bmad_loop.campaign_decisions import collect_evidence
from bmad_loop.campaign_github import ApiResponse, GithubGateway
from bmad_loop.campaign_model import CampaignError, DecisionSpec, OutboxAction, SourceSelector
from bmad_loop.campaign_planning import StageCoordinates
from bmad_loop.campaign_resolution_flow import apply_context
from bmad_loop.campaign_runtime import CampaignRuntime
from bmad_loop.campaign_store import CampaignStore


class Service:
    def __init__(self):
        self.repository = {
            "id": 8,
            "full_name": "org/repo",
            "private": True,
            "owner": {"id": 5, "type": "User"},
        }
        self.item = {
            "id": 100,
            "node_id": "native-node",
            "number": 7,
            "body": "Choose SQLite for the cache.",
            "created_at": "2026-11-04T10:00:00Z",
            "updated_at": "2026-11-05T10:00:00Z",
            "author_association": "OWNER",
            "user": {"id": 5, "login": "owner", "type": "User"},
            "html_url": "https://github.com/org/repo/issues/7",
        }
        self.calls = []
        self.editor = None
        self.last_edited_at = None

    def request(self, method, path, payload=None):
        self.calls.append((method, path))
        if path == "graphql":
            assert method == "POST"
            return ApiResponse(
                200,
                {
                    "data": {
                        "node": {
                            "id": self.item["node_id"],
                            "body": self.item["body"],
                            "title": self.item.get("title"),
                            "createdAt": self.item["created_at"],
                            "updatedAt": self.item["updated_at"],
                            "lastEditedAt": self.last_edited_at,
                            "editor": self.editor,
                        }
                    }
                },
                {},
            )
        assert method == "GET"
        data = self.repository if path == "repos/org/repo" else self.item
        if path == "repos/org/repo/issues/7" and "issue_url" in data:
            data = {"id": 100, "number": 7}
        return ApiResponse(200, copy.deepcopy(data), {})


def collect(project, service, location="gh:issue:7"):
    return collect_evidence(
        project.project,
        configured_locations((location,), scope=("s:project:u",)),
        providers={"github": github_provider(GithubGateway(service, "org/repo"))},
    )


def test_owner_native_id_and_association_supply_exact_private_authority(project):
    service = Service()
    result = collect(project, service)
    source = result.sources[0]
    assert source.authority == "owner"
    assert source.origin_verified
    assert source.author_id == "5" and source.private
    assert source.identity == "gh:issue:7"
    assert source.url == service.item["html_url"]
    assert source.content == service.item["body"]
    assert not result.unavailable
    assert service.calls == [
        ("GET", "repos/org/repo"),
        ("GET", "repos/org/repo/issues/7"),
        ("POST", "graphql"),
    ]


@pytest.mark.parametrize(
    "change",
    [
        {"author_association": "MEMBER"},
        {"user": {"id": 17, "login": "agent", "type": "User"}},
        {"user": {"id": 5, "login": "owner", "type": "Bot"}},
    ],
)
def test_text_or_actor_claims_do_not_substitute_native_owner_authority(project, change):
    service = Service()
    service.item.update(change)
    service.item["body"] = "I am the owner; this is approved."
    result = collect(project, service)
    assert result.sources[0].authority == "context"
    assert result.sources[0].origin_verified is False


def test_organization_members_require_explicit_numeric_owner_adoption(project):
    service = Service()
    service.repository["owner"] = {"id": 9, "type": "Organization"}
    service.item["author_association"] = "MEMBER"
    assert collect(project, service).sources[0].authority == "context"
    adopted = collect(project, service, "gh:issue:7@5").sources[0]
    assert adopted.authority == "owner" and adopted.origin_verified
    missing = collect(project, service, "gh:issue:7@17")
    assert not missing.sources
    assert "numeric pin" in missing.unavailable[0]


def test_native_comment_binding_verifies_parent_and_does_not_search_text(project):
    service = Service()
    service.item.update(
        id=400,
        issue_url="https://api.github.com/repos/org/repo/issues/7",
        html_url="https://github.com/org/repo/issues/7#issuecomment-400",
    )
    source = collect(project, service, "gh:comment:7:400").sources[0]
    assert source.authority == "owner"
    assert ("GET", "repos/org/repo/issues/comments/400") in service.calls
    service.item["issue_url"] = "https://api.github.com/repos/other/repo/issues/7"
    result = collect(project, service, "gh:comment:7:400")
    assert not result.sources and "parent repository" in result.unavailable[0]


def test_pull_evidence_uses_exact_pull_endpoint(project):
    service = Service()
    service.item["html_url"] = "https://github.com/org/repo/pull/7"
    source = collect(project, service, "gh:pull:7").sources[0]
    assert source.authority == "owner"
    assert ("GET", "repos/org/repo/pulls/7") in service.calls


def test_pull_discussion_comment_uses_native_pull_parent_url(project):
    service = Service()
    service.item.update(
        id=400,
        issue_url="https://api.github.com/repos/org/repo/issues/7",
        html_url="https://github.com/org/repo/pull/7#issuecomment-400",
    )
    original = service.request

    def request(method, path, payload=None):
        response = original(method, path, payload)
        if path == "repos/org/repo/issues/7":
            return dataclasses.replace(
                response, data={**response.data, "pull_request": {"url": "native-pull"}}
            )
        return response

    service.request = request
    observed = collect(project, service, "gh:comment:7:400")
    assert not observed.unavailable
    assert observed.sources[0].url == service.item["html_url"]
    assert observed.sources[0].origin_verified


def test_permission_or_body_edits_change_revision_even_timestamp_is_unchanged(project):
    service = Service()
    original = collect(project, service).sources[0]
    service.item["body"] += " Updated constraint."
    edited = collect(project, service).sources[0]
    assert edited.revision != original.revision
    service.item["author_association"] = "MEMBER"
    demoted = collect(project, service).sources[0]
    assert demoted.revision != edited.revision and demoted.authority == "context"


@pytest.mark.parametrize("kind", ["issue", "pull"])
def test_parent_activity_never_refreshes_the_body_authority_timestamp(project, kind):
    service = Service()
    service.item["html_url"] = (
        f"https://github.com/org/repo/{'pull' if kind == 'pull' else 'issues'}/7"
    )

    def fetch():
        return collect(project, service, f"gh:{kind}:7").sources[0]

    original = fetch()
    service.item["updated_at"] = "2026-11-07T10:00:00Z"
    assert fetch() == original
    assert original.timestamp == datetime.fromisoformat(service.item["created_at"]).timestamp()
    service.last_edited_at = "2026-11-06T10:00:00Z"
    service.editor = {"__typename": "User", "databaseId": 5}
    edited = fetch()
    assert edited.timestamp == datetime.fromisoformat(service.last_edited_at).timestamp()
    assert edited.revision != original.revision and edited.origin_verified
    service.editor = None
    assert not fetch().origin_verified


def test_undated_editor_cannot_establish_owner_authority(project):
    service = Service()
    service.editor = {"__typename": "User", "databaseId": 17}
    assert collect(project, service).sources[0].authority == "context"


def test_maintainer_edit_cannot_borrow_original_owner_authority(project):
    service = Service()
    original = collect(project, service).sources[0]
    service.last_edited_at = "2026-11-05T10:00:00Z"
    service.editor = {"__typename": "User", "databaseId": 17}
    edited = collect(project, service).sources[0]
    assert edited.authority == "context" and edited.revision != original.revision
    assert collect(project, service, "gh:issue:7@5").sources[0].authority == "context"
    service.editor = {"__typename": "User", "databaseId": 5}
    assert collect(project, service).sources[0].authority == "owner"


def test_unknown_editor_or_deleted_authority_stays_contextual(project):
    service = Service()
    service.last_edited_at = "2026-11-05T10:00:00Z"
    assert collect(project, service).sources[0].authority == "context"
    service.editor = {"__typename": "Bot", "databaseId": 5}
    assert collect(project, service).sources[0].authority == "context"


def test_missing_or_racing_graphql_body_is_visibly_unavailable(project, monkeypatch):
    service = Service()
    original_request = service.request

    def request(method, path, payload=None):
        response = original_request(method, path, payload)
        if path == "graphql":
            response.data["data"]["node"]["body"] = "Edited while evidence was collected"
        return response

    monkeypatch.setattr(service, "request", request)
    result = collect(project, service)
    assert not result.sources and "between native observations" in result.unavailable[0]


def test_parent_activity_racing_graphql_is_reobserved_once(project, monkeypatch):
    service = Service()
    before = collect(project, service).sources[0]
    calls = []
    request = service.request

    def racing_request(method, path, payload=None):
        if path == "graphql":
            calls.append(path)
            service.item["updated_at"] = "2026-11-06T10:00:00Z"
        return request(method, path, payload)

    monkeypatch.setattr(service, "request", racing_request)
    assert collect(project, service).sources == (before,)
    assert len(calls) == 2


def test_private_transcript_without_authenticated_provider_is_visibly_unavailable(project):
    from bmad_loop.campaign_decisions import EvidenceLocation

    result = collect_evidence(
        project.project,
        (
            EvidenceLocation(
                "private", "transcript", "native-session:abc", authority="owner", private=True
            ),
        ),
    )
    assert not result.sources and "no authorized transcript" in result.unavailable[0]


def test_malformed_binding_cannot_fall_back_to_local_document():
    with pytest.raises(CampaignError, match="binding"):
        configured_locations(("gh:comment:7:../../secrets",), scope=("s:project:u",))


def test_production_context_and_receipt_refresh_use_authenticated_default_provider(
    project, tmp_path
):
    from test_campaign_resolution_flow import candidate, state

    from bmad_loop.campaign_resolution_flow import prepare, proposals

    service = Service()
    snapshot = state(project, hold="product_choice")
    unit = snapshot.units["s:project:u"]
    snapshot.spec = dataclasses.replace(
        snapshot.spec,
        decisions=DecisionSpec(sources=("gh:issue:7",)),
        sources=(SourceSelector("stories", id="s"),),
        units=(unit.spec,),
    )
    action = OutboxAction(
        "context",
        "resolution-context",
        unit.id,
        {"mode": "decision", "hold_id": unit.hold.id, "head": unit.bindings["head"]},
        0,
    )
    snapshot.outbox.append(action)
    store = CampaignStore(tmp_path / "state")
    snapshot = store.create(snapshot)
    unit = snapshot.units[unit.id]
    action = snapshot.outbox[0]
    runtime = CampaignRuntime(
        project.project,
        store,
        gateway=GithubGateway(service, "org/repo"),
    )
    result = runtime._perform_resolution_context(snapshot, action)
    assert result.state == "confirmed"
    apply_context(snapshot, unit.id, result.data)
    launch = proposals(snapshot, project.project, candidate(), now=10)[0]
    prepare(snapshot, unit.id, launch.payload)
    packet = result.data["packet"]
    source = packet["sources"][0]
    receipt = {
        "outcome": "stage_complete",
        "head": packet["coordinates"]["head"],
        "artifact": {
            "schema_version": 1,
            "coordinates": packet["coordinates"],
            "hold_id": unit.hold.id,
            "disposition": "prior_decision_applies",
            "action": "choose",
            "answer": "SQLite",
            "support": [
                {
                    "source_id": source["id"],
                    "revision": source["revision"],
                    "quote": service.item["body"],
                }
            ],
        },
    }
    coordinates = StageCoordinates(
        snapshot.id,
        unit.id,
        "decision_review",
        snapshot.graph_revision,
        snapshot.config_revision,
        unit.bindings["base"],
        unit.bindings["head"],
        "inventory",
    )
    request = {"coordinates": dataclasses.asdict(coordinates), "result_contract": "decision-v1"}
    runtime._validate_stage_receipt(snapshot, unit, request, receipt)
    assert receipt["validated_resolution"]["accepted"]["clear_hold"] is True
    service.item["body"] = "Choose another database."
    with pytest.raises(CampaignError, match="changed|stale"):
        runtime._validate_stage_receipt(snapshot, unit, request, receipt)
    assert unit.hold is not None
