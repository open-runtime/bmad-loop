"""Authenticated, explicitly configured GitHub evidence for owner decisions.

Bindings use ``gh:issue:N``, ``gh:pull:N`` or
``gh:comment:ISSUE_NUMBER:COMMENT_ID``; ``@NUMERIC_AUTHOR_ID`` explicitly adopts
that account as authority. Repository ownership is otherwise verified from live
native OWNER association and the repository's numeric owner identity. Retrieved
words and model-produced actor labels never establish authorization.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from typing import Callable

from .campaign_decisions import EvidenceLocation, EvidenceSource
from .campaign_github import GithubError, GithubGateway
from .campaign_model import CampaignError

_LOCATION = re.compile(
    r"gh:(?:(issue|pull):([1-9][0-9]*)|comment:([1-9][0-9]*):([1-9][0-9]*))(?:@([1-9][0-9]*))?"
)


def controller_comment(snapshot, source: EvidenceSource, *, prior_projections=()) -> bool:
    """Recognize exact acknowledged controller text, never an account or marker.

    Explicitly configured comment sources have the same publication provenance
    boundary as discovered discussions. A new comment or an edited projection
    from the publishing account remains ordinary authenticated evidence.
    """
    matched = _LOCATION.fullmatch(source.identity)
    if source.kind != "github" or not matched or not matched.group(4):
        return False
    if not source.author_id.isdecimal():
        return False
    from .campaign_runtime_intake import _owned_projection

    observation = {
        "kind": "comment",
        "object_id": matched.group(4),
        "payload": {"body": source.content, "user": {"id": int(source.author_id)}},
    }
    return _owned_projection(snapshot, observation) or any(
        prior.repository == snapshot.repository
        and prior.common_dir == snapshot.common_dir
        and _owned_projection(prior, observation)
        for prior in prior_projections
    )


def configured_locations(
    sources: tuple[str, ...], *, scope: tuple[str, ...]
) -> tuple[EvidenceLocation, ...]:
    """Interpret owner-bound source identifiers, leaving local policy unchanged."""
    output = []
    for value in sources:
        if value.startswith("gh:"):
            matched = _LOCATION.fullmatch(value)
            if not matched:
                raise CampaignError("invalid GitHub decision source binding")
            output.append(
                EvidenceLocation(
                    "github:" + value,
                    "github",
                    value,
                    authority="owner",
                    scope=scope,
                    expected_author=matched.group(5) or "",
                )
            )
        else:
            output.append(
                EvidenceLocation(
                    "document:" + value,
                    "document",
                    value,
                    authority="adopted_policy",
                    scope=scope,
                )
            )
    return tuple(output)


def github_provider(gateway: GithubGateway) -> Callable[[EvidenceLocation], EvidenceSource]:
    """Return one bounded repository-scoped source, with refreshed native proof.

    Comments are fetched directly by native ID, then checked against their exact
    parent issue. Thread listings and text searches cannot silently select a
    different comment or borrow an unrelated repository's authority.
    """

    def retrieve(location: EvidenceLocation, *, retry_activity: bool = True) -> EvidenceSource:
        matched = _LOCATION.fullmatch(location.location)
        if location.kind != "github" or not matched:
            raise CampaignError("invalid GitHub decision provider identity")
        kind = matched.group(1) or "comment"
        number = int(matched.group(2) or matched.group(3))
        pinned_author = matched.group(5) or ""
        if location.expected_author != pinned_author:
            raise CampaignError("GitHub decision author pin differs from configured binding")
        try:
            parent_is_pull = False
            repository = gateway.get(gateway.root)
            if not isinstance(repository, dict) or type(repository.get("id")) is not int:
                raise CampaignError("GitHub repository evidence lacks native identity")
            if str(repository.get("full_name", "")).lower() != gateway.repository.lower():
                raise CampaignError("GitHub repository evidence identity changed")
            if kind == "comment":
                comment_id = int(matched.group(4))
                parent = gateway.issue(number)
                if (
                    not isinstance(parent, dict)
                    or parent.get("number") != number
                    or not parent.get("id")
                ):
                    raise CampaignError("GitHub comment parent identity missing")
                parent_is_pull = bool(parent.get("pull_request"))
                item = gateway.get(f"{gateway.root}/issues/comments/{comment_id}")
                if not isinstance(item, dict):
                    raise CampaignError("GitHub comment evidence is not an object")
                expected_parents = {
                    f"https://api.github.com/{gateway.root}/issues/{number}",
                    f"https://api.github.com/repositories/{repository.get('id')}/issues/{number}",
                }
                if item.get("id") != comment_id or item.get("issue_url") not in expected_parents:
                    raise CampaignError("GitHub comment native ID or parent repository changed")
            else:
                item = gateway.pull(number) if kind == "pull" else gateway.issue(number)
                if not isinstance(item, dict):
                    raise CampaignError("GitHub issue/pull evidence is not an object")
                if item.get("number") != number or (kind == "issue" and item.get("pull_request")):
                    raise CampaignError("GitHub issue/pull evidence kind or number changed")
            if type(item.get("id")) is not int or item["id"] <= 0:
                raise CampaignError("GitHub decision source lacks immutable numeric identity")
            user = item.get("user") or {}
            if (
                not isinstance(user, dict)
                or type(user.get("id")) is not int
                or user["id"] <= 0
                or not user.get("login")
            ):
                raise CampaignError("GitHub decision source lacks authenticated author identity")
            if pinned_author and str(user["id"]) != pinned_author:
                raise CampaignError(
                    "GitHub decision source author differs from adopted numeric pin"
                )
            stamp = str(item.get("updated_at", ""))
            content = item.get("body")
            if not isinstance(content, str):
                raise CampaignError("GitHub decision source has no text body")
            owner = repository.get("owner") or {}
            if not isinstance(owner, dict):
                raise CampaignError("GitHub repository owner evidence is not an object")
            native_owner = (
                item.get("author_association") == "OWNER"
                and owner.get("type") == "User"
                and type(owner.get("id")) is int
                and user["id"] == owner.get("id")
            )
            # REST preserves the original author when a maintainer edits a body.
            # Verify the current body and its actual editor through the Comment
            # interface before treating these words as that author's decision.
            node_id = item.get("node_id")
            if not isinstance(node_id, str) or not node_id:
                raise CampaignError("GitHub decision source lacks its native node ID")
            response = gateway.transport.request(
                "POST",
                "graphql",
                {
                    "query": "query($id:ID!){node(id:$id){id ... on Issue{title} ... on PullRequest{title} ... on Comment{body createdAt updatedAt lastEditedAt editor{__typename ... on User{databaseId}}}}}",
                    "variables": {"id": node_id},
                },
            )
            graph = response.data
            if response.status != 200 or not isinstance(graph, dict) or graph.get("errors"):
                raise CampaignError("GitHub decision current-editor evidence is unavailable")
            node = (graph.get("data") or {}).get("node")
            if (
                not isinstance(node, dict)
                or node.get("id") != node_id
                or node.get("body") != content
                or (kind != "comment" and node.get("title") != item.get("title"))
                or not node.get("createdAt")
                or node["createdAt"] != item.get("created_at")
            ):
                raise CampaignError("GitHub decision body changed between native observations")
            if node.get("updatedAt") != stamp:
                if retry_activity and node.get("updatedAt"):
                    return retrieve(location, retry_activity=False)
                raise CampaignError("GitHub decision activity changed between native observations")
            if "lastEditedAt" not in node or "editor" not in node:
                raise CampaignError("GitHub decision editor coverage is incomplete")

            def moment(value):
                if not isinstance(value, str):
                    raise CampaignError("GitHub decision source has no content timestamp")
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    raise CampaignError("GitHub decision content timestamp has no timezone")
                return parsed.timestamp()

            created = moment(node["createdAt"])
            edited = node["lastEditedAt"]
            content_time = created if edited is None else moment(edited)
            if not created <= content_time <= moment(stamp):
                raise CampaignError("GitHub decision timestamps disagree")
            editor = node["editor"]
            unedited_or_same_author = (edited is None and editor is None) or (
                edited is not None
                and isinstance(editor, dict)
                and editor.get("__typename") == "User"
                and type(editor.get("databaseId")) is int
                and editor["databaseId"] == user["id"]
            )
            authority_verified = (
                user.get("type") == "User"
                and bool(pinned_author or native_owner)
                and unedited_or_same_author
            )
            revision_fields = {
                "id": item["id"],
                "parent": number,
                "body": content,
                "title": item.get("title"),
                "user": {key: user.get(key) for key in ("id", "login", "type")},
                "association": item.get("author_association"),
                "owner": {key: owner.get(key) for key in ("id", "type")},
                "editor": node["editor"],
                "lastEditedAt": node["lastEditedAt"],
            }
            if kind == "comment":
                # Preserve stored native comment revision IDs across upgrades.
                revision_fields["updated_at"] = stamp
            else:
                # A parent's updated_at also advances for discussion activity.
                # Its individual comments carry their own identities and clocks.
                revision_fields.update(
                    created_at=node["createdAt"],
                    repository_id=repository["id"],
                    node_id=node_id,
                    state=item.get("state"),
                    state_reason=item.get("state_reason"),
                    draft=item.get("draft"),
                    locked=item.get("locked"),
                    milestone=(item.get("milestone") or {}).get("id"),
                    labels=sorted((label["id"], label["name"]) for label in item.get("labels", ())),
                    assignees=sorted(person["id"] for person in item.get("assignees", ())),
                )
                if kind == "pull":
                    for side in ("base", "head"):
                        ref = item.get(side) or {}
                        revision_fields[side] = [
                            ref.get("ref"),
                            ref.get("sha"),
                            (ref.get("repo") or {}).get("id"),
                        ]
            revision = hashlib.sha256(
                json.dumps(revision_fields, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
            base = f"https://github.com/{gateway.repository}/{'pull' if kind == 'pull' or parent_is_pull else 'issues'}/{number}"
            url = base + (f"#issuecomment-{item['id']}" if kind == "comment" else "")
            if str(item.get("html_url", "")).lower() != url.lower():
                raise CampaignError("GitHub decision source URL differs from its native binding")
            return EvidenceSource(
                location.id,
                location.kind,
                location.location,
                f"github:{item['id']}:{revision}",
                content,
                authority=location.authority,
                scope=location.scope,
                author_id=str(user["id"]),
                timestamp=content_time,
                origin_verified=authority_verified,
                private=bool(repository.get("private")) or location.private,
                url=url,
                approved_head=location.approved_head,
                release_hold=location.release_hold,
                supersedes=location.supersedes,
            )
        except (GithubError, ValueError, TypeError, KeyError, AttributeError) as exc:
            raise CampaignError(
                f"GitHub decision evidence unavailable: {type(exc).__name__}: {exc}"
            ) from exc

    return retrieve
