"""Record the actual merged product before releasing canonical tracking.

An administrative merge may integrate a newer base after the last client read.
Ancestry alone does not establish that the resulting tree was reviewed/tested.
"""

from __future__ import annotations

import copy
import dataclasses
import hashlib
from dataclasses import asdict
from pathlib import Path

from . import verify
from .campaign_model import (
    CampaignError,
    CampaignUnit,
    Hold,
    UnitState,
    source_to_dict,
    validate_graph,
)
from .campaign_publication import Candidate
from .campaign_supervisor import Observation, Proposal
from .platform_util import safe_segment


def assess(project, candidate, merge) -> dict:
    merged = merge.merge_commit
    result = verify.git_bytes(project, "show", "-s", "--format=%P", merged)
    if result.returncode:
        raise CampaignError("Cannot observe actual merge parents for landing acceptance")
    parents = result.stdout.decode("ascii").split()
    tree = verify.revision_tree_oid(project, merged)
    covered = (
        bool(parents)
        and all(verify.is_ancestor(project, parent, candidate.head) for parent in parents)
        and tree == verify.revision_tree_oid(project, candidate.head)
    )
    return {
        "schema_version": 1,
        "state": "covered" if covered else "pending",
        "candidate": asdict(candidate),
        "merge_commit": merged,
        "merge_tree": tree,
        "merge_parents": parents,
        "canonical_head": merge.canonical_head,
        "reason": (
            ""
            if covered
            else "Actual merged product requires selected verification and independent review"
        ),
    }


def satisfied(unit) -> bool:
    debt = unit.evidence.get("landing_acceptance")
    return not debt or debt.get("state") in {"covered", "accepted"}


def candidate(unit):
    debt = unit.evidence["landing_acceptance"]
    return dataclasses.replace(Candidate(**debt["candidate"]), head=debt["merge_commit"])


def proposals(runtime, snapshot, unit, reserve):
    if satisfied(unit):
        return (Proposal("sync-tracking", unit.id),)
    debt = unit.evidence["landing_acceptance"]
    if debt.get("repair_unit"):
        repair = snapshot.units[debt["repair_unit"]]
        if repair.state == UnitState.DELIVERED:
            return (Proposal("landing-repair-confirm", unit.id),)
        return ()
    if debt.get("failure"):
        return (Proposal("landing-repair", unit.id),)
    if not debt.get("workspace"):
        return (Proposal("landing-checkout", unit.id),)
    payload = {
        "landing": True,
        "landing_group": debt["group"],
        "verification_workspace": debt["workspace"],
    }
    if not debt.get("verification"):
        if reserve("verify"):
            return (
                Proposal(
                    "launch", unit.id, {**payload, "kind": "verify", "role": "landing_verify"}
                ),
            )
        return ()
    route = runtime._role(
        snapshot,
        "publication_review",
        developer_family=unit.evidence.get("developer_family", ""),
        developer_families=tuple(unit.evidence.get("developer_families", [])),
    )
    if route and reserve("review"):
        return (
            Proposal(
                "launch",
                unit.id,
                {
                    **payload,
                    "kind": "stage",
                    "role": "publication_review",
                    "stage": "publication_review",
                    "read_only": True,
                    "candidate": asdict(route),
                },
            ),
        )
    return ()


