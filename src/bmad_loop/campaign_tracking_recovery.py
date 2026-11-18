"""Bounded recovery of proven-unsent delivery closes, never uncertain writes.

Three successor preparations are permitted for an immutable close lineage. The
allowance survives restart and does not reset native attempts or approvals.
Changed human discussion, ownership, scope or acceptance remains explicit debt;
the operator can resolve its actual remote condition, including closing the
issue as completed. Missing historical no-send evidence never authorizes a
replacement. Remote evidence is untrusted input, not approval authority.
"""

from __future__ import annotations

import copy
import dataclasses
import hashlib
import json

from .campaign_github import GithubError, MutationIntent
from .campaign_model import UnitState
from .campaign_store import add_event
from .campaign_supervisor import Observation, Proposal

MAX_REPLANS = 3
MAX_COMMENTS = 500
MAX_OBSERVATION_BYTES = 1024 * 1024
RETRY_SECONDS = 60
KEY = "tracking_recovery"
PENDING_OBLIGATIONS = (
    "unresolved_approval_requirements",
    "pending_failure_hold",
    "pending_failure_holds",
    "operator_prior_hold",
)


def _digest(value) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _members(snapshot, unit, parent_key):
    if parent_key:
        from .campaign_completion_scope import parent_members

        return parent_members(snapshot, parent_key)
    return (unit.id,)


def _obligations(snapshot, unit):
    """Bind live authority without copying acceptance or decision histories."""
    acceptance = unit.evidence.get("acceptance", {})
    decisions = {
        key: _digest(value)
        for key, value in snapshot.bindings.get("decision_receipts", {}).items()
        if value.get("coordinates", {}).get("unit") == unit.id
    }
    return {
        "acceptance": {key: acceptance.get(key) for key in ("revision", "criteria")},
        "decision_input_revision": unit.evidence.get("decision_input_revision", ""),
        "decision_receipts": decisions,
        "decision_requests": {
            key: _digest(value)
            for key, value in snapshot.bindings.get("decision_requests", {}).items()
            if value.get("receipt") in decisions
        },
        **{
            key: _digest(unit.evidence.get(key))
            for key in (
                "decisions",
                "unresolved_approval_requirements",
                "pending_failure_hold",
                "pending_failure_holds",
                "operator_prior_hold",
            )
        },
    }


def acceptance_wait(unit) -> str:
    """Current required criteria need the controller's current complete proof."""
    ledger = unit.evidence.get("acceptance") or {}
    if not isinstance(ledger, dict):
        return "Tracking acceptance ledger is unknown or malformed"
    criteria = ledger.get("criteria", [])
    if not isinstance(criteria, (list, tuple)) or any(
        not isinstance(row, dict)
        or not isinstance(row.get("id"), str)
        or not row["id"]
        or type(row.get("required")) is not bool
        for row in criteria
    ):
        return "Tracking acceptance criteria are unknown or malformed"
    required = [row for row in criteria if row["required"]]
    if not required:
        return ""
    current = ledger.get("current") or {}
    if (
        not isinstance(current, dict)
        or current.get("complete") is not True
        or type(ledger.get("revision")) is not int
        or type(current.get("revision")) is not int
        or current.get("revision") != ledger["revision"]
        or not isinstance(current.get("criteria"), (list, tuple))
    ):
        return "Tracking waits for complete proof of the current required acceptance revision"
    rows = current["criteria"]
    if any(
        not isinstance(row, dict)
        or not isinstance(row.get("id"), str)
        or not row["id"]
        or type(row.get("required")) is not bool
        for row in rows
    ):
        return "Tracking current acceptance proof has unknown criterion identity"
    by_id = {row["id"]: row for row in rows}
    if (
        len(by_id) != len(rows)
        or len({row["id"] for row in criteria}) != len(criteria)
        or set(by_id) != {row["id"] for row in criteria}
    ):
        return "Tracking current acceptance proof has ambiguous criterion identity"
    if any(
        any(
            by_id.get(row["id"], {}).get(field) != row.get(field)
            for field in ("id", "text", "required")
        )
        or by_id.get(row["id"], {}).get("status") != "passed"
        for row in required
    ):
        return "Tracking waits for current proof of every required criterion"
    return ""


