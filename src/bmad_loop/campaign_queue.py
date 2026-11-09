"""Authenticated merge-group observation, isolated G verification and checks.

GitHub owns queue ordering. A queue entry must supply a distinct synthetic head;
the ordinary PR head is never substituted for it. Local verification receipts
cannot complete a hosted check: the required app's exact G check is independently
read back, and lost check-creation acknowledgements are reconciled by external ID.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable

from . import verify
from .campaign_github import GithubError, GithubGateway, MutationIntent
from .campaign_merge_adapter import AdapterReceipt, verify_queue_candidate
from .campaign_model import GitHubSpec
from .campaign_publication import Candidate, RemoteRules
from .childrun import run_argv
from .platform_util import atomic_write_text, is_link_like


@dataclass(frozen=True)
class QueueGroup:
    repository: str
    pr_number: int
    target: str
    entry_id: str
    head: str
    base: str
    pr_head: str
    state: str
    observed_at: float

    @property
    def digest(self) -> str:
        # Observation time is freshness, not the identity of an unchanged group.
        value = asdict(self)
        value.pop("observed_at")
        value.pop("state")
        return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


@dataclass(frozen=True)
class CheckCapability:
    ready: bool
    repository: str
    context: str
    app_id: int
    mode: str
    observed_at: float
    reason: str = ""
    producer: str = ""
    rules_fingerprint: str = ""


def observe_group(
    gateway: GithubGateway, candidate: Candidate, *, clock: Callable[[], float] = time.time
) -> QueueGroup | None:
    owner, name = gateway.repository.split("/", 1)
    query = "query($owner:String!,$name:String!,$number:Int!){repository(owner:$owner,name:$name){nameWithOwner pullRequest(number:$number){number headRefOid baseRefName mergeQueueEntry{id state baseCommit{oid} headCommit{oid} pullRequest{number headRefOid baseRefName}}}}}"
    response = gateway.transport.request(
        "POST",
        "graphql",
        {
            "query": query,
            "variables": {"owner": owner, "name": name, "number": candidate.pr_number},
        },
    )
    if response.status != 200 or not isinstance(response.data, dict) or response.data.get("errors"):
        raise GithubError("merge queue entry observation unavailable")
    repository = (response.data.get("data") or {}).get("repository") or {}
    pr = repository.get("pullRequest") or {}
    if (
        repository.get("nameWithOwner") != candidate.repository
        or gateway.repository != candidate.repository
        or pr.get("number") != candidate.pr_number
        or pr.get("headRefOid") != candidate.head
        or pr.get("baseRefName") != candidate.target
    ):
        raise GithubError("queued PR repository/head/target identity changed")
    entry = pr.get("mergeQueueEntry")
    if entry is None:
        return None
    member = entry.get("pullRequest") or {}
    head, base = (
        (entry.get("headCommit") or {}).get("oid"),
        (entry.get("baseCommit") or {}).get("oid"),
    )
    if (
        member.get("number") != candidate.pr_number
        or member.get("headRefOid") != candidate.head
        or member.get("baseRefName") != candidate.target
        or not entry.get("id")
        or entry.get("state")
        not in {"QUEUED", "AWAITING_CHECKS", "MERGEABLE", "UNMERGEABLE", "LOCKED"}
    ):
        raise GithubError("merge queue membership/state is unbound or unknown")
    if not head or not base or head == candidate.head:
        return None  # GitHub has not supplied a distinct combined candidate yet.
    if not all(
        isinstance(oid, str) and re.fullmatch(r"[0-9a-f]{40,64}", oid) for oid in (head, base)
    ):
        raise GithubError("merge queue returned malformed combined candidate identities")
    return QueueGroup(
        candidate.repository,
        candidate.pr_number,
        candidate.target,
        entry["id"],
        head,
        base,
        candidate.head,
        entry["state"],
        clock(),
    )


def capability(
    spec: GitHubSpec,
    repository: str,
    project: Path,
    rules: RemoteRules,
    *,
    runner: Callable = run_argv,
    clock: Callable[[], float] = time.time,
) -> CheckCapability:
    """Read a configured authenticated host producer probe; unknown stays blocked.

    The probe is operator-configured code, not model output or a manifest boolean.
    It must report exact app/context/repository and actual checks-write permission
    (native) or the qualified required-check producer (external workflow mode).
    """
    now = clock()
    required = next(
        (check for check in rules.checks if check.context == spec.candidate_check_context), None
    )
    app_id = spec.candidate_check_app_id or (required.app_id if required else 0) or 0

    def result(ready: bool, reason: str = "", producer: str = "") -> CheckCapability:
        return CheckCapability(
            ready,
            repository,
            spec.candidate_check_context,
            app_id,
            spec.candidate_publish,
            now,
            reason,
            producer,
            rules.fingerprint,
        )

    if (
        not required
        or type(app_id) is not int
        or app_id < 1
        or required.app_id != app_id
        or not rules.queue
        or rules.queue_merge_method != "MERGE"
    ):
        return result(
            False, "required queue check/app and history-preserving queue policy are not qualified"
        )
    if not spec.candidate_producer_probe:
        return result(
            False,
            "configure an authenticated required-check producer probe; CLI presence/repo push access does not prove checks-write capability",
        )
    observed = runner(spec.candidate_producer_probe, cwd=project, timeout=15, encoding="utf-8")
    now = clock()
    if (
        observed.returncode
        or observed.timed_out
        or observed.interrupted
        or len(observed.stdout.encode()) > 1024 * 1024
    ):
        return result(False, "required-check producer probe unavailable")
    try:
        raw = json.loads(observed.stdout)
        age = now - raw.get("observed_at", 0)
        if (
            raw.get("schema_version") != 1
            or raw.get("repository") != repository
            or raw.get("context") != spec.candidate_check_context
            or raw.get("app_id") != app_id
            or raw.get("mode") != spec.candidate_publish
            or raw.get("authenticated") is not True
            or not raw.get("producer")
            or not 0 <= age <= 120
        ):
            return result(False, "required-check producer probe has stale/unbound authority")
        ready = (
            raw.get("checks_write") is True
            if spec.candidate_publish == "native"
            else raw.get("publishes_required_check") is True
        )
        return result(
            ready,
            "" if ready else "required-check producer cannot publish this app/context",
            str(raw["producer"]),
        )
    except (ValueError, TypeError, AttributeError):
        return result(False, "required-check producer probe returned malformed evidence")


def _git(repo: Path, *args: str) -> str:
    observed = verify.git_bytes(repo, *args)
    if observed.returncode:
        raise GithubError("queue verification Git operation failed: " + args[0])
    return observed.stdout.decode("utf-8").strip()


def assert_checkout(workspace: Path, common_dir: Path, group: QueueGroup) -> None:
    if (
        (workspace / _git(workspace, "rev-parse", "--git-common-dir")).resolve()
        != common_dir.resolve()
        or verify.rev_parse_head(workspace) != group.head
        or verify.git_bytes(workspace, "symbolic-ref", "-q", "HEAD").returncode == 0
        or not verify.worktree_clean(workspace)
    ):
        raise GithubError("merge-group verification checkout identity/clean detached head changed")


def allocate_checkout(
    project: Path, root: Path, state_dir: Path, group: QueueGroup, *, remote: str
) -> Path:
    """Allocate/adopt only this action's detached G checkout under explicit root."""
    if not root.is_absolute() or not remote or remote.startswith("-") or root.is_symlink():
        raise GithubError("queue verification requires explicit project worktree root and remote")
    if state_dir.resolve().is_relative_to(project.resolve()):
        raise GithubError("queue allocation authority must be outside the editable repository")
    root = root.resolve()
    common = (project / _git(project, "rev-parse", "--git-common-dir")).resolve()
    workspace = root / ("queue-" + group.digest[:24])
    intent = state_dir / (group.digest + ".json")
    value = {
        "schema_version": 1,
        "group": asdict(group),
        "digest": group.digest,
        "workspace": str(workspace),
        "common_dir": str(common),
    }

    def existing() -> Path | None:
        if not workspace.exists():
            return None
        if is_link_like(intent) or not intent.is_file():
            raise GithubError("existing queue verification checkout has no owned intent")
        old = json.loads(intent.read_text(encoding="utf-8"))
        if (
            old.get("digest") != group.digest
            or old.get("workspace") != str(workspace)
            or old.get("common_dir") != str(common)
        ):
            raise GithubError("queue checkout allocation authority changed")
        assert_checkout(workspace, common, group)
        return workspace

    from .campaign_workers import worktree_metadata_lock

    with worktree_metadata_lock(project):
        if (owned := existing()) is not None:
            return owned
        state_dir.mkdir(parents=True, exist_ok=True)
        atomic_write_text(intent, json.dumps(value, sort_keys=True), follow_symlinks=False)
    _git(project, "fetch", "--no-tags", "--no-write-fetch-head", "--refmap=", remote, group.head)
    root.mkdir(parents=True, exist_ok=True)
    with worktree_metadata_lock(project):
        if (owned := existing()) is not None:
            return owned
        _git(project, "worktree", "add", "--detach", str(workspace), group.head)
        assert_checkout(workspace, common, group)
    if not verify.is_ancestor(workspace, group.pr_head, group.head) or not verify.is_ancestor(
        workspace, group.base, group.head
    ):
        raise GithubError("server queue candidate does not preserve PR/base ancestry")
    return workspace


