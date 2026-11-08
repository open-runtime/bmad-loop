"""Fresh read-only release evidence for an explicit operator takeover.

Release never trusts a cached quiescent flag, stale head or old source snapshot.
It neither stops a process nor repairs owner edits. Invalidation is archived
before the normal publication pipeline re-establishes acceptance.
"""

from __future__ import annotations

from pathlib import Path

from . import campaign_hold, runs, verify
from .campaign_model import CampaignError, Hold, UnitState
from .campaign_source_publication import SourceBinding, SourcePublication
from .campaign_store import add_event, transition
from .campaign_workers import assert_workspace


def observe(runtime, snapshot, action) -> dict:
    unit = snapshot.units[action.unit]
    if not unit.hold or unit.hold.kind != "operator" or unit.hold.id != action.payload["hold_id"]:
        return {"obsolete": True}
    if any(
        row.unit == unit.id
        and row.state in {"prepared", "sent_unknown"}
        and row.kind in {"github", "push", "checkpoint", "sync-tracking"}
        for row in snapshot.outbox
    ):
        raise CampaignError("release waits for previously issued publication effects to reconcile")
    if unit.claim:
        if not unit.claim.quiescent:
            raise CampaignError("release requires confirmed worker quiescence")
        directory = Path(unit.claim.run_dir)
        pid, identity = runs.read_named_pid_identity(directory / "engine.pid")
        if unit.claim.status != "not_launched" and (
            pid is None or identity is None or runs.probe_liveness(pid, identity) != "dead"
        ):
            raise CampaignError("release cannot confirm the exact worker process is dead")
        if unit.claim.run_id:
            from .adapters.multiplexer import configure_multiplexer, get_multiplexer

            configure_multiplexer(
                runtime.template(snapshot).policy.mux.backend or None, origin=runtime.project
            )
            mux = (
                runtime._native_mux(snapshot)
                if snapshot.spec.paradev.reflect
                else get_multiplexer()
            )
            if mux.list_window_ids(runs.session_name(unit.claim.run_id)):
                raise CampaignError("release preserves a live or residual coding terminal")
    head = ""
    source = None
    if unit.bindings.get("workspace"):
        workspace = Path(unit.bindings["workspace"])
        assert_workspace(workspace, Path(snapshot.common_dir), unit.bindings["branch"])
        if not verify.worktree_clean(workspace):
            raise CampaignError("release requires owner edits to be committed in the same branch")
        head = verify.rev_parse_head(workspace)
        if unit.state in {
            UnitState.MERGE_REQUESTED,
            UnitState.MERGED_PENDING_SYNC,
            UnitState.DELIVERED,
        } and head != unit.evidence.get("integrated", {}).get("head", head):
            raise CampaignError("release cannot replace the head of an already submitted merge")
        old = unit.bindings.get("head") or unit.bindings.get("base")
        if old and not verify.is_ancestor(workspace, old, head):
            raise CampaignError("release refuses rewritten candidate history")
        binding = unit.bindings.get("canonical_source")
        if binding and binding.get("path"):
            expected = SourceBinding.from_dict(binding)
            selector = runtime.source(snapshot, unit)
            observed = SourcePublication(
                runtime.project, runtime.store.directory(snapshot.id) / "source-publication"
            ).capture(selector, unit.spec)
            fields = ("unit_id", "source_root", "store_repo", "path", "kind")
            published = unit.evidence.get("source_publication", {})
            own_publication = (
                published.get("state") == "confirmed"
                and published.get("source_revision") == observed.revision
            )
            if any(getattr(observed, field) != getattr(expected, field) for field in fields) or (
                observed.revision != expected.revision and not own_publication
            ):
                raise CampaignError("release source changed; reviewed source amendment required")
            source = observed.to_dict()
    return {
        "hold_id": unit.hold.id,
        "command_id": action.payload["command_id"],
        "head": head,
        "source": source,
        "claim": unit.claim.launch_id if unit.claim else "",
    }


def apply(runtime, snapshot, action, data) -> None:
    unit = snapshot.units[action.unit]
    if data.get("obsolete"):
        return
    if not unit.hold or unit.hold.kind != "operator" or unit.hold.id != data["hold_id"]:
        raise CampaignError("release proof no longer owns the operator hold")
    if (
        (unit.claim.launch_id if unit.claim else "") != data["claim"]
        or unit.claim
        and not unit.claim.quiescent
    ):
        raise CampaignError("release worker identity changed after observation")
    # Recheck the cheap mutable coordinate before accepting the effect receipt.
    if data["head"] and verify.rev_parse_head(Path(unit.bindings["workspace"])) != data["head"]:
        raise CampaignError("release head changed after observation")
    invalidated = {}
    landed = unit.state in {
        UnitState.MERGE_REQUESTED,
        UnitState.MERGED_PENDING_SYNC,
        UnitState.DELIVERED,
    }
    for key in (
        ()
        if landed
        else ("review", "native_review_pair", "verification", "integrated", "pushed", "admission")
    ):
        if key in unit.evidence:
            invalidated[key] = unit.evidence.pop(key)
    unit.evidence.setdefault("operator_release_history", []).append(
        {"proof": data, "invalidated": invalidated}
    )
    if data["head"]:
        unit.bindings["head"] = data["head"]
    prior = unit.evidence.pop("operator_prior_hold", None)
    pending = prior or unit.evidence.pop("pending_failure_hold", None)
    unit.hold = Hold(**pending) if pending else None
    campaign_hold.promote_pending(unit)
    unit.bindings.pop("operator_release", None)
    for stale in snapshot.outbox:
        if (
            stale.unit == unit.id
            and stale.state in {"prepared", "sent_unknown"}
            and stale.kind in {"prepare-merge", "prepare-ready", "prepare-draft", "ready"}
        ):
            stale.state = "rejected"
            stale.error = "operator release invalidated prior candidate acceptance"
    if unit.state in {
        UnitState.SYNCING,
        UnitState.VERIFYING,
        UnitState.REVIEWING,
        UnitState.READY_TO_PUBLISH,
    }:
        if unit.state != UnitState.SYNCING:
            transition(snapshot, unit.id, UnitState.SYNCING)
    elif unit.state in {UnitState.RUNNING, UnitState.CLAIMED}:
        transition(snapshot, unit.id, UnitState.READY)
    campaign_hold.write(
        campaign_hold.channel(runtime.store.directory(snapshot.id), unit.id),
        campaign=snapshot.id,
        unit=unit.id,
        hold="",
    )
    add_event(snapshot, "operator-release-confirmed", unit=unit.id, hold=data["hold_id"])