def _context(snapshot, unit, parent_key, source_publication=None):
    return {
        "campaign": snapshot.id,
        "repository": snapshot.repository,
        "unit": unit.id,
        "config_revision": snapshot.config_revision,
        "parent_key": parent_key,
        "members": {
            name: {
                "spec": dataclasses.asdict(member.spec),
                "obligations": _obligations(snapshot, member),
                "issue": member.bindings.get("issue"),
                "pr": member.bindings.get("pr"),
                "canonical_source": member.bindings.get("canonical_source"),
                "source_revision": member.bindings.get("source_revision"),
                "head": member.bindings.get("head"),
                "branch": member.bindings.get("branch"),
                "merge": member.evidence.get("merge"),
                "review": member.evidence.get("review"),
                "developer_families": member.evidence.get("developer_families"),
                "landing_acceptance": member.evidence.get("landing_acceptance"),
                "source_publication": (
                    source_publication
                    if name == unit.id and source_publication is not None
                    else member.evidence.get("source_publication")
                ),
            }
            for name in _members(snapshot, unit, parent_key)
            for member in (snapshot.units[name],)
        },
    }


def _observe(gateway, snapshot, number):
    from .campaign_runtime_intake import _owned_projection

    before = gateway.issue(number)
    comments = []
    count = size = 0
    for page in gateway.iter_pages(f"{gateway.root}/issues/{number}/comments"):
        for row in page:
            count += 1
            size += len(json.dumps(row).encode())
            if count > MAX_COMMENTS or size > MAX_OBSERVATION_BYTES:
                raise GithubError("Tracking discussion exceeds bounded complete-observation limit")
            if not _owned_projection(
                snapshot, {"kind": "comment", "object_id": str(row.get("id", "")), "payload": row}
            ):
                comments.append(row)
    try:
        parent = gateway.get(f"{gateway.root}/issues/{number}/parent")
    except GithubError as exc:
        if exc.status != 404:
            raise
        parent = None
    issue = gateway.issue(number)
    if not issue.get("updated_at") or any(
        before.get(key) != issue.get(key) for key in ("id", "number", "updated_at")
    ):
        raise GithubError("Tracking issue changed during complete discussion observation")
    if issue.get("number") != number or not issue.get("id"):
        raise GithubError("Tracking issue native identity is unproven")
    labels = {str(row.get("name", "")).lower() for row in issue.get("labels", [])}
    holds = {name.lower() for name in snapshot.spec.github.hold_labels}
    # Preserve every issue field except lifecycle/timestamp and benign label
    # projections. Human comments are accounted independently by exact content.
    protected = {
        key: value
        for key, value in issue.items()
        if key
        not in {
            "updated_at",
            "state",
            "state_reason",
            "closed_at",
            "closed_by",
            "comments",
            "labels",
        }
    }
    protected["policy_labels"] = sorted(
        name for name in labels if name in holds or name.startswith(("bmad:", "status:", "status/"))
    )
    value = {
        "issue": protected,
        "human_comments": sorted(comments, key=lambda row: str(row.get("id"))),
        "parent": parent,
    }
    if len(json.dumps(value).encode()) > MAX_OBSERVATION_BYTES:
        raise GithubError("Tracking preimage exceeds bounded complete-observation limit")
    return issue, {
        "schema_version": 1,
        "repository": snapshot.repository,
        "number": number,
        "id": issue["id"],
        "updated_at": issue["updated_at"],
        "digest": _digest(value),
        "held": bool(labels & holds),
        "provenance": "untrusted GitHub issue, complete comments and native parent observation",
        "complete": True,
    }


def prepare_close(
    gateway,
    snapshot,
    unit,
    number,
    *,
    action_id,
    source_confirmed,
    parent_key="",
    source_publication=None,
) -> MutationIntent:
    """Prepare a close and the immutable baseline needed for a safe replan."""
    intent = gateway.close_issue_intent(
        number,
        action_id=action_id,
        generation=snapshot.generation,
        source_confirmed=source_confirmed,
    )
    plan = {
        "schema_version": 1,
        "root_action": action_id,
        "replans": 0,
        "parent_key": parent_key,
        "context": _digest(_context(snapshot, unit, parent_key, source_publication)),
    }
    if intent.state != "prepared":
        return dataclasses.replace(
            intent,
            coordinates={
                **(intent.coordinates or {}),
                "id": (intent.receipt or {}).get("id"),
                "tracking_replan": plan,
            },
        )
    issue, preimage = _observe(gateway, snapshot, number)
    if preimage["held"]:
        raise GithubError(
            "Configured issue hold label prevents tracking closure", category="permission"
        )
    if issue.get("id") != (intent.coordinates or {}).get("id") or issue.get("updated_at") != (
        intent.coordinates or {}
    ).get("updated_at"):
        raise GithubError("Tracking preimage changed before its preparation was committed")
    return dataclasses.replace(
        intent,
        coordinates={
            **(intent.coordinates or {}),
            "tracking_replan": {**plan, "preimage": preimage},
        },
    )