def run_verification(request: dict[str, Any], envelope) -> dict[str, Any]:
    """Disposable worker effect; this never performs a GitHub mutation."""
    candidate = Candidate(**request["candidate"])
    group = QueueGroup(**request["queue_group"])
    workspace = Path(request["workspace"])
    if (
        candidate.repository != group.repository
        or candidate.pr_number != group.pr_number
        or candidate.head != group.pr_head
        or candidate.target != group.target
        or envelope.unit_id != candidate.unit_id
    ):
        raise GithubError("queue verification request belongs to another candidate")
    assert_checkout(workspace, Path(request["common_dir"]), group)
    receipt = verify_queue_candidate(
        tuple(request["commands"]),
        workspace,
        envelope.directory / "queue-command",
        candidate,
        group_head=group.head,
        group_base=group.base,
        context=request["check_context"],
        rules_fingerprint=request["rules_fingerprint"],
        timeout_s=request.get("timeout_s", 600),
        candidate_kind=request.get("candidate_kind", "merge-group"),
    )
    return {
        "outcome": "queue_verified" if receipt.state == "ready" else "queue_verification_waiting",
        "queue_group": asdict(group),
        "verification": asdict(receipt),
        "head": group.head,
        "reason": receipt.reason,
        "candidate_kind": request.get("candidate_kind", "merge-group"),
    }


