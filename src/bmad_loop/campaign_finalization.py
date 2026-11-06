"""Finish a retained, accepted COMMITTING task without reverting its product.

The original run stays intact. A frozen child seeds an owned one-unit continuation
with every attempt/session/token counter preserved, independently verifies the
retained candidate, then enters only the existing append-only commit path. The
ordinary campaign resume guard remains closed for all other phases/identities.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
from pathlib import Path
from typing import Any, Callable

from . import bmadconfig, runs, runsetup, verify
from .campaign_execution import CampaignExecution, bind_run, worker_policy
from .campaign_fingerprint import CandidateFingerprint, capture
from .campaign_recovery import CampaignRecovery, Quiescence, RecoveryHold
from .journal import Journal, load_state, save_state, state_lock
from .model import Phase, RunState, StoryTask
from .platform_util import (
    atomic_write_text,
    file_lock,
    is_link_like,
    root_identity_record,
    safe_segment,
)


def _read(path: Path) -> dict[str, Any]:
    if is_link_like(path) or not path.is_file() or path.stat().st_size > 16 * 1024 * 1024:
        raise RecoveryHold("retained finalization requires bounded original records")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RecoveryHold("retained finalization record must be an object")
    return value


def _write(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, json.dumps(value, sort_keys=True), follow_symlinks=False)


def _source_roots(workspace: Path, paths, state: RunState) -> tuple[Path, ...]:
    selected = [
        paths.output_folder,
        workspace / ".bmad-loop/policy.toml",
        workspace / ".bmad-loop/profiles",
    ]
    if state.source == "stories":
        from .stories import resolve_spec_folder

        folder = resolve_spec_folder(workspace, state.spec_folder)
        if not folder.resolve().is_relative_to(workspace):
            raise RecoveryHold("retained stories source is outside the private checkout")
        selected.extend(
            [folder / "SPEC.md", folder / "stories.yaml", folder / "stories"]
            if folder.resolve() == workspace
            else [folder]
        )
    return tuple(
        path for path in selected if path.exists() and path.resolve().is_relative_to(workspace)
    )


def continuation_run_id(request: dict[str, Any]) -> str | None:
    """Locate only the trusted retained run for mux/stop ownership read-back."""
    if request.get("kind") != "recovery":
        return None
    handoff = _read(Path(request["recovery_request"]))
    if handoff.get("task", {}).get("phase") != "committing":
        return None
    if handoff.get("campaign_id") != request.get("campaign_id") or handoff.get(
        "unit_id"
    ) != request.get("unit_id"):
        raise RecoveryHold("retained actor lookup belongs to another campaign unit")
    action = request["recovery_action"]
    path = (
        Path(request["publication_state"])
        / "recovery"
        / hashlib.sha256(action.encode()).hexdigest()
        / "action.json"
    )
    if not path.exists():
        return safe_segment(request["run_id"])
    record = _read(path)
    run_id = record.get("retained_run_id")
    if (
        record.get("strategy") != "retained-finalization"
        or record.get("action_id") != action
        or record.get("parent") != handoff["head"]
        or not isinstance(run_id, str)
        or not run_id
        or safe_segment(run_id) != run_id
    ):
        raise RecoveryHold("retained actor run binding is uncertain")
    directory = Path(request["workspace"]) / runs.RUNS_DIR / run_id
    if directory.exists():
        if directory.is_symlink():
            raise RecoveryHold("retained actor run directory is redirected")
        intent = _read(path.with_suffix(".retained.json"))
        if (
            intent.get("run_dir") != str(directory.resolve())
            or intent.get("request_digest")
            != hashlib.sha256(Path(request["recovery_request"]).read_bytes()).hexdigest()
        ):
            raise RecoveryHold("retained actor allocation identity changed")
        state = load_state(directory)
        binding = state.campaign_binding
        if (
            binding.get("campaign_id") != request["campaign_id"]
            or binding.get("unit_id") != request["unit_id"]
            or state.run_id != run_id
        ):
            raise RecoveryHold("retained actor state belongs to another unit")
    return run_id


@dataclasses.dataclass(frozen=True)
class RetainedPermit:
    """In-process capability minted only after a bound, quiescent handoff."""

    campaign_id: str
    unit_id: str
    run_id: str
    story_key: str
    head: str
    fingerprint: CandidateFingerprint
    source_roots: tuple[Path, ...]
    attempt: int
    review_cycle: int
    sessions: int
    task_tokens: dict[str, Any]
    verification_passed: bool = False


def validate_resume(engine, task: StoryTask) -> bool:
    """The Engine's narrow exception; a boolean or a task phase is insufficient."""
    permit = getattr(engine, "_campaign_retained_permit", None)
    context = engine.campaign_execution
    if not isinstance(permit, RetainedPermit) or not permit.verification_passed or context is None:
        return False
    if (
        context.campaign_id != permit.campaign_id
        or context.unit_id != permit.unit_id
        or engine.state.run_id != permit.run_id
        or len(engine.state.tasks) != 1
        or task.story_key != permit.story_key
        or task.phase != Phase.COMMITTING
        or task.attempt != permit.attempt
        or task.review_cycle != permit.review_cycle
        or len(task.sessions) != permit.sessions
        or task.tokens.to_dict() != permit.task_tokens
    ):
        return False
    workspace = engine.workspace.root.resolve()
    observed = capture(workspace, source_roots=permit.source_roots, exclude_paths=(engine.run_dir,))
    return observed == permit.fingerprint and observed.head == permit.head


