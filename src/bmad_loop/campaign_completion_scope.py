"""Selection delivery does not grant authority to close a native parent scope."""

from .campaign_model import CampaignSnapshot, UnitState


def parent_keys(snapshot: CampaignSnapshot, key: str) -> tuple[str, ...]:
    """Join aliases only through a complete, exact native parent identity."""
    bindings = snapshot.bindings.get("github_hierarchy", {})
    bound = bindings.get(key, {})
    identity = tuple(bound.get(field) for field in ("kind", "database_id", "number"))
    if not all(identity):
        return (key,)
    return tuple(
        sorted(
            {
                key,
                *(
                    name
                    for name, row in bindings.items()
                    if name.startswith(("epic:", "initiative:"))
                    and tuple(row.get(field) for field in ("kind", "database_id", "number"))
                    == identity
                ),
            }
        )
    )


def parent_members(snapshot: CampaignSnapshot, key: str) -> tuple[str, ...]:
    """Include every known alias and declared descendant of a parent scope."""
    keys = set(parent_keys(snapshot, key))
    members = {
        unit.id
        for unit in snapshot.units.values()
        if (
            f"epic:{unit.spec.source}:{unit.spec.initiative or snapshot.spec.name}:{unit.spec.epic}"
            in keys
            or f"initiative:{unit.spec.source}:{unit.spec.initiative or snapshot.spec.name}" in keys
        )
    }
    while descendants := {
        unit.id
        for unit in snapshot.units.values()
        if unit.spec.parent in members and unit.id not in members
    }:
        members.update(descendants)
    return tuple(sorted(members))


def _confirmed_nonwork_pr(snapshot, unit, cancellation) -> bool:
    from .campaign_action_history import actions

    native_id = cancellation.get("native_id")
    pr = unit.bindings.get("pr") or {}
    if type(native_id) is not int or native_id <= 0:
        return False
    for action in actions(snapshot, kinds=("github",), unit=unit.id, native_id=str(native_id)):
        if action.state != "confirmed" or action.payload.get("purpose") != "intake-cancel":
            continue
        intent = action.receipt.get("intent") or {}
        receipt = intent.get("receipt") or {}
        head = receipt.get("head") or {}
        if (
            intent.get("kind") == "close_pr"
            and intent.get("state") == "confirmed"
            and receipt.get("id") == native_id
            and receipt.get("number") == pr.get("number")
            and receipt.get("node_id") == pr.get("node_id")
            and receipt.get("state") == "closed"
            and receipt.get("merged") is False
            and head.get("ref") == unit.bindings.get("branch")
            and head.get("sha") == cancellation.get("head")
        ):
            return True
    return False


