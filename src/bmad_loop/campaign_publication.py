"""Exact-candidate campaign publication gates and asynchronous merge read-back.

These methods are bounded observations/effects for an executor, never a polling
loop. The supervisor owns stage transitions and persists every returned receipt.
Only ``merged`` plus fresh canonical ancestry can release tracking publication.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote

from .campaign_github import GithubError, GithubGateway, JsonObject, MutationIntent
from .verify import git_bytes


@dataclass(frozen=True)
class Candidate:
    repository: str
    unit_id: str
    pr_number: int
    head: str
    base: str
    source_revision: str
    graph_revision: int
    config_revision: str
    target: str = "main"


@dataclass(frozen=True)
class PublicationEvidence:
    candidate: Candidate
    verified: bool
    review_approved: bool
    writer_quiescent: bool
    clean: bool
    developer_family: str
    reviewer_family: str
    required_findings: tuple[str, ...] = ()
    held: bool = False
    observed_model: str = ""
    verification_receipts: tuple[str, ...] = ()
    review_receipt: str = ""


@dataclass(frozen=True)
class RequiredCheck:
    context: str
    app_id: int | None = None


@dataclass(frozen=True)
class RemoteRules:
    checks: tuple[RequiredCheck, ...]
    strict: bool
    queue: bool
    required_reviews: int
    last_push_approval: bool
    conversation_resolution: bool
    fingerprint: str
    complete: bool = True
    queue_merge_method: str = ""
    merge_commits_allowed: bool = True


@dataclass(frozen=True)
class PublicationObservation:
    repository: str
    pr: JsonObject
    base_head: str
    rules: RemoteRules
    checks: tuple[JsonObject, ...]
    statuses: tuple[JsonObject, ...]
    reviews: tuple[JsonObject, ...]
    threads: tuple[JsonObject, ...]
    issue: JsonObject
    observed_at: float
    review_decision: str = ""
    requested_reviewers: tuple[str, ...] = ()


@dataclass(frozen=True)
class GateResult:
    ready: bool
    reasons: tuple[str, ...]
    candidate: Candidate
    rules_fingerprint: str


@dataclass(frozen=True)
class MergeReceipt:
    state: str
    request_id: str = ""
    merge_commit: str = ""
    canonical_head: str = ""
    reason: str = ""
    raw: JsonObject | None = None


def _fingerprint(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def read_rules(gateway: GithubGateway, target: str) -> RemoteRules:
    """Combine rulesets and classic protection; a forbidden read is never absence.

    merge-async is always submitted with bypass_rules=false. Unlike the old
    synchronous endpoint, this forces applicable rules even for an eligible
    bypass actor. Strict checks remain necessary to close the moving-base race.
    """
    branch = quote(target, safe="")
    effective = gateway.pages(f"{gateway.root}/rules/branches/{branch}")
    try:
        classic = gateway.get(f"{gateway.root}/branches/{branch}/protection")
    except GithubError as exc:
        if exc.status != 404:
            raise
        # Confirm branch/repository visibility before interpreting protection 404.
        gateway.get(f"{gateway.root}/branches/{branch}")
        classic = {}
    checks: dict[tuple[str, int | None], RequiredCheck] = {}
    strict = False
    queue = False
    queue_method = ""
    merge_allowed = True
    required_reviews = 0
    last_push = False
    conversation = False
    for rule in effective:
        kind = rule.get("type")
        params = rule.get("parameters", {})
        if kind == "required_status_checks":
            strict |= params.get("strict_required_status_checks_policy") is True
            for row in params.get("required_status_checks", []):
                context = row.get("context")
                if not isinstance(context, str) or not context:
                    raise GithubError("Required check rule has no context")
                app_id = row.get("integration_id")
                if app_id == -1:
                    app_id = None
                checks[context, app_id] = RequiredCheck(context, app_id)
        elif kind == "merge_queue":
            queue = True
            queue_method = str(params.get("merge_method", "")).upper()
        elif kind == "required_linear_history":
            merge_allowed = False
        elif kind == "pull_request":
            if "allowed_merge_methods" in params:
                merge_allowed &= "merge" in params["allowed_merge_methods"]
            required_reviews = max(
                required_reviews, int(params.get("required_approving_review_count", 0))
            )
            last_push |= params.get("require_last_push_approval") is True
            conversation |= params.get("required_review_thread_resolution") is True
    status_rules = classic.get("required_status_checks") or {}
    strict |= status_rules.get("strict") is True
    for row in status_rules.get("checks", []):
        app_id = row.get("app_id")
        if app_id == -1:
            app_id = None
        checks[row["context"], app_id] = RequiredCheck(row["context"], app_id)
    for context in status_rules.get("contexts", []):
        if not any(key[0] == context for key in checks):
            checks[context, None] = RequiredCheck(context)
    review_rules = classic.get("required_pull_request_reviews") or {}
    required_reviews = max(
        required_reviews, int(review_rules.get("required_approving_review_count", 0))
    )
    last_push |= review_rules.get("require_last_push_approval") is True
    conversation |= (classic.get("required_conversation_resolution") or {}).get("enabled") is True
    return RemoteRules(
        tuple(checks.values()),
        strict,
        queue,
        required_reviews,
        last_push,
        conversation,
        _fingerprint([effective, classic]),
        queue_merge_method=queue_method,
        merge_commits_allowed=merge_allowed,
    )


class PublicationController:
    def __init__(
        self,
        gateway: GithubGateway,
        *,
        clock: Callable[[], float] = time.time,
        max_observation_age_s: float = 30,
    ):
        self.gateway = gateway
        self.clock = clock
        self.max_observation_age_s = max_observation_age_s

    def inspect(self, pr_number: int) -> PublicationObservation:
        gateway = self.gateway
        pr = gateway.pull(pr_number)
        target = pr["base"]["ref"]
        head = pr["head"]["sha"]
        branch = gateway.get(f"{gateway.root}/git/ref/heads/{quote(target, safe='')}")
        checks = gateway.pages(
            f"{gateway.root}/commits/{head}/check-runs?filter=all", collection="check_runs"
        )
        statuses = gateway.pages(f"{gateway.root}/commits/{head}/statuses")
        # The server's reviewDecision includes last-push/self-review eligibility.
        owner, name = gateway.repository.split("/", 1)
        query = "query($owner:String!,$name:String!,$number:Int!){repository(owner:$owner,name:$name){pullRequest(number:$number){reviewDecision}}}"
        response = gateway.transport.request(
            "POST",
            "graphql",
            {"query": query, "variables": {"owner": owner, "name": name, "number": pr_number}},
        )
        if response.status != 200 or response.data.get("errors"):
            raise GithubError("Server review eligibility could not be observed")
        try:
            decision = response.data["data"]["repository"]["pullRequest"]["reviewDecision"] or ""
        except (TypeError, KeyError) as exc:
            raise GithubError("Server review eligibility response is incomplete") from exc
        return PublicationObservation(
            gateway.repository,
            pr,
            branch["object"]["sha"],
            read_rules(gateway, target),
            tuple(checks),
            tuple(statuses),
            tuple(gateway.reviews(pr_number)),
            tuple(gateway.review_threads(pr_number)),
            gateway.issue(pr_number),
            self.clock(),
            decision,
            tuple(row["login"] for row in pr.get("requested_reviewers", [])),
        )

    def evaluate(
        self,
        candidate: Candidate,
        evidence: PublicationEvidence,
        observation: PublicationObservation,
        *,
        merge_mode: str = "direct-strict",
        independent_families: bool = False,
        queue_evidence_context: str = "",
        hold_labels: tuple[str, ...] = ("hold", "owner-hold", "blocked", "approval-required"),
        queued_base: str = "",
        canonical_merge: MergeReceipt | None = None,
    ) -> GateResult:
        reasons: list[str] = []
        if candidate != evidence.candidate:
            reasons.append("Acceptance evidence belongs to a different candidate")
        if (
            candidate.repository != self.gateway.repository
            or observation.repository != candidate.repository
        ):
            reasons.append("Repository identity changed")
        if observation.pr.get("number") != candidate.pr_number:
            reasons.append("PR identity changed")
        if observation.pr.get("head", {}).get("sha") != candidate.head:
            reasons.append("PR head changed; repeat acceptance")
        reconciled_merge = bool(
            canonical_merge
            and canonical_merge.state == "merged_pending_sync"
            and bool(canonical_merge.canonical_head)
            and canonical_merge.merge_commit == observation.pr.get("merge_commit_sha")
            and observation.pr.get("merged") is True
        )
        if canonical_merge is not None and not reconciled_merge:
            reasons.append("Canonical merged-PR proof does not match the current observation")
        if (
            observation.base_head != candidate.base
            and not reconciled_merge
            and not (merge_mode == "queue" and queued_base == observation.base_head)
        ):
            reasons.append("Canonical target moved; synchronize and repeat acceptance")
        if observation.pr.get("base", {}).get("repo", {}).get("full_name") != candidate.repository:
            reasons.append("PR targets another repository")
        if observation.pr.get("base", {}).get("ref") != candidate.target:
            reasons.append("PR target branch changed; repeat acceptance for the intended target")
        age = self.clock() - observation.observed_at
        if age < 0 or age > self.max_observation_age_s:
            reasons.append("Remote observation expired")
        if not evidence.writer_quiescent or not evidence.clean:
            reasons.append("Candidate writer/tree is not quiescent and clean")
        if not evidence.verified or not evidence.verification_receipts:
            reasons.append("Final candidate verification is missing")
        if (
            not evidence.review_approved
            or not evidence.review_receipt
            or not evidence.observed_model
        ):
            reasons.append("Attributed full-diff publication review is missing")
        if evidence.required_findings:
            reasons.append("Required local review findings remain")
        if evidence.held:
            reasons.append("Unit has an active hold")
        if independent_families and (
            not evidence.developer_family
            or not evidence.reviewer_family
            or evidence.developer_family == evidence.reviewer_family
        ):
            reasons.append("Independent provider-family review is required")
        pr = observation.pr
        if not reconciled_merge and (pr.get("state") != "open" or pr.get("draft") is not False):
            reasons.append("PR must be open and ready for review")
        if pr.get("merged") and not reconciled_merge:
            reasons.append("Already merged; reconcile tracking instead of resubmitting")
        if not reconciled_merge and (
            pr.get("mergeable") is not True
            or pr.get("mergeable_state")
            not in {
                "clean",
                "unstable",
                "has_hooks",
            }
        ):
            reasons.append("GitHub mergeability is not confirmed")
        labels = {str(row.get("name", "")).lower() for row in observation.issue.get("labels", [])}
        if labels.intersection(label.lower() for label in hold_labels):
            reasons.append("Remote owner/review hold is active")
        rules = observation.rules
        if not rules.complete:
            reasons.append("Effective rules are incomplete")
        if not rules.merge_commits_allowed:
            reasons.append("Target policy forbids history-preserving merge commits")
        if merge_mode in {"direct-strict", "project-command"}:
            if not rules.strict or not rules.checks:
                reasons.append("Server-enforced up-to-date required checks are missing")
            if rules.queue:
                reasons.append("Target requires merge queue; direct publication is not allowed")
        elif merge_mode == "queue":
            if not rules.queue:
                reasons.append("Target does not enforce a merge queue")
            if rules.queue_merge_method != "MERGE":
                reasons.append("Merge queue must preserve every commit with MERGE")
            if not queue_evidence_context or queue_evidence_context not in {
                check.context for check in rules.checks
            }:
                reasons.append("Required merge_group candidate evidence producer is missing")
        else:
            reasons.append("Unsupported merge mode")
        for check in rules.checks:
            check_reason = _check_reason(
                check, observation.checks, observation.statuses, candidate.head
            )
            if check_reason:
                reasons.append(check_reason)
        # A review's commit_id must match this head. Dismissed/replaced reviews do not count.
        latest: dict[str, JsonObject] = {}
        for review in sorted(observation.reviews, key=lambda row: row.get("id", 0)):
            state = review.get("state")
            if state not in {"COMMENTED", "PENDING"}:
                latest[str(review.get("user", {}).get("login", ""))] = review
        if any(row.get("state") == "CHANGES_REQUESTED" for row in latest.values()):
            reasons.append("A reviewer requests changes")
        approvers = {
            login
            for login, row in latest.items()
            if login
            and login != pr.get("user", {}).get("login")
            and row.get("state") == "APPROVED"
            and row.get("commit_id") == candidate.head
        }
        if len(approvers) < rules.required_reviews:
            reasons.append("Required current-head GitHub approvals are missing")
        if (
            rules.required_reviews or rules.last_push_approval
        ) and observation.review_decision != "APPROVED":
            reasons.append("GitHub has not confirmed eligible approval under current review rules")
        if any(thread.get("isResolved") is not True for thread in observation.threads):
            reasons.append("Unresolved review threads remain")
        return GateResult(not reasons, tuple(reasons), candidate, rules.fingerprint)

    def prepare_merge(
        self,
        candidate: Candidate,
        evidence: PublicationEvidence,
        *,
        action_id: str,
        generation: int,
        merge_mode: str = "direct-strict",
        merge_method: str = "merge",
        independent_families: bool = False,
        queue_evidence_context: str = "",
    ) -> MutationIntent:
        # This fresh observation is intentionally inside the final submission stage.
        observation = self.inspect(candidate.pr_number)
        gate = self.evaluate(
            candidate,
            evidence,
            observation,
            merge_mode=merge_mode,
            independent_families=independent_families,
            queue_evidence_context=queue_evidence_context,
        )
        if not gate.ready:
            raise GithubError("Publication held: " + "; ".join(gate.reasons))
        if merge_method != "merge":
            raise ValueError("Campaign publication must preserve every commit with a merge commit")
        payload: JsonObject = {
            "sha": candidate.head,
            "merge_action": "merge_queue" if merge_mode == "queue" else "direct_merge",
            "bypass_rules": False,
        }
        if merge_mode != "queue":
            payload["merge_method"] = merge_method
        return MutationIntent(
            action_id,
            "merge",
            "PUT",
            f"{self.gateway.root}/pulls/{candidate.pr_number}/merge-async",
            payload,
            generation=generation,
            coordinates={**asdict(candidate), "rules_fingerprint": gate.rules_fingerprint},
        )

    def reconcile_request(
        self,
        intent: MutationIntent,
        candidate: Candidate,
        evidence: PublicationEvidence,
        *,
        generation: int,
        allow_retry: bool = False,
        merge_mode: str = "direct-strict",
        merge_method: str = "merge",
        independent_families: bool = False,
        queue_evidence_context: str = "",
    ) -> MutationIntent:
        """Recover a lost async acknowledgement through GitHub's 409 contract.

        Caller first persists a bounded retry authorization in campaign authority.
        No UUID-list endpoint exists. Repeating the same request after a fresh
        gate pass returns an existing request or starts a new bounded attempt;
        this is never a blind replay of an issue/PR create operation.
        """
        if intent.kind != "merge" or intent.state != "sent_unknown":
            raise GithubError("Only uncertain merge intents can be reconciled")
        if intent.generation != generation:
            raise GithubError(
                "Coordinator must explicitly adopt an old merge intent before effects"
            )
        pr = self.gateway.pull(candidate.pr_number)
        if pr.get("head", {}).get("sha") != candidate.head:
            return replace(intent, error="PR head changed during uncertain merge")
        if pr.get("merged") is True and pr.get("merge_commit_sha"):
            return replace(
                intent,
                state="confirmed",
                receipt={"status": "merged", "details": {"sha": pr["merge_commit_sha"]}},
                error="",
            )
        if pr.get("state") != "open":
            return replace(intent, error="PR closed without a confirmed merge")
        receipt = intent.receipt or {}
        request_id = (receipt.get("details") or {}).get("uuid")
        if request_id:
            observed = self.merge_status(
                candidate.pr_number, request_id=request_id, expected_head=candidate.head
            )
            if observed.state in {"pending", "enqueued", "merged_pending_sync"}:
                return replace(intent, state="confirmed", receipt=observed.raw or receipt, error="")
            return replace(intent, error=observed.reason)
        if not allow_retry:
            return replace(
                intent,
                error="Recovering a missing merge UUID requires a persisted bounded retry authorization",
            )
        prepared = self.prepare_merge(
            candidate,
            evidence,
            action_id=intent.action_id,
            generation=generation,
            merge_mode=merge_mode,
            merge_method=merge_method,
            independent_families=independent_families,
            queue_evidence_context=queue_evidence_context,
        )
        if prepared.payload != intent.payload or prepared.path != intent.path:
            return replace(
                intent, error="Merge replay options differ from the original durable intent"
            )
        if intent.coordinates and intent.coordinates.get("rules_fingerprint") != (
            prepared.coordinates or {}
        ).get("rules_fingerprint"):
            return replace(
                intent, error="Publication rules changed during uncertain merge; repeat acceptance"
            )
        return self.gateway.execute(intent, generation=generation)

    def merge_status(
        self, pr_number: int, *, request_id: str = "", expected_head: str = ""
    ) -> MergeReceipt:
        pr = self.gateway.pull(pr_number)
        if expected_head and pr.get("head", {}).get("sha") != expected_head:
            return MergeReceipt("held", request_id, reason="PR head changed during merge", raw=pr)
        if pr.get("merged") is True:
            sha = pr.get("merge_commit_sha")
            if not sha:
                raise GithubError("Merged PR has no merge commit identity")
            return MergeReceipt("merged_pending_sync", request_id, sha, raw=pr)
        if pr.get("state") == "closed":
            return MergeReceipt("held", request_id, reason="PR closed without merge", raw=pr)
        if not request_id:
            return MergeReceipt(
                "unknown",
                reason="No confirmed async request identity; reconcile before retry",
                raw=pr,
            )
        try:
            result = self.gateway.get(
                f"{self.gateway.root}/pulls/{pr_number}/merge-async/{quote(request_id, safe='')}"
            )
        except GithubError as exc:
            if exc.status == 404:
                return MergeReceipt(
                    "unknown",
                    request_id,
                    reason="Async request expired or unavailable; PR remains unmerged",
                    raw=pr,
                )
            raise
        details = result.get("details") or {}
        if details.get("bypass_rules") is True:
            return MergeReceipt(
                "held", request_id, reason="Existing merge request bypasses rules", raw=result
            )
        if expected_head and details.get("expected_head_sha", expected_head) != expected_head:
            return MergeReceipt(
                "held", request_id, reason="Existing merge request has another head", raw=result
            )
        status = result.get("status")
        if status == "failed":
            return MergeReceipt(
                "held", request_id, reason=str(details.get("message", "Merge failed")), raw=result
            )
        if status in {"pending", "enqueued"}:
            return MergeReceipt(status, request_id, raw=result)
        # A merged request response alone is insufficient; next PR read proves it.
        return MergeReceipt(
            "unknown", request_id, reason="Merge result needs independent PR read-back", raw=result
        )

    def confirm_canonical(
        self,
        candidate: Candidate,
        receipt: MergeReceipt,
        repo: Path,
        *,
        remote: str = "origin",
        target: str = "main",
    ) -> MergeReceipt:
        if receipt.state != "merged_pending_sync" or not receipt.merge_commit:
            raise GithubError("Remote merge must be confirmed before canonical publication")
        if not remote or remote.startswith("-") or not target or target.startswith("-"):
            raise ValueError("Invalid canonical remote/target")
        ref = (
            f"refs/bmad-campaign/observed/{hashlib.sha256(candidate.unit_id.encode()).hexdigest()}"
        )
        result = git_bytes(repo, "fetch", "--no-tags", "--", remote, f"refs/heads/{target}:{ref}")
        if result.returncode:
            raise GithubError("Could not fetch canonical target after merge")
        head = git_bytes(repo, "rev-parse", "--verify", ref)
        ancestor = git_bytes(repo, "merge-base", "--is-ancestor", receipt.merge_commit, ref)
        if head.returncode or ancestor.returncode:
            raise GithubError("Reported merge commit is not reachable from fresh canonical target")
        # Do not test H ancestry here: squash/rebase merge methods intentionally replace H.
        return MergeReceipt(
            "merged_pending_sync",
            receipt.request_id,
            receipt.merge_commit,
            head.stdout.decode("utf-8").strip(),
            raw=receipt.raw,
        )


def _check_reason(
    required: RequiredCheck,
    checks: tuple[JsonObject, ...],
    statuses: tuple[JsonObject, ...],
    head: str,
) -> str:
    matching_checks = [
        row
        for row in checks
        if row.get("name") == required.context
        and row.get("head_sha") == head
        and (required.app_id is None or row.get("app", {}).get("id") == required.app_id)
    ]
    matching_statuses = [
        row
        for row in statuses
        if row.get("context") == required.context and (required.app_id is None)
    ]
    # Checks and statuses with the same name are both required by GitHub.
    if not matching_checks and not matching_statuses:
        return f"Required check missing: {required.context} (app {required.app_id})"
    if matching_checks:
        newest = max(matching_checks, key=lambda row: row.get("id", 0))
        if newest.get("status") != "completed" or newest.get("conclusion") not in {
            "success",
            "neutral",
            "skipped",
        }:
            return f"Required check pending/failed: {required.context}"
    if matching_statuses:
        newest = max(matching_statuses, key=lambda row: row.get("id", 0))
        if newest.get("state") != "success":
            return f"Required status pending/failed: {required.context}"
    return ""
