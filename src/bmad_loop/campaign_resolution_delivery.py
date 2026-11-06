"""Finish accepted repairs using observed Git, verification and push evidence.

Diagnosis authorizes a bounded next step. It never counts as the step's result:
a repair must leave owned changes, preserve both conflict heads, pass the actual
commands in a separate worker, and settle its append-only push before rearming.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
from pathlib import Path
from typing import Any

from . import verify
from .campaign_model import CampaignError, EvidenceCoordinates
from .campaign_resolution import ResolutionResult
from .campaign_supervisor import Observation, Proposal


def next_step(snapshot: Any, unit: Any) -> Proposal | None:
    accepted = unit.evidence["accepted_resolution"]
    identity = accepted["id"]
    verified = unit.evidence.get("resolution_verification", {})
    if verified.get("resolution_id") == identity:
        return Proposal("resolution-finish", unit.id, {"proposal_id": identity})
    attempts = unit.evidence.get("resolution_effect_attempts", {}).get(identity, 0)
    if attempts >= snapshot.spec.resolution.max_attempts_per_unit * 2:
        return Proposal("resolution-exhausted", unit.id, {"proposal_id": identity})
    repaired = unit.evidence.get("repair_candidate", {})
    if (
        accepted["action"] in {"reverify", "retry", "reprobe"}
        or repaired.get("resolution_id") == identity
    ):
        commands = [list(command) for command in accepted["verification"]]
        if accepted.get("command"):
            commands.insert(0, list(accepted["command"]))
        return Proposal(
            "launch",
            unit.id,
            {
                "kind": "verify",
                "stage": "resolution_verify",
                "commands": commands,
                "resolution_proposal": accepted,
            },
        )
    if accepted["action"] == "wait":
        # A wait is retained durable debt, never a fabricated successful retry.
        return None
    return None


def prepare(unit: Any, payload: dict[str, Any]) -> None:
    accepted = payload.get("resolution_proposal")
    if not accepted:
        return
    if unit.evidence.get("accepted_resolution", {}).get("id") != accepted["id"]:
        raise CampaignError("repair launch does not match its accepted diagnosis")
    attempts = unit.evidence.setdefault("resolution_effect_attempts", {})
    attempts[accepted["id"]] = attempts.get(accepted["id"], 0) + 1


def validate_repair(snapshot: Any, unit: Any, receipt: dict[str, Any]) -> dict[str, Any]:
    accepted = unit.evidence["accepted_resolution"]
    workspace = Path(unit.bindings["workspace"])
    parent, head = accepted["coordinates"]["head"], receipt["head"]
    if not verify.is_ancestor(workspace, parent, head) or not verify.worktree_clean(workspace):
        raise CampaignError("repair did not preserve a clean descendant candidate")
    if receipt.get("protected_source_before") != receipt.get(
        "protected_source_after"
    ) or not receipt.get("protected_source_before"):
        raise CampaignError("repair changed its frozen source identity or policy")
    preserved = tuple(accepted["preserved_refs"])
    preserved_heads = []
    for reference in preserved:
        preserved_head = verify.rev_parse_revision(workspace, reference)
        preserved_heads.append(preserved_head)
        if not verify.is_ancestor(workspace, preserved_head, head):
            raise CampaignError("repair omitted a preserved conflict head")
    result = verify.git_bytes(workspace, "diff", "--name-only", "-z", parent, head)
    if result.returncode:
        raise CampaignError("repair changed-path observation failed")
    paths = tuple(path.decode("utf-8") for path in result.stdout.split(b"\0") if path)
    scopes = tuple(accepted["affected_paths"])
    mirrors = set(receipt.get("artifact_mirrors", {}).values())
    for path in paths:
        if path in mirrors and path.startswith("docs/bmad-campaign/"):
            continue
        if any(
            verify.file_bytes_at_revision(workspace, reference, path)
            == verify.file_bytes_at_revision(workspace, head, path)
            for reference in preserved_heads
            if reference != parent
        ):
            continue  # unchanged content carried from the preserved main side
        if not any(
            scope in {"*", "."}
            or path == scope.rstrip("/")
            or path.startswith(scope.rstrip("/") + "/")
            for scope in scopes
        ):
            raise CampaignError("repair changed a path outside its accepted repair scope")
    if not paths and not accepted.get("command"):
        raise CampaignError("repair produced no observable correction")
    return {
        "resolution_id": accepted["id"],
        "head": head,
        "parent": parent,
        "changed_paths": list(paths),
        "receipt": receipt["launch_id"],
    }


def finish(runtime: Any, snapshot: Any, unit: Any, action: Any) -> Observation:
    accepted = unit.evidence["accepted_resolution"]
    if action.payload["proposal_id"] != accepted["id"]:
        return Observation("rejected", reason="repair completion belongs to a superseded diagnosis")
    if (
        not unit.claim
        or not unit.claim.quiescent
        or unit.hold is None
        or unit.hold.kind != "resolution"
    ):
        return Observation("rejected", reason="repair completion requires its held quiescent actor")
    verification = unit.evidence.get("resolution_verification", {})
    if (
        verification.get("resolution_id") != accepted["id"]
        or verification.get("outcome") != "verified"
    ):
        return Observation("rejected", reason="repair lacks separate successful verification")
    workspace = Path(unit.bindings["workspace"])
    head = verify.rev_parse_head(workspace)
    coordinates = EvidenceCoordinates(**accepted["coordinates"])
    if (
        verification.get("head") != head
        or not verify.worktree_clean(workspace)
        or not verify.is_ancestor(workspace, coordinates.head, head)
        or coordinates.graph_revision != snapshot.graph_revision
        or coordinates.config_revision != snapshot.config_revision
    ):
        return Observation("rejected", reason="verified repair candidate or configuration changed")
    source_identity = hashlib.sha256(
        json.dumps(
            snapshot.bindings.get("source_observations", {}).get(unit.id, {}), sort_keys=True
        ).encode()
    ).hexdigest()
    if source_identity != coordinates.source_revision:
        return Observation("rejected", reason="canonical source observation changed during repair")
    pushed = runtime._pusher(snapshot, unit).push(action.id, head)
    if pushed.state != "confirmed":
        return Observation("unknown", reason="repair checkpoint push awaits remote readback")
    records = tuple(
        (tuple(row["argv"]), int(row["returncode"]))
        for row in verification.get("commands", [])
        if "argv" in row
    )
    observed = dict(records)
    probe = tuple(accepted.get("command", ()))
    if accepted["action"] == "reprobe" and not probe:
        return Observation(
            "rejected", reason="reprobe requires an explicitly qualified observable command"
        )
    if probe and observed.get(probe) != 0:
        return Observation(
            "rejected", reason="authorized environment reprobe was not observed successful"
        )
    result = ResolutionResult(
        accepted["id"],
        coordinates,
        coordinates.head,
        head,
        True,
        True,
        source_identity,
        verification=records,
        reprobe_passed=True,
        recovery_push_confirmed=True,
    )
    return Observation("confirmed", {"result": dataclasses.asdict(result)})


def apply(snapshot: Any, unit: Any, data: dict[str, Any]) -> None:
    from .campaign_resolution_flow import finish_repair

    raw = dict(data["result"])
    raw["coordinates"] = EvidenceCoordinates(**raw["coordinates"])
    raw["verification"] = tuple((tuple(command), code) for command, code in raw["verification"])
    finish_repair(snapshot, unit.id, ResolutionResult(**raw))
    for key in (
        "accepted_resolution",
        "repair_candidate",
        "resolution_verification",
        "resolution_next_step",
    ):
        unit.evidence.pop(key, None)
