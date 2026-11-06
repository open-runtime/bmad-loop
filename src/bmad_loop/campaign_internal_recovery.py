"""Service a trusted child retry handoff without rewinding published history.

The failed actor is already quiescent. Recovery freezes its immutable request,
uses the campaign push fence, preserves index/working residue, restores only
owned code and same-transaction source preimages, and confirms a descendant push
before re-arm. Source CAS failures keep the fence and evidence intact.
"""

from __future__ import annotations

import base64
import dataclasses
import hashlib
import json
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from . import bmadconfig, deferredwork, sprintstatus, verify
from .campaign_execution import CampaignExecution
from .campaign_recovery import CampaignRecovery, Quiescence, RecoveryHold
from .campaign_workers import assert_workspace, common_directory
from .journal import Journal
from .model import Phase, RunState, StoryTask
from .platform_util import (
    atomic_write_bytes_confined,
    atomic_write_text,
    is_link_like,
    root_identity_record,
    unlink_confined,
)
from .recovery_flow import RecoveryFlow
from .tickettree import TicketTree


def _digest(value: str | None) -> str:
    return hashlib.sha256((value or "").encode()).hexdigest()


def _json_file(path: Path) -> dict[str, Any]:
    if is_link_like(path) or not path.is_file() or path.stat().st_size > 16 * 1024 * 1024:
        raise RecoveryHold("internal recovery record must be a bounded regular file")
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or raw.get("schema_version") != 1:
        raise RecoveryHold("unsupported internal recovery record")
    return raw


def _git(root: Path, *args: str) -> bytes:
    result = verify.git_bytes(root, *args)
    if result.returncode:
        raise RecoveryHold(f"internal recovery Git observation failed: {args[0]}")
    return result.stdout


def _encode(data: bytes | None) -> str | None:
    return base64.b64encode(data).decode("ascii") if data is not None else None


def _decode(data: str | None) -> bytes | None:
    return base64.b64decode(data, validate=True) if data is not None else None


def _read(path: Path) -> bytes | None:
    if is_link_like(path):
        raise RecoveryHold("internal recovery refuses redirected source")
    try:
        if path.stat().st_size > 16 * 1024 * 1024 or not path.is_file():
            raise RecoveryHold("internal recovery source must be a bounded regular file")
        return path.read_bytes()
    except FileNotFoundError:
        return None


def _scope(path: str, allowed: tuple[str, ...]) -> bool:
    return any(
        scope in {"*", "."} or path == scope.rstrip("/") or path.startswith(scope.rstrip("/") + "/")
        for scope in allowed
        if not scope.startswith("resource:")
    )


