"""Preserve external Git authorship separately from campaign model telemetry.

These facts describe exact commits imported from an already-owned PR. Git names,
signatures and commit messages never establish a model family, owner authority or
review independence. The caller owns scope, ancestry, quiescence and fresh-review
checks; this module cannot clear campaign attribution debt or grant publication.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import os
import re
from pathlib import Path

from . import verify
from .campaign_model import CampaignError
from .campaign_store import add_event

MAX_COMMITS = 4096
MAX_COMMIT_BYTES = 8 * 1024 * 1024
_OID = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")


def _oid(value) -> str:
    if not isinstance(value, str) or _OID.fullmatch(value) is None:
        raise CampaignError("external contribution has an invalid commit identity")
    return value


def _git(workspace: Path, *args: str) -> bytes:
    result = verify.git_bytes(
        workspace,
        *args,
        env={
            **{key: value for key, value in os.environ.items() if not key.startswith("GIT_")},
            "GIT_NO_REPLACE_OBJECTS": "1",
        },
    )
    if result.returncode:
        raise CampaignError("external contribution Git observation failed: " + args[0])
    return result.stdout


def _identity(raw: bytes) -> dict:
    return {
        "display": raw.decode("utf-8", errors="replace"),
        "raw_base64": base64.b64encode(raw).decode("ascii"),
    }


def collect(
    workspace: Path,
    *,
    repository: str,
    pr_number: int,
    branch: str,
    local_before: str,
    remote_head: str,
    target_head: str,
    pr_node_id: str = "",
    provenance_before: str | None = None,
) -> dict:
    """Read every novel remote commit, or refuse before returning partial facts.

    IDs have a bounded sentinel query; each immutable commit's size is checked
    before reading its contents. The 4,096-commit/8-MiB cumulative limits produce
    explicit observation failure, never truncated or apparently empty provenance.
    History already present before the first observation, and accepted-target
    history, is not a newly imported writer. Keep that first checkpoint even if
    the worker already merged the observed remote head before becoming quiescent.
    """
    if (
        not isinstance(repository, str)
        or not repository
        or type(pr_number) is not int
        or pr_number <= 0
        or not isinstance(branch, str)
        or not branch
        or not isinstance(pr_node_id, str)
    ):
        raise CampaignError("external contribution has invalid PR coordinates")
    heads = tuple(_oid(value) for value in (local_before, remote_head, target_head))
    origin = _oid(provenance_before if provenance_before is not None else local_before)
    raw_ids = _git(
        workspace,
        "rev-list",
        "--topo-order",
        "--reverse",
        f"--max-count={MAX_COMMITS + 1}",
        heads[1],
        "--not",
        origin,
        heads[2],
        "--",
    )
    ids = raw_ids.decode("ascii").splitlines()
    if len(ids) > MAX_COMMITS:
        raise CampaignError("external contribution exceeds its commit-count bound")
    if len(set(ids)) != len(ids):
        raise CampaignError("external contribution commit observation contains duplicates")
    commits = []
    observed_bytes = 0
    for value in ids:
        sha = _oid(value)
        size_raw = _git(workspace, "cat-file", "-s", sha).strip()
        if not size_raw.isdigit():
            raise CampaignError("external contribution commit size is unavailable")
        size = int(size_raw)
        if size <= 0 or size > MAX_COMMIT_BYTES - observed_bytes:
            raise CampaignError("external contribution exceeds its commit-byte bound")
        raw = _git(workspace, "cat-file", "commit", sha)
        if len(raw) != size:
            raise CampaignError("external contribution commit size changed")
        header, separator, message = raw.partition(b"\n\n")
        fields = header.splitlines()
        authors = [line[7:] for line in fields if line.startswith(b"author ")]
        committers = [line[10:] for line in fields if line.startswith(b"committer ")]
        if not separator or len(authors) != 1 or len(committers) != 1:
            raise CampaignError("external contribution commit authorship is malformed")
        commits.append(
            {
                "sha": sha,
                "parents": [
                    _oid(line[7:].decode("ascii")) for line in fields if line.startswith(b"parent ")
                ],
                "author": _identity(authors[0]),
                "committer": _identity(committers[0]),
                "object_sha256": hashlib.sha256(raw).hexdigest(),
                "size_bytes": size,
                "message_sha256": hashlib.sha256(message).hexdigest(),
                "signature_present": any(line.startswith(b"gpgsig ") for line in fields),
                "signature_verification": "not_observed",
                "model_status": "unobserved_external",
                "observed_models": [],
                "provider_families": [],
            }
        )
        observed_bytes += size
    return {
        "schema_version": 1,
        "repository": repository,
        "pr_number": pr_number,
        "pr_node_id": pr_node_id,
        "branch": branch,
        "local_before": heads[0],
        "provenance_before": origin,
        "remote_head": heads[1],
        "target_head": heads[2],
        "history_selection": "remote-not-provenance-before-or-target",
        "complete": True,
        "commit_count": len(commits),
        "commit_bytes": observed_bytes,
        "commits": commits,
    }


def record(snapshot, unit, receipt: dict) -> bool:
    """Append exact external provenance without changing model attribution gates.

    Integration conflicts retain observations too: candidate_head then names the
    actual stopped candidate, not a claim that all three histories already merged.
    A same-proof replay is a no-op; changed proof bytes retain a separate record.
    """
    value = receipt.get("external_contribution")
    sync = receipt.get("remote_head_sync")
    pr = unit.bindings.get("pr", {})
    if not isinstance(value, dict) or not isinstance(sync, dict):
        raise CampaignError("external contribution lacks its exact remote-head binding")
    if (
        type(value.get("schema_version")) is not int
        or value.get("schema_version") != 1
        or value.get("complete") is not True
        or type(sync.get("schema_version")) is not int
        or sync.get("schema_version") != 1
        or not isinstance(sync.get("id"), str)
        or not sync["id"]
        or type(value.get("pr_number")) is not int
        or value["pr_number"] <= 0
        or not isinstance(value.get("branch"), str)
        or not value["branch"]
        or receipt.get("outcome") not in {"integrated", "integration_conflict"}
        or value.get("repository") != snapshot.repository
        or sync.get("repository") != snapshot.repository
        or value.get("pr_number") != pr.get("number")
        or sync.get("pr_number") != pr.get("number")
        or value.get("pr_node_id") != sync.get("pr_node_id")
        or (pr.get("node_id") and value.get("pr_node_id") != pr["node_id"])
        or value.get("branch") != unit.bindings.get("branch")
        or sync.get("branch") != unit.bindings.get("branch")
        or sync.get("target") != snapshot.spec.target
        or value.get("local_before") != sync.get("local_head")
        or value.get("provenance_before") != sync.get("provenance_before", sync.get("local_head"))
        or value.get("remote_head") != sync.get("head")
        or sync.get("graph_revision") != snapshot.graph_revision
        or sync.get("config_revision") != snapshot.config_revision
        or sync.get("source_revision") != unit.bindings.get("source_revision", "")
    ):
        raise CampaignError("external contribution differs from the owned PR coordinates")
    for key in ("local_before", "provenance_before", "remote_head", "target_head"):
        _oid(value.get(key))
    _oid(receipt.get("head"))
    if receipt.get("target_head") != value["target_head"]:
        raise CampaignError("external contribution target differs from integration evidence")
    commits = value.get("commits")
    if (
        not isinstance(commits, list)
        or value.get("history_selection") != "remote-not-provenance-before-or-target"
        or type(value.get("commit_count")) is not int
        or value["commit_count"] != len(commits)
        or len(commits) > MAX_COMMITS
        or type(value.get("commit_bytes")) is not int
        or not 0 <= value["commit_bytes"] <= MAX_COMMIT_BYTES
    ):
        raise CampaignError("external contribution coverage is incomplete or unbounded")
    seen = set()
    total = 0
    for row in commits:
        if not isinstance(row, dict):
            raise CampaignError("external contribution commit is not an object")
        sha = _oid(row.get("sha"))
        if (
            sha in seen
            or row.get("model_status") != "unobserved_external"
            or row.get("observed_models") != []
            or row.get("provider_families") != []
            or type(row.get("size_bytes")) is not int
            or row["size_bytes"] <= 0
        ):
            raise CampaignError("external Git facts cannot establish model attribution")
        if (
            not isinstance(row.get("parents"), list)
            or type(row.get("signature_present")) is not bool
            or row.get("signature_verification") != "not_observed"
            or not re.fullmatch(r"[0-9a-f]{64}", str(row.get("object_sha256", "")))
            or not re.fullmatch(r"[0-9a-f]{64}", str(row.get("message_sha256", "")))
        ):
            raise CampaignError("external contribution commit facts are malformed")
        for parent in row["parents"]:
            _oid(parent)
        for key in ("author", "committer"):
            identity = row.get(key)
            if not isinstance(identity, dict) or not isinstance(identity.get("raw_base64"), str):
                raise CampaignError("external contribution authorship bytes are missing")
            try:
                raw_identity = base64.b64decode(identity["raw_base64"], validate=True)
            except ValueError as exc:
                raise CampaignError("external contribution authorship bytes are invalid") from exc
            if identity.get("display") != raw_identity.decode("utf-8", errors="replace"):
                raise CampaignError("external contribution authorship display differs from bytes")
        seen.add(sha)
        total += row["size_bytes"]
    if total != value["commit_bytes"]:
        raise CampaignError("external contribution byte coverage differs")
    entry = {
        "schema_version": 1,
        "remote_head_sync": copy.deepcopy(sync),
        "provenance": copy.deepcopy(value),
        "candidate_head": receipt["head"],
        "launch_id": receipt.get("launch_id", ""),
        "outcome": receipt["outcome"],
        "independence_scope": "authenticated_campaign_and_imported_model_families",
        "external_model_independence": "unproven",
    }
    entry["id"] = hashlib.sha256(
        json.dumps(entry, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()
    previous = unit.evidence.get("external_contributions", [])
    if any(item == entry for item in previous):
        return False
    unit.evidence["external_contributions"] = [*previous, entry]
    add_event(
        snapshot,
        "external-contribution-preserved",
        unit=unit.id,
        contribution=entry["id"],
        remote_head=value["remote_head"],
        commits=len(commits),
        model_status="unobserved_external",
    )
    return True


def summary(unit) -> dict:
    """Bound public provenance by entry count; retain exact owned receipt links.

    Author bytes and private commit bodies stay in the original worker receipt.
    Counts cover the full ledger; omitted summary rows remain explicit. Repeated
    observations can mention the same commit, so distinct commits are counted by
    their exact identity rather than by the number of integration attempts.
    """
    entries = unit.evidence.get("external_contributions", [])
    debt = unit.evidence.get("remote_head_sync", {})
    commits = {
        row["sha"] for entry in entries for row in entry.get("provenance", {}).get("commits", [])
    }
    latest = []
    for entry in entries[-16:]:
        launch = entry.get("launch_id", "")
        receipt_path = (
            f"workers/{launch}/receipt.json"
            if isinstance(launch, str)
            and launch
            and launch not in {".", ".."}
            and "/" not in launch
            and "\\" not in launch
            else None
        )
        value = entry.get("provenance", {})
        latest.append(
            {
                "id": entry.get("id"),
                "candidate_head": entry.get("candidate_head"),
                "remote_head": value.get("remote_head"),
                "outcome": entry.get("outcome"),
                "commit_count": value.get("commit_count"),
                "launch_id": launch,
                "receipt": receipt_path,
                "model_status": "unobserved_external",
            }
        )
    return {
        "observations": len(entries),
        "distinct_commits": len(commits),
        "unobserved_external_model_commits": len(commits),
        "campaign_attribution_incomplete": bool(
            unit.evidence.get("developer_attribution_incomplete")
        ),
        "remote_head_changes": len(unit.evidence.get("remote_head_refreshes", [])),
        "pending": {
            key: debt[key]
            for key in ("id", "head", "local_head", "anchor", "provenance_before")
            if key in debt
        }
        or None,
        "hold_kind": unit.hold.kind if unit.hold else None,
        "recent": latest,
        "omitted_observations": max(0, len(entries) - len(latest)),
        "receipt_path_basis": "campaign_directory",
        "limitation": "Git provenance does not prove independence from unobserved external models.",
    }
