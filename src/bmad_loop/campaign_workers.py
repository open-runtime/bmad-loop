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
import time
from pathlib import Path
from typing import Any

from . import bmadconfig, childrun, runs, runsetup, verify
from .adapters.base import SessionSpec
from .campaign_execution import CampaignExecution, CampaignExecutionError, worker_policy
from .campaign_model import CampaignError, SourceSelector, UnitSpec
from .engine import Engine
from .journal import TASK_CYCLE_ARTIFACTS, load_state
from .platform_util import atomic_write_text, detach_kwargs, safe_segment, session_task_id
from .stories_engine import StoriesEngine


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


def allocate_worktree(repo: Path, root: Path, *, branch: str, unit_id: str, base: str) -> Path:
    """Allocate only beneath the explicitly configured project worktree root."""
    root = root.expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    name = safe_segment(unit_id)
    workspace = root / name
    if workspace.exists():
        raise CampaignError("workspace allocation uncertain: reconcile its prior action")
    git(repo, "check-ref-format", "--branch", branch)
    git(repo, "worktree", "add", "-b", branch, str(workspace), base)
    assert_workspace(workspace, common_directory(repo), branch)
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
    directory = request_path.parent
    # An existing startup/receipt, including a dead child, is reconciled by the
    # coordinator.  This API never guesses whether retrying the spawn is safe.
    if (directory / "startup.json").exists() or (directory / "receipt.json").exists():
        raise CampaignError("launch already has a startup binding or receipt")
    log = (directory / "child.log").open("ab")
    process_options: dict[str, Any] = detach_kwargs()
    try:
        return subprocess.Popen(
            [sys.executable, "-m", "bmad_loop.campaign_workers", str(request_path)],
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            env=env,
            **process_options,
        )
    finally:
        log.close()


