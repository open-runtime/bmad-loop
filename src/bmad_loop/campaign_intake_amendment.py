"""Resume an unmerged story against a reviewed acceptance amendment.

The old actor drains before this effect. Its branch, source files and receipts
are retained. A new private source generation copies fresh canonical artifacts;
no completed private board or ticket is regressed or restored. The next legacy
engine writes only that new generation through its existing sole-writer APIs.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
from pathlib import Path
from typing import Any

from . import bmadconfig, ticketstore, verify
from .campaign_intake_flow import INBOX
from .campaign_model import CampaignError, SourceSelector, UnitSpec, UnitState, unit_from_dict
from .campaign_planning import execution_spec
from .campaign_planning_publication import source_root
from .campaign_runtime_intake import control
from .campaign_source_publication import SourceBinding, SourcePublication
from .campaign_sources import _overlay_table, _shield, source_paths
from .campaign_store import add_event, transition
from .platform_util import atomic_write_bytes_confined, atomic_write_text, is_link_like


class AmendmentUncertain(CampaignError):
    """An existing publisher/refresh must reconcile before another effect."""


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def validate_initial_source(request: dict[str, Any]) -> None:
    """Worker boundary: exact selected native source before the first session."""
    expected = request.get("initial_source")
    if expected is None:
        return
    workspace = Path(request["workspace"]).resolve()
    path = workspace / expected["path"]
    if (
        Path(expected["path"]).is_absolute()
        or ".." in Path(expected["path"]).parts
        or is_link_like(path)
        or not path.resolve().is_relative_to(workspace)
        or not path.is_file()
        or path.stat().st_size > 8 * 1024 * 1024
    ):
        raise CampaignError("initial native source is redirected, missing or unbounded")
    if _digest(path.read_bytes()) != expected["sha256"]:
        raise CampaignError("initial native source changed after reviewed source activation")
    publisher = SourcePublication(
        workspace, Path(request["execution"]).parent / "initial-source-validation"
    )
    observed = publisher.capture(SourceSelector(**request["source"]), UnitSpec(**request["unit"]))
    if Path(observed.path).resolve() != path.resolve() or observed.revision != expected["sha256"]:
        raise CampaignError("native configuration points at a different initial source")


def _validate_generation(record: dict, generation: Path) -> None:
    actual = {}
    for path in generation.rglob("*"):
        if is_link_like(path):
            raise CampaignError("retained amendment source generation is redirected")
        if path.is_dir():
            continue
        if not path.is_file() or path.stat().st_size > 8 * 1024 * 1024:
            raise CampaignError("retained amendment source generation is unbounded")
        actual[path.relative_to(generation).as_posix()] = _digest(path.read_bytes())
    if actual != record["files"]:
        raise CampaignError("retained amendment source changed before acknowledgement")


def _current(snapshot, target, plan_id, proposal_id, *, action_id=""):
    plan = snapshot.units[plan_id]
    value = control(plan)
    proposal = snapshot.bindings[INBOX]["proposals"][proposal_id]
    if (
        not value
        or value.get("state") != "refreshing"
        or value.get("proposal_id") != proposal_id
        or target.id not in value.get("amendments", {})
    ):
        raise CampaignError("amendment no longer owns its delivered source plan")
    if (
        plan.state != UnitState.DELIVERED
        or proposal["state"] != "awaiting_refresh"
        or plan.evidence.get("planning_flow", {}).get("state") != "activated"
    ):
        raise CampaignError("amendment must wait for reviewed merged source/graph delivery")
    if (
        target.state
        in {
            UnitState.MERGE_REQUESTED,
            UnitState.MERGED_PENDING_SYNC,
            UnitState.DELIVERED,
            UnitState.CANCELED,
        }
        or target.claim
        and not target.claim.quiescent
    ):
        raise CampaignError("amendment cannot replace a live actor or landed contract")
    if any(
        action.unit == target.id
        and action.id != action_id
        and action.state in {"prepared", "sent_unknown"}
        and action.kind not in {"reflection", "observe-worker"}
        for action in snapshot.outbox
    ):
        raise AmendmentUncertain("amendment waits for original unit publishers to reconcile")
    expected = next(unit_from_dict(item) for item in proposal["units"] if item["id"] == target.id)
    if target.spec != expected:
        raise CampaignError("amendment graph contract changed after source publication")
    for version in proposal["input_versions"]:
        row = snapshot.bindings[INBOX]["records"][version]["observation"]
        if snapshot.bindings[INBOX]["latest"].get(row["kind"] + ":" + row["object_id"]) != version:
            raise CampaignError("incoming owner intent changed before private source refresh")
    return plan, value, proposal


def _new_files(project: Path, root: Path, head: str) -> dict[str, bytes]:
    """Copy fresh canonical source content into an empty generation, bounded."""
    result: dict[str, bytes] = {}
    consumed = 0
    if root.is_relative_to(project):
        relative = root.relative_to(project).as_posix()
        listing = verify.git_bytes(project, "ls-tree", "-rz", head, "--", ":(literal)" + relative)
        if listing.returncode:
            raise CampaignError("amendment cannot observe canonical source tree")
        for record in listing.stdout.split(b"\0"):
            if not record:
                continue
            metadata, raw_path = record.split(b"\t", 1)
            if metadata.split()[0] not in {b"100644", b"100755"}:
                raise CampaignError("amendment source tree contains redirected or special content")
            path = raw_path.decode("utf-8")
            name = (project / path).relative_to(root).as_posix()
            data = verify.file_bytes_at_revision(project, head, path)
            if data is None:
                raise CampaignError("canonical source blob disappeared during refresh")
            result[name] = data
            consumed += len(data)
            if consumed > 128 * 1024 * 1024 or len(result) > 10000:
                raise CampaignError("amendment source generation exceeds its bounded copy limit")
    if root.exists():
        if is_link_like(root):
            raise CampaignError("canonical source root is redirected")
        for path in root.rglob("*"):
            relative = path.relative_to(root)
            if any(
                part in {".git", ".bmad-loop"} or part.endswith(".lock") for part in relative.parts
            ):
                continue
            if is_link_like(path):
                raise CampaignError("canonical source content is redirected")
            if path.is_dir() or relative.as_posix() in result:
                continue
            if not path.is_file() or path.stat().st_size > 8 * 1024 * 1024:
                raise CampaignError("canonical source content is unavailable or unbounded")
            before = path.stat()
            data = path.read_bytes()
            after = path.stat()
            if (before.st_mtime_ns, before.st_size) != (after.st_mtime_ns, after.st_size):
                raise CampaignError("canonical source changed while copying private generation")
            result[relative.as_posix()] = data
            consumed += len(data)
            if consumed > 128 * 1024 * 1024 or len(result) > 10000:
                raise CampaignError("amendment source generation exceeds its bounded copy limit")
    return result


def perform(runtime, snapshot, action) -> dict[str, Any]:
    target = snapshot.units[action.unit]
    plan, value, proposal = _current(
        snapshot,
        target,
        action.payload["planning_unit"],
        action.payload["proposal_id"],
        action_id=action.id,
    )
    workspace = Path(target.bindings["workspace"]).resolve()
    gateway = runtime.gateway(snapshot)
    pr = gateway.pull(target.bindings["pr"]["number"])
    if (
        pr.get("merged") is not False
        or pr.get("state") != "open"
        or pr.get("head", {}).get("ref") != target.bindings["branch"]
    ):
        raise CampaignError("acceptance amendment requires the same open, unmerged owned PR")
    if pr.get("draft") is not True:
        intent = gateway.prepare_ready(
            pr["node_id"],
            action_id=action.id + "-draft",
            generation=snapshot.generation,
            draft=True,
            expected_head=pr["head"]["sha"],
            expected_updated_at=pr["updated_at"],
            expected_draft=False,
        )
        return {"intent": intent.to_dict(), "purpose": "amendment-draft"}
    directory = runtime.store.directory(snapshot.id) / "amendments" / action.id
    directory.mkdir(parents=True, exist_ok=True)
    record_path = directory / "refresh.json"
    record = json.loads(record_path.read_text(encoding="utf-8")) if record_path.exists() else {}
    if record.get("receipt"):
        if verify.rev_parse_head(workspace) != record["receipt"]["head"]:
            raise CampaignError("retained amendment branch moved before refresh acknowledgement")
        _validate_generation(record, Path(record["receipt"]["generation"]))
        return record["receipt"]
    if not verify.worktree_clean(workspace):
        raise CampaignError("amendment preserves uncommitted branch edits; commit/quiesce first")
    old_head = record.get("old_head") or verify.rev_parse_head(workspace)
    if not verify.is_ancestor(workspace, old_head, verify.rev_parse_head(workspace)):
        raise CampaignError("amendment cannot replace retained branch ancestry")
    if not verify.is_ancestor(workspace, pr["head"]["sha"], verify.rev_parse_head(workspace)):
        raise CampaignError("remote PR branch has foreign work absent from the retained checkout")
    previous = SourceBinding.from_dict(target.bindings["canonical_source"])
    preserved_sources = record.get("preserved_sources", [])
    if not preserved_sources:
        artifacts = target.evidence.get("source_artifacts", [])
        if artifacts:
            preserved_sources = [
                {"path": str(workspace / row["path"]), "sha256": row["sha256"]} for row in artifacts
            ]
        else:
            old_selector = SourceSelector(**target.bindings["effective_source"])
            old_view = SourcePublication(workspace, directory / "old-binding").capture(
                old_selector, target.spec
            )
            if old_view.revision != previous.revision:
                raise CampaignError(
                    "unstarted private source has newer edits; preserve its original view"
                )
            preserved_sources = [{"path": old_view.path, "sha256": old_view.revision}]
        record["preserved_sources"] = preserved_sources
        atomic_write_text(
            record_path,
            json.dumps({**record, "old_head": old_head}, sort_keys=True),
            follow_symlinks=False,
        )
    for artifact in preserved_sources:
        path = Path(artifact["path"])
        if (
            is_link_like(path)
            or not path.resolve().is_relative_to(workspace)
            or _digest(path.read_bytes()) != artifact["sha256"]
        ):
            raise CampaignError(
                "old private source has newer edits; retain it for explicit reconciliation"
            )
    source = runtime.source(snapshot, target)
    root = source_root(runtime.project, source).resolve()
    published = snapshot.bindings["source_observations"][target.id]
    canonical_path = Path(published["path"]).resolve()
    if (
        not canonical_path.is_relative_to(root)
        or published.get("remote_publication_confirmed") is not True
    ):
        raise CampaignError("amendment canonical artifact is outside its reviewed source authority")
    canonical, _ = runtime._source_publisher(snapshot)._canonical(
        action.id + ":amendment", plan.evidence["merge"]["merge_commit"]
    )
    files = _new_files(runtime.project, root, canonical)
    relative = canonical_path.relative_to(root).as_posix()
    if relative not in files or _digest(files[relative]) != published["sha256"]:
        raise CampaignError("canonical owner source changed after amendment planning approval")
    hashes = {name: _digest(data) for name, data in files.items()}
    if record.get("files") and record["files"] != hashes:
        raise CampaignError("canonical source generation changed during uncertain refresh")
    record.update(old_head=old_head, files=hashes, canonical_head=canonical)
    atomic_write_text(record_path, json.dumps(record, sort_keys=True), follow_symlinks=False)
    generation = workspace / ".bmad-loop" / "campaign-amendments" / action.id / "source"
    for component in (generation, *generation.parents):
        if component == workspace:
            break
        if is_link_like(component):
            raise CampaignError("private amendment generation is redirected")
    generation.mkdir(parents=True, exist_ok=True)
    for name, data in files.items():
        path = generation / name
        if path.exists():
            if is_link_like(path) or not path.is_file() or path.read_bytes() != data:
                raise CampaignError("private amendment generation has foreign edits")
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_bytes_confined(path, data, confine_root=generation)
    effective = source
    if source.kind == "stories":
        effective = dataclasses.replace(source, path=generation.relative_to(workspace).as_posix())
    elif source.kind == "tickets":
        ticketstore.redirect_mount_config(
            workspace,
            main_store=generation,
            initiative=source.initiative or source_paths(runtime.project, source).active_initiative,
        )
    else:
        canonical_paths = source_paths(runtime.project, source)
        local_paths = bmadconfig.load_paths(workspace)
        mapped = {}
        for name in ("output_folder", "implementation_artifacts", "planning_artifacts"):
            origin = getattr(canonical_paths, name).resolve()
            mapped[name] = (
                generation / origin.relative_to(root)
                if origin.is_relative_to(root)
                else getattr(local_paths, name)
            )
            if not mapped[name].resolve().is_relative_to(workspace):
                raise CampaignError("amended native source still reaches a shared writer")
        overlay = workspace / bmadconfig.CENTRAL_LAYERS_REL[-1]
        if verify.path_tracked(
            workspace, overlay.relative_to(workspace).as_posix()
        ) or is_link_like(overlay):
            raise CampaignError("private amendment requires an untracked native user overlay")
        text = overlay.read_text(encoding="utf-8") if overlay.exists() else ""
        text = _overlay_table(text, "core", {"output_folder": str(mapped["output_folder"])})
        text = _overlay_table(
            text,
            "modules.bmm",
            {
                name: str(mapped[name])
                for name in ("implementation_artifacts", "planning_artifacts")
            },
        )
        overlay.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(overlay, text, follow_symlinks=False)
        _shield(workspace, (overlay,))
    _shield(workspace, (generation,))
    observed = SourcePublication(workspace, directory / "binding").capture(effective, target.spec)
    if observed.status not in {"draft", "ready-for-dev", "backlog"}:
        raise CampaignError("reviewed amendment did not provide an executable fresh native source")
    binding = dataclasses.replace(
        observed,
        project=str(runtime.project),
        source_root=str(root),
        path=str(canonical_path),
        versioning=previous.versioning,
        code_relative=(
            canonical_path.relative_to(runtime.project).as_posix()
            if canonical_path.is_relative_to(runtime.project)
            else ""
        ),
        store_repo=previous.store_repo,
        selector_path=source.path,
    )
    spec_path = workspace / target.bindings["spec_path"]
    atomic_write_text(
        spec_path,
        execution_spec(
            target.spec,
            campaign_id=snapshot.id,
            graph_revision=snapshot.graph_revision,
            source=published,
        ),
        follow_symlinks=False,
    )
    verify.commit_paths(
        workspace,
        "docs: apply reviewed acceptance amendment\n\nBMAD-Amendment-Action: " + action.id,
        [spec_path],
    )
    head = verify.rev_parse_head(workspace)
    if not verify.is_ancestor(workspace, old_head, head):
        raise CampaignError("amendment checkpoint lost retained branch ancestry")
    pushed = runtime._pusher(snapshot, target).push(action.id + "-refresh", head)
    if pushed.state != "confirmed":
        raise AmendmentUncertain("amendment checkpoint push remains uncertain")
    receipt = {
        "action_id": action.id,
        "unit": target.id,
        "planning_unit": plan.id,
        "proposal_id": value["proposal_id"],
        "pr_number": pr["number"],
        "branch": target.bindings["branch"],
        "old_head": old_head,
        "head": head,
        "canonical_head": canonical,
        "effective_source": dataclasses.asdict(effective),
        "canonical_source": binding.to_dict(),
        "source_revision": published["sha256"],
        "generation": str(generation),
        "initial_source": {
            "path": Path(observed.path).relative_to(workspace).as_posix(),
            "sha256": observed.revision,
        },
        "manifest": str(record_path),
    }
    record["receipt"] = receipt
    atomic_write_text(record_path, json.dumps(record, sort_keys=True), follow_symlinks=False)
    return receipt


def apply(snapshot, target, data) -> None:
    plan, value, proposal = _current(
        snapshot, target, data["planning_unit"], data["proposal_id"], action_id=data["action_id"]
    )
    if (
        data["unit"] != target.id
        or data["branch"] != target.bindings["branch"]
        or data["pr_number"] != target.bindings["pr"]["number"]
        or target.bindings["head"] != data["old_head"]
    ):
        raise CampaignError("amendment refresh receipt no longer owns the retained candidate")
    target.bindings.update(
        head=data["head"],
        effective_source=data["effective_source"],
        canonical_source=data["canonical_source"],
        source_revision=data["source_revision"],
        ready_for_review=False,
    )
    target.bindings.setdefault("private_paths", []).append(data["generation"])
    invalidated = {}
    for key in (
        "admission",
        "integrated",
        "verification",
        "review",
        "pushed",
        "merge",
        "source_artifacts",
        "source_publication",
    ):
        if key in target.evidence:
            invalidated[key] = target.evidence.pop(key)
    target.evidence.setdefault("amendment_history", []).append(
        {"receipt": data, "invalidated": invalidated}
    )
    if (
        target.hold
        and target.hold.kind == "intake_plan_change"
        and target.hold.id == value["attempt_id"]
    ):
        target.hold = None
    if target.state != UnitState.READY:
        transition(snapshot, target.id, UnitState.READY)
    target.legacy_outcome = ""
    value.setdefault("refreshed", []).append(target.id)
    for amendment in target.bindings.get("intake_amendments", []):
        if amendment["attempt_id"] == value["attempt_id"]:
            amendment["state"] = "source_published"
    if set(value["refreshed"]) == set(value["amendments"]):
        value["state"] = "completed"
        proposal["state"] = "activated"
        for version in proposal["input_versions"]:
            snapshot.bindings[INBOX]["records"][version]["state"] = "resolved"
    add_event(
        snapshot,
        "intake-amendment-refreshed",
        unit=target.id,
        planning_unit=plan.id,
        head=data["head"],
        retained_head=data["old_head"],
    )
