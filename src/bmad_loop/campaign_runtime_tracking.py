"""Automatic draft-PR delivery of canonical source debt.

Tracking is a real control unit with its own specification, checkout, draft PR,
verification, independent review and merge. It never marks the original delivered.
The original stays merged_pending_sync until its ordinary publication retries
read back the source on fresh main. Worker preparation is deterministic and uses
only the source publication module's existing sole-writer capabilities.
"""

from __future__ import annotations

import base64
import dataclasses
import hashlib
import json
from pathlib import Path

from . import sprintstatus, verify
from .adapters.paradev_runtime import private_json, read_private_json
from .campaign_model import (
    CampaignError,
    CampaignSnapshot,
    CampaignUnit,
    SourceSelector,
    UnitSpec,
    UnitState,
    source_to_dict,
    validate_graph,
)
from .campaign_source_publication import (
    ArtifactChange,
    SourceBinding,
    SourcePublication,
    SourcePublicationResult,
    completed_plan_bytes,
    digest,
)
from .campaign_store import add_event
from .platform_util import is_link_like


def _digest(value) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _encoded(value: bytes | None):
    return base64.b64encode(value).decode("ascii") if value is not None else None


def _decoded(value):
    return base64.b64decode(value, validate=True) if value is not None else None


def _relative(project: Path, path: str) -> str:
    absolute = Path(path)
    if not absolute.is_absolute() or not absolute.is_relative_to(project):
        raise CampaignError("Tracking source must be owned by the code repository")
    relative = absolute.relative_to(project).as_posix()
    if ".." in Path(relative).parts or not relative:
        raise CampaignError("Tracking source path escaped its code repository")
    return relative


def prepare(
    snapshot: CampaignSnapshot,
    original: CampaignUnit,
    publication: SourcePublicationResult | dict,
    *,
    state_dir: Path,
    publisher_project: Path,
    source: SourceSelector,
    merge: dict,
    acceptance: dict,
    binding: SourceBinding | None = None,
    artifact: bytes | None = None,
    changes: tuple[ArtifactChange, ...] = (),
) -> dict:
    """Persist once before admitting a deterministic, deduplicated tracking unit."""
    result = (
        dataclasses.asdict(publication) if dataclasses.is_dataclass(publication) else publication
    )
    if (
        result.get("state") != "tracking_pr_required"
        or original.state != UnitState.MERGED_PENDING_SYNC
    ):
        raise CampaignError("Tracking control requires confirmed merge with actual source debt")
    if original.spec.kind == "tracking":
        raise CampaignError("A tracking control unit cannot recursively create tracking debt")
    project = publisher_project.resolve()
    if changes and (result.get("proposal") or {}).get("kind") == "planning-tracking":
        selected = {str(project / row["path"]) for row in result["proposal"]["artifacts"]}
        changes = tuple(change for change in changes if str(Path(change.path)) in selected)
        if not changes:
            raise CampaignError("Planning tracking proposal has no exact owned artifact changes")
    if state_dir.resolve().is_relative_to(project):
        raise CampaignError("Tracking authority must live outside editable checkouts")
    if not changes and binding is None:
        raise CampaignError("Tracking debt lacks canonical source authority")
    if changes and binding is not None:
        raise CampaignError(
            "Tracking request cannot mix planning and completion source authorities"
        )
    if changes:
        paths = tuple(sorted({_relative(project, change.path) for change in changes}))
    else:
        assert binding is not None
        paths = (_relative(project, binding.path),)
    if artifact is not None and (
        acceptance.get("artifact_digest") != digest(artifact)
        or verify.file_bytes_at_revision(
            project, merge["merged_commit"], acceptance.get("review_path", "")
        )
        != artifact
    ):
        raise CampaignError("Tracking source artifact differs from its actual reviewed merge")
    payload = {
        "schema_version": 1,
        "campaign_id": snapshot.id,
        "original_unit": original.id,
        "original_spec": dataclasses.asdict(original.spec),
        "project": str(project),
        "source": source_to_dict(source),
        "binding": binding.to_dict() if binding else None,
        "artifact": _encoded(artifact),
        "merge": merge,
        "acceptance": acceptance,
        "changes": [
            {
                **dataclasses.asdict(change),
                "data": _encoded(change.data),
                "preimage": _encoded(change.preimage),
            }
            for change in changes
        ],
        "paths": paths,
        "remote": snapshot.spec.remote,
        "target": snapshot.spec.target,
        "graph_revision": snapshot.graph_revision,
        "config_revision": snapshot.config_revision,
    }
    debt = _digest(payload)
    unit_id = "tracking:" + original.id + ":" + debt[:16]
    spec = UnitSpec(
        unit_id,
        original.spec.source,
        original.spec.epic,
        "Publish canonical BMAD source for " + original.spec.title,
        selector=original.spec.selector,
        after=original.spec.after,
        scopes=paths,
        kind="tracking",
        parent=original.id,
        initiative=original.spec.initiative,
        paths=paths,
        acceptance=(
            "Publish exactly the reviewed canonical source paths through this draft PR.",
            "Retain original merged work and finish source synchronization before closing its issue.",
        ),
    )
    payload["tracking_unit"] = unit_id
    payload["debt_digest"] = debt
    path = state_dir.resolve() / "tracking-requests" / (debt + ".json")
    if path.exists():
        if read_private_json(path) != json.loads(json.dumps(payload)):
            raise CampaignError("Tracking debt authority changed after preparation")
    else:
        private_json(path, payload)
    return json.loads(
        json.dumps(
            {
                "unit": dataclasses.asdict(spec),
                "request": str(path),
                "digest": hashlib.sha256(path.read_bytes()).hexdigest(),
                "original_unit": original.id,
                "debt_digest": debt,
            }
        )
    )