def _binding(
    workspace: Path,
    handoff: dict[str, Any],
    launch: dict[str, Any],
    context: CampaignExecution,
    recovery: CampaignRecovery,
    allowed_paths: tuple[str, ...],
) -> dict[str, Any]:
    task = StoryTask.from_dict(handoff["task"])
    task.rebase_spec_paths_on(workspace)
    baseline = task.baseline_commit
    if (
        not baseline
        or baseline == "NO_VCS"
        or not verify.is_ancestor(workspace, baseline, handoff["head"])
    ):
        raise RecoveryHold("internal retry requires its exact ancestor attempt baseline")
    if task.story_key != launch["unit"]["selector"]:
        raise RecoveryHold("internal retry task differs from selected unit")
    if task.phase == Phase.COMMITTING or handoff.get("cause") in {
        "finalization-failed",
        "finalization-interrupted",
    }:
        raise RecoveryHold(
            "accepted finalization requires preserved-candidate re-verification, not baseline rollback"
        )
    source = launch["source"]
    paths = bmadconfig.load_paths(workspace, want_tickets=source["kind"] == "tickets")
    if not paths.repo_root.resolve().is_relative_to(workspace) or any(
        not path.resolve().is_relative_to(workspace)
        for path in (paths.output_folder, paths.implementation_artifacts, paths.planning_artifacts)
    ):
        raise RecoveryHold("internal retry would write outside its private source view")
    sources: dict[str, dict[str, Any]] = {}

    def add(path: Path, data: bytes | None, writer: str, **extra: Any) -> None:
        if not path.is_absolute():
            path = workspace / path
        if not path.is_relative_to(workspace):
            raise RecoveryHold("internal source preimage is outside its owned checkout")
        relative = path.relative_to(workspace).as_posix()
        recovery._file(relative)  # confined literal path, no links/special files
        sources[relative] = {
            "before": _encode(_read(path)),
            "bytes": _encode(data),
            "writer": writer,
            **extra,
        }

    if task.pre_harvest_ledger_captured:
        ledger = paths.deferred_work
        current = _read(ledger)
        target = (
            task.pre_harvest_ledger.encode("utf-8") if task.pre_harvest_ledger is not None else None
        )
        if current != target:
            current_text = current.decode("utf-8") if current is not None else None
            if _digest(current_text) != task.post_engine_ledger_digest:
                raise RecoveryHold("attempt ledger diverged from its engine-owned CAS anchor")
            add(ledger, target, "ledger", anchor=task.post_engine_ledger_digest)
    if task.dispatched_spec_file and task.dispatched_spec_snapshot is not None:
        raw_snapshot_path = task.dispatched_spec_file
        plan = Path(raw_snapshot_path)
        if not plan.is_absolute():
            raise RecoveryHold("internal retry spec lacks an absolute semantic dispatch anchor")
        if task.ticket is not None:
            if task.ticket.ref != task.story_key or Path(task.ticket.plan) != plan:
                raise RecoveryHold("ticket retry plan does not match its original dispatch binding")
            tree = TicketTree(workspace, folder=paths.initiative_folder)
            found = tree.find(task.ticket.ref)
            if Path(found.plan) != plan:
                raise RecoveryHold("ticket retry source identity moved after dispatch")
            add(plan, task.dispatched_spec_snapshot, "ticket")
        else:
            add(plan, task.dispatched_spec_snapshot, "spec")
    elif task.resolved_redrive and not task.dispatched_spec_absent:
        raise RecoveryHold("resolved retry cannot prove its corrected spec preimage")
    if source["kind"] == "sprint-status":
        board = paths.sprint_status
        relative = board.relative_to(workspace).as_posix()
        baseline_board = verify.worktree_file_bytes_at_revision(workspace, baseline, relative)
        if baseline_board is None:
            raise RecoveryHold("sprint retry lacks its baseline row authority")
        target = sprintstatus.status_in_bytes(baseline_board, task.story_key)
        current = sprintstatus.story_status(board, task.story_key)
        if target is None or current is None:
            raise RecoveryHold("sprint retry row is missing or ambiguous")
        if current != target:
            desired = sprintstatus.advanced_bytes(
                board.read_bytes(),
                task.story_key,
                target,
                allow_regression=True,
                campaign_recovery=True,
            )
            if desired is None:
                raise RecoveryHold("sprint retry could not compute owned row restoration")
            add(board, desired, "sprint", original_status=current, target_status=target)
    protected = (
        paths.output_folder,
        paths.implementation_artifacts,
        paths.planning_artifacts,
        workspace / ".bmad-loop",
        workspace / "_bmad",
        workspace / ".claude",
        workspace / ".agents",
    )
    changed: set[str] = set()
    for arguments in (
        ("diff", "--name-only", "-z", baseline),
        ("diff", "--name-only", "--cached", "-z", baseline),
        ("ls-files", "--others", "--exclude-standard", "-z"),
    ):
        changed.update(
            os.fsdecode(path) for path in _git(workspace, *arguments).split(b"\0") if path
        )
    owned = []
    retained = []
    for relative in sorted(changed):
        path = recovery._file(relative)
        if relative in sources:
            continue
        if any(path.is_relative_to(root) for root in protected) or relative in (
            task.baseline_untracked or []
        ):
            retained.append(relative)
            continue
        if not _scope(relative, allowed_paths):
            raise RecoveryHold(
                f"internal retry changed content outside declared ownership: {relative}"
            )
        owned.append(relative)
    return {
        "schema_version": 1,
        "campaign_id": context.campaign_id,
        "unit_id": context.unit_id,
        "attempt": context.attempt,
        "generation": context.generation,
        "config_revision": context.config_revision,
        "parent": handoff["head"],
        "baseline": baseline,
        "owned_paths": owned,
        "retained_paths": retained,
        "source_preimages": sources,
        "baseline_untracked": task.baseline_untracked,
        "story_key": task.story_key,
        "initiative_folder": str(paths.initiative_folder) if paths.initiative_folder else "",
    }


