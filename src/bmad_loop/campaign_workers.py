"""Owned campaign child processes and independently verifiable receipts.

Each unit/stage is a separate interpreter.  A launch is prepared durably before
Popen; a missing acknowledgement is uncertain, never permission to launch twice.
The child is the only owner of its engine, journal, index and coding sessions.
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import os
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from . import bmadconfig, childrun, runs, runsetup, verify
from .adapters.base import SessionSpec
from .campaign_execution import CampaignExecution, CampaignExecutionError, worker_policy
from .campaign_model import CampaignError, SourceSelector, UnitSpec
from .engine import Engine
from .journal import TASK_CYCLE_ARTIFACTS, load_state
from .platform_util import (
    atomic_write_text,
    detach_kwargs,
    file_lock,
    is_link_like,
    safe_segment,
    session_task_id,
)
from .stories_engine import StoriesEngine

_LAUNCH_KEYS = ("campaign_id", "unit_id", "attempt", "generation", "launch_id")


@dataclasses.dataclass
class _OwnedLaunch:
    binding: tuple[Any, ...]
    owner_pid: int
    process: subprocess.Popen[Any] | None = None
    spawning: bool = True


# Survive coordinator-object replacement within this interpreter. Never infer
# parenthood from a persisted PID: only the Popen returned by our own spawn may
# be polled. Unacknowledged exits retain a small tombstone, not a process handle.
_OWNED_LAUNCHES: dict[Path, _OwnedLaunch] = {}
_OWNED_LAUNCHES_LOCK = threading.Lock()


def _after_fork() -> None:
    global _OWNED_LAUNCHES_LOCK
    _OWNED_LAUNCHES_LOCK = threading.Lock()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_after_fork)


def _reap_owned(directory: Path, expected: dict[str, Any], *, has_binding: bool) -> str | None:
    with _OWNED_LAUNCHES_LOCK:
        key = directory.resolve()
        owned = _OWNED_LAUNCHES.get(key)
        if owned is None or owned.owner_pid != os.getpid():
            return None
        if owned.binding != tuple(expected.get(k) for k in _LAUNCH_KEYS):
            raise CampaignError("owned launch binding does not match its durable claim")
        if owned.spawning:
            return "unknown"
        if owned.process is not None:
            # poll is nonblocking and reaps only this exact direct child. An
            # unreaped POSIX zombie otherwise still looks alive to PID probes.
            if owned.process.poll() is None:
                return "running"
            owned.process = None
        if has_binding:
            del _OWNED_LAUNCHES[key]
        return None


def git(repo: Path, *args: str) -> str:
    result = verify.git_bytes(repo, *args)
    if result.returncode:
        raise CampaignError(
            f"git {args[0]} failed: {result.stderr.decode('utf-8', errors='replace')[-2000:]}"
        )
    return result.stdout.decode("utf-8", errors="strict").strip()


def common_directory(repo: Path) -> Path:
    raw = git(repo, "rev-parse", "--git-common-dir")
    return (repo / raw).resolve()


def assert_workspace(workspace: Path, common: Path, branch: str) -> None:
    if common_directory(workspace) != common.resolve():
        raise CampaignError("workspace belongs to another Git common directory")
    if git(workspace, "symbolic-ref", "--short", "HEAD") != branch:
        raise CampaignError("worker branch binding changed")


@contextmanager
def worktree_metadata_lock(repo: Path) -> Iterator[Path]:
    """Protect short shared Git-admin mutations; never sessions or network I/O."""
    common = common_directory(repo)
    with file_lock(common / "bmad-campaign-worktree-allocation.lock"):
        yield common


def allocate_worktree(
    repo: Path,
    root: Path,
    *,
    branch: str,
    unit_id: str,
    base: str,
    allocation_dir: Path | None = None,
) -> Path:
    """Serialize short Git metadata writes and reconcile only this exact intent.

    Git can create a branch before worktree registration fails. The saved
    absence/base/path proof permits using that same untouched branch on retry;
    it never permits deleting, resetting or adopting an existing foreign branch.
    Model sessions and normal worker execution never hold this metadata lock.
    """
    root = root.expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    name = safe_segment(unit_id)
    workspace = root / name
    with worktree_metadata_lock(repo) as common:
        git(repo, "check-ref-format", "--branch", branch)
        resolved = verify.rev_parse_revision(repo, base)
        expected = {
            "schema_version": 1,
            "common_dir": str(common),
            "branch": branch,
            "workspace": str(workspace),
            "base": resolved,
            "branch_absent": True,
        }
        path = (
            allocation_dir / "worktree-allocation-intent.json"
            if allocation_dir is not None
            else None
        )
        record = None
        if path is not None and path.exists():
            if is_link_like(path) or not path.is_file() or path.stat().st_size > 16384:
                raise CampaignError("worktree allocation intent is redirected or unavailable")
            record = json.loads(path.read_text(encoding="utf-8"))
            if record != expected:
                raise CampaignError("worktree allocation intent coordinates changed")
        reference = "refs/heads/" + branch
        check = verify.git_bytes(repo, "show-ref", "--verify", "--quiet", reference)
        if check.returncode not in (0, 1):
            raise CampaignError("worktree branch preimage is unavailable")
        exists = check.returncode == 0
        if record is None:
            if workspace.exists() or exists:
                raise CampaignError(
                    "unrecorded existing branch/worktree cannot be adopted by allocation"
                )
            if path is not None:
                path.parent.mkdir(parents=True, exist_ok=True)
                atomic_write_text(path, json.dumps(expected, sort_keys=True), follow_symlinks=False)
        registered = git(repo, "worktree", "list", "--porcelain", "-z")
        bound = []
        for block in registered.split("\0\0"):
            fields: dict[str, str] = {}
            for value in block.split("\0"):
                if value:
                    field, _, content = value.partition(" ")
                    fields[field] = content
            if fields.get("branch") == reference:
                bound.append(Path(fields["worktree"]).resolve())
        if workspace.exists():
            if record is None or bound != [workspace] or is_link_like(workspace):
                raise CampaignError("existing allocation workspace has no exact owned registration")
            assert_workspace(workspace, common, branch)
            if not verify.is_ancestor(repo, resolved, verify.rev_parse_head(workspace)):
                raise CampaignError("allocation workspace no longer preserves its original base")
            return workspace
        if bound:
            raise CampaignError("allocation branch is already registered to another worktree")
        if exists:
            if record is None or verify.rev_parse_revision(repo, reference) != resolved:
                raise CampaignError(
                    "allocation orphan branch changed or lacks its original absence proof"
                )
            git(repo, "worktree", "add", str(workspace), branch)
        else:
            git(repo, "worktree", "add", "-b", branch, str(workspace), resolved)
        assert_workspace(workspace, common, branch)
        if verify.rev_parse_head(workspace) != resolved:
            raise CampaignError("new allocation branch moved before baseline confirmation")
        return workspace


def prepare_launch(execution: CampaignExecution, request: dict[str, Any]) -> Path:
    path, digest = execution.save()
    request = {
        **request,
        "schema_version": 1,
        "execution": str(path),
        "digest": digest,
        "campaign_id": execution.campaign_id,
        "unit_id": execution.unit_id,
        "attempt": execution.attempt,
        "generation": execution.generation,
    }
    target = execution.directory / "request.json"
    if target.exists():
        if json.loads(target.read_text(encoding="utf-8")) != request:
            raise CampaignExecutionError("cannot replace a prepared launch")
    else:
        atomic_write_text(target, json.dumps(request, sort_keys=True))
    return target


def spawn(request_path: Path, *, env: dict[str, str] | None = None) -> subprocess.Popen[Any]:
    directory = request_path.parent.resolve()
    # An existing startup/receipt, including a dead child, is reconciled by the
    # coordinator.  This API never guesses whether retrying the spawn is safe.
    with _OWNED_LAUNCHES_LOCK:
        if directory in _OWNED_LAUNCHES:
            raise CampaignError("launch already has an owned process or uncertain spawn")
        if (directory / "startup.json").exists() or (directory / "receipt.json").exists():
            raise CampaignError("launch already has a startup binding or receipt")
        request = json.loads(request_path.read_text(encoding="utf-8"))
        owned = _OwnedLaunch(tuple(request[k] for k in _LAUNCH_KEYS), os.getpid())
        _OWNED_LAUNCHES[directory] = owned
    # Reserve before launching, without serializing slow OS executable checks
    # across independent units. Observers see an uncertain in-progress launch.
    process_options: dict[str, Any] = detach_kwargs()
    try:
        with (directory / "child.log").open("ab") as log:
            process = subprocess.Popen(
                [sys.executable, "-m", "bmad_loop.campaign_workers", str(request_path)],
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                env=env,
                **process_options,
            )
            with _OWNED_LAUNCHES_LOCK:
                owned.process = process
                owned.spawning = False
    except BaseException:
        with _OWNED_LAUNCHES_LOCK:
            # An exception during process creation is not an acknowledgement
            # that no child exists. Retain the reservation for reconciliation.
            owned.spawning = False
        raise
    return process


def observe(directory: Path, *, expected: dict[str, Any]) -> dict[str, Any]:
    receipt_path, startup_path = directory / "receipt.json", directory / "startup.json"
    startup = (
        json.loads(startup_path.read_text(encoding="utf-8")) if startup_path.exists() else None
    )
    if startup is not None:
        for key in _LAUNCH_KEYS:
            if startup.get(key) != expected.get(key):
                raise CampaignError(f"startup {key} does not match its durable claim")
    owned_state = _reap_owned(
        directory, expected, has_binding=startup is not None or receipt_path.exists()
    )
    if startup is None:
        return {"state": "unknown", "reason": "startup binding not observed"}
    if owned_state is not None:
        # A known-live handle outranks contradictory/stale persisted PID
        # evidence. A constructor still in progress is uncertain, never done.
        return {"state": owned_state, "startup": startup}
    pid, identity = runs.read_named_pid_identity(directory / "engine.pid")
    if pid is None or identity is None:
        return {"state": "unknown", "reason": "worker process identity is unavailable"}
    live = runs.probe_liveness(pid, identity)
    if live != "dead":
        return {"state": "running" if live == "alive" else "unknown", "startup": startup}
    if not receipt_path.exists():
        return {
            "state": "interrupted",
            "startup": startup,
            "reason": "worker exited without receipt",
        }
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    if any(
        receipt.get(k) != startup.get(k)
        for k in ("campaign_id", "unit_id", "attempt", "generation", "launch_id")
    ):
        raise CampaignError("worker receipt binding mismatch")
    if receipt.get("schema_version") != 1:
        raise CampaignError("unsupported worker receipt")
    workspace = Path(startup["workspace"])
    request_path = directory / "request.json"
    request = json.loads(request_path.read_text(encoding="utf-8")) if request_path.exists() else {}
    if request.get("kind") == "queue_verify" or request.get("landing_group"):
        from .campaign_queue import QueueGroup, assert_checkout

        if (
            Path(request["workspace"]).resolve() != workspace.resolve()
            or request["common_dir"] != startup["common_dir"]
        ):
            raise CampaignError("queue verification startup workspace binding changed")
        assert_checkout(
            workspace,
            Path(startup["common_dir"]),
            QueueGroup(**(request.get("landing_group") or request["queue_group"])),
        )
    else:
        assert_workspace(workspace, Path(startup["common_dir"]), startup["branch"])
    if verify.rev_parse_head(workspace) != receipt.get("head"):
        raise CampaignError("workspace head changed after worker receipt")
    if receipt.get("outcome") == "locally_complete" and (
        not receipt.get("run_dir") or not receipt.get("run_id")
    ):
        raise CampaignError("local completion requires the persisted legacy run identity")
    if receipt.get("run_dir"):
        state = load_state(Path(receipt["run_dir"]))
        if state.run_id != receipt.get("run_id"):
            raise CampaignError("legacy run identity does not match worker receipt")
        if receipt.get("outcome") == "locally_complete" and (
            not state.finished
            or state.paused
            or not state.tasks
            or any(task.phase.value != "done" for task in state.tasks.values())
        ):
            raise CampaignError("legacy run does not prove local completion")
        if receipt.get("outcome") == "locally_complete":
            artifacts = receipt.get("source_artifacts")
            if not isinstance(artifacts, list):
                raise CampaignError("worker source artifact receipt is missing")
            expected_paths = set()
            for task in state.tasks.values():
                if task.spec_file:
                    path = runs.task_spec_path(task, state)
                    expected_paths.add(path.resolve())
            observed_paths = set()
            for artifact in artifacts:
                for key in ("path", "review_path"):
                    relative = artifact.get(key)
                    if (
                        not isinstance(relative, str)
                        or Path(relative).is_absolute()
                        or ".." in Path(relative).parts
                    ):
                        raise CampaignError("worker source receipt path is invalid")
                    path = workspace / relative
                    if (
                        path.is_symlink()
                        or not path.resolve().is_relative_to(workspace.resolve())
                        or not path.is_file()
                        or path.stat().st_size > 8 * 1024 * 1024
                    ):
                        raise CampaignError("worker source artifact is unavailable or redirected")
                    if hashlib.sha256(path.read_bytes()).hexdigest() != artifact.get("sha256"):
                        raise CampaignError("worker source artifact changed after acceptance")
                observed_paths.add((workspace / artifact["path"]).resolve())
            if expected_paths != observed_paths:
                raise CampaignError(
                    "worker source artifact receipt does not match the selected legacy task"
                )
    return {"state": "finished", "startup": startup, "receipt": receipt}


def _tracking(request: dict[str, Any], envelope: CampaignExecution) -> dict[str, Any]:
    from .campaign_runtime_tracking import run_tracking
    from .journal import Journal
    from .model import RunState
    from .platform_util import root_identity_record

    directory = envelope.directory / "tracking-journal"
    directory.mkdir(parents=True, exist_ok=True)
    pin = RunState(
        request["launch_id"],
        request["workspace"],
        "",
        run_dir_identity=root_identity_record(directory),
    )
    return run_tracking(request, envelope, journal=Journal(directory, pin=pin))


def _execution(request: dict[str, Any], envelope: CampaignExecution) -> dict[str, Any]:
    from .campaign_intake_amendment import validate_initial_source

    review_handoff = request.get("handoff_state", {}).get("native_role") == "review"
    # A continuation binds the exact captured post-development private source,
    # which has legitimately changed since the original ready-for-dev import.
    if not review_handoff:
        validate_initial_source(request)
    workspace = Path(request["workspace"])
    source = SourceSelector(**request["source"])
    unit = UnitSpec(**request["unit"])
    pol = worker_policy(envelope.policy)
    pol = dataclasses.replace(pol, stories=dataclasses.replace(pol.stories, source=source.kind))
    paths = bmadconfig.load_paths(workspace, want_tickets=source.kind == "tickets")
    if source.kind == "tickets" and not paths.output_folder.resolve().is_relative_to(
        workspace.resolve()
    ):
        raise CampaignError("worker would write a non-private ticket store")
    run_id = request["run_id"]
    if review_handoff:
        from .campaign_continuation import compose_review

        composed = compose_review(
            request,
            envelope,
            make_adapters=lambda project, run_dir, policy, **kwargs: _adapters(
                request, envelope, project, run_dir, policy, **kwargs
            ),
            engine_cls=Engine,
            stories_engine_cls=StoriesEngine,
        )
    else:
        composed = runsetup.compose_run(
            project=workspace,
            paths=paths,
            policy=pol,
            run_id=run_id,
            epic_filter=int(unit.epic) if source.kind == "sprint-status" and unit.epic else None,
            story_filter=unit.selector,
            max_stories=1,
            source=source.kind,
            spec_folder=source.path if source.kind == "stories" else "",
            sweep_factory=lambda *a, **k: None,
            make_adapters=lambda project, run_dir, policy, **kwargs: _adapters(
                request, envelope, project, run_dir, policy, **kwargs
            ),
            engine_cls=Engine,
            stories_engine_cls=StoriesEngine,
            trusted_config_digest=runsetup.config_digest(
                pol, workspace, profiles=envelope.profiles
            ),
            profiles=envelope.profiles,
            plugin_grants=envelope.grants,
            ticket_epic_filter=unit.epic if source.kind == "tickets" else "",
            campaign_execution=envelope,
        )
    atomic_write_text(
        composed.run_dir / "campaign-worker.json",
        json.dumps(
            {
                "campaign_id": envelope.campaign_id,
                "unit_id": envelope.unit_id,
                "execution": request["execution"],
                "digest": request["digest"],
            }
        ),
    )
    composed.journal.append("campaign-config-frozen", config_revision=envelope.config_revision)
    summary = composed.engine.run()
    state = load_state(composed.run_dir)
    recovery = envelope.directory / "recovery-request.json"
    done = bool(
        state.finished
        and not state.paused
        and state.tasks
        and all(task.phase.value == "done" for task in state.tasks.values())
    )
    source_artifacts = []
    if done:
        for task in state.tasks.values():
            if not task.spec_file:
                continue
            path = runs.task_spec_path(task, state)
            if (
                path.is_symlink()
                or not path.resolve().is_relative_to(workspace.resolve())
                or not path.is_file()
                or path.stat().st_size > 8 * 1024 * 1024
            ):
                raise CampaignError(
                    "accepted unit source is missing, redirected or outside the private checkout"
                )
            data = path.read_bytes()
            relative = path.relative_to(workspace).as_posix()
            tracked = (
                verify.git_bytes(
                    workspace, "ls-files", "--error-unmatch", "--", relative
                ).returncode
                == 0
            )
            mirror = path
            if not tracked:
                mirror = (
                    workspace
                    / "docs"
                    / "bmad-campaign"
                    / safe_segment(unit.id)
                    / "accepted-source.md"
                )
                mirror.parent.mkdir(parents=True, exist_ok=True)
                mirror.write_bytes(data)
                verify.commit_paths(
                    workspace, f"docs: publish accepted source for {unit.title}", [mirror]
                )
            source_artifacts.append(
                {
                    "path": relative,
                    "review_path": mirror.relative_to(workspace).as_posix(),
                    "sha256": hashlib.sha256(data).hexdigest(),
                    "kind": source.kind,
                }
            )
    sessions = [record.to_dict() for task in state.tasks.values() for record in task.sessions]
    from .campaign_interruptions import execution_interruption

    return {
        "interruption": execution_interruption(sessions) if not done else None,
        "outcome": (
            "recovery_required"
            if recovery.exists()
            else (
                "locally_complete"
                if done
                else (
                    "operator_held"
                    if state.paused and "campaign operator hold" in (state.paused_reason or "")
                    else "paused"
                )
            )
        ),
        "run_dir": str(composed.run_dir),
        "run_id": run_id,
        "summary": dataclasses.asdict(summary),
        "sessions": sessions,
        "source_artifacts": source_artifacts,
    }


def _adapters(
    request: dict[str, Any],
    envelope: CampaignExecution,
    project: Path,
    run_dir: Path,
    policy,
    **kwargs,
):
    from .adapters.headless import HeadlessAdapter
    from .campaign_headless import capability_for
    from .campaign_model import RoleCandidate
    from .campaign_route_readiness import permissions_for, selected_capability

    permission = permissions_for(
        request.get("role", "execution"),
        stage=request.get("stage", ""),
        read_only=bool(request.get("read_only")),
    )
    routes = request.get("headless_capabilities", {})
    profiles = dict(kwargs.get("profiles") or envelope.profiles)
    selected_caps = {}
    for role in ("dev", "review", "triage", "retro"):
        if request.get("kind") == "stage" and role != "triage":
            continue
        selected = policy.adapter.resolved(role)
        cap = selected_capability(
            profiles[role],
            RoleCandidate(selected.name, selected.model, selected.effort),
            routes,
            permission,
        )
        if cap is None:
            continue
        selected_caps[role] = cap
        profiles[role] = dataclasses.replace(profiles[role], adapter="bounded-headless")
    handoff_role = (
        "triage"
        if request.get("kind") == "stage"
        else "review" if request.get("handoff_state", {}).get("native_role") == "review" else "dev"
    )
    if request.get("handoff_packet") and handoff_role not in selected_caps:
        raise CampaignError(
            "Handoff requires a qualified execution route with inline input delivery"
        )
    kwargs["profiles"] = profiles
    adapters = runsetup.make_adapters(project, run_dir, policy, **kwargs)
    for role, adapter in adapters.items():
        if request.get("kind") == "stage" and role != "triage":
            continue
        if not isinstance(adapter, HeadlessAdapter):
            if request.get("managed_communication"):
                raise CampaignError("Managed communication requires a qualified headless adapter")
            continue
        selected = policy.adapter.resolved(role)
        cap = selected_caps.get(role) or capability_for(
            routes, selected.name, selected.model, permission, effort=selected.effort
        )
        adapter.set_capability(
            cap,
            request.get("headless_grants"),
            request.get("headless_coordinates"),
            request.get("handoff_packet"),
        )
        if request.get("managed_communication"):
            adapter.set_managed_communication(request["managed_communication"])
    if request.get("claim"):
        from .campaign_model import WorkerClaim
        from .campaign_reflection_flow import observe_adapter

        claim = WorkerClaim(**request["claim"])
        seen = set()
        for adapter in adapters.values():
            if id(adapter) in seen:
                continue
            seen.add(id(adapter))
            observe_adapter(
                adapter,
                directory=envelope.directory,
                run_dir=run_dir,
                claim=claim,
                campaign_id=envelope.campaign_id,
                environment=request.get("reflection_environment", {}),
                context=request.get("sibling_context", ""),
            )
    return adapters


def _protected_sources(workspace: Path, request: dict[str, Any]) -> tuple[Path, ...]:
    from . import stories, ticketstore

    configured = request.get("sources") or [request.get("source", {})]
    sources = [
        source
        for source in configured
        if source.get("kind") in {"stories", "tickets", "sprint-status"}
    ]
    roots = [
        workspace / ".bmad-loop" / "policy.toml",
        workspace / ".bmad-loop" / "profiles",
        workspace / ticketstore.USER_CONFIG_REL,
    ]
    if sources:
        paths = bmadconfig.load_paths(
            workspace, want_tickets=any(source["kind"] == "tickets" for source in sources)
        )
        roots.append(paths.output_folder)
    roots.extend(
        stories.resolve_spec_folder(workspace, source.get("path", ""))
        for source in sources
        if source["kind"] == "stories"
    )
    roots.extend(Path(path) for path in request.get("private_paths", []))
    result = []
    for path in roots:
        if not path.exists():
            continue
        if not path.resolve().is_relative_to(workspace.resolve()):
            raise CampaignError("protected source escaped the worker's private checkout")
        if path not in result:
            result.append(path)
    return tuple(result)


def _stage(request: dict[str, Any], envelope: CampaignExecution) -> dict[str, Any]:
    workspace = Path(request["workspace"])
    role = request["role"]
    candidate = request["candidate"]
    pol = worker_policy(envelope.policy)
    selection = dataclasses.replace(
        pol.adapter.triage,
        name=candidate["profile"],
        model=candidate.get("model", ""),
        effort=candidate.get("effort", ""),
    )
    pol = dataclasses.replace(pol, adapter=dataclasses.replace(pol.adapter, triage=selection))
    profiles = dict(envelope.profiles)
    profiles["triage"] = envelope.profiles[request["profile_key"]]
    stage_dir = envelope.directory / request["run_id"]
    stage_dir.mkdir(parents=True, exist_ok=True)
    adapter = _adapters(request, envelope, workspace, stage_dir, pol, profiles=profiles)["triage"]
    from .journal import Journal
    from .model import RunState
    from .platform_util import root_identity_record

    stage_pin = RunState(
        request["launch_id"],
        request["workspace"],
        "",
        run_dir_identity=root_identity_record(stage_dir),
    )
    stage_journal = Journal(stage_dir, pin=stage_pin)
    if hasattr(adapter, "journal"):
        adapter.journal = stage_journal
    task_id = session_task_id("campaign", role, envelope.attempt, envelope.generation)
    result_path = stage_dir / "tasks" / task_id / TASK_CYCLE_ARTIFACTS[0]
    completion_instruction = (
        "\n\nReturn the structured completion result through native JSON output. "
        if getattr(adapter, "requires_inline_context", False)
        else f"\n\nWrite the structured completion result to {result_path}. "
    )
    prompt = (
        request["prompt"]
        + completion_instruction
        + (
            'It must be one JSON object with "status":"done" and the requested artifact fields. '
            "Do not change orchestration policy, publish, merge, or start other agents."
        )
    )
    if not request.get("read_only"):
        from .campaign_checkpoint import instruction as checkpoint_instruction

        prompt += checkpoint_instruction(envelope.checkpoint_interval_s)
    from .campaign_stage_prompt import instruction, materialize

    input_packet = materialize(envelope.directory, prompt)
    spec = SessionSpec(
        task_id=task_id,
        role=role,
        prompt=(
            prompt
            if getattr(adapter, "requires_inline_context", False)
            else instruction(input_packet)
        ),
        cwd=workspace,
        env={
            "BMAD_LOOP_MODE": "1",
            "BMAD_LOOP_RUN_DIR": str(stage_dir),
            "BMAD_LOOP_TASK_ID": task_id,
            "BMAD_LOOP_EVENTS_DIR": str(runs.events_dir_for(workspace, stage_dir.name)),
            "BMAD_LOOP_STORY_KEY": envelope.unit_id,
        },
        model=candidate.get("model", ""),
        effort=candidate.get("effort", ""),
        timeout_s=request.get("timeout_s", pol.limits.session_timeout_min * 60),
        token_budget=request.get("max_tokens") or pol.limits.max_tokens_per_session,
        token_budget_mode=pol.limits.session_budget_mode,
        stall_nudges_cap=pol.limits.workflow_stall_nudges_cap,
    )
    before = verify.rev_parse_head(workspace)
    fingerprint = None
    protected_sources: tuple[Path, ...] = ()
    if request.get("read_only") or request.get("result_contract") == "repair-v1":
        from .campaign_fingerprint import capture

        protected_sources = _protected_sources(workspace, request)
        fingerprint = capture(
            workspace, source_roots=protected_sources, exclude_paths=(envelope.directory,)
        )
    result = adapter.run(spec)
    usage = adapter.read_usage(result)
    from .campaign_usage import save_stage_usage

    save_stage_usage(request, envelope.directory, usage, result)
    input_packet.verify()
    models = tuple(usage.models or ()) if usage else ()
    observed = models[0] if models else ""
    model_source = "transcript_primary" if observed else ""
    if not observed:
        observed = next(
            (
                row["model"]
                for row in stage_journal.entries()
                if row.get("kind") == "session-model"
                and row.get("task_id") == task_id
                and isinstance(row.get("model"), str)
            ),
            "",
        )
        model_source = "native_session_hook" if observed else ""
    from .campaign_interruptions import CampaignStageInterrupted, session_interruption

    interruption = session_interruption(result)
    if interruption is not None:
        interruption["role"] = role
        raise CampaignStageInterrupted(role, interruption)
    actual = result.result_json
    if (
        result.status != "completed"
        or not isinstance(actual, dict)
        or actual.get("status") != "done"
    ):
        raise CampaignError(f"{role} session did not produce a completed structured result")
    from .adapters.headless import HeadlessAdapter

    model_matches = (
        adapter.matches_selected_model(spec, observed)
        if isinstance(adapter, HeadlessAdapter)
        else observed == candidate.get("model")
    )
    if candidate.get("model") and not model_matches:
        raise CampaignError(
            f"{role} observed model is missing or differs from selection: {observed!r}"
        )
    if request.get("read_only") and (
        verify.rev_parse_head(workspace) != before or not verify.worktree_clean(workspace)
    ):
        raise CampaignError(f"{role} changed the candidate under read-only review")
    if fingerprint is not None:
        from .campaign_fingerprint import capture
        from .campaign_resolution import diagnosis_unchanged

        after_fingerprint = capture(
            workspace, source_roots=protected_sources, exclude_paths=(envelope.directory,)
        )
        if request.get("read_only"):
            diagnosis_unchanged(fingerprint.to_dict(), after_fingerprint.to_dict())
        elif fingerprint.source != after_fingerprint.source:
            raise CampaignError("technical repair changed frozen source or policy content")
    if not request.get("read_only"):
        if request.get("stage") == "closure":
            from .campaign_closure import validate as validate_closure

            validate_closure(
                workspace, UnitSpec(**request["unit"]), request["closure_manifest"], actual
            )
        artifacts = actual.get("artifacts", actual.get("artifact_paths", []))
        if not isinstance(artifacts, list) or not all(isinstance(path, str) for path in artifacts):
            raise CampaignError("stage product artifacts are invalid")
        from .campaign_planning_publication import mirror_artifacts

        paths, artifact_mirrors = mirror_artifacts(workspace, envelope.unit_id, artifacts)
        if paths:
            verify.commit_paths(workspace, f"docs: record campaign {role} artifacts", paths)
    from .campaign_headless import provider_family

    family = provider_family(observed)
    if candidate.get("provider_family") and candidate["provider_family"] != family:
        raise CampaignError("observed provider family differs from the selected role")
    return {
        "outcome": "stage_complete",
        "role": role,
        "artifact": actual,
        "selected_model": candidate.get("model", ""),
        "observed_model": observed,
        "observed_model_source": model_source,
        "observed_models": list(models),
        "provider_family": family,
        "provider_families": sorted(
            {provider_family(model) for model in models if provider_family(model)}
            | ({family} if family else set())
        ),
        "native_binding": result.native_binding,
        "session_id": result.session_id,
        "input_packet": {
            "path": str(input_packet.path),
            "sha256": input_packet.sha256,
            "size_bytes": input_packet.size_bytes,
        },
        "transcript_path": result.transcript_path,
        "usage": dataclasses.asdict(usage) if usage else None,
        "base": before,
        "artifact_mirrors": artifact_mirrors if not request.get("read_only") else {},
        "protected_source_before": fingerprint.source if fingerprint is not None else "",
        "protected_source_after": after_fingerprint.source if fingerprint is not None else "",
    }


def _verify(request: dict[str, Any], envelope: CampaignExecution) -> dict[str, Any]:
    from .campaign_acceptance import provenance
    from .campaign_fingerprint import capture
    from .campaign_resolution import diagnosis_unchanged

    workspace = Path(request["workspace"])
    before = verify.rev_parse_head(workspace)
    protected = _protected_sources(workspace, request)
    fingerprint = capture(workspace, source_roots=protected, exclude_paths=(envelope.directory,))
    proof = provenance(
        workspace,
        before,
        (
            list(envelope.policy.verify.commands)
            if request.get("legacy_verify")
            else request["commands"]
        ),
        config_revision=str(request.get("config_revision", "")),
    )

    def receipt(outcome, records):
        return {"outcome": outcome, "commands": records, "base": before, "proof": proof}

    def unchanged() -> None:
        current = capture(workspace, source_roots=protected, exclude_paths=(envelope.directory,))
        diagnosis_unchanged(fingerprint.to_dict(), current.to_dict())

    if request.get("legacy_verify"):
        results = verify.run_verify_commands(envelope.policy, workspace)
        records = [dataclasses.asdict(row) for row in results]
        unchanged()
        if any(row.returncode != 0 for row in results):
            return receipt("verification_failed", records)
        if verify.rev_parse_head(workspace) != before or not verify.worktree_clean(workspace):
            raise CampaignError("verification changed the candidate")
        return receipt("verified", records)
    records = []
    for command in request["commands"]:
        result = childrun.run_argv(
            command, cwd=workspace, timeout=request.get("timeout_s", 1800), encoding="utf-8"
        )
        records.append(
            {
                "argv": command,
                "returncode": result.returncode,
                "timed_out": result.timed_out,
                "stdout": str(result.stdout)[-16000:],
                "stderr": str(result.stderr)[-16000:],
            }
        )
        unchanged()
        if result.returncode != 0 or result.timed_out:
            return receipt("verification_failed", records)
    if verify.rev_parse_head(workspace) != before or not verify.worktree_clean(workspace):
        raise CampaignError("verification changed the candidate")
    return receipt("verified", records)


def _integrate(request: dict[str, Any], envelope: CampaignExecution) -> dict[str, Any]:
    workspace = Path(request["workspace"])
    if not verify.worktree_clean(workspace):
        raise CampaignError("integration requires a clean quiescent candidate")
    before = verify.rev_parse_head(workspace)
    # Worktrees share refs. Each launch must retain its own fresh target, and
    # fetching it must not opportunistically race on origin/main or FETCH_HEAD.
    prefix = f"refs/bmad-campaign/{envelope.campaign_id}/integration/{request['launch_id']}"
    observation_ref = f"{prefix}/observed-target"
    git(
        workspace,
        "fetch",
        "--no-tags",
        "--no-write-fetch-head",
        "--refmap=",
        request["remote"],
        f"refs/heads/{request['target']}:{observation_ref}",
    )
    target = verify.rev_parse_revision(workspace, observation_ref)
    git(workspace, "update-ref", f"{prefix}/candidate", before)
    git(workspace, "update-ref", f"{prefix}/target", target)
    base = git(workspace, "merge-base", before, target)
    if verify.is_ancestor(workspace, target, before):
        return {
            "outcome": "integrated",
            "base": target,
            "head": before,
            "preserved_heads": [before, target],
            "merge_base": base,
        }
    result = verify.git_bytes(workspace, "merge", "--no-edit", target)
    if result.returncode:
        conflicts = git(workspace, "diff", "--name-only", "--diff-filter=U").splitlines()
        if conflicts:
            return {
                "outcome": "integration_conflict",
                "base": target,
                "preserved_heads": [before, target],
                "merge_base": base,
                "conflicts": conflicts,
                "preserved_refs": [f"{prefix}/candidate", f"{prefix}/target"],
            }
        raise CampaignError("integration failed without a resolvable conflict")
    head = verify.rev_parse_head(workspace)
    if not verify.is_ancestor(workspace, before, head) or not verify.is_ancestor(
        workspace, target, head
    ):
        raise CampaignError("integration lost one of its preserved heads")
    return {
        "outcome": "integrated",
        "base": target,
        "head": head,
        "preserved_heads": [before, target],
        "merge_base": base,
    }


def _queue_verify(request: dict[str, Any], envelope: CampaignExecution) -> dict[str, Any]:
    from .campaign_queue import run_verification

    return run_verification(request, envelope)


def _verify_existing(request: dict[str, Any], envelope: CampaignExecution) -> dict[str, Any]:
    from .campaign_existing import verify_existing

    return verify_existing(request, envelope)


def _recover(request: dict[str, Any], envelope: CampaignExecution) -> dict[str, Any]:
    from .campaign_internal_recovery import recover
    from .campaign_recovery import Quiescence

    original = Path(request["recovery_request"])
    handoff = json.loads(original.read_text(encoding="utf-8"))
    if (
        handoff.get("unit_id") != envelope.unit_id
        or handoff.get("campaign_id") != envelope.campaign_id
    ):
        raise CampaignError("recovery request belongs to another campaign unit")
    if (
        handoff.get("cause") in {"finalization-failed", "finalization-interrupted"}
        or handoff.get("task", {}).get("phase") == "committing"
    ):
        from .campaign_finalization import resume

        return resume(
            request,
            envelope,
            make_adapters=lambda project, run_dir, policy, **kwargs: _adapters(
                request, envelope, project, run_dir, policy, **kwargs
            ),
        )
    quiescence = Quiescence(
        handoff["head"], handoff["generation"], True, True, request["quiescence_receipt"]
    )
    result = recover(
        Path(request["canonical_project"]),
        Path(request["workspace"]),
        Path(request["publication_state"]),
        original,
        request["recovery_action"],
        remote=request["remote"],
        branch=request["branch"],
        quiescence=quiescence,
        allowed_paths=tuple(request["allowed_paths"]),
    )
    return {
        "outcome": "recovered",
        "recovery": result,
        "dev_attempts_spent": int(handoff["task"].get("attempt", 0)),
        "review_cycles_spent": int(handoff["task"].get("review_cycle", 0)),
    }


def child_main(request_path: Path) -> int:
    request = json.loads(request_path.read_text(encoding="utf-8"))
    envelope = CampaignExecution.load(Path(request["execution"]), request["digest"])
    from .adapters.multiplexer import configure_multiplexer

    configure_multiplexer(envelope.policy.mux.backend or None, origin=Path(request["workspace"]))
    if request_path.parent.resolve() != envelope.directory.resolve():
        raise CampaignExecutionError("child request is outside its execution directory")
    workspace = Path(request["workspace"])
    if request["kind"] == "queue_verify" or request.get("landing_group"):
        from .campaign_queue import QueueGroup, assert_checkout

        assert_checkout(
            workspace,
            Path(request["common_dir"]),
            QueueGroup(**(request.get("landing_group") or request["queue_group"])),
        )
    else:
        assert_workspace(workspace, Path(request["common_dir"]), request["branch"])
    binding = {
        key: request[key]
        for key in (
            "campaign_id",
            "unit_id",
            "attempt",
            "generation",
            "launch_id",
            "workspace",
            "branch",
            "common_dir",
        )
    }
    runs.write_named_pid(envelope.directory / "engine.pid", os.getpid())
    started_at = time.time()
    atomic_write_text(
        envelope.directory / "startup.json",
        json.dumps({**binding, "schema_version": 1, "started_at": started_at}),
    )
    base = verify.rev_parse_head(workspace)
    stop_probe = childrun.install_stop_probe(
        lambda: runs.read_stop_request_mode(envelope.directory) == "hard"
    )
    from . import campaign_hold

    hold_probe = campaign_hold.install(
        lambda: (
            bool(request.get("hold_channel"))
            and campaign_hold.active(
                Path(request["hold_channel"]), campaign=envelope.campaign_id, unit=envelope.unit_id
            )
        )
    )
    result: dict[str, Any]
    monitor = None
    try:
        if request.get("checkpoint_interval_s") and (
            request["kind"] == "execution"
            or (request["kind"] == "stage" and not request.get("read_only"))
        ):
            from .campaign_checkpoint import ProgressMonitor

            monitor = ProgressMonitor(request, envelope.directory)
            monitor.start()
        action = {
            "execution": _execution,
            "tracking": _tracking,
            "stage": _stage,
            "verify": _verify,
            "integrate": _integrate,
            "recovery": _recover,
            "queue_verify": _queue_verify,
            "verify_existing": _verify_existing,
        }.get(request["kind"])
        if action is None:
            raise CampaignError("unsupported campaign worker kind")
        result = action(request, envelope)
    except Exception as exc:
        from .campaign_interruptions import CampaignStageInterrupted

        result = {
            "outcome": "operator_held" if "campaign operator hold" in str(exc) else "failed",
            "error": f"{type(exc).__name__}: {exc}",
        }
        if isinstance(exc, CampaignStageInterrupted):
            result["interruption"] = exc.interruption
    finally:
        if monitor is not None:
            monitor.stop(result)
        campaign_hold.reset(hold_probe)
        childrun.reset_stop_probe(stop_probe)
    from .campaign_usage import worker_accounting

    accounting = worker_accounting(request, result)
    if accounting is not None:
        result["accounting"] = accounting
    receipt = {
        **binding,
        **result,
        "schema_version": 1,
        "base": result.get("base", base),
        "head": verify.rev_parse_head(workspace),
        "started_at": started_at,
        "finished_at": time.time(),
        "stage": request.get("stage", ""),
        "result_contract": request.get("result_contract", ""),
        "landing": bool(request.get("landing")),
    }
    atomic_write_text(envelope.directory / "receipt.json", json.dumps(receipt, default=str))
    return (
        0
        if result["outcome"]
        in {"locally_complete", "stage_complete", "verified", "queue_verified", "existing_verified"}
        else 1
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Internal campaign worker")
    parser.add_argument("request", type=Path)
    raise SystemExit(child_main(parser.parse_args().request))
