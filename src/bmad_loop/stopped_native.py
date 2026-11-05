"""Explicit continuation of a stopped, bound dev with no SessionRecord.

Journal/hook evidence grants permission to resume a conversation, never evidence
of completed work. Only a real adapter result can enter normal verification.
Unknown ownership or an interrupted launch reservation fails closed.
"""

from __future__ import annotations

import hashlib
import json
import math
import stat
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import bmadconfig
from . import policy as policy_mod
from . import runs, signals, verify
from .adapters.multiplexer import MultiplexerError, get_multiplexer, mux_usable
from .adapters.profile import load_profiles
from .journal import Journal
from .model import Phase, RunState, StoryTask
from .native_binding import resume_refusal
from .platform_util import session_task_id
from .policy import Policy


def _require(ok: bool, reason: str) -> None:
    if not ok:
        raise ValueError(reason)


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _file(path: Path) -> dict[str, Any]:
    before = path.lstat()
    _require(
        stat.S_ISREG(before.st_mode) and before.st_nlink == 1,
        f"not a single-link regular file: {path}",
    )
    data = path.read_bytes()
    after = path.lstat()
    _require(
        (before.st_dev, before.st_ino, before.st_mode, before.st_size, before.st_mtime_ns)
        == (after.st_dev, after.st_ino, after.st_mode, after.st_size, after.st_mtime_ns),
        f"file changed while reading: {path}",
    )
    return {
        "path": str(path.resolve()),
        "sha256": _digest(data),
        "mode": before.st_mode,
        "device": before.st_dev,
        "inode": before.st_ino,
    }


def _json_rows(path: Path) -> list[dict[str, Any]]:
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
    _require(all(isinstance(row, dict) for row in rows), "unreadable native evidence")
    return rows


def mux_quiescent(run_id: str) -> None:
    mux = get_multiplexer()
    _require(mux_usable(mux), "cannot prove native transport ownership")
    _require(
        not mux.has_session(runs.session_name(run_id)),
        "the run still has a native transport session",
    )
    # has_session(False) alone also represents a failed transport probe. The
    # strict listing raises on uncertainty and returns [] only for a dead session.
    _require(
        not mux.list_window_ids(runs.session_name(run_id)),
        "the run still has native transport windows",
    )


def _ownership(run_dir: Path, run_id: str) -> None:
    _require(
        runs.engine_liveness(run_dir) == "dead", "the prior engine is live or ownership is unknown"
    )
    try:
        mux_quiescent(run_id)
    except (OSError, MultiplexerError) as exc:
        raise ValueError(f"cannot prove native transport ownership: {exc}") from exc


def snapshot(state: RunState, task: StoryTask, project: Path) -> dict[str, Any]:
    """Pin current source and explicit possibly-ignored plan/ledger bytes."""
    paths = runs.paths_for_state(
        bmadconfig.load_paths(project, want_tickets=runs.resolves_as_tickets(state)), state
    )
    root = paths.repo_root
    _require(bool(task.spec_file), "the stopped dev has no bound plan")
    plan = runs.task_spec_path(task, state)
    _require(plan.is_file(), "the stopped dev's bound plan is missing")
    if runs.resolves_as_tickets(state):
        from . import ticketstore
        from .tickettree import TicketTree

        pol = policy_mod.load(project / policy_mod.POLICY_FILE)
        conflict = ticketstore.store_conflict(
            project,
            state.source,
            pol.scm.isolation,
            recorded=runs.live_store_topology(state, project) or None,
            initiative=state.initiative if state.parent_initiative is not None else None,
        )
        _require(conflict is None, f"ticket store changed: {conflict}")
        pin, folder = runs.tree_pin(state, paths)
        tree = TicketTree(project, pinned_initiative=pin, folder=folder)
        _require(task.ticket is not None, "missing ticket binding")
        found = tree.find(task.story_key)
        _require(
            found.plan is not None and Path(found.plan).resolve() == plan.resolve(),
            "the live ticket no longer binds the stopped plan",
        )

    def git_bytes(*args: str) -> bytes:
        proc = verify._run_git(["git", "-C", str(root), *args], root, binary=True)
        _require(proc.returncode == 0, "cannot snapshot source")
        return proc.stdout

    status = _digest(git_bytes("status", "--porcelain=v1", "-z", "--untracked-files=all"))
    diffs = [
        git_bytes(*args)
        for args in (
            ["diff", "--binary", "HEAD"],
            ["diff", "--cached", "--binary", "HEAD"],
        )
    ]
    untracked = verify.untracked_files(root)
    files = [_file(root / name) for name in sorted(untracked)]
    explicit = []
    for path in (plan, paths.deferred_work):
        explicit.append(_file(path) if path.exists() else {"path": str(path), "absent": True})
    return {
        "root": str(root.resolve()),
        "head": verify.rev_parse_head(root),
        "status": status,
        "diffs": [_digest(x) for x in diffs],
        "untracked": files,
        "explicit": explicit,
    }