def _check_matches(row: dict[str, Any], intent: MutationIntent) -> bool:
    expected = intent.coordinates or {}
    return (
        row.get("head_sha") == expected.get("group_head")
        and row.get("name") == intent.payload["name"]
        and row.get("external_id") == intent.payload["external_id"]
        and (row.get("app") or {}).get("id") == expected.get("app_id")
        and row.get("status") == "completed"
        and row.get("conclusion") == intent.payload["conclusion"]
    )


def prepare_check(
    candidate: Candidate,
    group: QueueGroup,
    receipt: AdapterReceipt,
    capability: CheckCapability,
    gateway: GithubGateway,
    *,
    action_id: str,
    generation: int,
    now: float | None = None,
    conclusion: str = "success",
) -> MutationIntent:
    now = time.time() if now is None else now
    if (
        not capability.ready
        or capability.mode != "native"
        or capability.repository != candidate.repository
        or not 0 <= now - capability.observed_at <= 120
        or conclusion not in {"success", "failure"}
        or (
            receipt.state != "ready"
            if conclusion == "success"
            else receipt.state not in {"held", "unknown"}
        )
        or receipt.candidate != candidate
        or receipt.group_head != group.head
        or receipt.group_base != group.base
        or receipt.rules_fingerprint != capability.rules_fingerprint
        or receipt.context != capability.context
        or (conclusion == "success" and not receipt.hosted_check_pending)
        or group.pr_head != candidate.head
        or group.repository != candidate.repository
        or group.pr_number != candidate.pr_number
        or gateway.repository != candidate.repository
        or group.target != candidate.target
        or not 0 <= now - group.observed_at <= 120
    ):
        raise GithubError("local queue evidence/producer authority is stale or incomplete")
    external_id = (
        "bmad-campaign-queue:" + hashlib.sha256((action_id + group.digest).encode()).hexdigest()
    )

    return MutationIntent(
        action_id,
        "queue_check",
        "POST",
        f"{gateway.root}/check-runs",
        {
            "name": capability.context,
            "head_sha": group.head,
            "status": "completed",
            "conclusion": conclusion,
            "external_id": external_id,
            "output": {
                "title": (
                    "Combined candidate verified"
                    if conclusion == "success"
                    else "Combined candidate verification failed"
                ),
                "summary": "Exact G/B/H command receipt " + receipt.request_digest,
            },
        },
        generation=generation,
        coordinates={
            "candidate": asdict(candidate),
            "group_head": group.head,
            "group_base": group.base,
            "group_digest": group.digest,
            "rules_fingerprint": receipt.rules_fingerprint,
            "app_id": capability.app_id,
        },
    )