def _load(descriptor: dict) -> dict:
    path = Path(descriptor["request"])
    if is_link_like(path) or not path.is_file() or path.stat().st_size > 64 * 1024 * 1024:
        raise CampaignError("Tracking authority is missing, redirected or unbounded")
    if hashlib.sha256(path.read_bytes()).hexdigest() != descriptor["digest"]:
        raise CampaignError("Tracking authority digest changed")
    payload = read_private_json(path)
    if (
        _digest(
            {
                key: value
                for key, value in payload.items()
                if key not in ("tracking_unit", "debt_digest")
            }
        )
        != payload["debt_digest"]
    ):
        raise CampaignError("Tracking debt content identity differs from its prepared digest")
    if (
        payload["debt_digest"] != descriptor["debt_digest"]
        or payload["original_unit"] != descriptor["original_unit"]
    ):
        raise CampaignError("Tracking authority names different debt")
    return payload


def admit(snapshot: CampaignSnapshot, descriptor: dict) -> CampaignUnit:
    """Single-writer admission; no original completion or fake graph delivery."""
    payload = _load(descriptor)
    if (
        payload["campaign_id"] != snapshot.id
        or payload["config_revision"] != snapshot.config_revision
    ):
        raise CampaignError("Tracking authority has stale campaign coordinates")
    original = snapshot.units[descriptor["original_unit"]]
    if original.state != UnitState.MERGED_PENDING_SYNC:
        raise CampaignError("Tracking original no longer has merged source debt")
    spec = UnitSpec(
        **{
            **descriptor["unit"],
            **{
                key: tuple(descriptor["unit"].get(key, ()))
                for key in ("after", "scopes", "acceptance", "paths")
            },
        }
    )
    if spec.kind != "tracking" or spec.id != payload["tracking_unit"] or spec.parent != original.id:
        raise CampaignError("Tracking control specification differs from its debt authority")
    if spec.id in snapshot.units:
        existing = snapshot.units[spec.id]
        if existing.bindings.get("tracking") != descriptor:
            raise CampaignError("Tracking unit identity was reused for different debt")
        if existing.state == UnitState.DELIVERED:
            raise CampaignError(
                "Canonical source debt reappeared after its tracking unit was delivered"
            )
        return existing
    if (
        payload["graph_revision"] > snapshot.graph_revision
        or json.loads(json.dumps(dataclasses.asdict(original.spec))) != payload["original_spec"]
    ):
        raise CampaignError(
            "New tracking authority has changed original requirements or future graph coordinates"
        )
    controls = original.bindings.setdefault("tracking_units", [])
    if any(snapshot.units[key].state != UnitState.DELIVERED for key in controls):
        raise CampaignError("Original source debt already has a live tracking control unit")
    unit = CampaignUnit(
        spec,
        bindings={"tracking": descriptor},
        evidence={
            "developer_family": original.evidence.get("developer_family") or "deterministic",
            "tracking_author": "bmad-loop deterministic source writer",
        },
    )
    validate_graph([row.spec for row in snapshot.units.values()] + [spec])
    snapshot.units[unit.id] = unit
    snapshot.graph_revision += 1
    controls.append(unit.id)
    add_event(
        snapshot,
        "tracking-control-admitted",
        unit=unit.id,
        original_unit=original.id,
        debt_digest=descriptor["debt_digest"],
        source_graph_revision=payload["graph_revision"],
        admitted_graph_revision=snapshot.graph_revision,
    )
    return unit