def recover(
    project: Path,
    workspace: Path,
    state_dir: Path,
    request_path: Path,
    action_id: str,
    remote: str,
    branch: str,
    quiescence: Quiescence,
    allowed_paths: tuple[str, ...],
) -> dict[str, Any]:
    """Execute/resume one trusted Engine rollback handoff through append-only Git."""
    project, workspace, state_dir = project.resolve(), workspace.resolve(), state_dir.resolve()
    if request_path.name != "recovery-request.json" or state_dir.is_relative_to(workspace):
        raise RecoveryHold(
            "internal recovery requires external authority and its exact handoff file"
        )
    handoff, launch = _json_file(request_path), _json_file(request_path.parent / "request.json")
    context = CampaignExecution.load(Path(launch["execution"]), launch["digest"])
    if request_path.parent.resolve() != context.directory.resolve():
        raise RecoveryHold("internal recovery execution directory changed")
    for key in ("campaign_id", "unit_id", "attempt", "generation", "config_revision"):
        if handoff.get(key) != getattr(context, key):
            raise RecoveryHold(f"internal recovery {key} differs from frozen execution")
    if any(
        launch.get(key) != handoff[key]
        for key in ("campaign_id", "unit_id", "attempt", "generation")
    ):
        raise RecoveryHold("internal recovery launch binding changed")
    if (
        Path(launch["workspace"]).resolve() != workspace
        or launch["branch"] != branch
        or common_directory(project) != common_directory(workspace)
    ):
        raise RecoveryHold("internal recovery project/workspace identity differs")
    assert_workspace(workspace, common_directory(project), branch)
    quiescence.require(verify.rev_parse_head(workspace))
    if quiescence.generation != context.generation:
        raise RecoveryHold("internal recovery quiescence belongs to another generation")
    engine = CampaignRecovery(workspace, state_dir, remote, branch)
    binding_path = (
        state_dir
        / "internal-recovery"
        / hashlib.sha256(action_id.encode()).hexdigest()
        / "binding.json"
    )
    request_digest = hashlib.sha256(request_path.read_bytes()).hexdigest()
    if binding_path.exists():
        binding = _json_file(binding_path)
        if (
            binding["request_digest"] != request_digest
            or binding["allowed_paths"] != list(allowed_paths)
            or binding["action_id"] != action_id
        ):
            raise RecoveryHold("internal recovery action was reused with changed authority")
    else:
        if quiescence.head != handoff["head"]:
            raise RecoveryHold("internal retry candidate moved before its recovery intent")
        binding = _binding(workspace, handoff, launch, context, engine, allowed_paths)
        binding.update(
            action_id=action_id, request_digest=request_digest, allowed_paths=list(allowed_paths)
        )
        binding_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(binding_path, json.dumps(binding, sort_keys=True), follow_symlinks=False)
    journal_dir = binding_path.parent / "journal"
    journal_dir.mkdir(parents=True, exist_ok=True)
    journal_pin = RunState(
        action_id, str(workspace), "", run_dir_identity=root_identity_record(journal_dir)
    )
    journal = Journal(journal_dir, pin=journal_pin)
    preimages = binding["source_preimages"]
    expected = {relative: _decode(row["bytes"]) for relative, row in preimages.items()}
    pin = os.stat(workspace)

    def restore_sources(values: dict[str, bytes | None], transaction: str) -> None:
        if values != expected or transaction != action_id:
            raise RecoveryHold("source restore is not from this captured recovery transaction")
        for relative, desired in values.items():
            row = preimages[relative]
            path = engine._file(relative)
            before = _decode(row["before"])
            if _read(path) == desired:
                continue
            if row["writer"] == "ledger":
                with deferredwork.ledger_lock(path):
                    current = _read(path)
                    if (
                        current != before
                        or _digest(current.decode("utf-8") if current is not None else None)
                        != row["anchor"]
                    ):
                        raise RecoveryHold("ledger CAS changed before internal source restore")
                    if desired is None:
                        unlink_confined(
                            path, confine_root=workspace, root_identity=pin, missing_ok=True
                        )
                    else:
                        atomic_write_bytes_confined(
                            path, desired, confine_root=workspace, root_identity=pin
                        )
            elif row["writer"] == "ticket":

                class CapturedTransactionTree(TicketTree):
                    @contextmanager
                    def _plan_lock(self, plan: Path) -> Iterator[None]:
                        with super()._plan_lock(plan):
                            if _read(plan) not in (before, desired):
                                raise RecoveryHold(
                                    "ticket source changed before internal preimage restore"
                                )
                            yield

                tree = CapturedTransactionTree(
                    workspace,
                    folder=(
                        Path(binding["initiative_folder"]) if binding["initiative_folder"] else None
                    ),
                )
                tree.restore_preimage(path, desired, journal)
            elif row["writer"] == "sprint":
                result = sprintstatus.advance(
                    path,
                    binding["story_key"],
                    row["target_status"],
                    allow_regression=True,
                    campaign_recovery=True,
                    expected_current=(row["original_status"], row["target_status"]),
                )
                if result != row["target_status"]:
                    raise RecoveryHold("owned sprint row did not restore")
            elif row["writer"] == "spec":
                if desired is None or _read(path) != before:
                    raise RecoveryHold("attempt-owned spec changed before its captured restore")
                RecoveryFlow._restore_attempt_owned_spec_bytes(
                    path, desired, confine_root=workspace, root_identity=pin
                )
            else:
                raise RecoveryHold("unknown internal source sole writer")

    if not engine._path(action_id).exists():
        engine.begin(
            action_id,
            expected_head=binding["parent"],
            baseline=binding["baseline"],
            owned_paths=tuple(binding["owned_paths"]),
            baseline_untracked=(
                tuple(binding["baseline_untracked"])
                if binding["baseline_untracked"] is not None
                else None
            ),
            preserve_paths=tuple(binding["retained_paths"]),
            source_preimages=expected,
            pending_pushes=engine.pusher.observations(),
            quiescence=quiescence,
        )
    existing = _json_file(engine._path(action_id))
    if existing["state"] == "complete" and engine.pusher.fenced() is None:
        if quiescence.head != existing["head"] or engine.pusher.remote_head() != existing["head"]:
            raise RecoveryHold("completed internal recovery moved before acknowledgement")
        settled = engine.finish(action_id, quiescence=quiescence)
    else:
        corrected = engine.recover(
            action_id, quiescence=quiescence, restore_sources=restore_sources
        )
        final_quiescence = dataclasses.replace(quiescence, head=corrected.head)
        settled = engine.finish(
            action_id,
            quiescence=final_quiescence,
            verify_sources=lambda values: all(
                _read(engine._file(relative)) == data for relative, data in values.items()
            ),
        )
    return {
        **dataclasses.asdict(settled),
        "campaign_id": context.campaign_id,
        "unit_id": context.unit_id,
        "attempt": context.attempt,
        "generation": context.generation,
        "config_revision": context.config_revision,
        "baseline": binding["baseline"],
        "retained_paths": binding["retained_paths"],
        "source_restored": True,
    }