def head_candidate(candidate: Candidate, *, observed_at: float) -> QueueGroup:
    """Local H verification coordinates, explicitly not server queue membership."""
    return QueueGroup(
        candidate.repository,
        candidate.pr_number,
        candidate.target,
        "pr-head:" + candidate.head,
        candidate.head,
        candidate.base,
        candidate.head,
        "PR_HEAD",
        observed_at,
    )


def prepare_pr_check(
    candidate: Candidate,
    receipt: AdapterReceipt,
    capability: CheckCapability,
    gateway: GithubGateway,
    *,
    action_id: str,
    generation: int,
    now: float | None = None,
    conclusion: str = "success",
) -> MutationIntent:
    """Publish the required App/context on H before enqueue, using exact H/B proof.

    Shares the durable check identity/readback protocol with G publication. The
    candidate-kind coordinate prevents treating this local record as evidence of
    actual merge-queue membership or combined-candidate verification.
    """
    from dataclasses import replace

    intent = prepare_check(
        candidate,
        head_candidate(candidate, observed_at=receipt.observed_at),
        receipt,
        capability,
        gateway,
        action_id=action_id,
        generation=generation,
        now=now,
        conclusion=conclusion,
    )
    return replace(
        intent,
        coordinates={**(intent.coordinates or {}), "candidate_kind": "pr-head"},
        payload={
            **intent.payload,
            "output": {
                "title": (
                    "PR candidate verified"
                    if conclusion == "success"
                    else "PR candidate verification failed"
                ),
                "summary": "Exact H/B command receipt " + receipt.request_digest,
            },
        },
    )


def validate_check_response(intent: MutationIntent, response: dict[str, Any]) -> bool:
    return _check_matches(response, intent)


def reconcile_check(gateway: GithubGateway, intent: MutationIntent) -> MutationIntent:
    from dataclasses import replace

    expected = intent.coordinates or {}
    rows = gateway.pages(
        f"{gateway.root}/commits/{expected['group_head']}/check-runs?filter=all",
        collection="check_runs",
    )
    selected = [row for row in rows if row.get("external_id") == intent.payload["external_id"]]
    if len(selected) > 1:
        raise GithubError("multiple check runs claim the same durable queue action")
    if not selected:
        return replace(
            intent, error="queue check creation remains unknown; no blind duplicate POST"
        )
    if not _check_matches(selected[0], intent):
        raise GithubError("queue check producer/head/context/result identity conflicts")
    return replace(intent, state="confirmed", receipt=selected[0], error="")


def observe_check(
    gateway: GithubGateway, group: QueueGroup, context: str, app_id: int
) -> dict[str, Any] | None:
    """Read the latest exact G/context/App result, including explicit failures."""
    rows = gateway.pages(
        f"{gateway.root}/commits/{group.head}/check-runs?filter=all", collection="check_runs"
    )
    selected = [
        row
        for row in rows
        if row.get("name") == context
        and row.get("head_sha") == group.head
        and (row.get("app") or {}).get("id") == app_id
    ]
    if not selected:
        return None
    return max(selected, key=lambda row: int(row["id"]))


def observe_required_check(
    gateway: GithubGateway, group: QueueGroup, context: str, app_id: int
) -> dict[str, Any] | None:
    newest = observe_check(gateway, group, context, app_id)
    return (
        newest
        if newest and newest.get("status") == "completed" and newest.get("conclusion") == "success"
        else None
    )