def observe(directory: Path, *, expected: dict[str, Any]) -> dict[str, Any]:
    receipt_path, startup_path = directory / "receipt.json", directory / "startup.json"
    startup = (
        json.loads(startup_path.read_text(encoding="utf-8")) if startup_path.exists() else None
    )
    if startup is None:
        return {"state": "unknown", "reason": "startup binding not observed"}
    for key in ("campaign_id", "unit_id", "attempt", "generation", "launch_id"):
        if startup.get(key) != expected.get(key):
            raise CampaignError(f"startup {key} does not match its durable claim")
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
    if request.get("kind") == "queue_verify":
        from .campaign_queue import QueueGroup, assert_checkout

        if (
            Path(request["workspace"]).resolve() != workspace.resolve()
            or request["common_dir"] != startup["common_dir"]
        ):
            raise CampaignError("queue verification startup workspace binding changed")
        assert_checkout(
            workspace, Path(startup["common_dir"]), QueueGroup(**request["queue_group"])
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
        trusted_config_digest=runsetup.config_digest(pol, workspace, profiles=envelope.profiles),
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
    return {
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
        "sessions": [record.to_dict() for task in state.tasks.values() for record in task.sessions],
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
    adapters = runsetup.make_adapters(project, run_dir, policy, **kwargs)
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
    task_id = session_task_id("campaign", role, envelope.attempt, envelope.generation)
    result_path = stage_dir / "tasks" / task_id / TASK_CYCLE_ARTIFACTS[0]
    prompt = request["prompt"] + (
        f"\n\nWrite the structured completion result to {result_path}. "
        'It must be one JSON object with "status":"done" and the requested artifact fields. '
        "Do not change orchestration policy, publish, merge, or start other agents."
    )
    spec = SessionSpec(
        task_id=task_id,
        role=role,
        prompt=prompt,
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
    models = tuple(usage.models or ()) if usage else ()
    observed = models[0] if len(models) == 1 else ""
    actual = result.result_json
    if (
        result.status != "completed"
        or not isinstance(actual, dict)
        or actual.get("status") != "done"
    ):
        raise CampaignError(f"{role} session did not produce a completed structured result")
    if candidate.get("model") and observed != candidate["model"]:
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
        artifacts = actual.get("artifacts", actual.get("artifact_paths", []))
        if not isinstance(artifacts, list) or not all(isinstance(path, str) for path in artifacts):
            raise CampaignError("stage product artifacts are invalid")
        from .campaign_planning_publication import mirror_artifacts

        paths, artifact_mirrors = mirror_artifacts(workspace, envelope.unit_id, artifacts)
        if paths:
            verify.commit_paths(workspace, f"docs: record campaign {role} artifacts", paths)
    family = (
        "openai"
        if observed.startswith(("gpt-", "o1", "o3", "o4"))
        else "anthropic" if observed.startswith("claude-") else ""
    )
    if candidate.get("provider_family") and candidate["provider_family"] != family:
        raise CampaignError("observed provider family differs from the selected role")
    return {
        "outcome": "stage_complete",
        "role": role,
        "artifact": actual,
        "selected_model": candidate.get("model", ""),
        "observed_model": observed,
        "provider_family": family,
        "session_id": result.session_id,
        "transcript_path": result.transcript_path,
        "usage": dataclasses.asdict(usage) if usage else None,
        "base": before,
        "artifact_mirrors": artifact_mirrors if not request.get("read_only") else {},
        "protected_source_before": fingerprint.source if fingerprint is not None else "",
        "protected_source_after": after_fingerprint.source if fingerprint is not None else "",
    }


def _verify(request: dict[str, Any], envelope: CampaignExecution) -> dict[str, Any]:
    from .campaign_fingerprint import capture
    from .campaign_resolution import diagnosis_unchanged

    workspace = Path(request["workspace"])
    before = verify.rev_parse_head(workspace)
    protected = _protected_sources(workspace, request)
    fingerprint = capture(workspace, source_roots=protected, exclude_paths=(envelope.directory,))

    def unchanged() -> None:
        current = capture(workspace, source_roots=protected, exclude_paths=(envelope.directory,))
        diagnosis_unchanged(fingerprint.to_dict(), current.to_dict())

    if request.get("legacy_verify"):
        results = verify.run_verify_commands(envelope.policy, workspace)
        records = [dataclasses.asdict(row) for row in results]
        unchanged()
        if any(row.returncode != 0 for row in results):
            return {"outcome": "verification_failed", "commands": records, "base": before}
        if verify.rev_parse_head(workspace) != before or not verify.worktree_clean(workspace):
            raise CampaignError("verification changed the candidate")
        return {"outcome": "verified", "commands": records, "base": before}
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
            return {"outcome": "verification_failed", "commands": records, "base": before}
    if verify.rev_parse_head(workspace) != before or not verify.worktree_clean(workspace):
        raise CampaignError("verification changed the candidate")
    return {"outcome": "verified", "commands": records, "base": before}


def _integrate(request: dict[str, Any], envelope: CampaignExecution) -> dict[str, Any]:
    workspace = Path(request["workspace"])
    if not verify.worktree_clean(workspace):
        raise CampaignError("integration requires a clean quiescent candidate")
    before = verify.rev_parse_head(workspace)
    observation_ref = f"refs/bmad-campaign/{envelope.campaign_id}/target"
    git(
        workspace,
        "fetch",
        "--no-tags",
        request["remote"],
        f"refs/heads/{request['target']}:{observation_ref}",
    )
    target = verify.rev_parse_revision(workspace, observation_ref)
    prefix = f"refs/bmad-campaign/{envelope.campaign_id}/integration/{request['launch_id']}"
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
    if request["kind"] == "queue_verify":
        from .campaign_queue import QueueGroup, assert_checkout

        assert_checkout(
            workspace, Path(request["common_dir"]), QueueGroup(**request["queue_group"])
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
    atomic_write_text(
        envelope.directory / "startup.json",
        json.dumps({**binding, "schema_version": 1, "started_at": time.time()}),
    )
    base = verify.rev_parse_head(workspace)
    stop_probe = childrun.install_stop_probe(
        lambda: runs.read_stop_request_mode(envelope.directory) == "hard"
    )
    from . import campaign_hold

    hold_probe = campaign_hold.install(
        lambda: bool(request.get("hold_channel"))
        and campaign_hold.active(
            Path(request["hold_channel"]), campaign=envelope.campaign_id, unit=envelope.unit_id
        )
    )
    result: dict[str, Any]
    try:
        action = {
            "execution": _execution,
            "tracking": _tracking,
            "stage": _stage,
            "verify": _verify,
            "integrate": _integrate,
            "recovery": _recover,
            "queue_verify": _queue_verify,
        }.get(request["kind"])
        if action is None:
            raise CampaignError("unsupported campaign worker kind")
        result = action(request, envelope)
    except Exception as exc:
        result = {
            "outcome": "operator_held" if "campaign operator hold" in str(exc) else "failed",
            "error": f"{type(exc).__name__}: {exc}",
        }
    finally:
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
        "finished_at": time.time(),
        "stage": request.get("stage", ""),
        "result_contract": request.get("result_contract", ""),
    }
    atomic_write_text(envelope.directory / "receipt.json", json.dumps(receipt, default=str))
    return (
        0
        if result["outcome"] in {"locally_complete", "stage_complete", "verified", "queue_verified"}
        else 1
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Internal campaign worker")
    parser.add_argument("request", type=Path)
    raise SystemExit(child_main(parser.parse_args().request))
