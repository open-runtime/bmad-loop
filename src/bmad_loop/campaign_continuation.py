"""Continue a fenced campaign review in a fresh native conversation.

The coordinator supplies immutable predecessor evidence after draining writers
and publication debt. This collaborator independently binds that evidence to the
retained tree and one legacy task. It copies history into a new run and grants
one in-process review entry; ordinary legacy resume and campaign recovery stay
closed. The successor owes normal verification, publication review and merge.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
from pathlib import Path
from typing import Any, Callable

from . import bmadconfig, runs, runsetup, verify
from .campaign_execution import CampaignExecution, bind_run, worker_policy
from .campaign_finalization import _source_roots
from .campaign_fingerprint import CandidateFingerprint, capture
from .campaign_interruptions import execution_interruption
from .campaign_model import CampaignError
from .journal import Journal, load_state, save_state, state_lock
from .model import Phase, RunState, StoryTask
from .platform_util import is_link_like, root_identity_record, safe_segment
from .statemachine import campaign_review_continuation


def _hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _archive(handoff: dict[str, Any], key: str) -> dict[str, Any]:
    path = Path(handoff[key + "_path"])
    if is_link_like(path) or not path.is_file() or path.stat().st_size > 16 * 1024 * 1024:
        raise CampaignError("review continuation archive is redirected or unbounded")
    data = path.read_bytes()
    if len(data) > 16 * 1024 * 1024 or hashlib.sha256(data).hexdigest() != handoff[key + "_sha256"]:
        raise CampaignError("review continuation archive changed: " + key)
    value = json.loads(data)
    if not isinstance(value, dict):
        raise CampaignError("review continuation archive is not an object")
    return value


@dataclasses.dataclass(frozen=True)
class ReviewPermit:
    campaign_id: str
    unit_id: str
    run_id: str
    task_digest: str
    fingerprint: CandidateFingerprint
    source_roots: tuple[Path, ...]
    policy_digest: str


def validate_resume(engine, task: StoryTask) -> bool:
    permit = getattr(engine, "_campaign_review_permit", None)
    context = engine.campaign_execution
    if not isinstance(permit, ReviewPermit) or context is None:
        return False
    if (
        context.campaign_id != permit.campaign_id
        or context.unit_id != permit.unit_id
        or engine.state.run_id != permit.run_id
        or len(engine.state.tasks) != 1
        or task.phase != Phase.REVIEW_VERIFY
        or _hash(task.to_dict()) != permit.task_digest
        or _hash(engine.policy.to_dict()) != permit.policy_digest
    ):
        return False
    return (
        capture(
            engine.workspace.root.resolve(),
            source_roots=permit.source_roots,
            exclude_paths=(engine.run_dir,),
        )
        == permit.fingerprint
    )


def continue_review(engine, task: StoryTask) -> bool:
    if not validate_resume(engine, task):
        return False
    # Consume before calling hooks or spawning: the permit is not a general
    # recovery exemption after another session changes the task or candidate.
    engine._campaign_review_permit = None
    engine.journal.append("campaign-review-continuation", story_key=task.story_key)
    engine._review_and_commit(task, resumed_loop=True)
    return True


def compose_review(
    request: dict[str, Any],
    envelope: CampaignExecution,
    *,
    make_adapters: Callable,
    engine_cls=None,
    stories_engine_cls=None,
    tickets_engine_cls=None,
) -> runsetup.ComposedRun:
    """Mint a fresh run only for an exact stopped native review boundary."""
    from .campaign_workers import assert_workspace
    from .engine import Engine
    from .stories_engine import StoriesEngine

    handoff = request["handoff_state"]
    predecessor = request["native_predecessor"]
    workspace = Path(request["workspace"]).resolve()
    packet = request.get("handoff_packet") or {}
    if (
        not request.get("handoff_id")
        or not handoff.get("capture_digest")
        or handoff.get("native_role") != "review"
        or predecessor.get("quiescent") is not True
        or packet.get("read_only") is not False
        or predecessor.get("launch_id") == request.get("launch_id")
        or Path(predecessor["workspace"]).resolve() != workspace
        or predecessor.get("branch") != request["branch"]
    ):
        raise CampaignError("review continuation lacks a stopped same-worktree handoff")
    assert_workspace(workspace, Path(request["common_dir"]), request["branch"])
    original_dir = Path(handoff["original_run_dir"])
    original_id = safe_segment(handoff["original_run_id"])
    if original_dir != runs.run_dir_for(workspace, original_id) or is_link_like(original_dir):
        raise CampaignError("review continuation original run location changed")
    original = load_state(original_dir)
    archived = _archive(handoff, "original_state")
    receipt = _archive(handoff, "predecessor_receipt")
    selector = request["unit"]["selector"]
    if (
        original.to_dict() != archived
        or original.run_id != original_id
        or Path(original.project).resolve() != workspace
        or original.source != request["source"]["kind"]
        or (
            original.source == "stories"
            and original.spec_folder != request["source"].get("path", "")
        )
        or len(original.tasks) != 1
        or selector not in original.tasks
        or original.campaign_binding.get("campaign_id") != envelope.campaign_id
        or original.campaign_binding.get("unit_id") != envelope.unit_id
        or original.campaign_binding.get("config_revision") != envelope.config_revision
    ):
        raise CampaignError("review continuation original task/configuration changed")
    original_envelope = CampaignExecution.load(
        Path(original.campaign_binding["execution"]), original.campaign_binding["digest"]
    )
    if (
        predecessor.get("run_id") != original_id
        or Path(predecessor["run_dir"]).resolve() != original_envelope.directory.resolve()
        or any(
            predecessor.get(key) != getattr(original_envelope, key)
            for key in ("attempt", "generation")
        )
    ):
        raise CampaignError("review continuation stopped claim belongs to another original run")
    for key in ("campaign_id", "unit_id"):
        if receipt.get(key) != getattr(envelope, key):
            raise CampaignError("review continuation receipt belongs to another unit")
    for key in ("attempt", "generation", "launch_id"):
        if receipt.get(key) != predecessor.get(key):
            raise CampaignError("review continuation predecessor receipt identity changed")
    if receipt.get("run_id") != original_id or receipt.get("run_dir") != str(original_dir):
        raise CampaignError("review continuation receipt does not bind its original run")
    # A still-live parent process is never rendered harmless by an archived
    # terminal receipt. Native descendants are separately fenced by the
    # coordinator's checked writer-release contract before minting this launch.
    if runs.engine_liveness(Path(predecessor["run_dir"])) != "dead":
        raise CampaignError("review continuation predecessor is not stopped")
    task = original.tasks[selector]
    sessions = [item.to_dict() for item in task.sessions]
    interruption = execution_interruption(sessions)
    reported = receipt.get("interruption") or {}
    if (
        not interruption
        or interruption["role"] != "review"
        or interruption["kind"] not in {"quota", "transport", "canceled"}
        or any(
            reported.get(key) != interruption[key]
            for key in ("kind", "role", "session_id", "task_id")
        )
        or receipt.get("sessions") != sessions
        or task.phase not in {Phase.REVIEW_RUNNING, Phase.REVIEW_VERIFY, Phase.ESCALATED}
        or task.worktree_path
        or task.implemented_review is not None
        or task.stopped_native
        or task.defer_reason is not None
    ):
        raise CampaignError("review continuation requires an exact interrupted ordinary review")
    paths = bmadconfig.load_paths(workspace, want_tickets=original.source == "tickets")
    root_names = handoff.get("source_roots")
    if not isinstance(root_names, list) or any(not isinstance(value, str) for value in root_names):
        raise CampaignError("review continuation requires explicit private source roots")
    roots = tuple(workspace / value for value in root_names)
    if any(
        Path(value).is_absolute()
        or ".." in Path(value).parts
        or not path.resolve().is_relative_to(workspace)
        for value, path in zip(root_names, roots)
    ) or not set(_source_roots(workspace, paths, original)).issubset(roots):
        raise CampaignError("review continuation private source coverage changed")
    fingerprint = capture(workspace, source_roots=roots)
    if fingerprint.to_dict() != handoff["fingerprint"]:
        raise CampaignError("review continuation retained candidate changed")
    if not task.baseline_commit or not verify.is_ancestor(
        workspace, task.baseline_commit, fingerprint.head
    ):
        raise CampaignError("review continuation no longer retains its development ancestor")
    pol = worker_policy(envelope.policy)
    selected = request.get("review_candidate") or {}
    if (
        not pol.review.enabled
        or pol.limits.max_review_cycles <= 0
        or selected.get("profile") != pol.adapter.review.name
        or selected.get("model", "") != pol.adapter.review.model
        or selected.get("effort", "") != pol.adapter.review.effort
    ):
        raise CampaignError("review continuation has no eligible frozen review allowance")
    # The envelope contains the campaign's remaining allowance; the copied task
    # counters are absolute native history. Offset the ceiling, not the history.
    pol = dataclasses.replace(
        pol,
        stories=dataclasses.replace(pol.stories, source=original.source),
        limits=dataclasses.replace(
            pol.limits,
            max_dev_attempts=task.charged_attempts + pol.limits.max_dev_attempts,
            max_review_cycles=task.charged_review_cycles + pol.limits.max_review_cycles,
        ),
        recovery=dataclasses.replace(pol.recovery, max_session_resumes=0),
    )
    state = RunState.from_dict(original.to_dict())
    task = state.tasks[selector]
    campaign_review_continuation(task)
    task.generation += 1
    task.resume_blocked = True
    task.rebase_spec_paths_on(workspace)
    state.run_id = safe_segment(request["run_id"])
    state.replay_of = original.run_id
    state.clear_pause()
    state.finished = False
    state.max_stories = 1
    state.story_filter = selector
    state.policy_snapshot = pol.to_dict()
    run_dir = runs.run_dir_for(workspace, state.run_id)
    # Lost launch acknowledgement is reconciled by the coordinator. Existing
    # continuation state is never silently replayed or overwritten here.
    run_dir.mkdir(parents=True, exist_ok=False)
    state.run_dir_identity = root_identity_record(run_dir)
    state.campaign_binding = bind_run(workspace, run_dir, envelope)
    with state_lock(run_dir):
        save_state(run_dir, state)
        runs.write_trusted_config_digest(
            workspace,
            state.run_id,
            runsetup.config_digest(pol, workspace, profiles=envelope.profiles),
        )
        runs.write_pid(run_dir)
    journal = Journal(run_dir, pin=state)
    adapters = make_adapters(workspace, run_dir, pol, profiles=envelope.profiles)
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
        story_filter=selector,
        sweep_factory=lambda *a, **k: None,
        plugin_grants=envelope.grants,
        campaign_execution=envelope,
        **extra,
    )
    engine._campaign_review_permit = ReviewPermit(
        envelope.campaign_id,
        envelope.unit_id,
        state.run_id,
        _hash(task.to_dict()),
        fingerprint,
        roots,
        _hash(pol.to_dict()),
    )
    journal.append(
        "campaign-review-handoff",
        story_key=selector,
        predecessor=original.run_id,
        handoff_id=request["handoff_id"],
    )
    return runsetup.ComposedRun(engine, state.run_id, run_dir, state, journal)
