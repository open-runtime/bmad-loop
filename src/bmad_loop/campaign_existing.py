"""Verify an already-terminal source through a normal new draft-PR pipeline.

Source status is a selector for this path, never delivery evidence. There is no
legacy Engine and no attempt to pick done work again. The bounded child checks
the exact private source, verifies existing code deterministically, and returns
reviewable source bytes. Current-main integration, fresh full-diff model review,
hosted checks, merge and tracking remain owed by the campaign.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from . import sprintstatus, stories, ticketlayout, verify
from .campaign_model import CampaignError, SourceSelector, UnitSpec
from .tickettree import TicketTree


class ExistingDeliveryHeld(CampaignError):
    """A prior mapped tracker still carries a current explicit publication hold."""


@dataclass(frozen=True)
class PriorDeliveryProof:
    repository: str
    unit_id: str
    specification_digest: str
    source_sha256: str
    prior_campaign: str
    prior_revision: int
    prior_pr: int
    head: str
    merge_commit: str
    canonical_head: str
    graph_revision: int
    config_revision: int
    tracking_issue: dict[str, Any] = field(default_factory=dict)
    pull_request: dict[str, Any] = field(default_factory=dict)
    source_verified: bool = True
    tracking_verified: bool = True
    schema_version: int = 1
    source_products: tuple[tuple[str, str], ...] = ()


def specification_digest(unit: UnitSpec) -> str:
    return hashlib.sha256(json.dumps(asdict(unit), sort_keys=True).encode()).hexdigest()


def terminal_status(observation: dict[str, Any]) -> str:
    """Normalize the native terminal selector without granting delivery."""
    state = observation.get("state", "")
    status = observation.get("status", "")
    if state == "dropped" or status == "dropped":
        return "dropped"
    return "done" if state == "done" or status == "done" else ""


def requires_existing(snapshot, unit) -> bool:
    adoption = unit.bindings.get("adoption", {})
    if (
        adoption.get("state") == "owned"
        and adoption.get("manifest", {}).get("mode") == "verify-existing"
    ):
        return True
    if unit.spec.kind == "closure" and unit.bindings.get("workspace"):
        # A historical done label without its native retrospective still owes
        # the bounded closure writer, even when its code is already on main.
        from .campaign_sources import source_paths

        workspace = Path(unit.bindings["workspace"])
        source = SourceSelector(**unit.bindings["effective_source"])
        paths = source_paths(workspace, source)
        if paths.initiative_folder is None:
            return False
        retrospective = ticketlayout.retrospective_doc(paths.initiative_folder, unit.spec.selector)
        if (
            not retrospective.is_file()
            or retrospective.is_symlink()
            or not retrospective.resolve().is_relative_to(workspace.resolve())
            or retrospective.stat().st_size > 8 * 1024 * 1024
            or len(retrospective.read_bytes().strip()) < 40
        ):
            return False
    return (
        terminal_status(snapshot.bindings.get("source_observations", {}).get(unit.id, {})) == "done"
    )


def dependency_reconciliation(units, observations, all_units, all_observations):
    """Include only terminal native prerequisites omitted by selection filters.

    Each added unit still owes its own verification draft and delivery. Nonterminal
    missing dependencies and dropped prerequisites fail instead of being credited.
    """
    result = list(units)
    known = {unit.id for unit in result}
    indexed = {unit.id: unit for unit in all_units}
    pending = [
        dependency for unit in result for dependency in unit.after if dependency not in known
    ]
    while pending:
        key = pending.pop(0)
        if key in known:
            continue
        proposed = indexed.get(key)
        observed = all_observations.get(key, {})
        if proposed is None or terminal_status(observed) != "done":
            raise CampaignError(
                f"dependencies outside selected graph have no verified terminal reconciliation path: {key}"
            )
        result.append(proposed)
        known.add(key)
        observations[key] = {**observed, "dependency_reconciliation": True}
        pending.extend(dependency for dependency in proposed.after if dependency not in known)
    return result


def prior_delivery(runtime, snapshot, unit) -> PriorDeliveryProof | None:
    """Reuse only a previously verified native delivery, freshly read remotely.

    A merely closed tracker or a prose link to a PR is insufficient. External
    pre-campaign work deliberately falls back to a fresh verification draft.
    """
    from .campaign_model import UnitState
    from .campaign_publication import Candidate, PublicationController, PublicationRequirements

    source = runtime.source(snapshot, unit)
    try:
        path, _ = terminal_path(runtime.project, source, unit.spec)
    except CampaignError:
        return None
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    products = {path.relative_to(runtime.project).as_posix(): digest}
    retrospective_digest = None
    if unit.spec.kind == "closure":
        retrospective = path.parent / (unit.spec.selector + "-retrospective.md")
        retrospective_digest = hashlib.sha256(retrospective.read_bytes()).hexdigest()
        products[retrospective.relative_to(runtime.project).as_posix()] = retrospective_digest
    current_selector = asdict(source)
    for previous in reversed(runtime.store.list()[-1000:]):
        if (
            previous.id == snapshot.id
            or previous.repository != snapshot.repository
            or previous.common_dir != snapshot.common_dir
        ):
            continue
        prior = previous.units.get(unit.id)
        if (
            prior is None
            or prior.state != UnitState.DELIVERED
            or prior.spec != unit.spec
            or (prior.claim and not prior.claim.quiescent)
        ):
            continue
        selected = next(
            (value for value in previous.spec.sources if value.id == prior.spec.source), None
        )
        if selected is None or asdict(selected) != current_selector:
            continue
        artifact = next(
            (
                row
                for row in prior.evidence.get("source_artifacts", ())
                if row.get("sha256") == digest
            ),
            None,
        )
        if unit.spec.kind == "closure" and not any(
            row.get("kind") == "closure-retrospective" and row.get("sha256") == retrospective_digest
            for row in prior.evidence.get("source_artifacts", ())
        ):
            continue
        merged = prior.evidence.get("merge", {})
        accepted = prior.evidence.get("verification", {})
        review = prior.evidence.get("review", {})
        issue = prior.bindings.get("issue") or {}
        if (
            artifact is None
            or not merged.get("merge_commit")
            or not accepted.get("launch_id")
            or accepted.get("outcome") != "verified"
            or review.get("validated", {}).get("verdict") != "approved"
            or not review.get("launch_id")
            or not review.get("observed_model")
            or not issue.get("number")
        ):
            continue
        head = prior.evidence.get("integrated", {}).get("head")
        base = prior.evidence.get("integrated", {}).get("base")
        pr = prior.bindings.get("pr") or {}
        if not head or not base or not pr.get("number"):
            continue
        gateway = runtime.gateway(snapshot)
        tracker = gateway.get(f"{gateway.root}/issues/{issue['number']}")
        if tracker.get("id") != issue.get("id") and issue.get("id") is not None:
            continue
        if tracker.get("state") != "closed":
            continue
        labels = {str(row.get("name", "")).lower() for row in tracker.get("labels", ())}
        if labels.intersection(value.lower() for value in snapshot.spec.github.hold_labels):
            raise ExistingDeliveryHeld(
                f"previously delivered source tracker #{issue['number']} has a current explicit hold"
            )
        candidate = Candidate(
            snapshot.repository,
            unit.id,
            pr["number"],
            head,
            base,
            prior.bindings.get("source_revision", ""),
            previous.graph_revision,
            str(previous.config_revision),
            snapshot.spec.target,
        )
        controller = PublicationController(
            gateway, requirements=PublicationRequirements.from_spec(snapshot.spec.github)
        )
        receipt = controller.merge_status(pr["number"], expected_head=head)
        if receipt.state != "merged_pending_sync" or receipt.merge_commit != merged["merge_commit"]:
            continue
        canonical = controller.confirm_canonical(
            candidate,
            receipt,
            runtime.project,
            remote=snapshot.spec.remote,
            target=snapshot.spec.target,
        )
        if not verify.is_ancestor(runtime.project, head, canonical.canonical_head):
            continue
        if any(
            (
                data := verify.file_bytes_at_revision(
                    runtime.project, canonical.canonical_head, relative
                )
            )
            is None
            or hashlib.sha256(data).hexdigest() != revision
            for relative, revision in products.items()
        ):
            continue
        refreshed_path, _ = terminal_path(runtime.project, source, unit.spec)
        if refreshed_path != path or any(
            hashlib.sha256((runtime.project / relative).read_bytes()).hexdigest() != revision
            for relative, revision in products.items()
        ):
            continue
        return PriorDeliveryProof(
            snapshot.repository,
            unit.id,
            specification_digest(unit.spec),
            digest,
            previous.id,
            previous.revision,
            pr["number"],
            head,
            receipt.merge_commit,
            canonical.canonical_head,
            snapshot.graph_revision,
            snapshot.config_revision,
            dict(issue),
            dict(pr),
            source_products=tuple(sorted(products.items())),
        )
    return None


def terminal_path(
    workspace: Path, source: SourceSelector, unit: UnitSpec, *, allow_built: bool = False
) -> tuple[Path, str]:
    """Read the selected native source; no lifecycle/status write is permitted."""
    from .campaign_sources import source_paths

    paths = source_paths(workspace, source)
    if source.kind == "stories":
        folder = stories.resolve_spec_folder(workspace, source.path)
        state = stories.resolve_story_spec(folder, unit.selector)
        if state.status != "done" or state.path is None:
            raise CampaignError("existing story source is not an unambiguous done spec")
        path, status = state.path, state.status
    elif source.kind == "sprint-status":
        path = paths.sprint_status
        status = sprintstatus.story_status(path, unit.selector)
        if status != "done":
            raise CampaignError("existing sprint source selected row is not done")
    elif source.kind == "tickets":
        if paths.initiative_folder is None:
            raise CampaignError("existing ticket verification requires a selected initiative")
        tree = TicketTree(workspace, folder=paths.initiative_folder)
        if unit.kind == "closure":
            board = tree.status()
            epic = next((row for row in board.epics if row.slug == unit.selector), None)
            if board.problems or epic is None or epic.status != "done":
                raise CampaignError("existing epic closure is not canonically done")
            if any(
                row.state not in {"done", "dropped"}
                for row in board.tickets
                if row.epic == unit.selector
            ):
                raise CampaignError("existing epic closure still has unfinished source children")
            path = ticketlayout.epic_file(paths.initiative_folder, unit.selector)
            retrospective = ticketlayout.retrospective_doc(paths.initiative_folder, unit.selector)
            if (
                not retrospective.is_file()
                or retrospective.is_symlink()
                or not retrospective.resolve().is_relative_to(workspace.resolve())
                or retrospective.stat().st_size > 8 * 1024 * 1024
                or len(retrospective.read_bytes().strip()) < 40
            ):
                raise CampaignError(
                    "existing epic closure lacks a substantive native retrospective"
                )
            status = epic.status
        else:
            found = tree.find(unit.selector)
            if (
                not (
                    (found.row.state == "done" and found.row.status == "done")
                    or (allow_built and found.row.status == "built")
                )
                or not found.plan
            ):
                raise CampaignError("existing ticket source is not done with an accepted plan")
            path, status = Path(found.plan), found.row.status
    else:
        raise CampaignError(
            "existing verification supports only native tickets/stories/sprint-status"
        )
    if not path.is_absolute():
        path = workspace / path
    if (
        path.is_symlink()
        or not path.resolve().is_relative_to(workspace.resolve())
        or not path.is_file()
        or path.stat().st_size > 8 * 1024 * 1024
    ):
        raise CampaignError(
            "existing source is missing, redirected, unbounded or outside the owned checkout"
        )
    return path, status


def verify_existing(request: dict[str, Any], envelope) -> dict[str, Any]:
    """Use only configured verification; ordinary publication review follows."""
    from .campaign_workers import _verify
    from .platform_util import safe_segment

    workspace = Path(request["workspace"])
    source = SourceSelector(**request["source"])
    unit = UnitSpec(**request["unit"])
    allow_built = request.get("adoption_mode") == "verify-existing"
    path, status = terminal_path(workspace, source, unit, allow_built=allow_built)
    before = path.read_bytes()
    candidate = verify.rev_parse_head(workspace)
    verified = _verify(request, envelope)
    if verified["outcome"] != "verified":
        return {**verified, "existing_source": True, "source_status": status}
    after_path, after_status = terminal_path(workspace, source, unit, allow_built=allow_built)
    if after_path != path or after_status != status or path.read_bytes() != before:
        raise CampaignError("existing source changed while its candidate was verified")
    if verify.rev_parse_head(workspace) != candidate:
        raise CampaignError("existing-code verifier moved the accepted branch")
    if status == "built":
        from .journal import Journal
        from .model import RunState
        from .platform_util import root_identity_record

        journal_dir = envelope.directory / "existing-source-journal"
        journal_dir.mkdir(parents=True, exist_ok=True)
        pin = RunState(
            "existing-source",
            str(workspace),
            "",
            run_dir_identity=root_identity_record(journal_dir),
        )
        from .campaign_sources import source_paths

        paths = source_paths(workspace, source)
        tree = TicketTree(workspace, folder=paths.initiative_folder)
        tree.mark(
            unit.selector,
            "done",
            reason="accept",
            expected_plan=path,
            journal=Journal(journal_dir, pin=pin),
        )
        path, status = terminal_path(workspace, source, unit)
        before = path.read_bytes()
        relative = path.relative_to(workspace).as_posix()
        if (
            verify.git_bytes(workspace, "ls-files", "--error-unmatch", "--", relative).returncode
            == 0
        ):
            verify.commit_paths(workspace, "docs: accept verified retained ticket product", [path])
    relative = path.relative_to(workspace).as_posix()
    mirror = path
    if verify.git_bytes(workspace, "ls-files", "--error-unmatch", "--", relative).returncode:
        mirror = workspace / "docs/bmad-campaign" / safe_segment(unit.id) / "existing-source.md"
        mirror.parent.mkdir(parents=True, exist_ok=True)
        mirror.write_bytes(before)
        verify.commit_paths(
            workspace, "docs: record existing accepted source for reconciliation", [mirror]
        )
    artifact = {
        "path": relative,
        "review_path": mirror.relative_to(workspace).as_posix(),
        "sha256": hashlib.sha256(before).hexdigest(),
        "kind": "closure" if unit.kind == "closure" else source.kind,
    }
    artifacts = [artifact]
    if unit.kind == "closure":
        retrospective = path.parent / (unit.selector + "-retrospective.md")
        data = retrospective.read_bytes()
        retro_relative = retrospective.relative_to(workspace).as_posix()
        retro_mirror = retrospective
        if verify.git_bytes(
            workspace, "ls-files", "--error-unmatch", "--", retro_relative
        ).returncode:
            retro_mirror = (
                workspace
                / "docs/bmad-campaign"
                / safe_segment(unit.id)
                / "existing-retrospective.md"
            )
            retro_mirror.parent.mkdir(parents=True, exist_ok=True)
            retro_mirror.write_bytes(data)
            verify.commit_paths(
                workspace, "docs: record existing accepted retrospective", [retro_mirror]
            )
        artifacts.append(
            {
                "path": retro_relative,
                "review_path": retro_mirror.relative_to(workspace).as_posix(),
                "sha256": hashlib.sha256(data).hexdigest(),
                "kind": "closure-retrospective",
            }
        )
    proof = {
        "schema_version": 1,
        "unit_id": unit.id,
        "selector": unit.selector,
        "source": source.kind,
        "status": status,
        "verified_head": candidate,
        "source_sha256": artifact["sha256"],
        "commands": verified["commands"],
        "delivery_pending": True,
        "no_development_session": True,
    }
    return {
        "outcome": "existing_verified",
        "existing_source": True,
        "source_artifacts": artifacts,
        "source_terminal_receipt": proof,
        "commands": verified["commands"],
        "base": candidate,
        "head": verify.rev_parse_head(workspace),
        "sessions": [],
    }