def pending_tracking(snapshot: CampaignSnapshot, original: CampaignUnit) -> tuple[str, ...]:
    return tuple(
        key
        for key in original.bindings.get("tracking_units", ())
        if snapshot.units[key].state != UnitState.DELIVERED
    )


def _expected_after(payload, relative: str, before: bytes | None) -> bytes:
    if payload["changes"]:
        project = Path(payload["project"])
        value = next(
            change
            for change in payload["changes"]
            if _relative(project, change["path"]) == relative
        )
        after = _decoded(value["data"])
    else:
        binding = SourceBinding.from_dict(payload["binding"])
        artifact = _decoded(payload["artifact"])
        if binding.kind == "sprint-status":
            after = (
                sprintstatus.advanced_bytes(before, binding.selector, "done")
                if before is not None
                else None
            )
        elif binding.kind == "tickets":
            accepted = artifact if artifact is not None else before
            after = completed_plan_bytes(accepted) if accepted is not None else None
        else:
            after = artifact
    if after is None:
        raise CampaignError("Tracking transaction cannot produce a canonical source artifact")
    return after


def _validate_transaction(payload, descriptor, record, workspace):
    if (
        record.get("request_digest") != descriptor["digest"]
        or record.get("workspace") != str(workspace)
        or set(record.get("files", {})) != set(payload["paths"])
    ):
        raise CampaignError("Tracking transaction differs from its exact prepared authority")
    for relative, row in record["files"].items():
        before = verify.file_bytes_at_revision(workspace, record["base"], relative)
        if before != _decoded(row["before"]) or _expected_after(
            payload, relative, before
        ) != _decoded(row["after"]):
            raise CampaignError(
                "Tracking transaction bytes differ from reviewed artifacts and actual base"
            )