def _first_submission(action):
    """A later local refusal cannot resolve an earlier unacknowledged write."""
    return (
        not action.receipt
        and not action.error
        and not any(
            key in action.payload
            for key in ("retry_authorized", "update_retries", "retry_window_at")
        )
    )


def observation(snapshot, action, result, *, now):
    """Project a known no-send result without giving it delivery credit."""
    if (
        action.payload.get("purpose") not in {"tracking", "parent-tracking"}
        or result.kind != "close_issue"
    ):
        return None
    if result.state == "sent_unknown":
        from .campaign_github_faults import mutation_observation

        observed = mutation_observation(result, now=now)
        return dataclasses.replace(
            observed,
            state="waiting",
            retry_at=max(now + RETRY_SECONDS, observed.retry_at or now),
        )
    fault = result.fault or {}
    if (
        result.state != "rejected"
        or fault.get("certainty") != "not_submitted"
        or fault.get("category") not in {"precondition", "tracking_discrepancy"}
    ):
        return None
    if not _first_submission(action):
        return Observation(
            "waiting",
            {"intent": dataclasses.replace(result, state="sent_unknown").to_dict()},
            "An earlier close submission remains uncertain; this no-send result cannot authorize replan",
            now + RETRY_SECONDS,
        )
    original = MutationIntent.from_dict(action.payload["intent"])
    if (
        fault.get("schema_version") != 1
        or result.action_id != action.id
        or any(
            getattr(result, name) != getattr(original, name)
            for name in (
                "action_id",
                "kind",
                "method",
                "path",
                "payload",
                "generation",
                "coordinates",
            )
        )
    ):
        return Observation(
            "waiting",
            reason="Tracking no-send evidence is not bound to this exact request",
            retry_at=now + RETRY_SECONDS,
        )
    if fault["category"] == "precondition":
        proof = (result.receipt or {}).get("precondition", {})
        expected = {
            "schema_version": 1,
            "repository": snapshot.repository,
            "action_id": original.action_id,
            "generation": original.generation,
            "kind": original.kind,
            "method": original.method,
            "path": original.path,
            "payload": original.payload,
            "expected": original.coordinates,
            "submitted": False,
        }
        if (
            any(proof.get(key) != value for key, value in expected.items())
            or result.action_id != action.id
        ):
            return Observation(
                "waiting",
                reason="Tracking no-send evidence is not bound to this action",
                retry_at=now + RETRY_SECONDS,
            )
    parent_key = action.payload.get("parent_key", "")
    if action.payload["purpose"] == "parent-tracking":
        keys = _target_keys(snapshot, action, original)
        if not keys:
            return Observation(
                "waiting",
                reason="Tracking no-send parent target is unproven",
                retry_at=now + RETRY_SECONDS,
            )
        parent_key = parent_key or keys[0]
    return Observation(
        "confirmed",
        {
            "intent": result.to_dict(),
            "canceled_before_write": True,
            KEY: {
                "schema_version": 1,
                "predecessor": action.id,
                "original": original.to_dict(),
                "purpose": action.payload["purpose"],
                "tracking_key": action.payload.get("tracking_key", ""),
                "parent_key": parent_key,
                "reason": result.error,
                "context": (original.coordinates or {}).get("tracking_replan", {}).get("context")
                or _digest(_context(snapshot, snapshot.units[action.unit], parent_key)),
                "state": "waiting",
                "next_retry_at": now + RETRY_SECONDS,
                "no_send": result.to_dict(),
            },
        },
    )


def active(unit) -> bool:
    return any(row.get("state") == "waiting" for row in unit.evidence.get(KEY, {}).values())