def parent_acceptance_wait(snapshot, unit) -> str:
    """A proved nonwork cancellation discharges only the standard intake duties."""
    from .campaign_runtime_intake import CONTROL_ACCEPTANCE, control, optional_canceled
    from .campaign_tracking_recovery import acceptance_wait

    if unit.state != UnitState.CANCELED:
        return acceptance_wait(unit)
    value = control(unit)
    if not value or not optional_canceled(unit):
        return "Parent tracking requires proof of the canceled member's nonwork disposition"
    from .campaign_acceptance import criteria
    from .campaign_action_history import actions
    from .campaign_intake_flow import INBOX, duplicate_current

    accepted = value.get("disposition", {})
    evidence = snapshot.bindings.get(INBOX, {})
    attempt = evidence.get("attempts", {}).get(value.get("attempt_id"), {})
    versions = value.get("input_versions")
    issue_cancel = value.get("issue_cancellation") or {}
    issue_receipt = issue_cancel.get("receipt") or {}
    issue = unit.bindings.get("issue") or {}
    if (
        issue_cancel.get("kind") != "close_issue_not_planned"
        or issue_cancel.get("state") != "confirmed"
        or issue_receipt.get("state") != "closed"
        or issue_receipt.get("state_reason") != "not_planned"
        or type(issue.get("database_id")) is not int
        or issue_receipt.get("id") != issue["database_id"]
        or type(issue.get("number")) is not int
        or issue_receipt.get("number") != issue["number"]
        or (issue_cancel.get("coordinates") or {}).get("disposition_id") != value.get("attempt_id")
    ):
        return "Parent tracking lacks its confirmed native nonwork issue cancellation"
    if (
        unit.spec.kind != "planning"
        or unit.spec.source != "campaign-intake"
        or tuple(unit.spec.scopes) != ("resource:campaign-plan",)
        or unit.id != f"campaign:{snapshot.id}:intake:{str(value.get('packet_id', ''))[:16]}"
        or not isinstance(accepted, dict)
        or not versions
        or attempt.get("state") != "completed"
        or attempt.get("disposition") != accepted
        or list(attempt.get("input_versions", ())) != list(versions)
        or accepted.get("disposition") != value["cancellation"].get("disposition")
        or accepted.get("disposition") in {"hold", "needs_information"}
        or (
            accepted.get("disposition") in {"attach", "repair_current_pr", "duplicate"}
            and (
                accepted.get("unit_id") == unit.id or accepted.get("unit_id") not in snapshot.units
            )
        )
        or not value.get("disposition_noted")
        or value.get("proposal_id")
        or value.get("amendments")
        or any(
            evidence.get("records", {}).get(version, {}).get("state")
            not in {"resolved", "superseded"}
            or evidence["records"][version].get("control_unit") != unit.id
            or evidence["records"][version].get("disposition") != accepted
            for version in versions
        )
    ):
        return "Parent tracking retains unresolved intake disposition or source obligations"
    if accepted["disposition"] == "duplicate" and not duplicate_current(
        snapshot, attempt, accepted
    ):
        return (
            "Parent tracking requires retained duplicate target identity and exact intake coverage"
        )
    ledger = unit.evidence.get("acceptance") or {}
    rows = ledger.get("criteria")
    if (
        tuple(unit.spec.acceptance) != CONTROL_ACCEPTANCE
        or not isinstance(rows, (tuple, list))
        or any(not isinstance(row, dict) for row in rows)
        or [{k: row.get(k) for k in ("id", "text", "required")} for row in rows]
        != criteria(unit.id, CONTROL_ACCEPTANCE)
        or ledger.get("amendments")
        or unit.evidence.get("source_artifacts")
        or any(
            not isinstance(row, dict) or row.get("required", True) and not row.get("resolved")
            for row in unit.evidence.get("required_findings", ())
        )
    ):
        return "Parent tracking retains additional acceptance or findings on the canceled intake control"
    cancellation = value["cancellation"]
    head = cancellation.get("head")
    if not _confirmed_nonwork_pr(snapshot, unit, cancellation):
        return "Parent tracking cannot bind the nonwork cancellation to the closed native PR"
    start = [
        row.receipt
        for row in actions(snapshot, kinds=("prepare-unit",), unit=unit.id)
        if row.state == "confirmed"
    ]
    if (
        not head
        or head != unit.bindings.get("head")
        or not all(unit.bindings.get(k) for k in ("workspace", "branch"))
        or not start
        or any(
            any(
                row.get(field) != unit.bindings.get(field)
                for field in ("workspace", "branch", "head")
            )
            for row in start
        )
    ):
        return "Parent tracking cannot verify that the canceled intake branch retains its original bootstrap"
    return ""


def parent_wait_reason(snapshot: CampaignSnapshot, key: str) -> str:
    """A delivered selection may settle its own scope, never a foreign one."""

    members = parent_members(snapshot, key)
    if not members:
        return "Parent tracking has no proven campaign member scope"
    return _member_wait_reason(snapshot, members)


def all_members_settled(snapshot: CampaignSnapshot) -> bool:
    """The aggregate marker and terminal transition retain late member obligations."""
    return (
        bool(snapshot.units)
        and all(
            unit.state in {UnitState.DELIVERED, UnitState.CANCELED}
            for unit in snapshot.units.values()
        )
        and not _member_wait_reason(snapshot, snapshot.units)
    )


def terminal_completion_wait(snapshot, unit):
    """Explain why a terminal-looking member still prevents aggregate closure."""
    if (
        snapshot.phase == "delivered"
        or unit.hold
        or unit.state not in {UnitState.DELIVERED, UnitState.CANCELED}
    ):
        return ""
    return _member_wait_reason(snapshot, (unit.id,))


def _member_wait_reason(snapshot, members):
    for name in members:
        unit = snapshot.units[name]
        if unit.hold:
            return f"Parent tracking retains member hold: {name}"
        if any(
            unit.evidence.get(field)
            for field in (
                "unresolved_approval_requirements",
                "pending_failure_hold",
                "pending_failure_holds",
                "operator_prior_hold",
            )
        ):
            return f"Parent tracking retains pending member obligations: {name}"
        if unit.claim and not unit.claim.quiescent:
            return f"Parent tracking waits for member quiescence: {name}"
        if reason := parent_acceptance_wait(snapshot, unit):
            return f"Parent tracking retains member acceptance: {name}: {reason}"
        if unit.state not in {UnitState.DELIVERED, UnitState.CANCELED}:
            return f"Parent tracking waits for required member delivery: {name}"
    return ""


def retained_parent_reason(snapshot: CampaignSnapshot, key: str) -> str:
    """Keep selected-source epics/milestones open, even with no remote children.

    Match durable source namespaces rather than mutable parent titles. A sparse
    GitHub projection is not evidence that the rest of a native epic is done.
    """
    keys = parent_keys(snapshot, key)
    for source in snapshot.spec.sources:
        if source.completion_scope != "selected-units":
            continue
        namespace = f"{source.id or source.kind}:{source.initiative or snapshot.spec.name}"
        if any(
            item == "initiative:" + namespace or item.startswith("epic:" + namespace + ":")
            for item in keys
        ):
            return f"Source {source.id or source.kind} delivers selected units; parent scope remains open"
    return ""
