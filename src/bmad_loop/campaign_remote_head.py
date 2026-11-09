"""Reconcile append-only contributions to an already-owned PR.

Remote visibility grants no new ownership. Observations bind the existing native
PR/ref, and only a quiescent integration worker may consume the recorded debt.
Checkpoint backup and equality of a publication candidate remain separate facts.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from . import verify
from .campaign_model import CampaignError, Hold, UnitState
from .campaign_store import add_event, transition


class RemoteHeadPending(RuntimeError):
    """Native PR and Git observations need a fresh read before admitting work."""


def observe(runtime, snapshot, unit, *, pr=None):
    """Read exact native ownership; no branch or working tree is changed."""
    binding = unit.bindings.get("pr", {})
    configured = verify.git_bytes(runtime.project, "remote", "get-url", snapshot.spec.remote)
    if configured.returncode or configured.stdout.decode().strip() != snapshot.remote_url:
        raise CampaignError("remote-head recovery configured remote identity changed")
    if not binding.get("number") or not binding.get("node_id"):
        raise CampaignError("remote-head recovery requires an already-owned native PR")
    pr = pr or runtime.gateway(snapshot).pull(binding["number"])
    if (
        pr.get("number") != binding["number"]
        or pr.get("node_id") != binding["node_id"]
        or pr.get("state") != "open"
        or pr.get("merged") is not False
        or pr.get("head", {}).get("ref") != unit.bindings.get("branch")
        or pr.get("head", {}).get("repo", {}).get("full_name") != snapshot.repository
        or pr.get("base", {}).get("repo", {}).get("full_name") != snapshot.repository
        or pr.get("base", {}).get("ref") != snapshot.spec.target
    ):
        raise CampaignError("remote-head recovery lost its exact open PR/ref/repository binding")
    pusher = runtime._pusher(snapshot, unit)
    remote = pusher.remote_head()
    if not remote:
        raise CampaignError("owned remote branch disappeared")
    if pr.get("head", {}).get("sha") != remote:
        raise RemoteHeadPending("native PR and branch head observations disagree; refresh required")
    workspace = Path(unit.bindings["workspace"])
    local = verify.rev_parse_head(workspace)
    if unit.claim and unit.claim.quiescent and local != unit.bindings.get("head"):
        raise CampaignError("quiescent local candidate differs from its observed worker head")
    anchor = unit.bindings.get("remote_head_anchor") or unit.bindings.get("last_pushed_head")
    if not anchor or not pusher._ancestor(anchor, remote):
        raise CampaignError("remote branch lost its authenticated owned checkpoint ancestry")
    prior = unit.evidence.get("remote_head_sync")
    if prior and not pusher._ancestor(prior["head"], remote):
        raise CampaignError("remote branch lost its previously observed external contribution")
    incorporated = pusher._ancestor(remote, local)
    if incorporated and not prior and remote == anchor:
        return {
            "remote_head_current": remote,
            "local_head": local,
            "remote_recovery_fence": pusher.fenced(),
        }
    if incorporated and prior:
        if not pusher._ancestor(prior["head"], local):
            raise CampaignError("local candidate lost the preserved external contribution")
    value = {
        "schema_version": 1,
        "repository": snapshot.repository,
        "pr_number": binding["number"],
        "pr_node_id": binding["node_id"],
        "branch": unit.bindings["branch"],
        "target": snapshot.spec.target,
        "anchor": anchor,
        "head": remote,
        "local_head": local,
        "provenance_before": (
            prior.get("provenance_before", prior["local_head"])
            if prior
            else anchor if incorporated else local
        ),
        "graph_revision": snapshot.graph_revision,
        "config_revision": snapshot.config_revision,
        "source_revision": unit.bindings.get("source_revision", ""),
    }
    value["id"] = hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()
    if prior and prior["head"] == remote:
        value["id"] = prior["id"]
    return {"remote_head_sync": value, "remote_recovery_fence": pusher.fenced()}


def checked(runtime, snapshot, unit, *, pr=None):
    try:
        return observe(runtime, snapshot, unit, pr=pr)
    except CampaignError as exc:
        return {"remote_head_error": str(exc)}


def invalidate(snapshot, unit):
    for name in (
        "integrated",
        "verification",
        "review",
        "native_review_pair",
        "pushed",
        "pr_head_check",
        "stage_boundary_ready",
    ):
        unit.evidence.pop(name, None)
    if unit.state in {UnitState.VERIFYING, UnitState.REVIEWING, UnitState.READY_TO_PUBLISH}:
        transition(snapshot, unit.id, UnitState.SYNCING)


def apply(runtime, snapshot, unit, data):
    unit.evidence.pop("remote_head_check_needed", None)
    if unit.state == UnitState.MERGE_REQUESTED and snapshot.spec.github.merge_mode == "queue":
        unit.evidence.setdefault("queue", {})["remote_head_pending"] = data
        return
    if data.get("remote_head_current"):
        unit.bindings["remote_head_anchor"] = data["remote_head_current"]
        return
    debt = data["remote_head_sync"]
    prior = unit.evidence.get("remote_head_sync")
    history = unit.evidence.setdefault("remote_head_refreshes", [])
    if not any(row["head"] == debt["head"] for row in history):
        history.append(debt)
    unit.evidence["remote_head_sync"] = debt
    if not unit.claim or unit.claim.quiescent:
        invalidate(snapshot, unit)
    if not prior or prior["head"] != debt["head"]:
        add_event(snapshot, "remote-head-observed", unit=unit.id, head=debt["head"], id=debt["id"])
    fence = data.get("remote_recovery_fence")
    if fence:
        runtime._set_hold(
            unit,
            Hold(
                "remote_recovery_fence",
                "external branch changes require the owning recovery to reconcile its preserved preimages",
                id=debt["id"],
                evidence={"fence": fence, "remote_head_sync": debt},
            ),
        )
    elif len(history) > snapshot.spec.github.max_remote_head_refreshes:
        runtime._set_hold(
            unit,
            Hold(
                "operator",
                "remote PR head changed repeatedly; release explicitly to retry the preserved candidate",
                id="remote-head-starved:" + debt["id"],
                actor="campaign",
                evidence={
                    "remote_head_refreshes": len(history),
                    "limit": snapshot.spec.github.max_remote_head_refreshes,
                },
            ),
        )


def request(unit, *, snapshot=None):
    """Freeze the latest quiescent local head with the immutable remote debt."""
    debt = unit.evidence.get("remote_head_sync")
    if not debt:
        return {}
    current = {**debt, "local_head": unit.bindings["head"]}
    if snapshot is not None:
        current.update(
            graph_revision=snapshot.graph_revision,
            config_revision=snapshot.config_revision,
            source_revision=unit.bindings.get("source_revision", ""),
        )
    payload = {"remote_head_sync": current}
    continuation = unit.evidence.get("remote_head_continuation")
    if continuation:
        payload["remote_resume_state"] = continuation["state"]
    elif unit.state in {UnitState.READY, UnitState.RUNNING}:
        payload["remote_resume_state"] = unit.state.value
    return payload


def resume_unfinished(snapshot, unit):
    continuation = unit.evidence.get("remote_head_continuation")
    if not continuation or unit.evidence.get("remote_head_sync"):
        return
    integrated = unit.evidence.get("integrated", {})
    if not integrated or unit.evidence.get("pushed") != integrated.get("head"):
        return
    target = UnitState(continuation["state"])
    if target not in {UnitState.READY, UnitState.RUNNING}:
        raise CampaignError("remote-head continuation cannot grant story completion")
    if unit.state != target:
        transition(snapshot, unit.id, target)
    for name in ("integrated", "pushed", "verification", "review", "native_review_pair"):
        unit.evidence.pop(name, None)
    unit.evidence.pop("remote_head_continuation")
    add_event(snapshot, "remote-head-unfinished-stage-resumed", unit=unit.id, state=target.value)


def accept(snapshot, unit, receipt):
    debt = unit.evidence.get("remote_head_sync")
    received = receipt.get("remote_head_sync")
    if not received:
        return
    if not debt or any(
        received.get(key) != debt.get(key) for key in ("id", "head", "anchor", "pr_node_id")
    ):
        raise CampaignError("integration receipt names a superseded external contribution")
    from .campaign_external_provenance import record

    record(snapshot, unit, receipt)
    if receipt["outcome"] == "integrated":
        unit.evidence.setdefault("remote_head_history", []).append(
            {"debt": debt, "receipt": receipt}
        )
        unit.evidence.pop("remote_head_sync", None)
        unit.bindings["remote_head_anchor"] = debt["head"]


def interrupt_resolution(runtime, snapshot, unit, action, data):
    """Retain a verified repair whose push was definitively not submitted.

    This is no successful-resolution receipt. The combined candidate must earn
    fresh integration, verification and review after the external contribution.
    """
    from .campaign_hold import PROTECTED_HOLDS

    changed = data["resolution_remote_changed"]
    accepted = unit.evidence.get("accepted_resolution", {})
    protected = unit.hold and unit.hold.kind in PROTECTED_HOLDS
    if (
        accepted.get("id") != changed["proposal_id"]
        or not unit.claim
        or not unit.claim.quiescent
        or not unit.hold
        or (
            not protected
            and (
                unit.hold.kind != "resolution"
                or unit.hold.evidence.get("proposal") != accepted["id"]
            )
        )
        or changed["head"] != data.get("head")
        or not data.get("backup_pending")
        or not data.get("remote_head_sync")
    ):
        raise CampaignError("external-head repair interruption lacks its exact quiescent authority")
    target = unit.resume_state or UnitState.READY
    if target not in {UnitState.READY, UnitState.SYNCING}:
        raise CampaignError("external-head repair interruption cannot grant story completion")
    names = ("accepted_resolution", "repair_candidate", "resolution_verification")
    unit.evidence.setdefault("resolution_remote_history", []).append(
        {
            "action_id": action.id,
            "status": "interrupted-by-external-head",
            "head": changed["head"],
            "remote_head_sync": data["remote_head_sync"],
            **{name: unit.evidence.get(name) for name in names},
        }
    )
    unit.evidence.setdefault("external_backup_debt", {})[action.id] = changed["head"]
    for name in (*names, "resolution_next_step"):
        unit.evidence.pop(name, None)
    if not protected:
        unit.hold = None
    unit.resume_state = None
    if unit.state != target:
        transition(snapshot, unit.id, target)
    apply(runtime, snapshot, unit, data)
    add_event(snapshot, "resolution-interrupted-by-external-head", unit=unit.id, action=action.id)
