"""GitHub campaign transport and recoverable mutations.

This gateway performs one bounded effect at a time. The caller persists an intent
*before* execute, and persists ``sent_unknown`` before handing it to an executor.
A failed create is reconciled, never blindly repeated. Remote text is data.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from collections.abc import Iterator
from dataclasses import asdict, dataclass, replace
from typing import Any, Literal, Protocol
from urllib.parse import urlencode

API_VERSION = "2026-03-10"
JsonObject = dict[str, Any]


class GithubError(RuntimeError):
    """An observation/effect failed; absence must not be inferred."""

    def __init__(self, message: str, *, status: int = 0, retry_after_s: float | None = None):
        super().__init__(message)
        self.status = status
        self.retry_after_s = retry_after_s


class UncertainMutation(GithubError):
    """The request may have reached GitHub. Reconcile before another effect."""


@dataclass(frozen=True)
class ApiResponse:
    status: int
    data: Any
    headers: dict[str, str]


class GithubTransport(Protocol):
    def request(self, method: str, path: str, payload: JsonObject | None = None) -> ApiResponse: ...


class GhTransport:
    """Authenticated gh CLI, no shell, credentials in output, or sleeping retries."""

    def __init__(self, *, timeout_s: float = 45, executable: str = "gh"):
        if timeout_s <= 0:
            raise ValueError("GitHub timeout must be positive")
        self.timeout_s = timeout_s
        self.executable = executable

    def request(self, method: str, path: str, payload: JsonObject | None = None) -> ApiResponse:
        if path.startswith("https://api.github.com/"):
            path = path.removeprefix("https://api.github.com/")
        if path.startswith(("-", "http:", "https:")) or "\n" in path:
            raise GithubError("Refusing a non-GitHub API path")
        argv = [
            self.executable,
            "api",
            "--include",
            "--method",
            method,
            "-H",
            "Accept: application/vnd.github+json",
            "-H",
            f"X-GitHub-Api-Version: {API_VERSION}",
            path,
        ]
        body = None
        if payload is not None:
            argv.extend(["--input", "-"])
            body = json.dumps(payload, ensure_ascii=False)
        try:
            result = subprocess.run(  # noqa: S603 — explicit argv, authenticated gh
                argv,
                input=body,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=self.timeout_s,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired, UnicodeError) as exc:
            error = UncertainMutation if method not in {"GET", "HEAD"} else GithubError
            raise error(f"GitHub {method} transport interrupted: {type(exc).__name__}") from exc
        output = result.stdout.replace("\r\n", "\n")
        header, separator, raw = output.partition("\n\n")
        match = re.match(r"HTTP/\S+ (\d{3})", header)
        if not separator or not match:
            error = UncertainMutation if method not in {"GET", "HEAD"} else GithubError
            raise error(f"GitHub {method} returned no parseable HTTP response")
        status = int(match.group(1))
        headers = {
            key.lower(): value.strip()
            for row in header.splitlines()[1:]
            if ":" in row
            for key, value in [row.split(":", 1)]
        }
        try:
            data = json.loads(raw) if raw.strip() else None
        except ValueError as exc:
            error = UncertainMutation if method not in {"GET", "HEAD"} else GithubError
            raise error("GitHub returned invalid JSON", status=status) from exc
        if status >= 400 and status != 409:
            retry = headers.get("retry-after")
            raise GithubError(
                f"GitHub {method} request refused (HTTP {status})",
                status=status,
                retry_after_s=float(retry) if retry and retry.isdigit() else None,
            )
        if result.returncode and status < 400:
            raise GithubError(f"gh failed after HTTP {status}", status=status)
        return ApiResponse(status, data, headers)


@dataclass(frozen=True)
class MutationIntent:
    action_id: str
    kind: str
    method: str
    path: str
    payload: JsonObject
    marker: str = ""
    state: Literal["prepared", "sent_unknown", "confirmed", "rejected"] = "prepared"
    receipt: JsonObject | None = None
    error: str = ""
    generation: int = 0
    coordinates: JsonObject | None = None
    retry_after_s: float | None = None

    def to_dict(self) -> JsonObject:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: JsonObject) -> MutationIntent:
        return cls(**value)


def managed_marker(campaign_id: str, unit_id: str, kind: str) -> str:
    """Stable opaque marker; user-supplied identities never become HTML."""
    key = json.dumps([campaign_id, unit_id, kind], separators=(",", ":"))
    return f"<!-- bmad-campaign:v1:{hashlib.sha256(key.encode()).hexdigest()} -->"


def managed_body(text: str, marker: str, links: tuple[str, ...] = ()) -> str:
    # No closing keywords: code merge must not close tracking before source sync.
    if re.search(r"(?i)\b(close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s+(?:[\w.-]+/[\w.-]+)?#\d+", text):
        raise ValueError(
            "Campaign bodies use reference links, not automatic issue closing keywords"
        )
    return "\n\n".join(part for part in (text.strip(), "\n".join(links), marker) if part)


class GithubGateway:
    def __init__(self, transport: GithubTransport, repository: str, *, max_pages: int = 1000):
        if not re.fullmatch(r"[\w.-]+/[\w.-]+", repository):
            raise ValueError("repository must be owner/name")
        if max_pages < 1:
            raise ValueError("max_pages must be positive")
        self.transport = transport
        self.repository = repository
        self.root = f"repos/{repository}"
        self.max_pages = max_pages

    def get(self, path: str) -> Any:
        response = self.transport.request("GET", path)
        if response.status != 200:
            raise GithubError(f"Incomplete GitHub observation: HTTP {response.status}")
        return response.data

    def iter_pages(self, path: str, *, collection: str | None = None) -> Iterator[list[JsonObject]]:
        """Complete pages, bounded by a visible limit; callers may stop at a scope boundary."""
        current = path + ("&" if "?" in path else "?") + "per_page=100"
        seen: set[str] = set()
        while current:
            if current in seen or len(seen) >= self.max_pages:
                raise GithubError("GitHub pagination incomplete or cyclic")
            seen.add(current)
            response = self.transport.request("GET", current)
            if response.status != 200:
                raise GithubError(f"GitHub page failed: HTTP {response.status}")
            try:
                data = response.data if collection is None else response.data[collection]
            except (KeyError, TypeError) as exc:
                raise GithubError("Unexpected GitHub collection schema") from exc
            if not isinstance(data, list) or any(not isinstance(row, dict) for row in data):
                raise GithubError("Unexpected GitHub list schema")
            yield data
            links = response.headers.get("link", "")
            next_page = re.search(r'<([^>]+)>;\s*rel="next"', links)
            current = next_page.group(1) if next_page else ""
            if current.startswith("https://api.github.com/"):
                current = current.removeprefix("https://api.github.com/")
            if current and not current.startswith(self.root + "/"):
                raise GithubError("Pagination escaped the selected repository")

    def pages(self, path: str, *, collection: str | None = None) -> list[JsonObject]:
        return [row for page in self.iter_pages(path, collection=collection) for row in page]

    def recent_pulls(self, since: str) -> list[JsonObject]:
        rows: list[JsonObject] = []
        for page in self.iter_pages(f"{self.root}/pulls?state=closed&sort=updated&direction=desc"):
            if any(not row.get("updated_at") for row in page):
                raise GithubError("Closed PR inventory has no updated-time coverage")
            rows.extend(row for row in page if row["updated_at"] >= since)
            if any(row["updated_at"] < since for row in page):
                break
        return rows

    def issues(self, *, state: str = "open", since: str = "") -> list[JsonObject]:
        query = {"state": state, "sort": "updated", "direction": "asc"}
        if since:
            query["since"] = since
        return [
            row
            for row in self.pages(f"{self.root}/issues?{urlencode(query)}")
            if "pull_request" not in row
        ]

    def pulls(self, *, state: str = "open") -> list[JsonObject]:
        return self.pages(
            f"{self.root}/pulls?{urlencode({'state': state, 'sort': 'updated', 'direction': 'desc'})}"
        )

    def pull(self, number: int) -> JsonObject:
        return self.get(f"{self.root}/pulls/{number}")

    def issue(self, number: int) -> JsonObject:
        return self.get(f"{self.root}/issues/{number}")

    def comments(self, number: int) -> list[JsonObject]:
        return self.pages(f"{self.root}/issues/{number}/comments")

    def reviews(self, number: int) -> list[JsonObject]:
        return self.pages(f"{self.root}/pulls/{number}/reviews")

    def files(self, number: int) -> list[JsonObject]:
        return self.pages(f"{self.root}/pulls/{number}/files")

    def review_threads(self, number: int) -> list[JsonObject]:
        owner, name = self.repository.split("/", 1)
        cursor: str | None = None
        rows: list[JsonObject] = []
        for _ in range(self.max_pages):
            query = """query($owner:String!,$name:String!,$number:Int!,$cursor:String){repository(owner:$owner,name:$name){pullRequest(number:$number){reviewThreads(first:100,after:$cursor){nodes{id isResolved isOutdated path line comments(first:100){nodes{id body author{login} createdAt url} pageInfo{hasNextPage endCursor}}} pageInfo{hasNextPage endCursor}}}}}"""
            response = self.transport.request(
                "POST",
                "graphql",
                {
                    "query": query,
                    "variables": {"owner": owner, "name": name, "number": number, "cursor": cursor},
                },
            )
            if response.status != 200 or response.data.get("errors"):
                raise GithubError("GitHub review-thread observation failed")
            try:
                page = response.data["data"]["repository"]["pullRequest"]["reviewThreads"]
                nodes = page["nodes"]
                for node in nodes:
                    if node["comments"]["pageInfo"]["hasNextPage"]:
                        self._expand_thread(node)
                rows.extend(nodes)
                if not page["pageInfo"]["hasNextPage"]:
                    return rows
                next_cursor = page["pageInfo"]["endCursor"]
                if not next_cursor or next_cursor == cursor:
                    raise GithubError("Review thread cursor did not advance")
                cursor = next_cursor
            except (KeyError, TypeError) as exc:
                raise GithubError("Missing review-thread coverage") from exc
        raise GithubError("Review-thread pagination exceeded limit")

    def _expand_thread(self, node: JsonObject) -> None:
        page = node["comments"]
        for _ in range(self.max_pages):
            if not page["pageInfo"]["hasNextPage"]:
                return
            cursor = page["pageInfo"].get("endCursor")
            if not cursor:
                raise GithubError("Review comment cursor missing")
            query = "query($id:ID!,$cursor:String){node(id:$id){... on PullRequestReviewThread{comments(first:100,after:$cursor){nodes{id body author{login} createdAt url} pageInfo{hasNextPage endCursor}}}}}"
            response = self.transport.request(
                "POST",
                "graphql",
                {"query": query, "variables": {"id": node["id"], "cursor": cursor}},
            )
            if response.status != 200 or response.data.get("errors"):
                raise GithubError("Review comment pagination failed")
            try:
                page = response.data["data"]["node"]["comments"]
                if page["pageInfo"]["hasNextPage"] and page["pageInfo"].get("endCursor") == cursor:
                    raise GithubError("Review comment cursor did not advance")
                node["comments"]["nodes"].extend(page["nodes"])
            except (KeyError, TypeError) as exc:
                raise GithubError("Review comment coverage incomplete") from exc
        raise GithubError("Review comment pagination exceeded limit")

    def prepare_create(
        self,
        kind: str,
        *,
        action_id: str,
        campaign_id: str,
        unit_id: str,
        title: str,
        body: str,
        branch: str = "",
        target: str = "main",
        milestone: int | None = None,
        generation: int = 0,
    ) -> MutationIntent:
        marker = managed_marker(campaign_id, unit_id, kind)
        payload: JsonObject = {"title": title, "body": managed_body(body, marker)}
        if kind == "pr":
            if not branch:
                raise ValueError("PR requires a bound branch")
            path = f"{self.root}/pulls"
            payload.update(head=branch, base=target, draft=True)
        elif kind == "issue":
            path = f"{self.root}/issues"
            if milestone is not None:
                payload["milestone"] = milestone
        elif kind == "milestone":
            path = f"{self.root}/milestones"
            payload["description"] = payload.pop("body")
        else:
            raise ValueError(f"Unsupported create kind: {kind}")
        return MutationIntent(action_id, kind, "POST", path, payload, marker, generation=generation)

    def execute(self, intent: MutationIntent, *, generation: int) -> MutationIntent:
        if generation != intent.generation:
            raise GithubError("Stale coordinator generation cannot mutate GitHub")
        if intent.state != "sent_unknown":
            raise GithubError("Persist sent_unknown before executing an outbound action")
        if intent.coordinates and "updated_at" in intent.coordinates:
            current = self.get(intent.path)
            if (
                current.get("id") != intent.coordinates.get("id")
                or current.get("updated_at") != intent.coordinates["updated_at"]
            ):
                raise GithubError(
                    "Remote shared field changed; reconcile human edits before mutation"
                )
        try:
            response = self.transport.request(intent.method, intent.path, intent.payload)
        except UncertainMutation as exc:
            return replace(intent, error=str(exc))
        except GithubError as exc:
            # 5xx/rate limit may follow acceptance at an intermediary. Retain uncertainty.
            if exc.status in {400, 401, 404, 405, 410, 422}:
                return replace(intent, state="rejected", error=str(exc))
            return replace(intent, error=str(exc), retry_after_s=exc.retry_after_s)
        if response.status not in {200, 201, 202, 204, 409}:
            return replace(intent, error=f"Unexpected effect response: HTTP {response.status}")
        receipt = response.data if isinstance(response.data, dict) else {"value": response.data}
        if intent.path == "graphql" and receipt.get("errors"):
            return replace(intent, error="GitHub GraphQL mutation returned errors", receipt=receipt)
        if intent.kind in {"ready", "draft"}:
            operation = (
                "convertPullRequestToDraft"
                if intent.kind == "draft"
                else "markPullRequestReadyForReview"
            )
            observed = (receipt.get("data") or {}).get(operation, {}).get("pullRequest", {})
            expected = intent.coordinates or {}
            if observed.get("id") != expected.get("node_id") or observed.get(
                "isDraft"
            ) is not expected.get("draft"):
                return replace(
                    intent,
                    error="Draft transition did not confirm the expected native PR",
                    receipt=receipt,
                )
        if intent.kind == "merge":
            details = receipt.get("details") or {}
            if (
                details.get("bypass_rules") is True
                or details.get("expected_head_sha", intent.payload["sha"]) != intent.payload["sha"]
            ):
                return replace(
                    intent,
                    error="Existing merge request has conflicting head or bypass settings",
                    receipt=receipt,
                )
            if receipt.get("status") == "pending":
                option_keys = ("expected_head_sha", "merge_action", "bypass_rules")
                if response.status == 409 and any(key not in details for key in option_keys):
                    return replace(
                        intent, error="Existing request options are incomplete", receipt=receipt
                    )
                if details.get(
                    "merge_action", intent.payload.get("merge_action")
                ) != intent.payload.get("merge_action") or details.get(
                    "merge_method", intent.payload.get("merge_method")
                ) != intent.payload.get(
                    "merge_method"
                ):
                    return replace(
                        intent, error="Existing merge request options differ", receipt=receipt
                    )
            if receipt.get("status") not in {"pending", "merged", "enqueued"}:
                return replace(
                    intent, error="Merge acknowledgement lacks a recognized status", receipt=receipt
                )
            if receipt.get("status") == "pending" and not details.get("uuid"):
                return replace(
                    intent, error="Pending merge acknowledgement lacks a UUID", receipt=receipt
                )
        return replace(
            intent, state="confirmed", receipt={"http_status": response.status, **receipt}, error=""
        )

    def reconcile_create(self, intent: MutationIntent) -> MutationIntent:
        if intent.state == "confirmed":
            return intent
        if intent.kind == "issue":
            candidates = self.issues(state="all")
        elif intent.kind == "pr":
            candidates = self.pulls(state="all")
        elif intent.kind == "milestone":
            candidates = self.pages(f"{self.root}/milestones?state=all")
        elif intent.kind == "comment":
            candidates = self.pages(intent.path)
        else:
            raise GithubError("Action has no create reconciliation strategy")
        matches = [
            row
            for row in candidates
            if intent.marker
            and intent.marker in str(row.get("body") or row.get("description") or "")
        ]
        if len(matches) != 1:
            return replace(
                intent,
                state="sent_unknown",
                error="Create remains uncertain" if not matches else "Conflicting marker matches",
            )
        found = matches[0]
        if intent.kind == "pr" and (
            found.get("head", {}).get("ref") != intent.payload.get("head")
            or found.get("base", {}).get("ref") != intent.payload.get("base")
        ):
            return replace(
                intent, state="sent_unknown", error="Marked PR has conflicting branch coordinates"
            )
        if found.get("title") != intent.payload.get("title") and intent.kind != "comment":
            return replace(
                intent,
                state="sent_unknown",
                error="Marked object's title conflicts; human change requires reconciliation",
            )
        return replace(intent, state="confirmed", receipt=found, error="")

    def prepare_comment(
        self,
        number: int,
        *,
        action_id: str,
        campaign_id: str,
        unit_id: str,
        text: str,
        generation: int = 0,
    ) -> MutationIntent:
        marker = managed_marker(campaign_id, unit_id, "transition:" + action_id)
        return MutationIntent(
            action_id,
            "comment",
            "POST",
            f"{self.root}/issues/{number}/comments",
            {"body": managed_body(text, marker)},
            marker,
            generation=generation,
        )

    def prepare_subissue(
        self,
        parent_number: int,
        child_number: int,
        *,
        action_id: str,
        child_database_id: int,
        generation: int = 0,
    ) -> MutationIntent:
        if self.issue(child_number).get("id") != child_database_id:
            raise GithubError("Subissue database identity changed")
        try:
            parent = self.get(f"{self.root}/issues/{child_number}/parent")
        except GithubError as exc:
            if exc.status != 404:
                raise
            parent = None  # The child itself was independently read above.
        if parent and parent.get("number") != parent_number:
            raise GithubError("Refusing to reparent existing human work")
        # Listing children also handles a lost response and prevents silent reparenting.
        children = self.pages(f"{self.root}/issues/{parent_number}/sub_issues")
        if any(child.get("id") == child_database_id for child in children):
            return MutationIntent(
                action_id,
                "subissue",
                "POST",
                "",
                {},
                state="confirmed",
                receipt={"parent": parent_number, "child_id": child_database_id},
                generation=generation,
            )
        return MutationIntent(
            action_id,
            "subissue",
            "POST",
            f"{self.root}/issues/{parent_number}/sub_issues",
            {"sub_issue_id": child_database_id},
            generation=generation,
            coordinates={"child_number": child_number},
        )

    def prepare_ready(
        self, node_id: str, *, action_id: str, generation: int, draft: bool = False
    ) -> MutationIntent:
        operation = "convertPullRequestToDraft" if draft else "markPullRequestReadyForReview"
        field = "ConvertPullRequestToDraftInput" if draft else "MarkPullRequestReadyForReviewInput"
        query = (
            f"mutation($input:{field}!){{{operation}(input:$input){{pullRequest{{id isDraft}}}}}}"
        )
        return MutationIntent(
            action_id,
            "draft" if draft else "ready",
            "POST",
            "graphql",
            {"query": query, "variables": {"input": {"pullRequestId": node_id}}},
            generation=generation,
            coordinates={"node_id": node_id, "draft": draft},
        )

    def ready(self, node_id: str, *, draft: bool = False) -> JsonObject:
        operation = "convertPullRequestToDraft" if draft else "markPullRequestReadyForReview"
        field = "ConvertPullRequestToDraftInput" if draft else "MarkPullRequestReadyForReviewInput"
        query = (
            f"mutation($input:{field}!){{{operation}(input:$input){{pullRequest{{id isDraft}}}}}}"
        )
        response = self.transport.request(
            "POST", "graphql", {"query": query, "variables": {"input": {"pullRequestId": node_id}}}
        )
        if response.status != 200 or response.data.get("errors"):
            raise UncertainMutation("Draft transition requires read-back")
        return response.data["data"][operation]["pullRequest"]

    def close_issue_intent(
        self, number: int, *, action_id: str, generation: int, source_confirmed: bool
    ) -> MutationIntent:
        if not source_confirmed:
            raise GithubError("Canonical source publication must precede issue closure")
        issue = self.issue(number)
        if issue.get("state") == "closed":
            return MutationIntent(
                action_id,
                "close_issue",
                "PATCH",
                "",
                {},
                state="confirmed",
                receipt=issue,
                generation=generation,
            )
        return MutationIntent(
            action_id,
            "close_issue",
            "PATCH",
            f"{self.root}/issues/{number}",
            {"state": "closed", "state_reason": "completed"},
            generation=generation,
            coordinates={"id": issue["id"], "updated_at": issue["updated_at"]},
        )

    def reconcile_update(self, intent: MutationIntent) -> MutationIntent:
        """Read desired fields back; unknown outcomes never erase human changes."""
        if intent.state == "confirmed":
            return intent
        current = self.get(intent.path)
        if intent.coordinates and current.get("id") != intent.coordinates.get(
            "id", current.get("id")
        ):
            return replace(intent, state="sent_unknown", error="Remote object identity changed")
        if all(current.get(key) == value for key, value in intent.payload.items()):
            return replace(intent, state="confirmed", receipt=current, error="")
        return replace(
            intent,
            state="sent_unknown",
            error="Desired update not observed; explicit revalidation required before retry",
        )

    def milestone_intent(
        self, number: int, milestone: int, *, action_id: str, generation: int
    ) -> MutationIntent:
        issue = self.issue(number)
        current = issue.get("milestone")
        if current and current.get("number") != milestone:
            raise GithubError("Existing milestone membership requires explicit migration")
        payload = {"milestone": milestone}
        return MutationIntent(
            action_id,
            "milestone_assignment",
            "PATCH",
            f"{self.root}/issues/{number}",
            payload,
            generation=generation,
            coordinates={"id": issue["id"], "updated_at": issue["updated_at"]},
        )

    def close_milestone_intent(
        self,
        number: int,
        *,
        action_id: str,
        generation: int,
        required_units_delivered: bool,
        managed_issue_ids: set[int],
    ) -> MutationIntent:
        if not required_units_delivered:
            raise GithubError("Milestone closure requires delivery of its complete required scope")
        milestone = self.get(f"{self.root}/milestones/{number}")
        members = self.pages(f"{self.root}/issues?state=open&milestone={number}")
        if members:
            unmanaged = [row["id"] for row in members if row["id"] not in managed_issue_ids]
            raise GithubError(
                "Milestone still has "
                + ("unmanaged open items" if unmanaged else "open tracking items")
            )
        return MutationIntent(
            action_id,
            "close_milestone",
            "PATCH",
            f"{self.root}/milestones/{number}",
            {"state": "closed"},
            generation=generation,
            coordinates={"id": milestone["id"], "updated_at": milestone["updated_at"]},
        )