def checkout(runtime, snapshot, action):
    from .campaign_queue import QueueGroup, allocate_checkout

    unit = snapshot.units[action.unit]
    debt = unit.evidence["landing_acceptance"]
    selected = candidate(unit)
    group = QueueGroup(
        snapshot.repository,
        selected.pr_number,
        selected.target,
        "landing:" + debt["merge_commit"],
        selected.head,
        selected.base,
        debt["candidate"]["head"],
        "MERGED",
        runtime.clock(),
    )
    root = Path(
        snapshot.spec.paradev.worktree_root
        if snapshot.spec.paradev.reflect
        else snapshot.spec.worktree_root
    )
    workspace = allocate_checkout(
        runtime.project,
        root,
        runtime.store.directory(snapshot.id) / "landing-allocations",
        group,
        remote=snapshot.spec.remote,
    )
    runtime._seed_checkout(workspace)
    source = runtime.source(snapshot, unit)
    private_paths = []
    if source.kind != "explicit":
        from .campaign_sources import prepare_source_view

        prepared = prepare_source_view(
            runtime.project,
            workspace,
            source,
            authority_dir=runtime.store.directory(snapshot.id)
            / "landing-sources"
            / safe_segment(unit.id),
        )
        source = prepared.selector
        private_paths = [str(path) for path in prepared.private_paths]
    return Observation(
        "confirmed",
        {
            "workspace": str(workspace),
            "group": asdict(group),
            "source": source_to_dict(source),
            "private_paths": private_paths,
        },
    )


def acceptance(unit, review):
    from .campaign_acceptance import evaluate

    view = copy.deepcopy(unit)
    view.evidence["verification"] = unit.evidence["landing_acceptance"]["verification"]
    return evaluate(view, candidate(unit), review=review)


def apply_worker(runtime, snapshot, unit, receipt):
    """Consume a quiescent landing actor without rewriting original PR bindings."""
    debt = unit.evidence["landing_acceptance"]
    if receipt["head"] != debt["merge_commit"]:
        raise CampaignError("Landing worker observed a different merged product")
    if receipt.get("outcome") == "operator_held":
        return
    if receipt.get("outcome") in {"interrupted", "failed"}:
        failures = debt.setdefault("worker_failures", [])
        if not any(row.get("launch_id") == receipt.get("launch_id") for row in failures):
            failures.append(receipt)
        if len(failures) >= snapshot.spec.resolution.max_attempts_per_unit:
            runtime._set_hold(
                unit,
                Hold(
                    "landing_verification_unavailable",
                    "Landing actor failed repeatedly; no product failure has been inferred",
                    id=receipt.get("launch_id", ""),
                ),
            )
        return
    if receipt.get("outcome") == "verified":
        debt.update(state="reviewing", verification=receipt)
    elif receipt.get("outcome") == "stage_complete":
        writers = set(unit.evidence.get("developer_families", ()))
        if unit.evidence.get("developer_family"):
            writers.add(unit.evidence["developer_family"])
        family = receipt.get("provider_family")
        independent = (
            bool(family)
            and bool(writers)
            and family not in writers
            and not unit.evidence.get("developer_attribution_incomplete")
        )
        proof = acceptance(unit, receipt)
        if (
            receipt.get("validated", {}).get("verdict") == "approved"
            and proof["complete"]
            and (not snapshot.spec.github.require_independent_review or independent)
        ):
            debt.update(state="accepted", review=receipt, acceptance=proof)
        else:
            debt.update(state="repair_pending", failure=receipt, acceptance=proof)
    else:
        debt.update(state="repair_pending", failure=receipt)


def prepare_repair(runtime, snapshot, action):
    unit = snapshot.units[action.unit]
    debt = unit.evidence["landing_acceptance"]
    if not debt.get("failure") or debt.get("repair_unit"):
        raise CampaignError("Landing repair requires unresolved exact merge evidence")
    return Observation("confirmed", {"merge_commit": debt["merge_commit"]})