def _target_identity(snapshot, intent):
    kind = {"close_issue": "issue", "close_milestone": "milestone"}.get(intent.kind)
    receipt, coordinates = intent.receipt or {}, intent.coordinates or {}
    native_id = coordinates.get("id") or receipt.get("id")
    if intent.path:
        prefix = f"repos/{snapshot.repository}/{kind}s/"
        if not kind or not intent.path.startswith(prefix):
            return None
        number = intent.path.removeprefix(prefix)
        if not number.isdecimal():
            return None
        number = int(number)
    else:
        number = receipt.get("number")
    if type(native_id) is not int or native_id <= 0 or type(number) is not int or number <= 0:
        return None
    return kind, native_id, number


def _target_keys(snapshot, action, intent):
    identity = _target_identity(snapshot, intent)
    if identity is None:
        return ()
    keys = tuple(
        sorted(
            key
            for key, binding in snapshot.bindings.get("github_hierarchy", {}).items()
            if key.startswith(("epic:", "initiative:"))
            and tuple(binding.get(field) for field in ("kind", "database_id", "number")) == identity
        )
    )
    expected = action.payload.get("parent_key")
    return keys if not expected or expected in keys else ()


def target_members(snapshot, action, intent) -> tuple[str, ...]:
    """Resolve current exact targets, including historical aggregate parent closes.

    An empty result is unknown authority, not an empty complete scope. The
    action's scheduling anchor never substitutes for its actual native target.
    """
    if action.payload.get("purpose") == "parent-tracking":
        from .campaign_completion_scope import parent_members

        return tuple(
            sorted(
                {
                    name
                    for key in _target_keys(snapshot, action, intent)
                    for name in parent_members(snapshot, key)
                }
            )
        )
    if action.payload.get("purpose") == "tracking":
        unit = snapshot.units[action.unit]
        binding = unit.bindings.get("issue", {})
        if _target_identity(snapshot, intent) == (
            "issue",
            binding.get("database_id"),
            binding.get("number"),
        ):
            return (unit.id,)
    return ()


def readback(runtime, snapshot, action, intent) -> MutationIntent:
    """Refresh a close's actual target without replaying an acknowledged write."""
    coordinates = dict(intent.coordinates or {})
    if intent.state == "confirmed" and not coordinates.get("tracking_acknowledgement"):
        coordinates["tracking_acknowledgement"] = {
            "schema_version": 1,
            "action_id": intent.action_id,
            "receipt": copy.deepcopy(intent.receipt),
            "generation": intent.generation,
        }
    identity = _target_identity(snapshot, intent)
    if identity is None or not target_members(snapshot, action, intent):
        return dataclasses.replace(
            intent,
            state="sent_unknown",
            coordinates=coordinates,
            error="Close readback native target is unknown",
        )
    kind, native_id, number = identity
    path = f"repos/{snapshot.repository}/{kind}s/{number}"
    desired = {"state": "closed", **({"state_reason": "completed"} if kind == "issue" else {})}
    coordinates.setdefault("id", native_id)
    try:
        current = runtime.gateway(snapshot).get(path)
        if not isinstance(current, dict):
            raise GithubError("Close readback has no native object payload")
    except GithubError as exc:
        return dataclasses.replace(
            intent,
            path=path,
            payload=desired,
            state="sent_unknown",
            coordinates=coordinates,
            error=str(exc),
            fault=exc.to_dict(),
            retry_after_s=exc.retry_after_s,
        )
    matches = current.get("id") == native_id and current.get("number") == number
    complete = matches and all(current.get(key) == value for key, value in desired.items())
    reason = (
        ""
        if complete
        else (
            "Close readback native identity changed"
            if not matches
            else "Actual native close is not completed; acknowledged work is readback-only"
        )
    )
    return dataclasses.replace(
        intent,
        path=path,
        payload=desired,
        coordinates=coordinates,
        state="confirmed" if complete else "sent_unknown",
        receipt=copy.deepcopy(current),
        error=reason,
        fault=None if complete else intent.fault,
    )