@dataclasses.dataclass(frozen=True)
class TrackingRecovery:
    """A prior exact staging transaction, never permission for arbitrary dirt."""

    path: Path
    request_digest: str

    def validate(self, workspace: Path) -> dict:
        if self.path.resolve().is_relative_to(workspace.resolve()):
            raise CampaignError("Tracking recovery proof must be outside the editing checkout")
        record = read_private_json(self.path)
        if record["request_digest"] != self.request_digest or record["workspace"] != str(
            workspace.resolve()
        ):
            raise CampaignError("Tracking recovery transaction identity changed")
        if verify.rev_parse_head(workspace) != record["base"]:
            raise CampaignError("Tracking recovery checkout head changed")
        result = verify.git_bytes(
            workspace, "status", "--porcelain=v1", "-z", "--untracked-files=all"
        )
        if result.returncode:
            raise CampaignError("Tracking recovery dirty-path inventory unavailable")
        for entry in result.stdout.split(b"\0"):
            if not entry:
                continue
            if entry[:2] not in (b" M", b"M ", b"MM", b"??", b"A ", b"AM"):
                raise CampaignError("Tracking recovery has an unsupported dirty path shape")
            relative = entry[3:].decode("utf-8")
            if relative not in record["files"]:
                raise CampaignError("Tracking recovery refuses unrelated dirty paths")
            path = workspace / relative
            if is_link_like(path):
                raise CampaignError("Tracking recovery source was redirected")
            data = path.read_bytes() if path.exists() else None
            row = record["files"][relative]
            if data not in (_decoded(row["before"]), _decoded(row["after"])):
                raise CampaignError("Tracking recovery source differs from its exact transaction")
            index = verify.git_bytes(
                workspace, "ls-files", "--stage", "-z", "--", ":(literal)" + relative
            )
            if index.returncode:
                raise CampaignError("Tracking recovery index observation failed")
            if index.stdout:
                staged = verify.git_bytes(workspace, "show", ":" + relative)
                if staged.returncode or staged.stdout not in (
                    _decoded(row["before"]),
                    _decoded(row["after"]),
                ):
                    raise CampaignError(
                        "Tracking recovery preserves differing staged source content"
                    )
        return record


def run_tracking(request: dict, envelope, *, journal) -> dict:
    """Deterministic child action; normal pipeline reviews and merges its commit."""
    descriptor = request["tracking"]
    payload = _load(descriptor)
    workspace = Path(request["workspace"]).resolve()
    if (
        payload["campaign_id"] != envelope.campaign_id
        or payload["tracking_unit"] != envelope.unit_id
        or payload["config_revision"] != envelope.config_revision
    ):
        raise CampaignError("Tracking child envelope differs from its prepared authority")
    project = Path(payload["project"])
    if workspace == project or Path(descriptor["request"]).resolve().is_relative_to(workspace):
        raise CampaignError("Tracking worker requires its own checkout and external authority")
    publisher = SourcePublication(
        project,
        envelope.directory / "source-publication",
        remote=payload["remote"],
        target=payload["target"],
    )
    source = SourceSelector(**payload["source"])
    original = UnitSpec(**payload["original_spec"])
    binding = SourceBinding.from_dict(payload["binding"]) if payload["binding"] else None
    changes = tuple(
        ArtifactChange(
            **{
                **change,
                "data": _decoded(change["data"]),
                "preimage": _decoded(change["preimage"]),
                "owned_rows": tuple(change.get("owned_rows", ())),
            }
        )
        for change in payload["changes"]
    )
    artifact = _decoded(payload["artifact"])
    transaction = Path(descriptor["request"]).parent / (payload["debt_digest"] + ".staging.json")
    marker = "BMAD-Tracking-Action: " + payload["debt_digest"]
    if transaction.exists():
        record = read_private_json(transaction)
        _validate_transaction(payload, descriptor, record, workspace)
        current = verify.rev_parse_head(workspace)
        if current != record["base"]:
            parents = verify.git_bytes(workspace, "show", "-s", "--format=%P%n%B", current)
            text = parents.stdout.decode("utf-8").splitlines()
            if parents.returncode or not text or text[0] != record["base"] or marker not in text:
                raise CampaignError(
                    "Tracking interrupted commit has different parent/action identity"
                )
            if any(
                verify.file_bytes_at_revision(workspace, current, relative)
                != _decoded(row["after"])
                for relative, row in record["files"].items()
            ):
                raise CampaignError(
                    "Tracking interrupted commit differs from accepted source bytes"
                )
            changed = verify.git_bytes(
                workspace, "diff-tree", "--no-commit-id", "--name-only", "-r", current
            )
            if changed.returncode or set(changed.stdout.decode("utf-8").splitlines()) - set(
                record["files"]
            ):
                raise CampaignError("Tracking interrupted commit contains unrelated paths")
            if not verify.worktree_clean(workspace):
                raise CampaignError("Tracking completed commit has subsequent uncommitted changes")
            return {
                "outcome": "tracking_complete",
                "head": current,
                "tracking": descriptor,
                "artifacts": [
                    {"path": relative, "sha256": digest(_decoded(row["after"]))}
                    for relative, row in record["files"].items()
                ],
            }
        recovery = TrackingRecovery(transaction, descriptor["digest"])
        recovery.validate(workspace)
    else:
        if not verify.worktree_clean(workspace):
            raise CampaignError("New tracking transaction requires a clean owned checkout")
        files = {}
        total_bytes = 0
        for relative in payload["paths"]:
            path = workspace / relative
            if is_link_like(path) or (
                path.exists() and (not path.is_file() or path.stat().st_size > 8 * 1024 * 1024)
            ):
                raise CampaignError("Tracking source is redirected, nonregular or unbounded")
            before = path.read_bytes() if path.exists() else None
            after = _expected_after(payload, relative, before)
            total_bytes += len(before or b"") + len(after)
            if total_bytes > 64 * 1024 * 1024:
                raise CampaignError("Tracking transaction source bytes exceed configured bound")
            files[relative] = {"before": _encoded(before), "after": _encoded(after)}
        record = {
            "schema_version": 1,
            "request_digest": descriptor["digest"],
            "workspace": str(workspace),
            "base": verify.rev_parse_head(workspace),
            "files": files,
        }
        private_json(transaction, record)
        recovery = None
    if changes:
        paths = publisher.prepare_tracking_artifacts(
            workspace,
            source,
            original,
            changes,
            merge=payload["merge"],
            acceptance=payload["acceptance"],
            journal=journal,
            recovery=recovery,
        )
    else:
        if binding is None:
            raise CampaignError("Tracking completion has no canonical source binding")
        paths = publisher.prepare_tracking_checkout(
            workspace,
            source,
            original,
            binding,
            mirror_bytes=artifact,
            merge=payload["merge"],
            acceptance=payload["acceptance"],
            journal=journal,
            recovery=recovery,
        )
    verify.commit_paths(
        workspace,
        "fix: publish canonical BMAD source\n\n" + marker,
        [workspace / relative for relative in record["files"]],
    )
    head = verify.rev_parse_head(workspace)
    if not verify.worktree_clean(workspace) or any(
        verify.file_bytes_at_revision(workspace, head, relative) != _decoded(row["after"])
        for relative, row in record["files"].items()
    ):
        raise CampaignError("Tracking commit did not publish exactly its accepted source paths")
    return {
        "outcome": "tracking_complete",
        "head": head,
        "tracking": descriptor,
        "changed_paths": [path.relative_to(workspace).as_posix() for path in paths],
        "artifacts": [
            {"path": relative, "sha256": digest(_decoded(row["after"]))}
            for relative, row in record["files"].items()
        ],
    }