def prepare(
    state: RunState, run_dir: Path, project: Path, native_id: str, policy: Policy
) -> dict[str, Any]:
    """Read-only admission; caller persists the returned authority under lock."""
    try:
        _require(str(uuid.UUID(native_id)) == native_id, "provide the exact canonical native UUID")
    except (ValueError, AttributeError) as exc:
        raise ValueError("provide the exact canonical native UUID") from exc
    _require(state.stopped and not state.finished, "the run must be stopped and unfinished")
    _require(
        state.run_type != "sweep" and policy.scm.isolation == "none",
        "native continuation requires an in-place story run",
    )
    _require(not state.paused_stage, "resolve the existing pause before native continuation")
    _ownership(run_dir, state.run_id)
    candidates = [t for t in state.tasks.values() if t.phase == Phase.DEV_RUNNING]
    _require(len(candidates) == 1, "expected one stopped in-flight dev")
    task = candidates[0]
    _require(not task.stopped_native, "a native continuation is already armed; use plain resume")
    _require(not task.worktree_path, "native continuation currently requires an in-place story")
    _require(task.attempt > 0 and not task.resume_blocked, "the stopped attempt is not resumable")
    _require(
        0 <= task.session_resumes < policy.recovery.max_session_resumes,
        "native resume budget exhausted or disabled",
    )
    logical = session_task_id(task.story_key, "dev", task.attempt, task.generation)
    _require(
        not any(s.task_id == logical for s in task.sessions),
        "the stopped attempt already has a session result",
    )
    _require(
        not any(s.session_id == native_id or s.resumed_from == native_id for s in task.sessions),
        "the native conversation already has an accounting record; "
        "use ordinary recorded-session recovery instead of --continue-native",
    )
    entries = Journal(run_dir, pin=state).entries()
    _require(
        not any(e.get("kind") == "journal-line-unreadable" for e in entries),
        "unreadable journal evidence",
    )
    starts = [i for i, e in enumerate(entries) if e.get("kind") == "session-start"]
    _require(bool(starts), "missing session start")
    tail = entries[starts[-1] :]
    start = tail[0]
    _require(
        start.get("task_id") == logical and start.get("role") == "dev",
        "latest launch is not this dev attempt",
    )
    cfg = policy.adapter.resolved("dev")
    _require(
        bool(start.get("adapter")) and start.get("adapter") == cfg.name,
        "the native adapter changed",
    )
    profile = load_profiles(project).get(cfg.name)
    _require(
        profile is not None and profile.resumable, "the native adapter does not support resume"
    )
    assert profile is not None
    profile = profile.for_permission_mode(cfg.permission_mode)
    bounds = [e for e in tail if e.get("kind") == "session-bound"]
    ends = [e for e in tail if e.get("kind") == "session-end"]
    _require(
        len(bounds) == 1
        and bounds[0].get("task_id") == logical
        and bounds[0].get("session_id") == native_id,
        "missing or ambiguous native binding",
    )
    _require(
        len(ends) == 1
        and ends[0].get("task_id") == logical
        and ends[0].get("status") == "aborted"
        and ends[0].get("error") == "RunStopped",
        "the native did not end by an explicit stop",
    )
    _require(
        tail.index(bounds[0]) < tail.index(ends[0])
        and any(e.get("kind") == "run-stop" for e in tail[tail.index(ends[0]) + 1 :]),
        "missing ordered stop evidence",
    )
    _require(
        not any(
            e.get("kind") in ("story-escalated", "story-deferred", "dev-decision") for e in tail
        ),
        "the stopped attempt already has a decision",
    )
    lifecycle = run_dir / "tasks" / logical / "session-lifecycle.jsonl"
    kills = [e for e in _json_rows(lifecycle) if e.get("event") == "kill-outcome"]
    _require(
        bool(kills) and kills[-1].get("reaped") is True and kills[-1].get("unreaped") == [],
        "the prior native was not proven reaped",
    )
    ended_at = kills[-1].get("ts")
    bound_at = bounds[0].get("ts")
    _require(
        type(ended_at) in (int, float) and type(bound_at) in (int, float),
        "the teardown has no timestamp for this native binding",
    )
    assert isinstance(ended_at, (int, float)) and isinstance(bound_at, (int, float))
    _require(
        math.isfinite(ended_at) and math.isfinite(bound_at) and ended_at >= bound_at * 1e9,
        "the teardown predates this native binding",
    )
    events = signals.session_events(
        runs.events_dir_for(project, state.run_id), run_dir / "events", logical
    )
    admitted, _foreign = signals.attribute_events(events)
    hooks = [e for e in admitted if e.event == "SessionStart"]
    _require(
        bool(hooks) and all(e.session_id == native_id for e in hooks),
        "missing or conflicting native hook evidence",
    )
    transcripts = {e.transcript_path for e in hooks}
    _require(
        len(transcripts) == 1 and all(isinstance(x, str) and bool(x) for x in transcripts),
        "missing or ambiguous transcript path",
    )
    transcript = Path(str(next(iter(transcripts))))
    transcript_identity = _file(transcript)
    binding = bounds[0].get("native_binding")
    refusal = resume_refusal(
        binding, profile, profile.binary, native_id, str(transcript), extra_args=cfg.extra_args
    )
    _require(
        refusal is None, f"the transcript does not identify a compatible bound native: {refusal}"
    )
    serial = task.session_resumes + 1
    artifact = session_task_id(
        task.story_key, "dev", task.attempt, task.generation, continuation=serial
    )
    _require(
        not (run_dir / "tasks" / artifact).exists()
        and not (run_dir / "logs" / f"{artifact}.log").exists(),
        "continuation artifacts already exist",
    )
    return {
        "version": 1,
        "story_key": task.story_key,
        "task_id": logical,
        "artifact_id": artifact,
        "native_id": native_id,
        "transcript_path": str(transcript),
        "native_binding": binding,
        "transcript_identity": transcript_identity,
        "adapter": cfg.name,
        "resumes": task.session_resumes,
        "attempt": task.attempt,
        "generation": task.generation,
        "baseline": task.baseline_commit,
        "snapshot": snapshot(state, task, project),
        "reserved": False,
    }