def success_guard(snapshot, action, intent) -> str:
    """Recheck current local obligations before a close receives delivery credit."""
    if action.payload.get("purpose") not in {"tracking", "parent-tracking"} or intent.kind not in {
        "close_issue",
        "close_milestone",
    }:
        return ""
    unit = snapshot.units[action.unit]
    parent_key = action.payload.get("parent_key", "")
    members = target_members(snapshot, action, intent)
    if not members:
        return "Tracking completion native target/member scope changed or is unproven"
    for name in members:
        member = snapshot.units[name]
        if member.hold or member.claim and not member.claim.quiescent:
            return "Tracking completion retains current member holds and live ownership"
    coordinates = intent.coordinates or {}
    expected = coordinates.get("tracking_recovery_context") or coordinates.get(
        "tracking_replan", {}
    ).get("context")
    if expected and expected != _digest(_context(snapshot, unit, parent_key)):
        return "Tracking completion source, ownership or acceptance obligations changed"
    if action.payload.get("purpose") != "parent-tracking" and (
        reason := next(
            (reason for name in members if (reason := acceptance_wait(snapshot.units[name]))), ""
        )
    ):
        return reason
    if any(
        snapshot.units[name].evidence.get(key) for name in members for key in PENDING_OBLIGATIONS
    ):
        return "Tracking completion retains unresolved approval or pending hold obligations"
    if action.payload.get("purpose") == "parent-tracking":
        from .campaign_completion_scope import parent_wait_reason, retained_parent_reason

        for key in _target_keys(snapshot, action, intent):
            if reason := retained_parent_reason(snapshot, key) or parent_wait_reason(snapshot, key):
                return reason
    else:
        from .campaign_landing_acceptance import satisfied
        from .campaign_runtime_tracking import pending_tracking

        if (
            unit.state != UnitState.MERGED_PENDING_SYNC
            or not satisfied(unit)
            or pending_tracking(snapshot, unit)
        ):
            return "Tracking completion waits for current merged acceptance and canonical source tracking"
    if intent.state == "confirmed" and (
        (intent.receipt or {}).get("state") != "closed"
        or intent.kind == "close_issue"
        and (intent.receipt or {}).get("state_reason") != "completed"
        or ((intent.receipt or {}).get("id"), (intent.receipt or {}).get("number"))
        != (_target_identity(snapshot, intent) or (None, None, None))[1:]
    ):
        return "Tracking completion requires an actual completed issue close"
    return ""


def pending_parents(snapshot):
    return {
        row["parent_key"]
        for unit in snapshot.units.values()
        for row in unit.evidence.get(KEY, {}).values()
        if row.get("state") == "waiting" and row.get("parent_key")
    }


def proposals(snapshot, *, now):
    return tuple(
        Proposal(
            "tracking-reconcile",
            unit.id,
            {"predecessor": predecessor, "parent_key": row.get("parent_key", "")},
        )
        for unit in snapshot.units.values()
        for predecessor, row in unit.evidence.get(KEY, {}).items()
        if row.get("state") == "waiting"
        and now >= row.get("next_retry_at", 0)
        and not any(
            action.kind == "tracking-reconcile"
            and action.payload.get("predecessor") == predecessor
            and action.state in {"prepared", "sent_unknown"}
            for action in snapshot.outbox
        )
    )