def confirm_tracking(snapshot: CampaignSnapshot, unit: CampaignUnit, *, state_dir: Path) -> dict:
    """Read back the real tracking merge before its own delivery can be recorded."""
    payload = _load(unit.bindings["tracking"])
    if unit.spec.kind != "tracking" or unit.state != UnitState.MERGED_PENDING_SYNC:
        raise CampaignError("Tracking confirmation requires its actual merged control unit")
    merge = unit.evidence["merge"]
    project = Path(payload["project"])
    publisher = SourcePublication(
        project, state_dir, remote=payload["remote"], target=payload["target"]
    )
    head, _ = publisher._canonical("tracking-confirm:" + unit.id, merge["merge_commit"])
    expected_head = unit.evidence["integrated"]["head"]
    for relative in payload["paths"]:
        expected = verify.file_bytes_at_revision(project, expected_head, relative)
        merged = verify.file_bytes_at_revision(project, merge["merge_commit"], relative)
        actual = verify.file_bytes_at_revision(project, head, relative)
        if expected is None or expected != merged or actual != merged:
            raise CampaignError("Canonical tracking source differs from its reviewed actual merge")
    return {
        "state": "confirmed",
        "canonical_head": head,
        "original_unit": payload["original_unit"],
        "tracking_unit": unit.id,
        "source_paths": payload["paths"],
    }