def admit_repair(runtime, snapshot, unit, data):
    """The repair inherits a merged parent's slot, never its completion debt."""
    from .campaign_store import add_event

    debt = unit.evidence["landing_acceptance"]
    if data["merge_commit"] != debt["merge_commit"]:
        raise CampaignError("Landing repair belongs to a different merge")
    root = snapshot.units[unit.bindings.get("landing_root", unit.id)]
    repairs = root.bindings.setdefault("landing_repair_units", [])
    key = "landing-repair:" + unit.id + ":" + debt["merge_commit"][:16]
    if key in snapshot.units:
        if snapshot.units[key].bindings.get("landing_parent") != unit.id:
            raise CampaignError("Landing repair identity was reused")
        debt["repair_unit"] = key
        return
    if (
        snapshot.spec.resolution.mode != "bounded-auto"
        or len(repairs) >= snapshot.spec.resolution.max_attempts_per_unit
    ):
        runtime._set_hold(
            unit,
            Hold(
                "landing_repair_required",
                "Merged product needs repair; automatic repair is disabled or its lifetime budget is exhausted",
                id=debt["merge_commit"],
            ),
        )
        return
    spec = dataclasses.replace(
        unit.spec,
        id=key,
        kind="landing_repair",
        parent=unit.id,
        title="Repair merged acceptance: " + unit.spec.title,
        selector="",
        after=unit.spec.after,
    )
    validate_graph([row.spec for row in snapshot.units.values()] + [spec])
    repair = CampaignUnit(
        spec,
        bindings={"landing_parent": unit.id, "landing_root": root.id},
        evidence={
            "landing_failure": copy.deepcopy(debt["failure"]),
            "landing_original_merge": debt["merge_commit"],
        },
    )
    snapshot.units[key] = repair
    snapshot.graph_revision += 1
    repairs.append(key)
    debt.update(state="repair_pending", repair_unit=key)
    add_event(
        snapshot,
        "landing-repair-admitted",
        unit=key,
        original=unit.id,
        merge_commit=debt["merge_commit"],
    )


def repair_product_input(snapshot, unit, workspace):
    """Read the original accepted draft at its immutable reviewed head.

    A native ticket may keep acceptance only in its source plan. The repair has
    no lifecycle selector; inheriting that selector would grant source writes.
    Its root's delivered draft already embeds the exact selected native input,
    including ignored/private plans, and remains reachable through the merge.
    """
    root = snapshot.units[unit.bindings["landing_root"]]
    head = root.evidence.get("integrated", {}).get("head", "")
    relative = root.bindings.get("spec_path", "")
    path = Path(relative)
    if not head or not relative or path.is_absolute() or ".." in path.parts:
        raise CampaignError("Landing repair lacks its original accepted product identity")
    if not verify.is_ancestor(workspace, head, "HEAD"):
        raise CampaignError("Landing repair base does not contain its accepted product")
    object_name = head + ":" + relative
    size = verify.git_bytes(workspace, "cat-file", "-s", object_name)
    try:
        length = int(size.stdout)
    except ValueError as exc:
        raise CampaignError("Landing repair accepted product is unavailable") from exc
    if size.returncode or not 0 < length <= 1024 * 1024:
        raise CampaignError("Landing repair accepted product exceeds its byte bound")
    data = verify.file_bytes_at_revision(workspace, head, relative)
    if data is None or len(data) != length:
        raise CampaignError("Landing repair accepted product observation is incomplete")
    return (
        {
            "path": relative + "@" + head,
            "sha256": hashlib.sha256(data).hexdigest(),
            "text": data.decode("utf-8"),
        },
    )


def confirm_repair(runtime, snapshot, action):
    unit = snapshot.units[action.unit]
    debt = unit.evidence["landing_acceptance"]
    repair = snapshot.units[debt["repair_unit"]]
    proof = repair.evidence.get("acceptance", {}).get("current", {})
    merge = repair.evidence.get("merge", {})
    if repair.state != UnitState.DELIVERED or not proof.get("complete") or not satisfied(repair):
        return Observation(
            "waiting",
            reason="Landing repair acceptance is not delivered",
            retry_at=runtime.clock() + 5,
        )
    if not merge.get("canonical_head") or not verify.is_ancestor(
        runtime.project, debt["merge_commit"], merge["canonical_head"]
    ):
        raise CampaignError("Landing repair does not preserve the original merged history")
    return Observation(
        "confirmed", {"repair_unit": repair.id, "repair_merge": merge, "acceptance": proof}
    )