def perform(runtime, snapshot, action):
    """Observe or prepare one linked successor; this function never writes GitHub."""
    unit = snapshot.units[action.unit]
    predecessor = action.payload["predecessor"]
    debt = unit.evidence.get(KEY, {}).get(predecessor)
    if not debt or debt.get("state") != "waiting":
        return Observation("confirmed", {"tracking_recovery_stale": True})
    original = MutationIntent.from_dict(debt["original"])
    gateway = runtime.gateway(snapshot)
    parent_key = debt.get("parent_key", "")

    def waiting(reason):
        return Observation("waiting", reason=reason, retry_at=runtime.clock() + RETRY_SECONDS)

    # Preserve holds even if the remote issue is already completed.
    if any(snapshot.units[name].hold for name in _members(snapshot, unit, parent_key)):
        return waiting("Tracking recovery retains a current member hold")
    if any(
        snapshot.units[name].claim and not snapshot.units[name].claim.quiescent
        for name in _members(snapshot, unit, parent_key)
    ):
        return waiting("Tracking recovery waits for member quiescence")
    if debt.get("context") != _digest(_context(snapshot, unit, parent_key)):
        return waiting("Tracking source, ownership or acceptance obligations changed; retain debt")
    if not parent_key and (
        reason := next(
            (
                reason
                for name in _members(snapshot, unit, parent_key)
                if (reason := acceptance_wait(snapshot.units[name]))
            ),
            "",
        )
    ):
        return waiting(reason)
    if any(
        snapshot.units[name].evidence.get(key)
        for name in _members(snapshot, unit, parent_key)
        for key in PENDING_OBLIGATIONS
    ):
        return waiting("Tracking recovery retains unresolved approval or pending hold obligations")
    if parent_key:
        from .campaign_parent_closure import guard

        fake_action = dataclasses.replace(
            action, payload={**action.payload, "scope_revision": snapshot.graph_revision}
        )
        blocked = guard(runtime, snapshot, fake_action, original)
        if blocked:
            return waiting(
                blocked.reason or blocked.data.get("reason") or "Parent tracking scope changed"
            )
    else:
        from .campaign_landing_acceptance import satisfied

        if unit.state != UnitState.MERGED_PENDING_SYNC or not satisfied(unit):
            return waiting("Tracking recovery waits for current merged acceptance")
    coordinates = original.coordinates or {}
    number = int(original.path.rsplit("/", 1)[-1])
    current = gateway.issue(number)
    if current.get("id") != coordinates.get("id"):
        return waiting("Tracking recovery native issue identity changed")
    if current.get("state") == "closed":
        if current.get("state_reason") != "completed":
            return waiting(
                "Human close reason is not completed; retain the issue and reconcile its actual condition"
            )
        intent = dataclasses.replace(
            original,
            action_id=action.id + "-observed",
            state="confirmed",
            receipt=current,
            error="",
            fault=None,
            generation=snapshot.generation,
            coordinates={**coordinates, "tracking_recovery_context": debt["context"]},
        )
        return Observation(
            "confirmed",
            {"tracking_recovery_successor": intent.to_dict(), "predecessor": predecessor},
        )
    plan = coordinates.get("tracking_replan") or {}
    if plan.get("schema_version") != 1 or not plan.get("preimage", {}).get("complete"):
        return waiting(
            "Legacy close lacks a complete recovery baseline; observe actual completed closure"
        )
    if plan.get("context") != _digest(_context(snapshot, unit, parent_key)):
        return waiting("Tracking source, ownership or acceptance obligations changed; retain debt")
    if type(plan.get("replans")) is not int or not 0 <= plan["replans"] < MAX_REPLANS:
        return waiting(
            "Tracking replan allowance exhausted (maximum 3); reconcile actual remote condition"
        )
    issue, preimage = _observe(gateway, snapshot, number)
    if preimage["held"] or preimage["digest"] != plan["preimage"].get("digest"):
        return waiting(
            "Tracking human discussion, ownership, status policy or parent scope changed; retain debt"
        )
    if issue.get("state") != "open":
        return waiting("Tracking issue lifecycle changed during recovery observation")
    successor = dataclasses.replace(
        original,
        action_id=action.id + "-close",
        state="prepared",
        receipt=None,
        error="",
        fault=None,
        generation=snapshot.generation,
        coordinates={
            **coordinates,
            "updated_at": issue["updated_at"],
            "tracking_replan": {
                **plan,
                "replans": plan["replans"] + 1,
                "preimage": preimage,
            },
        },
    )
    return Observation(
        "confirmed",
        {"tracking_recovery_successor": successor.to_dict(), "predecessor": predecessor},
    )


def apply(runtime, snapshot, action, data):
    """Commit a no-send debt or a single successor in the caller's transaction."""
    unit = snapshot.units[action.unit]
    if data.get(KEY):
        unit.evidence.setdefault(KEY, {}).setdefault(action.id, data[KEY])
        add_event(
            snapshot,
            "tracking-precondition-retained",
            unit=unit.id,
            action=action.id,
            reason=data[KEY]["reason"],
        )
        return
    if data.get("tracking_recovery_stale"):
        return
    predecessor = data["predecessor"]
    debt = unit.evidence.get(KEY, {}).get(predecessor)
    if not debt or debt.get("state") != "waiting":
        return
    successor = data["tracking_recovery_successor"]
    runtime._queue_intent(
        snapshot,
        unit,
        {
            "intent": successor,
            "purpose": debt["purpose"],
            "parent_key": debt.get("parent_key", ""),
        },
        tracking_key=debt.get("tracking_key", ""),
    )
    debt.update(state="replanned", successor=successor["action_id"])
    add_event(
        snapshot,
        "tracking-close-replanned",
        unit=unit.id,
        predecessor=predecessor,
        successor=successor["action_id"],
    )