def resume(
    request: dict[str, Any],
    envelope: CampaignExecution,
    *,
    make_adapters: Callable,
    engine_cls=None,
    stories_engine_cls=None,
    tickets_engine_cls=None,
) -> dict[str, Any]:
    """Run in the owned recovery child; never in a coordinator I/O thread.

    A crash after the retained commit can re-enter this same run. A failure in
    fresh verification preserves every byte and leaves the recovery fence for a
    typed repair/reverify path. No successful receipt bypasses campaign final
    synchronization, independent review, required CI or server merge gates.
    """
    from .engine import Engine
    from .stories_engine import StoriesEngine

    workspace = Path(request["workspace"]).resolve()
    original = Path(request["recovery_request"])
    handoff = _read(original)
    old_request = _read(original.parent / "request.json")
    original_envelope = CampaignExecution.load(
        Path(old_request["execution"]), old_request["digest"]
    )
    if (
        handoff.get("cause") not in {"finalization-failed", "finalization-interrupted", "resume"}
        or handoff.get("task", {}).get("phase") != "committing"
    ):
        raise RecoveryHold("retained finalization requires an accepted commit-window handoff")
    for key in ("campaign_id", "unit_id", "attempt", "generation", "config_revision"):
        if handoff.get(key) != getattr(original_envelope, key):
            raise RecoveryHold("retained finalization handoff identity changed")
    if (
        envelope.campaign_id != original_envelope.campaign_id
        or envelope.unit_id != original_envelope.unit_id
        or envelope.config_revision != original_envelope.config_revision
        or envelope.generation < original_envelope.generation
        or Path(old_request["workspace"]).resolve() != workspace
        or old_request["branch"] != request["branch"]
    ):
        raise RecoveryHold("retained finalization worker/configuration binding changed")
    from .campaign_workers import assert_workspace

    assert_workspace(workspace, Path(old_request["common_dir"]), request["branch"])
    head = verify.rev_parse_head(workspace)
    quiet = Quiescence(
        request.get("quiescence_head", handoff["head"]),
        handoff["generation"],
        True,
        True,
        request["quiescence_receipt"],
    )
    quiet.require(head)
    original_dir = workspace / runs.RUNS_DIR / safe_segment(old_request["run_id"])
    old_state = load_state(original_dir)
    if (
        old_state.run_id != old_request["run_id"]
        or Path(old_state.project).resolve() != workspace
        or old_state.source != old_request["source"]["kind"]
        or len(old_state.tasks) != 1
        or old_request["unit"]["selector"] not in old_state.tasks
    ):
        raise RecoveryHold("retained finalization original run selector/source changed")
    try:
        task = StoryTask.from_dict(handoff["task"])
        expected = old_state.tasks[task.story_key]
    except (ValueError, KeyError, TypeError) as exc:
        raise RecoveryHold("retained finalization task identity/phase is invalid") from exc
    if task.phase != Phase.COMMITTING or expected.to_dict() != task.to_dict() or task.worktree_path:
        raise RecoveryHold(
            "retained finalization requires the exact in-place accepted COMMITTING task"
        )
    if not task.baseline_commit or not verify.is_ancestor(workspace, task.baseline_commit, head):
        raise RecoveryHold("retained accepted history no longer descends from its attempt baseline")
    pol = worker_policy(original_envelope.policy)
    paths = bmadconfig.load_paths(workspace, want_tickets=old_state.source == "tickets")
    if old_state.source == "tickets" and not paths.output_folder.resolve().is_relative_to(
        workspace
    ):
        raise RecoveryHold("retained finalization source is not private to its worker")
    recovery = CampaignRecovery(
        workspace, Path(request["publication_state"]), request["remote"], request["branch"]
    )
    action = request["recovery_action"]
    if recovery._path(action).exists():
        row = _read(recovery._path(action))
        if (
            row.get("strategy") != "retained-finalization"
            or row.get("action_id") != action
            or row.get("parent") != handoff["head"]
            or not verify.is_ancestor(workspace, row["parent"], head)
            or (row.get("state") == "complete" and row.get("head") != head)
        ):
            raise RecoveryHold("retained recovery identity/history changed")
        with file_lock(recovery.pusher.scope / "push.lock"):
            if recovery.pusher.fenced() not in {None, action}:
                raise RecoveryHold("another recovery owns the retained continuation")
            for pending in recovery.pusher.observations():
                if pending.state != "superseded" and not verify.is_ancestor(
                    workspace, pending.head, head
                ):
                    raise RecoveryHold("pending checkpoint is outside retained history")
            remote = recovery.pusher.remote_head()
            if remote and not recovery.pusher._ancestor(remote, head):
                raise RecoveryHold("remote moved outside retained history")
            _write(
                recovery.pusher.scope / "fence.json",
                {"schema_version": 1, "action_id": action, "active": True},
            )
    else:
        if head != handoff["head"]:
            raise RecoveryHold("unbound retained candidate moved before preservation")
        row = recovery.begin(
            action,
            expected_head=head,
            baseline=head,
            owned_paths=(),
            baseline_untracked=tuple(task.baseline_untracked or ()),
            quiescence=quiet,
        )
        recovery._preserve(row)
        row["strategy"] = "retained-finalization"
        row["retained_run_id"] = safe_segment(request["run_id"])
        _write(recovery._path(action), row)
    run_id = safe_segment(row["retained_run_id"])
    run_dir = workspace / runs.RUNS_DIR / run_id
    if not run_dir.exists() and head != row["parent"]:
        raise RecoveryHold("retained branch moved before its continuation was allocated")
    intent = recovery._path(action).with_suffix(".retained.json")
    request_digest = hashlib.sha256(original.read_bytes()).hexdigest()
    if run_dir.exists():
        recorded = _read(intent)
        if recorded.get("request_digest") != request_digest or recorded.get("run_dir") != str(
            run_dir
        ):
            raise RecoveryHold("retained continuation allocation is uncertain")
        state = load_state(run_dir)
        if state.replay_of != old_state.run_id or state.story_filter != task.story_key:
            raise RecoveryHold("retained continuation binding changed")
        if len(state.tasks) != 1 or state.tasks[task.story_key].phase not in {
            Phase.COMMITTING,
            Phase.DONE,
        }:
            raise RecoveryHold(
                "retained continuation no longer owns its accepted finalization phase"
            )
        state.clear_pause()
        state.campaign_binding = bind_run(workspace, run_dir, envelope)
        with state_lock(run_dir):
            save_state(run_dir, state)
            runs.write_pid(run_dir)
    else:
        state = RunState.from_dict(old_state.to_dict())
        state.run_id = run_id
        state.replay_of = old_state.run_id
        state.clear_pause()
        state.finished = False
        state.max_stories = 1
        state.story_filter = task.story_key
        state.tasks = {task.story_key: task}
        task.generation += 1
        task.rebase_spec_paths_on(workspace)
        state.policy_snapshot = pol.to_dict()
        _write(
            intent,
            {
                "schema_version": 1,
                "request_digest": request_digest,
                "run_dir": str(run_dir),
                "original_run": str(original_dir),
                "head": head,
                "strategy": "retained-finalization",
            },
        )
        run_dir.mkdir(parents=True, exist_ok=False)
        state.run_dir_identity = root_identity_record(run_dir)
        state.campaign_binding = bind_run(workspace, run_dir, envelope)
        with state_lock(run_dir):
            save_state(run_dir, state)
            runs.write_trusted_config_digest(
                workspace,
                run_id,
                runsetup.config_digest(pol, workspace, profiles=original_envelope.profiles),
            )
            runs.write_pid(run_dir)
    journal = Journal(run_dir, pin=state)
    adapters = make_adapters(workspace, run_dir, pol, profiles=original_envelope.profiles)
    cls, extra = runsetup._engine_cls_for(
        state.source,
        state.spec_folder,
        engine_cls=engine_cls or Engine,
        stories_engine_cls=stories_engine_cls or StoriesEngine,
        tickets_engine_cls=tickets_engine_cls,
    )
    constructor: Any = cls
    engine = constructor(
        paths=paths,
        policy=pol,
        adapter=adapters["dev"],
        review_adapter=adapters["review"],
        retro_adapter=adapters["retro"],
        run_dir=run_dir,
        journal=journal,
        state=state,
        max_stories=1,
        epic_filter=state.epic_filter,
        story_filter=state.story_filter,
        sweep_factory=lambda *a, **k: None,
        plugin_grants=original_envelope.grants,
        campaign_execution=envelope,
        **extra,
    )
    retained_task = state.tasks[task.story_key]
    roots = _source_roots(workspace, paths, state)
    if retained_task.phase == Phase.COMMITTING:
        before_verification = capture(workspace, source_roots=roots, exclude_paths=(run_dir,))
        outcome, commands = engine._verify_commands_with_results(retained_task, "review")
        journal.append(
            "campaign-retained-reverified",
            story_key=task.story_key,
            ok=outcome.ok,
            command_count=len(commands.results),
        )
        if not outcome.ok:
            published = recovery.pusher.push(
                "retained-verification:" + action + ":" + head, head, recovery_action=action
            )
            if published.state != "confirmed" or published.remote_head != head:
                raise RecoveryHold(
                    "retained verification failure cannot settle its published accepted head"
                )
            for pending in recovery.pusher.observations():
                if pending.state in {"sent_unknown", "in_flight"}:
                    recovery.pusher.reconcile(pending.action_id)
            row.update(state="awaiting_reverify", verification_failure=outcome.reason)
            _write(recovery._path(action), row)
            with file_lock(recovery.pusher.scope / "push.lock"):
                _write(
                    recovery.pusher.scope / "fence.json",
                    {"schema_version": 1, "action_id": action, "active": False},
                )
            return {
                "outcome": "retained_verification_failed",
                "run_dir": str(run_dir),
                "run_id": run_id,
                "reason": outcome.reason,
                "head": verify.rev_parse_head(workspace),
                "retained_finalization": True,
                "preserved_head": head,
                "retained_finalization_pending": {
                    "recovery_request": str(original),
                    "recovery_action": action,
                    "run_id": run_id,
                    "original_launch": old_request["launch_id"],
                },
            }
        fingerprint = capture(workspace, source_roots=roots, exclude_paths=(run_dir,))
        if fingerprint != before_verification:
            raise RecoveryHold(
                "retained verification changed the accepted candidate; repair/review is required"
            )
        permit = RetainedPermit(
            envelope.campaign_id,
            envelope.unit_id,
            run_id,
            retained_task.story_key,
            fingerprint.head,
            fingerprint,
            roots,
            retained_task.attempt,
            retained_task.review_cycle,
            len(retained_task.sessions),
            retained_task.tokens.to_dict(),
            True,
        )
        engine._campaign_retained_permit = permit
    pending_stop = runs.read_stop_request_mode(envelope.directory)
    if pending_stop:
        atomic_write_text(
            run_dir / runs.STOP_REQUEST_FILE,
            json.dumps({"mode": pending_stop}),
            follow_symlinks=False,
        )
    summary = engine.run()
    state = load_state(run_dir)
    done = (
        state.finished
        and not state.paused
        and all(item.phase == Phase.DONE for item in state.tasks.values())
    )
    artifacts = []
    if done:
        for accepted in state.tasks.values():
            if not accepted.spec_file:
                continue
            path = runs.task_spec_path(accepted, state)
            if (
                path.is_symlink()
                or not path.resolve().is_relative_to(workspace)
                or not path.is_file()
                or path.stat().st_size > 8 * 1024 * 1024
            ):
                raise RecoveryHold(
                    "retained accepted source is redirected, missing or outside worker"
                )
            data = path.read_bytes()
            relative = path.relative_to(workspace).as_posix()
            mirror = path
            if verify.git_bytes(
                workspace, "ls-files", "--error-unmatch", "--", relative
            ).returncode:
                mirror = (
                    workspace
                    / "docs/bmad-campaign"
                    / safe_segment(envelope.unit_id)
                    / "accepted-source.md"
                )
                mirror.parent.mkdir(parents=True, exist_ok=True)
                mirror.write_bytes(data)
                verify.commit_paths(workspace, "docs: publish retained accepted source", [mirror])
            artifacts.append(
                {
                    "path": relative,
                    "review_path": mirror.relative_to(workspace).as_posix(),
                    "sha256": hashlib.sha256(data).hexdigest(),
                    "kind": state.source,
                }
            )
        final_head = verify.rev_parse_head(workspace)
        if not verify.is_ancestor(workspace, head, final_head):
            raise RecoveryHold("retained finalization rewrote its accepted ancestor")
        row.update(
            state="committed", head=final_head, tree=verify.revision_tree_oid(workspace, final_head)
        )
        _write(recovery._path(action), row)
        recovery_receipt = recovery.finish(
            action, quiescence=dataclasses.replace(quiet, head=final_head)
        )
    else:
        recovery_receipt = None
    return {
        "outcome": (
            "locally_complete"
            if done
            else (
                "recovery_required"
                if (envelope.directory / "recovery-request.json").exists()
                else "paused"
            )
        ),
        "run_dir": str(run_dir),
        "run_id": run_id,
        "summary": dataclasses.asdict(summary),
        "sessions": [record.to_dict() for item in state.tasks.values() for record in item.sessions],
        "source_artifacts": artifacts,
        "retained_finalization": True,
        "recovery": dataclasses.asdict(recovery_receipt) if recovery_receipt else None,
        "dev_attempts_spent": task.attempt,
        "review_cycles_spent": task.review_cycle,
    }
