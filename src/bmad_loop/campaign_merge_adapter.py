"""Bounded project publication gates, executors and exact candidate verification.

Legacy gate commands add requirements; they never waive publication gates or
perform the merge. REM's maintained merge_pr.py is called only with --dry-run, with a
zero polling window. Native submission still uses the durable GitHub gateway and
bypass_rules=false. An explicitly configured executor uses a separate durable
submit/reconcile protocol, never the legacy gate or native merge fallback.
Queue command output is local evidence, not a hosted check.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import time
import uuid
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Callable

from . import verify
from .campaign_fingerprint import capture
from .campaign_github import MutationIntent
from .campaign_model import CampaignError
from .campaign_publication import Candidate, PublicationController, PublicationEvidence
from .childrun import run_argv
from .platform_util import atomic_write_text, is_link_like


class MergeAdapterError(CampaignError):
    """A project adapter has no qualified, exact-candidate receipt."""


@dataclass(frozen=True)
class AdapterReceipt:
    state: str
    candidate: Candidate
    rules_fingerprint: str
    request_digest: str
    command_digest: str
    artifact: str
    observed_at: float
    reason: str = ""
    group_head: str = ""
    context: str = ""
    hosted_check_pending: bool = False
    group_base: str = ""

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> AdapterReceipt:
        raw = dict(value)
        raw["candidate"] = Candidate(**raw["candidate"])
        return cls(**raw)


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _digest(value: Any) -> str:
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _read(path: Path, bound: int = 1024 * 1024) -> dict[str, Any]:
    if is_link_like(path) or not path.is_file():
        raise MergeAdapterError("project adapter result missing or redirected")
    with path.open("rb") as stream:
        raw = stream.read(bound + 1)
    if len(raw) > bound:
        raise MergeAdapterError("project adapter result exceeds byte bound")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise MergeAdapterError("project adapter result must be a JSON object")
    return value


def _command(command: tuple[str, ...], project: Path) -> tuple[tuple[str, ...], str, bool]:
    if not command or any(not isinstance(arg, str) or not arg or "\0" in arg for arg in command):
        raise MergeAdapterError("project publication command must be explicit argv")
    forbidden = {"--admin", "--bypass", "--bypass-rules", "--squash", "--rebase", "--force"}
    if any(arg.split("=", 1)[0] in forbidden for arg in command):
        raise MergeAdapterError(
            "project publication commands cannot request bypass/history rewrites"
        )
    identities = []
    rem = False
    rem_index = -1
    for index, arg in enumerate(command):
        path = Path(arg)
        path = path if path.is_absolute() else project / path
        if index == 0:
            path = Path(shutil.which(arg) or str(path)).resolve()
        if path.is_file():
            if (index != 0 and path.is_symlink()) or path.stat().st_size > 64 * 1024 * 1024:
                raise MergeAdapterError("project publication command is redirected or oversized")
            identities.append(
                {
                    "path": str(path.resolve()),
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                }
            )
            if path.name == "merge_pr.py":
                rem = True
                rem_index = index
    if rem and (
        rem_index != len(command) - 1
        or any(arg not in {"-I", "-B", "-u"} for arg in command[1:rem_index])
    ):
        raise MergeAdapterError(
            "REM publication gate must be a direct script invocation without extra options"
        )
    return command, _digest({"argv": command, "files": identities}), rem


def _invoke(
    command: tuple[str, ...],
    project: Path,
    state_dir: Path,
    payload: dict[str, Any],
    *,
    runner: Callable,
    timeout_s: float,
    max_output_bytes: int,
) -> tuple[str, dict[str, Any], Path, str]:
    if not 0 < timeout_s <= 86400 or max_output_bytes < 1:
        raise ValueError("publication adapter requires bounded positive execution limits")
    if state_dir.resolve().is_relative_to(project.resolve()):
        raise MergeAdapterError("publication command records must be outside the editing checkout")
    command, digest, rem = _command(command, project)
    directory = state_dir / uuid.uuid4().hex
    directory.mkdir(parents=True, exist_ok=False)
    request = {"schema_version": 1, **payload}
    request_digest = _digest(request)
    request["request_digest"] = request_digest
    request_path, result_path = directory / "request.json", directory / "merge-adapter-result.json"
    atomic_write_text(request_path, _json(request), follow_symlinks=False)
    if rem:
        if payload["operation"] != "publication-gate":
            raise MergeAdapterError("REM merge gate is not a merge-group verification command")
        candidate = payload["candidate"]
        log = directory / "merge.log"
        argv = (
            *command,
            str(candidate["pr_number"]),
            "--repo",
            candidate["repository"],
            "--base",
            payload["target"],
            "--head",
            candidate["head"],
            "--dry-run",
            "--wait-min",
            "0",
            "--poll-s",
            "0",
            "--log",
            str(log),
        )
    else:
        argv = (
            *command,
            "--bmad-campaign-request",
            str(request_path),
            "--bmad-campaign-result",
            str(result_path),
        )
    result = runner(argv, cwd=project, timeout=timeout_s, encoding="utf-8")
    if result.timed_out or result.interrupted:
        return "unknown", {"reason": "project command timed out/interrupted"}, request_path, digest
    if len(result.stdout.encode()) + len(result.stderr.encode()) > max_output_bytes:
        return (
            "unknown",
            {"reason": "project command output exceeds byte bound"},
            request_path,
            digest,
        )
    atomic_write_text(directory / "stdout.log", result.stdout, follow_symlinks=False)
    atomic_write_text(directory / "stderr.log", result.stderr, follow_symlinks=False)
    # Command identity is checked again because a workspace task must not swap
    # the maintained gate between preflight and the final submission boundary.
    if _command(command, project)[1] != digest:
        raise MergeAdapterError("project publication command changed during execution")
    if rem:
        rows_path = directory / "merges.jsonl"
        if (
            not rows_path.is_file()
            or rows_path.is_symlink()
            or rows_path.stat().st_size > max_output_bytes
        ):
            return (
                "unknown",
                {"reason": "REM dry-run has no bounded machine gate record"},
                request_path,
                digest,
            )
        rows = rows_path.read_text(encoding="utf-8").splitlines()
        if len(rows) != 1:
            raise MergeAdapterError("REM dry-run gate record is ambiguous")
        record = json.loads(rows[0])
        candidate = payload["candidate"]
        if (
            record.get("pr") != candidate["pr_number"]
            or record.get("repo") != candidate["repository"]
            or record.get("head") != candidate["head"]
            or record.get("dry_run") is not True
        ):
            raise MergeAdapterError("REM gate record belongs to another PR/head/mode")
        ready = (
            result.returncode == 0 and record.get("rc") == 0 and record.get("outcome") == "dry-run"
        )
        return (
            "ready" if ready else "held",
            {"reason": "" if ready else "maintained REM publication gate remains held"},
            rows_path,
            digest,
        )
    if result.returncode != 0:
        return (
            "held",
            {"reason": "project publication command rejected the candidate"},
            request_path,
            digest,
        )
    record = _read(result_path, max_output_bytes)
    if (
        record.get("schema_version") != 1
        or record.get("request_digest") != request_digest
        or record.get("candidate") != payload["candidate"]
        or record.get("rules_fingerprint") != payload["rules_fingerprint"]
        or record.get("operation") != payload["operation"]
        or record.get("bypass_rules") is not False
        or record.get("merge_method") != "merge"
    ):
        raise MergeAdapterError(
            "project adapter receipt has stale coordinates or attempts to bypass publication"
        )
    return "ready" if record.get("ready") is True else "held", record, result_path, digest


def check_project_gate(
    command: tuple[str, ...],
    project: Path,
    state_dir: Path,
    candidate: Candidate,
    evidence: PublicationEvidence,
    controller: PublicationController,
    *,
    target: str,
    merge_mode: str = "direct-strict",
    queue_context: str = "",
    independent_families: bool = True,
    runner: Callable = run_argv,
    timeout_s: float = 120,
    max_output_bytes: int = 1024 * 1024,
    source_roots: tuple[Path, ...] = (),
    clock: Callable[[], float] = time.time,
) -> AdapterReceipt:
    """Add a project gate, then re-read every native gate before returning ready."""
    observation = controller.inspect(candidate.pr_number)
    if target != candidate.target:
        raise MergeAdapterError("project gate target differs from the accepted candidate target")
    gate = controller.evaluate(
        candidate,
        evidence,
        observation,
        merge_mode=merge_mode,
        independent_families=independent_families,
        queue_evidence_context=queue_context,
    )
    if not gate.ready:
        return AdapterReceipt(
            "held", candidate, gate.rules_fingerprint, "", "", "", clock(), "; ".join(gate.reasons)
        )
    payload = {
        "operation": "publication-gate",
        "candidate": asdict(candidate),
        "target": target,
        "rules_fingerprint": gate.rules_fingerprint,
        "merge_mode": merge_mode,
        "bypass_rules": False,
        "merge_method": "merge",
    }
    before = capture(project, source_roots=source_roots)
    state, result, artifact, command_digest = _invoke(
        command,
        project,
        state_dir,
        payload,
        runner=runner,
        timeout_s=timeout_s,
        max_output_bytes=max_output_bytes,
    )
    if capture(project, source_roots=source_roots) != before:
        raise MergeAdapterError("publication gate changed the candidate checkout")
    fresh = controller.inspect(candidate.pr_number)
    final = controller.evaluate(
        candidate,
        evidence,
        fresh,
        merge_mode=merge_mode,
        independent_families=independent_families,
        queue_evidence_context=queue_context,
    )
    if not final.ready or final.rules_fingerprint != gate.rules_fingerprint:
        state = "held"
        result = {"reason": "; ".join(final.reasons) or "remote publication rules changed"}
    return AdapterReceipt(
        state,
        candidate,
        gate.rules_fingerprint,
        _digest({"schema_version": 1, **payload}),
        command_digest,
        str(artifact),
        clock(),
        str(result.get("reason", "")),
    )


def verify_queue_candidate(
    command: tuple[str, ...],
    workspace: Path,
    state_dir: Path,
    candidate: Candidate,
    *,
    group_head: str,
    group_base: str,
    context: str,
    rules_fingerprint: str,
    runner: Callable = run_argv,
    timeout_s: float = 600,
    max_output_bytes: int = 1024 * 1024,
    source_roots: tuple[Path, ...] = (),
    clock: Callable[[], float] = time.time,
    candidate_kind: str = "merge-group",
) -> AdapterReceipt:
    """Verify exact combined G; caller publishes/observes required app check at G.

    The caller supplies a separately owned verification checkout. This function
    never checks out, resets or modifies another branch/worktree and never treats
    a head-only H test as evidence about G.
    """
    if candidate_kind not in {"merge-group", "pr-head"}:
        raise MergeAdapterError("unknown candidate verification kind")
    if (
        not re.fullmatch(r"[0-9a-f]{40,64}", group_head)
        or (group_head == candidate.head) != (candidate_kind == "pr-head")
        or (candidate_kind == "pr-head" and group_base != candidate.base)
        or not context
        or not rules_fingerprint
        or verify.rev_parse_head(workspace) != group_head
        or not verify.is_ancestor(workspace, candidate.base, group_base)
        or not verify.is_ancestor(workspace, candidate.head, group_head)
        or not verify.is_ancestor(workspace, group_base, group_head)
    ):
        raise MergeAdapterError("queue verifier must own the exact combined candidate G over H/B")
    before = capture(workspace, source_roots=source_roots)
    payload = {
        "operation": (
            "queue-candidate-verification"
            if candidate_kind == "merge-group"
            else "pr-head-verification"
        ),
        "candidate": asdict(candidate),
        "group": {"head": group_head, "base": group_base, "context": context},
        "rules_fingerprint": rules_fingerprint,
        "bypass_rules": False,
        "merge_method": "merge",
    }
    state, result, artifact, command_digest = _invoke(
        command,
        workspace,
        state_dir,
        payload,
        runner=runner,
        timeout_s=timeout_s,
        max_output_bytes=max_output_bytes,
    )
    if capture(workspace, source_roots=source_roots) != before:
        raise MergeAdapterError("queue verification command changed combined candidate G")
    if state == "ready":
        verification = result.get("verification", {})
        commands = verification.get("commands", [])
        codes = verification.get("returncodes", [])
        if (
            result.get("group") != payload["group"]
            or not isinstance(commands, list)
            or not commands
            or len(codes) != len(commands)
            or any(type(code) is not int or code != 0 for code in codes)
            or any(
                not isinstance(argv, list)
                or not argv
                or any(not isinstance(arg, str) or not arg for arg in argv)
                for argv in commands
            )
        ):
            raise MergeAdapterError(
                "combined candidate receipt lacks exact G/B and successful command evidence"
            )
    return AdapterReceipt(
        state,
        candidate,
        rules_fingerprint,
        _digest({"schema_version": 1, **payload}),
        command_digest,
        str(artifact),
        clock(),
        str(result.get("reason", "")),
        group_head,
        context,
        hosted_check_pending=state == "ready",
        group_base=group_base,
    )


def verify_pr_candidate(
    command: tuple[str, ...], workspace: Path, state_dir: Path, candidate: Candidate, **kwargs: Any
) -> AdapterReceipt:
    """Run the same verifier on exact H/B before native queue admission."""
    return verify_queue_candidate(
        command,
        workspace,
        state_dir,
        candidate,
        group_head=candidate.head,
        group_base=candidate.base,
        candidate_kind="pr-head",
        **kwargs,
    )


def prepare_execution(
    command: tuple[str, ...],
    project: Path,
    candidate: Candidate,
    evidence: PublicationEvidence,
    controller: PublicationController,
    *,
    action_id: str,
    generation: int,
    independent_families: bool = True,
) -> MutationIntent:
    """Prepare an actual executor intent; caller persists before dispatching it.

    The executor must enforce the supplied expected head/base and non-bypass
    merge policy atomically at its submission boundary. Existing REM dry-run
    scripts are not silently treated as implementations of this protocol.
    """
    command, digest, rem = _command(command, project)
    if rem:
        raise MergeAdapterError(
            "maintained REM gate needs a qualified submit/reconcile executor bridge"
        )
    observation = controller.inspect(candidate.pr_number)
    gate = controller.evaluate(
        candidate,
        evidence,
        observation,
        merge_mode="project-command",
        independent_families=independent_families,
    )
    if not gate.ready:
        raise MergeAdapterError("project publication held: " + "; ".join(gate.reasons))
    return MutationIntent(
        action_id,
        "project_merge",
        "COMMAND",
        "",
        {
            "command": list(command),
            "command_digest": digest,
            "candidate": asdict(candidate),
            "target": candidate.target,
            "rules_fingerprint": gate.rules_fingerprint,
            "bypass_rules": False,
            "merge_method": "merge",
            "request_id": action_id,
        },
        generation=generation,
        coordinates={**asdict(candidate), "rules_fingerprint": gate.rules_fingerprint},
    )


def _project_execution(
    intent: MutationIntent,
    project: Path,
    state_dir: Path,
    controller: PublicationController,
    *,
    generation: int,
    reconcile: bool,
    evidence: PublicationEvidence | None = None,
    independent_families: bool = True,
    runner: Callable = run_argv,
    timeout_s: float = 120,
    max_output_bytes: int = 1024 * 1024,
) -> MutationIntent:
    if (
        intent.kind != "project_merge"
        or intent.state != "sent_unknown"
        or intent.generation != generation
    ):
        raise MergeAdapterError(
            "persist current-generation uncertain project intent before execution"
        )
    payload = intent.payload
    candidate = Candidate(**payload["candidate"])
    command = tuple(payload["command"])
    if (
        controller.gateway.repository != candidate.repository
        or payload.get("request_id") != intent.action_id
        or payload.get("bypass_rules") is not False
        or payload.get("merge_method") != "merge"
        or payload.get("target") != candidate.target
        or intent.coordinates
        != {**asdict(candidate), "rules_fingerprint": payload["rules_fingerprint"]}
        or _command(command, project)[1] != payload["command_digest"]
    ):
        raise MergeAdapterError("project merge intent identity/policy changed")
    remote = controller.merge_status(candidate.pr_number, expected_head=candidate.head)
    if remote.state == "merged_pending_sync":
        return replace(
            intent,
            state="confirmed",
            receipt={
                "status": "merged",
                "details": {"sha": remote.merge_commit},
                "project_executor": True,
            },
            error="",
        )
    if remote.state == "held":
        return replace(intent, state="rejected", error=remote.reason)
    if not reconcile:
        if evidence is None:
            raise MergeAdapterError("project submission requires final publication evidence")
        fresh = prepare_execution(
            command,
            project,
            candidate,
            evidence,
            controller,
            action_id=intent.action_id,
            generation=generation,
            independent_families=independent_families,
        )
        if fresh.payload != payload:
            return replace(
                intent,
                state="rejected",
                error="publication authority changed before project submission",
            )
    before = capture(project)
    operation = "merge-reconcile" if reconcile else "merge-submit"
    try:
        state, record, _, _ = _invoke(
            command,
            project,
            state_dir,
            {**payload, "operation": operation},
            runner=runner,
            timeout_s=timeout_s,
            max_output_bytes=max_output_bytes,
        )
    except (OSError, ValueError, subprocess.SubprocessError, MergeAdapterError) as exc:
        # The command might have submitted before any malformed/lost receipt.
        return replace(
            intent, error=f"project merge outcome uncertain: {type(exc).__name__}: {exc}"
        )
    if capture(project) != before:
        return replace(
            intent, error="project merge command changed checkout; reconcile remote outcome"
        )
    remote = controller.merge_status(candidate.pr_number, expected_head=candidate.head)
    if remote.state == "merged_pending_sync":
        return replace(
            intent,
            state="confirmed",
            receipt={
                "status": "merged",
                "details": {"sha": remote.merge_commit},
                "project_executor": True,
            },
            error="",
        )
    if remote.state == "held":
        return replace(intent, state="rejected", error=remote.reason)
    if state == "unknown" or record.get("request_id") != intent.action_id:
        return replace(
            intent, error="project merge acknowledgement missing; reconcile without resubmission"
        )
    submission = record.get("submission")
    if submission == "not-submitted" and reconcile and record.get("definitive") is True:
        return replace(
            intent,
            state="rejected",
            receipt=record,
            error="executor confirms request was not submitted",
        )
    if submission in {"submitted", "pending"}:
        return replace(
            intent,
            state="confirmed",
            receipt={
                "status": "pending",
                "details": {"uuid": intent.action_id},
                "project_executor": True,
                "executor_receipt": record,
            },
            error="",
        )
    # A command's merged claim alone cannot establish completion.
    return replace(
        intent, error="project merge needs independent remote confirmation", receipt=record
    )


def execute_project_merge(
    intent: MutationIntent,
    project: Path,
    state_dir: Path,
    controller: PublicationController,
    *,
    evidence: PublicationEvidence,
    generation: int,
    **kwargs: Any,
) -> MutationIntent:
    return _project_execution(
        intent,
        project,
        state_dir,
        controller,
        evidence=evidence,
        generation=generation,
        reconcile=False,
        **kwargs,
    )


def reconcile_project_merge(
    intent: MutationIntent,
    project: Path,
    state_dir: Path,
    controller: PublicationController,
    *,
    generation: int,
    **kwargs: Any,
) -> MutationIntent:
    """Observe executor request identity; this never invokes merge-submit again."""
    return _project_execution(
        replace(intent, state="sent_unknown"),
        project,
        state_dir,
        controller,
        generation=generation,
        reconcile=True,
        **kwargs,
    )