def membership(gateway: GithubGateway, candidate: Candidate) -> dict[str, Any]:
    owner, name = gateway.repository.split("/", 1)
    query = "query($owner:String!,$name:String!,$number:Int!){repository(owner:$owner,name:$name){pullRequest(number:$number){id number headRefOid baseRefName merged state mergeQueueEntry{id state}}}}"
    response = gateway.transport.request(
        "POST",
        "graphql",
        {
            "query": query,
            "variables": {"owner": owner, "name": name, "number": candidate.pr_number},
        },
    )
    if response.status != 200 or response.data.get("errors"):
        raise GithubError("queue membership observation unavailable")
    pr = (response.data.get("data") or {}).get("repository", {}).get("pullRequest") or {}
    if (
        pr.get("number") != candidate.pr_number
        or pr.get("headRefOid") != candidate.head
        or pr.get("baseRefName") != candidate.target
    ):
        raise GithubError("queue membership PR/head/target changed")
    return pr


def prepare_dequeue(
    gateway: GithubGateway, candidate: Candidate, *, action_id: str, generation: int
) -> MutationIntent:
    pr = membership(gateway, candidate)
    if not pr.get("id") or pr.get("merged") is not False or pr.get("state") != "OPEN":
        raise GithubError("only the owned open/unmerged PR can be removed for repair")
    query = "mutation($input:DequeuePullRequestInput!){dequeuePullRequest(input:$input){clientMutationId pullRequest{id number headRefOid baseRefName mergeQueueEntry{id}}}}"
    return MutationIntent(
        action_id,
        "queue_dequeue",
        "POST",
        "graphql",
        {
            "query": query,
            "variables": {"input": {"pullRequestId": pr["id"], "clientMutationId": action_id}},
        },
        generation=generation,
        coordinates={"candidate": asdict(candidate), "node_id": pr["id"]},
    )


def reconcile_dequeue(
    gateway: GithubGateway,
    intent: MutationIntent,
    *,
    allow_retry: bool = False,
    generation: int | None = None,
) -> MutationIntent:
    from dataclasses import replace

    coordinates = intent.coordinates or {}
    candidate = Candidate(**coordinates["candidate"])
    removal = coordinates.get("remote_head_removal")
    if removal:
        # A lost removal acknowledgement is not permission to dequeue a later
        # entry. A new head can identify the same owned PR for absence readback
        # only; it does not inherit the original accepted candidate's evidence.
        if (
            removal.get("repository") != gateway.repository
            or removal.get("repository") != candidate.repository
            or removal.get("pr_number") != candidate.pr_number
            or removal.get("target") != candidate.target
            or removal.get("head") != candidate.head
            or removal.get("pr_node_id") != coordinates.get("node_id")
            or not removal.get("branch")
        ):
            raise GithubError("remote-head dequeue coordinates changed")
        current = gateway.pull(candidate.pr_number)
        if (
            current.get("number") != candidate.pr_number
            or current.get("node_id") != coordinates["node_id"]
            or (current.get("head") or {}).get("ref") != removal["branch"]
            or (current.get("head") or {}).get("repo", {}).get("full_name") != candidate.repository
            or (current.get("base") or {}).get("repo", {}).get("full_name") != candidate.repository
            or (current.get("base") or {}).get("ref") != candidate.target
        ):
            raise GithubError("remote-head dequeue lost owned PR/ref/repository identity")
        head = (current.get("head") or {}).get("sha")
        if not isinstance(head, str) or not re.fullmatch(r"[0-9a-f]{40,64}", head):
            raise GithubError("remote-head dequeue current head is unavailable")
        candidate = replace(candidate, head=head)
    pr = membership(gateway, candidate)
    if pr.get("id") != (intent.coordinates or {}).get("node_id"):
        raise GithubError("dequeue PR native identity changed")
    if pr.get("mergeQueueEntry") is None:
        if removal and not (
            (pr.get("merged") is False and pr.get("state") in {"OPEN", "CLOSED"})
            or (pr.get("merged") is True and pr.get("state") == "CLOSED")
        ):
            return replace(intent, error="queue removal lacks authenticated PR state")
        return replace(intent, state="confirmed", receipt=pr, error="")
    if allow_retry and not removal:
        if generation is None or generation != intent.generation:
            raise GithubError("queue dequeue retry requires current coordinator authorization")
        return gateway.execute(replace(intent, state="sent_unknown"), generation=generation)
    return replace(intent, error="queue removal remains pending; repair is not authorized")
