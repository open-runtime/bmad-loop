"""Durable GitHub intake observations and validated semantic-triage proposals.

Polling never admits backlog work by itself. A caller saves the returned batch
and cursor in one campaign transaction, then launches a bounded coding session
for semantic triage. Remote comments cannot supply policy or execution authority.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal

from .campaign_github import GithubGateway, JsonObject


@dataclass(frozen=True)
class IntakeObservation:
    repository: str
    object_id: str
    kind: str
    number: int
    updated_at: str
    digest: str
    author: str
    url: str
    payload: JsonObject

    @property
    def version_id(self) -> str:
        return f"{self.repository}:{self.kind}:{self.object_id}:{self.updated_at}:{self.digest}"

    def to_dict(self) -> JsonObject:
        return asdict(self)


@dataclass(frozen=True)
class IntakeBatch:
    observations: tuple[IntakeObservation, ...]
    next_cursor: str
    complete: bool
    pending_count: int


Disposition = Literal[
    "attach", "repair_current_pr", "new_story", "new_epic", "duplicate", "needs_information", "hold"
]


@dataclass(frozen=True)
class TriageProposal:
    schema_version: int
    disposition: Disposition
    input_versions: tuple[str, ...]
    unit_id: str
    rationale: str
    acceptance_criteria: tuple[str, ...]
    dependencies: tuple[str, ...] = ()
    related_issue: int | None = None
    related_pr: int | None = None
    within_scope: bool = False
    graph_revision: int = 0

    @classmethod
    def from_dict(cls, raw: JsonObject) -> TriageProposal:
        copied = dict(raw)
        for field in ("input_versions", "acceptance_criteria", "dependencies"):
            copied[field] = tuple(copied.get(field, ()))
        return cls(**copied)


def validate_triage(
    proposal: TriageProposal,
    observations: tuple[IntakeObservation, ...],
    *,
    graph_revision: int,
    known_units: set[str],
    required_review: bool = False,
    current_pr: int | None = None,
    allow_new_units: bool = False,
) -> None:
    if proposal.schema_version != 1 or proposal.graph_revision != graph_revision:
        raise ValueError("Triage proposal schema/graph revision is stale")
    kinds = {
        "attach",
        "repair_current_pr",
        "new_story",
        "new_epic",
        "duplicate",
        "needs_information",
        "hold",
    }
    if proposal.disposition not in kinds:
        raise ValueError("Unknown triage disposition")
    versions = {row.version_id for row in observations}
    if not proposal.input_versions or not set(proposal.input_versions).issubset(versions):
        raise ValueError("Triage input identities/revisions do not match durable observations")
    if not proposal.rationale.strip():
        raise ValueError("Triage disposition requires rationale")
    if required_review and (
        proposal.disposition != "repair_current_pr" or proposal.related_pr != current_pr
    ):
        raise ValueError("Required review fixes must repair their existing PR")
    if proposal.disposition in {"new_story", "new_epic"}:
        if not allow_new_units or not proposal.within_scope:
            raise ValueError("New work exceeds approved automatic-intake scope")
        if (
            not proposal.unit_id
            or proposal.unit_id in known_units
            or not proposal.acceptance_criteria
        ):
            raise ValueError("New work needs unique identity and actionable acceptance criteria")
    if (
        proposal.disposition in {"attach", "repair_current_pr"}
        and proposal.unit_id not in known_units
    ):
        raise ValueError("Triage attachment references an unknown unit")
    if set(proposal.dependencies) - known_units:
        raise ValueError("Triage proposal references unknown dependencies")


class GithubIntake:
    def __init__(self, gateway: GithubGateway, *, overlap_s: int = 120):
        self.gateway = gateway
        self.overlap_s = overlap_s

    def poll(
        self,
        *,
        cursor: str = "",
        seen_versions: set[str] | None = None,
        mode: str = "all-open-and-new",
        labels: tuple[str, ...] = (),
        selected_numbers: tuple[int, ...] = (),
        mapped_numbers: tuple[int, ...] = (),
        max_batch: int = 100,
        own_comment_markers: tuple[str, ...] = (),
        own_comment_author_ids: tuple[int, ...] = (),
    ) -> IntakeBatch:
        if max_batch < 1 or mode not in {"all-open-and-new", "selected"}:
            raise ValueError("Invalid intake batch size/mode")
        since = ""
        if cursor:
            instant = datetime.fromisoformat(cursor.replace("Z", "+00:00"))
            since = (
                (instant - timedelta(seconds=self.overlap_s))
                .astimezone(timezone.utc)
                .isoformat()
                .replace("+00:00", "Z")
            )
        rows = self.gateway.issues(state="all" if cursor else "open", since=since)
        selected = set(selected_numbers) | set(mapped_numbers)
        known_numbers = {row["number"] for row in rows}
        for number in sorted(selected - known_numbers):
            rows.append(self.gateway.issue(number))
        observations: list[IntakeObservation] = []
        known = seen_versions or set()
        next_cursor = cursor
        for row in sorted(rows, key=lambda item: (item.get("updated_at", ""), item["id"])):
            if "pull_request" in row:
                continue
            row_labels = {label["name"] for label in row.get("labels", [])}
            if (
                mode == "selected"
                and row["number"] not in selected
                and not row_labels.intersection(labels)
            ):
                continue
            observation = self._observation("issue", row, row["number"])
            if observation.version_id not in known:
                observations.append(observation)
            next_cursor = max(next_cursor, observation.updated_at)
        # Comments have independent IDs/versions; mapped PRs are legitimate issue endpoints.
        for number in sorted(set(mapped_numbers)):
            for comment in self.gateway.comments(number):
                if (comment.get("user") or {}).get("id") in own_comment_author_ids and any(
                    marker in str(comment.get("body", "")) for marker in own_comment_markers
                ):
                    continue
                observation = self._observation("comment", comment, number)
                if observation.version_id not in known:
                    observations.append(observation)
        observations.sort(key=lambda row: (row.updated_at, row.kind, row.object_id))
        batch = observations[:max_batch]
        # Never skip observations beyond the batch when persisting its cursor. The overlap
        # and durable version IDs replay them, including arbitrarily many equal timestamps.
        if len(observations) > max_batch:
            next_cursor = min(
                (row.updated_at for row in batch if row.kind == "issue"), default=cursor
            )
        return IntakeBatch(tuple(batch), next_cursor, True, max(0, len(observations) - len(batch)))

    def _observation(self, kind: str, row: JsonObject, number: int) -> IntakeObservation:
        if not row.get("id") or not row.get("updated_at"):
            raise ValueError("GitHub intake object has no durable identity/revision")
        encoded = json.dumps(row, sort_keys=True, separators=(",", ":"))
        return IntakeObservation(
            self.gateway.repository,
            str(row["id"]),
            kind,
            number,
            row["updated_at"],
            hashlib.sha256(encoded.encode()).hexdigest(),
            str((row.get("user") or {}).get("login", "")),
            str(row.get("html_url", "")),
            row,
        )