@dataclass(frozen=True)
class NativeResume:
    """Launch authority, deliberately not a fabricated completed SessionRecord."""

    session_id: str
    transcript_path: str
    native_binding: dict[str, Any]
    ended: str = "stopped"
    status: str = "aborted"


def validate(state: RunState, task: StoryTask, project: Path, policy: Policy) -> None:
    # The engine now owns the run, but no adapter launch owns the former
    # transport yet. Recheck after executable session hooks as well as before
    # dispatch so a newly appearing window cannot race this continuation.
    try:
        mux_quiescent(state.run_id)
    except (OSError, MultiplexerError) as exc:
        raise ValueError(f"cannot prove native transport ownership: {exc}") from exc
    held = task.stopped_native
    _require(
        held.get("version") == 1 and task.phase == Phase.DEV_RUNNING,
        "invalid native continuation authority",
    )
    _require(
        held.get("story_key") == task.story_key
        and held.get("task_id")
        == session_task_id(task.story_key, "dev", task.attempt, task.generation),
        "the logical native task changed",
    )
    _require(
        type(held.get("resumes")) is int and held["resumes"] >= 0,
        "invalid native resume reservation",
    )
    _require(
        held.get("artifact_id")
        == session_task_id(
            task.story_key, "dev", task.attempt, task.generation, continuation=held["resumes"] + 1
        ),
        "invalid native artifact reservation",
    )
    _require(
        task.attempt == held.get("attempt")
        and task.generation == held.get("generation")
        and task.baseline_commit == held.get("baseline"),
        "the stopped attempt changed",
    )
    _require(
        policy.adapter.resolved("dev").name == held.get("adapter"), "the native adapter changed"
    )
    _require(
        not task.resume_blocked
        and task.session_resumes == held.get("resumes")
        and task.session_resumes < policy.recovery.max_session_resumes,
        "native resume budget or ownership changed",
    )
    _require(
        snapshot(state, task, project) == held.get("snapshot"),
        "source, plan, or ticket binding changed after continuation admission",
    )
    transcript_path = held.get("transcript_path")
    _require(
        isinstance(transcript_path, str) and bool(transcript_path), "native transcript disappeared"
    )
    assert isinstance(transcript_path, str)
    _require(
        _file(Path(transcript_path)) == held.get("transcript_identity"),
        "native transcript changed after continuation admission",
    )
    cfg = policy.adapter.resolved("dev")
    profile = load_profiles(project).get(cfg.name)
    _require(
        profile is not None and profile.resumable, "the native adapter does not support resume"
    )
    assert profile is not None
    profile = profile.for_permission_mode(cfg.permission_mode)
    refusal = resume_refusal(
        held.get("native_binding"),
        profile,
        profile.binary,
        held.get("native_id"),
        transcript_path,
        extra_args=cfg.extra_args,
    )
    _require(refusal is None, f"the native binding changed: {refusal}")


def recorded(task: StoryTask) -> bool:
    held = task.stopped_native
    if not held or not task.sessions:
        return False
    last = task.sessions[-1]
    return (
        last.task_id == held.get("task_id")
        and last.artifact_task_id == held.get("artifact_id")
        and last.role == "dev"
        and not last.label
        and last.session_id == held.get("native_id")
        and last.resumed_from == held.get("native_id")
        and last.status == "completed"
        and last.result_json is not None
    )
