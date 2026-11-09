"""Production intake coordination through owned triage and planning draft PRs.

Effects read GitHub through the gateway; state mutators run only in the single
writer. Triage uses an ordinary child coding session in a registered planning
workspace. New work remains outside the executable graph until that same PR's
independently reviewed sources are published and its graph activates.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import re
from typing import Any, Mapping

from . import campaign_intake_flow as flow
from .campaign_action_history import actions as historical_actions
from .campaign_github import (
    GithubError,
    GithubGateway,
    MutationIntent,
    OptionalIntakeCancellation,
    managed_marker,
)
from .campaign_intake import GithubIntake
from .campaign_model import (
    CampaignError,
    CampaignSnapshot,
    CampaignUnit,
    Hold,
    RoleCandidate,
    UnitSpec,
    UnitState,
    unit_from_dict,
)
from .campaign_scheduler import resource_available
from .campaign_store import add_event, transition
from .campaign_supervisor import Proposal

CONTRACT = "intake-triage-v1"
_NONWORK = frozenset({"attach", "repair_current_pr", "duplicate", "needs_information", "hold"})


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def control(unit: CampaignUnit) -> dict[str, Any] | None:
    value = unit.bindings.get("intake_control")
    return value if isinstance(value, dict) and value.get("schema_version") == 1 else None


def owns_stage(unit: CampaignUnit) -> bool:
    value = control(unit)
    return bool(value and value.get("state") != "planning")


def ready_for_planning(snapshot: CampaignSnapshot, unit: CampaignUnit) -> bool:
    value = control(unit)
    if not value or not value.get("amendments"):
        return True
    return all(
        not (target.claim and not target.claim.quiescent)
        and not any(
            action.unit == target.id and action.state in {"prepared", "sent_unknown"}
            for action in snapshot.outbox
        )
        for key in value["amendments"]
        for target in (snapshot.units[key],)
    )


def _number(value: Any) -> int | None:
    number = value.get("number") if isinstance(value, dict) else value
    return number if type(number) is int and number > 0 else None


def _owned_projection(snapshot: CampaignSnapshot, observation: Mapping[str, Any]) -> bool:
    """Suppress only exact native objects and managed content, never a marker quote.

    Known comments are excluded by native receipt ID and exact written body.
    Edits to them become new input. Created hierarchy issues are excluded while
    their managed fields still match; comments on them are independently polled.
    """
    payload = observation["payload"]
    history = historical_actions(snapshot, native_id=observation["object_id"])
    for action in reversed(history):
        intent = action.receipt.get("intent") or action.payload.get("intent") or {}
        receipt = intent.get("receipt") or {}
        requested = intent.get("payload") or {}
        if (
            action.state != "confirmed"
            or intent.get("state") != "confirmed"
            or str(receipt.get("id", "")) != observation["object_id"]
        ):
            continue
        if observation["kind"] == "comment" and intent.get("kind") == "comment":
            return payload.get("body") == requested.get("body") and (
                not receipt.get("user", {}).get("id")
                or (payload.get("user") or {}).get("id") == receipt["user"]["id"]
            )
        if observation["kind"] == "issue" and intent.get("kind") == "issue":
            desired_state = "open"
            desired_milestone = requested.get("milestone")
            for later in history:
                update = later.receipt.get("intent") or later.payload.get("intent") or {}
                if (
                    later.state == "rejected"
                    or str(
                        (update.get("receipt") or {}).get("id")
                        or (update.get("coordinates") or {}).get("id", "")
                    )
                    != observation["object_id"]
                ):
                    continue
                if update.get("kind") in {"close_issue", "close_issue_not_planned"}:
                    desired_state = "closed"
                if update.get("kind") == "reopen_issue":
                    desired_state = "open"
                if update.get("kind") == "milestone_assignment":
                    desired_milestone = (update.get("payload") or {}).get("milestone")
            return (
                payload.get("title") == requested.get("title")
                and payload.get("body") == requested.get("body")
                and payload.get("state") == desired_state
                and payload.get("labels", []) == receipt.get("labels", [])
                and payload.get("assignees", []) == receipt.get("assignees", [])
                and (payload.get("milestone") or {}).get("number") == desired_milestone
            )
    if observation["kind"] == "issue":
        state = snapshot.bindings.get(flow.INBOX, {})
        previous_key = state.get("latest", {}).get("issue:" + observation["object_id"])
        previous = (
            state.get("records", {}).get(previous_key, {}).get("observation", {}).get("payload")
        )
        if previous is None:
            previous = next(
                (
                    row
                    for row in snapshot.bindings.get("inventory", {}).get("issues", [])
                    if str(row.get("id")) == observation["object_id"]
                ),
                None,
            )
        if previous is not None:
            desired_state = previous.get("state")
            desired_milestone = (previous.get("milestone") or {}).get("number")
            owned = False
            for action in history:
                intent = action.receipt.get("intent") or action.payload.get("intent") or {}
                if (
                    action.state == "rejected"
                    or str(
                        (intent.get("receipt") or {}).get("id")
                        or (intent.get("coordinates") or {}).get("id", "")
                    )
                    != observation["object_id"]
                ):
                    continue
                if intent.get("kind") in {"close_issue", "close_issue_not_planned"}:
                    desired_state, owned = "closed", True
                if intent.get("kind") == "reopen_issue":
                    desired_state, owned = "open", True
                if intent.get("kind") == "milestone_assignment":
                    desired_milestone, owned = intent["payload"]["milestone"], True
            if owned:
                return (
                    payload.get("body") == previous.get("body")
                    and payload.get("title") == previous.get("title")
                    and payload.get("labels", []) == previous.get("labels", [])
                    and payload.get("assignees", []) == previous.get("assignees", [])
                    and payload.get("state") == desired_state
                    and (payload.get("milestone") or {}).get("number") == desired_milestone
                )
    return False


def collect(
    snapshot: CampaignSnapshot,
    gateway: GithubGateway,
    *,
    now: float,
    prior_projections: tuple[CampaignSnapshot, ...] = (),
) -> dict[str, Any]:
    prior = snapshot.bindings.get("intake", {})
    mapped: set[int] = set()
    for unit in snapshot.units.values():
        for name in ("issue", "pr", "epic_issue"):
            number = _number(unit.bindings.get(name))
            if number:
                mapped.add(number)
    for binding in snapshot.bindings.get("github_hierarchy", {}).values():
        if binding.get("kind") == "issue" and _number(binding):
            mapped.add(binding["number"])
    batch = GithubIntake(gateway).poll(
        cursor=prior.get("cursor", ""),
        seen_versions=set(prior.get("seen_versions", [])),
        mode=snapshot.spec.intake.issues,
        labels=snapshot.spec.intake.labels,
        selected_numbers=snapshot.spec.intake.issue_numbers,
        mapped_numbers=tuple(sorted(mapped)),
        max_batch=snapshot.spec.intake.batch_size,
    )
    keep, suppressed, owned_observations = [], [], []
    for observation in batch.observations:
        if _owned_projection(snapshot, observation.to_dict()) or any(
            prior.repository == snapshot.repository
            and prior.common_dir == snapshot.common_dir
            and _owned_projection(prior, observation.to_dict())
            for prior in prior_projections
        ):
            suppressed.append(observation.version_id)
            owned_observations.append(observation.to_dict())
        else:
            keep.append(observation)
    return {
        "batch": dataclasses.asdict(dataclasses.replace(batch, observations=tuple(keep))),
        "observed_at": now,
        "suppressed_versions": suppressed,
        "owned_projection_observations": owned_observations,
    }


def apply_poll(snapshot: CampaignSnapshot, data: Mapping[str, Any]) -> None:
    prior = set(snapshot.bindings.get("intake", {}).get("suppressed_versions", []))
    # The poll ran against an immutable earlier snapshot. A concurrent write's
    # ACK may have arrived before this result is applied by the state writer.
    batch = dict(data["batch"])
    incoming = []
    for row in batch["observations"]:
        if _owned_projection(snapshot, row):
            from .campaign_intake import IntakeObservation

            prior.add(IntakeObservation(**row).version_id)
        else:
            incoming.append(row)
    batch["observations"] = incoming
    flow.apply_batch(snapshot, batch, observed_at=data["observed_at"])
    prior.update(data.get("suppressed_versions", []))
    intake = snapshot.bindings["intake"]
    intake["suppressed_versions"] = sorted(prior)
    intake["seen_versions"] = sorted(set(intake["seen_versions"]) | prior)


def proposals(snapshot: CampaignSnapshot, candidate: RoleCandidate | None) -> tuple[Proposal, ...]:
    if snapshot.phase != "active":
        return ()
    output = []
    for key, unit in snapshot.units.items():
        value = control(unit)
        # Preparing an intent and executing its GitHub write are distinct
        # durable actions. Wait for either action before proposing another
        # random-ID comment/cancellation for the same control.
        if value and any(
            action.unit == key
            and action.state in {"prepared", "sent_unknown"}
            and action.kind not in {"observe-worker", "reflection"}
            for action in snapshot.outbox
        ):
            continue
        if (
            value
            and value.get("state") == "completed"
            and value.get("amendments")
            and not value.get("completion_noted")
            and all(
                snapshot.units[target].state == UnitState.DELIVERED
                for target in value["amendments"]
            )
        ):
            if not any(
                action.unit == key
                and action.kind == "intake-completion-note"
                and action.state in {"prepared", "sent_unknown"}
                for action in snapshot.outbox
            ):
                output.append(Proposal("intake-completion-note", key))
            continue
        if value and value.get("state") == "refreshing":
            for target_id in value["amendments"]:
                target = snapshot.units[target_id]
                if (
                    target_id in value.get("refreshed", [])
                    or (
                        target.hold
                        and (
                            target.hold.kind != "intake_plan_change"
                            or target.hold.id != value["attempt_id"]
                        )
                    )
                    or (target.claim and not target.claim.quiescent)
                    or any(
                        action.unit == target_id and action.state in {"prepared", "sent_unknown"}
                        for action in snapshot.outbox
                    )
                ):
                    continue
                output.append(
                    Proposal(
                        "intake-refresh",
                        target_id,
                        {"planning_unit": key, "proposal_id": value["proposal_id"]},
                    )
                )
            continue
        if (
            value
            and value.get("state") in {"planning", "nonwork"}
            and not value.get("disposition_noted")
        ):
            if not any(
                action.unit == key
                and action.kind == "intake-note"
                and action.state in {"prepared", "sent_unknown"}
                for action in snapshot.outbox
            ):
                output.append(Proposal("intake-note", key))
            continue
        if (
            not value
            or value.get("state") != "nonwork"
            or unit.hold
            or (unit.claim and not unit.claim.quiescent)
        ):
            continue
        if not any(
            action.unit == key
            and action.kind
            == ("intake-cancel" if value.get("issue_canceled") else "intake-cancel-issue")
            and action.state in {"prepared", "sent_unknown"}
            for action in snapshot.outbox
        ):
            output.append(
                Proposal(
                    "intake-cancel" if value.get("issue_canceled") else "intake-cancel-issue", key
                )
            )
    # Planning reviews read the complete campaign graph. Its declaration writer
    # owns one planning transaction while execution of accepted work proceeds.
    if any(
        unit.spec.kind == "planning" and unit.state not in {UnitState.DELIVERED, UnitState.CANCELED}
        for unit in snapshot.units.values()
    ):
        return tuple(output)
    if candidate is None or not resource_available(snapshot, "planning"):
        return tuple(output)
    if any(
        action.kind == "intake-control" and action.state in {"prepared", "sent_unknown"}
        for action in snapshot.outbox
    ):
        return tuple(output)
    request = flow.next(snapshot)
    if request:
        output.append(
            Proposal(
                "intake-control",
                "",
                {**request.payload, "candidate": dataclasses.asdict(candidate)},
                action_id="intake-control-" + request.payload["packet_id"],
            )
        )
    return tuple(output)


def create_control(snapshot: CampaignSnapshot, payload: Mapping[str, Any]) -> str:
    versions = list(payload["input_versions"])
    key = "campaign:" + snapshot.id + ":intake:" + str(payload["packet_id"])[:16]
    if key in snapshot.units:
        existing = control(snapshot.units[key])
        if not existing or existing["input_versions"] != versions:
            raise CampaignError("intake control identity reused for different evidence")
        return key
    records = snapshot.bindings[flow.INBOX]["records"]
    if all(_owned_projection(snapshot, records[version]["observation"]) for version in versions):
        for version in versions:
            records[version]["state"] = "projection"
        add_event(snapshot, "intake-owned-projection", inputs=versions)
        return ""
    context = flow.packet(snapshot, versions)
    state = snapshot.bindings[flow.INBOX]
    for version in versions:
        if state["records"][version]["state"] != "pending":
            raise CampaignError("incoming evidence is already reserved or superseded")
    number = context["observations"][0]["number"]
    spec = UnitSpec(
        key,
        "campaign-intake",
        "intake",
        f"Triage GitHub issue #{number}",
        kind="planning",
        acceptance=(
            "Disposition the exact incoming GitHub evidence within approved scope.",
            "Review and deliver any proposed BMAD source graph before implementation.",
        ),
        scopes=("resource:campaign-plan",),
        order=-1,
    )
    unit = CampaignUnit(spec)
    unit.bindings["intake_control"] = {
        "schema_version": 1,
        "input_versions": versions,
        "state": "triage_pending",
        "optional": True,
        "packet_id": payload["packet_id"],
    }
    snapshot.units[key] = unit
    snapshot.graph_revision += 1
    for version in versions:
        state["records"][version]["state"] = "reserved"
        state["records"][version]["control_unit"] = key
    add_event(snapshot, "intake-control-created", unit=key, inputs=versions)
    return key


def baseline(snapshot: CampaignSnapshot, unit_id: str) -> str:
    value = control(snapshot.units[unit_id])
    if not value:
        return ""
    chunks = ["# Incoming GitHub intent", ""]
    for version in value["input_versions"]:
        row = snapshot.bindings[flow.INBOX]["records"][version]["observation"]
        payload = row["payload"]
        body = str(payload.get("body", ""))
        body = re.sub(
            r"(?:sk-(?:ant-)?|gh[pousr]_|github_pat_|xox[baprs]-)[A-Za-z0-9_-]+",
            "<redacted:credential>",
            body,
        )
        chunks += [
            f"Source: {row['url']}",
            f"Revision: {row['updated_at']} / {row['digest']}",
            f"Title: {payload.get('title', 'Discussion')}",
            "",
            body,
            "",
        ]
    return "\n".join(chunks)


def stage_proposal(
    snapshot: CampaignSnapshot, unit_id: str, candidate: RoleCandidate | None
) -> Proposal | None:
    unit = snapshot.units[unit_id]
    value = control(unit)
    if (
        not value
        or value["state"] != "triage_pending"
        or unit.hold
        or candidate is None
        or unit.state != UnitState.READY
        or (unit.claim and not unit.claim.quiescent)
        or not resource_available(snapshot, "planning")
    ):
        return None
    return Proposal(
        "launch",
        unit_id,
        {
            "kind": "stage",
            "stage": "triage",
            "role": "triage",
            "candidate": dataclasses.asdict(candidate),
            "read_only": True,
            "result_contract": CONTRACT,
        },
    )


def prepare_stage(
    snapshot: CampaignSnapshot, unit_id: str, payload: Mapping[str, Any], *, launch_id: str
) -> None:
    if payload.get("result_contract") != CONTRACT:
        return
    unit = snapshot.units[unit_id]
    value = control(unit)
    if not value or value["state"] != "triage_pending":
        raise CampaignError("triage launch no longer owns its intake control")
    rows = snapshot.bindings[flow.INBOX]["records"]
    for version in value["input_versions"]:
        if rows[version]["state"] != "reserved" or rows[version].get("control_unit") != unit_id:
            raise CampaignError("triage input reservation changed")
    for version in value["input_versions"]:
        rows[version]["state"] = "pending"
    selected = RoleCandidate(**payload["candidate"])
    flow.prepare_triage(
        snapshot, value["input_versions"], attempt_id=launch_id, selected_role=selected
    )
    value["state"] = "triage_running"
    value["attempt_id"] = launch_id


def stage_context(snapshot: CampaignSnapshot, unit_id: str) -> dict[str, Any]:
    value = control(snapshot.units[unit_id])
    if not value:
        return {}
    packet = flow.packet(snapshot, value["input_versions"])
    return {
        "intake": packet,
        "intake_coordinates": packet["intake_coordinates"],
        "triage_result_contract": packet["result_contract"],
        "current_unit_states": {key: unit.state.value for key, unit in snapshot.units.items()},
        "accepted_intake_proposal": value.get("proposal_id"),
        "proposed_intake_units": snapshot.bindings[flow.INBOX]["proposals"]
        .get(value.get("proposal_id"), {})
        .get("units", []),
        "triage_failures": value.get("failures", []),
        "amendment_constraints": "Preserve every existing unit identity, scope, dependency, source selector and PR. Changed acceptance requires reviewed source artifacts and a quiescent original actor. Delivered/merge-requested work requires a new scoped follow-up story identity; it cannot be amended or reopened.",
    }


def apply_triage(
    snapshot: CampaignSnapshot, unit_id: str, receipt: Mapping[str, Any], *, launch_id: str
) -> str | None:
    unit = snapshot.units[unit_id]
    value = control(unit)
    if (
        not value
        or value.get("attempt_id") != launch_id
        or value["state"] != "triage_running"
        or (unit.claim and not unit.claim.quiescent)
    ):
        raise CampaignError("triage result is not from its quiescent owned actor")
    selected = snapshot.bindings[flow.INBOX]["attempts"][launch_id]["selected_role"]
    observed = RoleCandidate(
        selected["profile"],
        receipt.get("observed_model", ""),
        selected["effort"],
        receipt.get("provider_family", ""),
    )
    raw = receipt["artifact"]
    requested = raw.get("proposal") or {}
    target = snapshot.units.get(requested.get("unit_id", ""))
    if (
        requested.get("disposition") == "repair_current_pr"
        and target
        and target.state
        in {
            UnitState.MERGE_REQUESTED,
            UnitState.MERGED_PENDING_SYNC,
            UnitState.DELIVERED,
        }
    ):
        raise CampaignError("landed work requires a new scoped story, not repair of a merged PR")
    proposal_id = flow.accept_triage(
        snapshot, raw, attempt_id=launch_id, observed_role=observed, planning_unit_id=unit_id
    )
    disposition = snapshot.bindings[flow.INBOX]["attempts"][launch_id]["disposition"]
    value["disposition"] = disposition
    if proposal_id:
        value.update(state="planning", optional=False, proposal_id=proposal_id)
        proposal = snapshot.bindings[flow.INBOX]["proposals"][proposal_id]
        if proposal.get("amendment"):
            value["amendments"] = proposal["preimages"]
        unit.evidence["intake_proposal_digest"] = snapshot.bindings[flow.INBOX]["proposals"][
            proposal_id
        ]["digest"]
    else:
        value["state"] = "nonwork"
        target = snapshot.units.get(disposition.get("unit_id"))
        if disposition["disposition"] == "repair_current_pr" and target:
            if target.state in {
                UnitState.MERGE_REQUESTED,
                UnitState.MERGED_PENDING_SYNC,
                UnitState.DELIVERED,
            }:
                raise CampaignError(
                    "landed work requires a new scoped story, not repair of a merged PR"
                )
            target.evidence.setdefault("required_findings", []).append(
                {
                    "id": launch_id,
                    "required": True,
                    "resolved": False,
                    "message": disposition["rationale"],
                    "input_versions": value["input_versions"],
                }
            )
            if not target.hold:
                target.hold = Hold(
                    "review_findings",
                    "Incoming feedback requires repair in the existing PR",
                    id=launch_id,
                    actor="intake",
                )
    add_event(
        snapshot,
        "intake-triage-accepted",
        unit=unit_id,
        disposition=disposition["disposition"],
        proposal=proposal_id,
    )
    return proposal_id


def validate_receipt(
    snapshot: CampaignSnapshot, unit_id: str, receipt: Mapping[str, Any], *, launch_id: str
) -> None:
    """Validate on a clone after the caller observes actual actor teardown.

    The clone models applying a completed receipt; it cannot establish process
    completion or publish state. The state-writer repeats validation on apply.
    """
    candidate = snapshot.clone()
    unit = candidate.units[unit_id]
    if not unit.claim or unit.claim.launch_id != launch_id:
        raise CampaignError("triage receipt no longer owns its launch")
    unit.claim.quiescent = True
    apply_triage(candidate, unit_id, receipt, launch_id=launch_id)


def fail_stage(
    snapshot: CampaignSnapshot, unit_id: str, receipt: Mapping[str, Any], *, launch_id: str
) -> None:
    unit = snapshot.units[unit_id]
    value = control(unit)
    if (
        not value
        or value.get("attempt_id") != launch_id
        or (unit.claim and not unit.claim.quiescent)
    ):
        raise CampaignError("failed triage has no quiescent owned attempt")
    reason = str(receipt.get("error", receipt.get("outcome", "triage failed")))
    flow.fail_triage(snapshot, launch_id, reason)
    value.setdefault("failures", []).append(reason)
    rows = snapshot.bindings[flow.INBOX]["records"]
    if all(rows[version]["state"] == "pending" for version in value["input_versions"]):
        for version in value["input_versions"]:
            rows[version]["state"] = "reserved"
            rows[version]["control_unit"] = unit_id
        value["state"] = "triage_pending"
        transition(snapshot, unit_id, UnitState.READY)
    else:
        unit.hold = Hold("triage_attempts_exhausted", reason, id=launch_id, actor="intake")


def disposition_intent(
    snapshot: CampaignSnapshot, unit_id: str, gateway: GithubGateway, *, action_id: str
) -> MutationIntent:
    unit = snapshot.units[unit_id]
    value = control(unit)
    if (
        not value
        or value.get("state") not in {"planning", "nonwork"}
        or not value.get("disposition")
    ):
        raise CampaignError("intake has no accepted disposition to reflect")
    state = snapshot.bindings[flow.INBOX]
    evidence = [state["records"][version]["observation"] for version in value["input_versions"]]
    number = evidence[0]["number"]
    disposition = value["disposition"]
    text = f"BMAD triage: **{disposition['disposition']}**.\n\n{disposition['rationale']}"
    pr = unit.bindings.get("pr") or {}
    if pr.get("url"):
        text += f"\n\nPlanning/triage draft: {pr['url']}"
    if disposition.get("related_pr"):
        text += f"\n\nRelated PR: https://github.com/{snapshot.repository}/pull/{disposition['related_pr']}"
    text += "\n\nEvidence:\n" + "\n".join(
        f"- {row['url']} ({row['updated_at']}, `{row['digest']}`)" for row in evidence
    )
    return gateway.prepare_comment(
        number,
        action_id=action_id,
        campaign_id=snapshot.id,
        unit_id=unit_id,
        text=text,
        generation=snapshot.generation,
    )


def apply_note(snapshot: CampaignSnapshot, unit_id: str, intent: Mapping[str, Any]) -> None:
    value = control(snapshot.units[unit_id])
    if (
        not value
        or intent.get("state") != "confirmed"
        or not (intent.get("receipt") or {}).get("id")
    ):
        raise CampaignError("intake disposition comment is not confirmed")
    value["disposition_noted"] = True


def completion_intent(
    snapshot: CampaignSnapshot, unit_id: str, gateway: GithubGateway, *, action_id: str
) -> MutationIntent:
    value = control(snapshot.units[unit_id])
    if (
        not value
        or value.get("state") != "completed"
        or not value.get("amendments")
        or any(snapshot.units[key].state != UnitState.DELIVERED for key in value["amendments"])
    ):
        raise CampaignError(
            "amendment completion reflection waits for actual merged source/tracking delivery"
        )
    observations = [
        snapshot.bindings[flow.INBOX]["records"][version]["observation"]
        for version in value["input_versions"]
    ]
    text = "BMAD feedback status: **addressed, owner issue open**.\n\nThe accepted evidence versions below are fulfilled by independently reviewed, merged work and confirmed canonical source publication."
    for key in value["amendments"]:
        unit = snapshot.units[key]
        publication = unit.evidence.get("source_publication") or {}
        if publication.get("state") != "confirmed" or not unit.evidence.get("merge", {}).get(
            "merge_commit"
        ):
            raise CampaignError("amendment completion lacks canonical delivery receipts")
        text += (
            f"\n\nDelivered PR: {unit.bindings['pr']['url']}\nMerge: `{unit.evidence['merge']['merge_commit']}`\nCanonical source: `{publication.get('source_revision') or publication.get('canonical_head')}`\nCoverage:\n"
            + "\n".join("- " + item for item in unit.spec.acceptance)
        )
    text += "\n\nEvidence versions:\n" + "\n".join(
        f"- {row['url']} ({row['updated_at']}, `{row['digest']}`)" for row in observations
    )
    return gateway.prepare_comment(
        observations[0]["number"],
        action_id=action_id,
        campaign_id=snapshot.id,
        unit_id=unit_id,
        text=text,
        generation=snapshot.generation,
    )


def apply_completion_note(
    snapshot: CampaignSnapshot, unit_id: str, intent: Mapping[str, Any]
) -> None:
    value = control(snapshot.units[unit_id])
    if (
        not value
        or intent.get("state") != "confirmed"
        or not (intent.get("receipt") or {}).get("id")
    ):
        raise CampaignError("amendment completion comment is not natively confirmed")
    value["completion_noted"] = True
    value["feedback_status"] = "addressed, owner issue open"


def validate_decomposition(
    snapshot: CampaignSnapshot, unit_id: str, raw: Mapping[str, Any]
) -> None:
    value = control(snapshot.units[unit_id])
    if not value or value["state"] != "planning":
        return
    proposal = snapshot.bindings[flow.INBOX]["proposals"][value["proposal_id"]]
    if proposal["state"] != "awaiting_planning_delivery":
        raise CampaignError("incoming evidence changed before source planning completed")
    expected = {unit["id"]: unit_from_dict(unit) for unit in proposal["units"]}
    proposed = {unit["id"]: unit_from_dict(unit) for unit in raw.get("units", [])}
    for key, unit in expected.items():
        if proposed.get(key) != unit:
            raise CampaignError("planning redefined the bounded accepted intake graph")
    for key, unit in proposed.items():
        if key not in expected and (key not in snapshot.units or snapshot.units[key].spec != unit):
            raise CampaignError("intake planning expands unrelated work or ownership")


def complete_delivery(snapshot: CampaignSnapshot, unit_id: str) -> None:
    unit = snapshot.units[unit_id]
    value = control(unit)
    if not value or value["state"] == "completed":
        return
    if (
        value["state"] != "planning"
        or unit.state != UnitState.DELIVERED
        or unit.evidence.get("planning_flow", {}).get("state") != "activated"
    ):
        raise CampaignError("intake graph is not delivered through its reviewed planning PR")
    state = snapshot.bindings[flow.INBOX]
    proposal = state["proposals"][value["proposal_id"]]
    if proposal["state"] != "awaiting_planning_delivery":
        raise CampaignError("intake evidence was superseded before delivery")
    expected = {item["id"]: unit_from_dict(item) for item in proposal["units"]}
    for key, spec in expected.items():
        if key not in snapshot.units or snapshot.units[key].spec != spec:
            raise CampaignError("planning activation differs from its accepted intake graph")
    if proposal.get("amendment"):
        proposal["state"] = "awaiting_refresh"
        value["state"] = "refreshing"
        add_event(
            snapshot,
            "intake-amendment-source-delivered",
            unit=unit_id,
            proposal=value["proposal_id"],
        )
        return
    primary = snapshot.units[proposal["triage"]["unit_id"]]
    number = proposal["triage"].get("related_issue")
    if number:
        source = snapshot.bindings.setdefault("source_observations", {}).setdefault(primary.id, {})
        source["tracker_id"] = f"https://github.com/{snapshot.repository}/issues/{number}"
    for version in proposal["input_versions"]:
        state["records"][version]["state"] = "resolved"
    proposal.update(state="activated", activated_graph_revision=snapshot.graph_revision)
    value["state"] = "completed"
    add_event(
        snapshot,
        "intake-planning-delivered",
        unit=unit_id,
        proposal=value["proposal_id"],
        units=list(expected),
    )


def validate_activation(snapshot: CampaignSnapshot, unit_id: str) -> None:
    value = control(snapshot.units[unit_id])
    if not value:
        return
    proposal = snapshot.bindings[flow.INBOX]["proposals"][value["proposal_id"]]
    if proposal["state"] != "awaiting_planning_delivery":
        raise CampaignError("incoming evidence changed before source graph activation")
    for version in proposal["input_versions"]:
        row = snapshot.bindings[flow.INBOX]["records"][version]["observation"]
        if (
            snapshot.bindings[flow.INBOX]["latest"].get(row["kind"] + ":" + row["object_id"])
            != version
        ):
            raise CampaignError("incoming issue version changed before source graph activation")
    for key, previous in value.get("amendments", {}).items():
        target = snapshot.units[key]
        if target.spec != unit_from_dict(previous) or target.state in {
            UnitState.MERGE_REQUESTED,
            UnitState.MERGED_PENDING_SYNC,
            UnitState.DELIVERED,
            UnitState.CANCELED,
        }:
            raise CampaignError(
                "original story changed or landed before amendment graph activation"
            )
        if (
            target.claim
            and not target.claim.quiescent
            or any(
                action.unit == key
                and action.state in {"prepared", "sent_unknown"}
                and action.kind not in {"reflection", "observe-worker"}
                for action in snapshot.outbox
            )
        ):
            raise CampaignError(
                "amendment graph activation waits for original actor and publishers to drain"
            )


def cancellation_intent(
    snapshot: CampaignSnapshot, unit_id: str, gateway: GithubGateway, *, action_id: str
) -> MutationIntent:
    unit = snapshot.units[unit_id]
    value = control(unit)
    if (
        not value
        or value["state"] != "nonwork"
        or value.get("disposition_noted") is not True
        or value.get("issue_canceled") is not True
        or value["disposition"]["disposition"] not in _NONWORK
        or (unit.claim and not unit.claim.quiescent)
    ):
        raise CampaignError("intake draft cancellation has no validated nonwork disposition")
    number = _number(unit.bindings.get("pr"))
    if not number:
        raise CampaignError("intake control has no bound draft PR")
    pr = gateway.pull(number)
    if (
        pr.get("merged")
        or pr.get("head", {}).get("ref") != unit.bindings.get("branch")
        or managed_marker(snapshot.id, unit_id, "pr") not in str(pr.get("body", ""))
    ):
        raise GithubError("intake cancellation cannot adopt another PR or cancel merged work")
    if pr.get("state") == "closed":
        return MutationIntent(
            action_id,
            "close_pr",
            "PATCH",
            "",
            {},
            state="confirmed",
            receipt=pr,
            generation=snapshot.generation,
        )
    if pr.get("draft") is not True:
        raise GithubError("operator made the intake PR ready; cancellation requires reconciliation")
    return MutationIntent(
        action_id,
        "close_pr",
        "PATCH",
        f"{gateway.root}/pulls/{number}",
        {"state": "closed"},
        generation=snapshot.generation,
        coordinates={
            "id": pr["id"],
            "updated_at": pr["updated_at"],
            "head": pr["head"]["sha"],
            "optional_intake_cancel": True,
        },
    )


def issue_cancellation_intent(
    snapshot: CampaignSnapshot, unit_id: str, gateway: GithubGateway, *, action_id: str
) -> MutationIntent:
    unit = snapshot.units[unit_id]
    value = control(unit)
    issue = unit.bindings.get("issue") or {}
    if (
        not value
        or value.get("state") != "nonwork"
        or not value.get("disposition_noted")
        or value["disposition"]["disposition"] not in _NONWORK
        or (unit.claim and not unit.claim.quiescent)
    ):
        raise CampaignError("internal issue cancellation requires validated nonwork disposition")
    return gateway.close_issue_not_planned_intent(
        issue["number"],
        capability=OptionalIntakeCancellation(
            snapshot.id, unit_id, issue["database_id"], value["attempt_id"]
        ),
        action_id=action_id,
        generation=snapshot.generation,
    )


def apply_issue_cancellation(
    snapshot: CampaignSnapshot, unit_id: str, intent: Mapping[str, Any]
) -> None:
    unit = snapshot.units[unit_id]
    value = control(unit)
    receipt = intent.get("receipt") or {}
    if (
        not value
        or value.get("state") != "nonwork"
        or intent.get("state") != "confirmed"
        or receipt.get("id") != unit.bindings.get("issue", {}).get("database_id")
        or receipt.get("state") != "closed"
        or receipt.get("state_reason") != "not_planned"
    ):
        raise CampaignError("optional internal issue cancellation is not natively confirmed")
    value["issue_canceled"] = True
    value["issue_cancellation"] = dict(intent)


def apply_cancellation(snapshot: CampaignSnapshot, unit_id: str, intent: Mapping[str, Any]) -> None:
    unit = snapshot.units[unit_id]
    value = control(unit)
    receipt = intent.get("receipt") or {}
    if (
        value
        and value.get("state") == "canceled"
        and optional_canceled(unit)
        and receipt.get("id") == value["cancellation"]["native_id"]
        and receipt.get("state") == "closed"
        and receipt.get("merged") is False
        and intent.get("state") == "confirmed"
    ):
        return
    if (
        not value
        or value.get("state") != "nonwork"
        or value.get("disposition_noted") is not True
        or value.get("issue_canceled") is not True
        or value["disposition"]["disposition"] not in _NONWORK
        or intent.get("state") != "confirmed"
        or receipt.get("number") != _number(unit.bindings.get("pr"))
        or receipt.get("state") != "closed"
        or receipt.get("merged") is not False
        or (unit.claim and not unit.claim.quiescent)
    ):
        raise CampaignError("optional intake cancellation is not independently confirmed")
    value.update(
        state="canceled",
        cancellation={
            "pr_number": receipt["number"],
            "native_id": receipt["id"],
            "head": receipt["head"]["sha"],
            "disposition": value["disposition"]["disposition"],
            "confirmed": True,
        },
    )
    transition(snapshot, unit_id, UnitState.CANCELED)


def optional_canceled(unit: CampaignUnit) -> bool:
    value = control(unit)
    proof = value.get("cancellation", {}) if value else {}
    return bool(
        value
        and value.get("optional") is True
        and value.get("state") == "canceled"
        and value.get("issue_canceled") is True
        and proof.get("confirmed") is True
        and proof.get("disposition") in _NONWORK
        and proof.get("pr_number") == _number(unit.bindings.get("pr"))
        and unit.state == UnitState.CANCELED
        and (not unit.claim or unit.claim.quiescent)
    )


def settled(snapshot: CampaignSnapshot) -> bool:
    """Unclassified intake and unactivated plans are real completion debt."""
    if snapshot.bindings.get("intake", {}).get("pending_count", 0):
        return False
    state = snapshot.bindings.get(flow.INBOX, {})
    if any(
        value
        and value.get("state") == "completed"
        and value.get("amendments")
        and not value.get("completion_noted")
        for unit in snapshot.units.values()
        for value in (control(unit),)
    ):
        return False
    return not any(
        row.get("state") in {"pending", "reserved", "triaging", "awaiting_planning_delivery"}
        or (row.get("state") == "held" and not row.get("disposition"))
        for row in state.get("records", {}).values()
    )
