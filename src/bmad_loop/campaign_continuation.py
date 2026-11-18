"""Continue fenced campaign work in a fresh native conversation.

The coordinator supplies immutable predecessor evidence after draining writers
and publication debt. This collaborator independently binds that evidence to the
retained tree and one legacy task. It copies history into a new run and grants
one in-process entry; ordinary legacy resume and campaign recovery stay
closed. The successor owes normal verification, publication review and merge.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Callable

from . import bmadconfig, runs, runsetup, verify
from .campaign_execution import CampaignExecution, bind_run, worker_policy
from .campaign_finalization import _source_roots
from .campaign_fingerprint import CandidateFingerprint, capture
from .campaign_interruptions import execution_interruption
from .campaign_model import CampaignError
from .journal import Journal, load_state, save_state, state_lock
from .model import PauseKind, Phase, RunState, StoryTask
from .platform_util import (
    create_exclusive_confined,
    is_link_like,
    recorded_root_identity,
    root_identity_record,
    safe_segment,
)
from .statemachine import campaign_development_continuation, campaign_review_continuation


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


@dataclasses.dataclass(frozen=True)
class DevelopmentPermit(ReviewPermit):
    """In-process entry backed by a consumed exact-successor controller permit."""

    authority_digest: str
    predecessor_directory: Path
    workspace: Path
    branch: str
    generation: int
    attempt: int
    config_revision: int
    request_json: str


def prepare_permit(request: dict[str, Any], envelope: CampaignExecution) -> dict[str, Any]:
    """Describe exact continuation authority; only the campaign store may consume it.

    This does not authorize a launch. The child independently reconstructs the
    same binding and requires the store's consumed receipt for this successor.
    Original review handoffs retain their existing v1 entry contract.
    """
    handoff = request["handoff_state"]
    predecessor = request["native_predecessor"]
    original = RunState.from_dict(_archive(handoff, "original_state"))
    selector = request["unit"]["selector"]
    if selector not in original.tasks or handoff.get("native_role") != "dev":
        raise CampaignError("development continuation requires its exact original task")
    if (
        type(request.get("pr_number")) is not int
        or request["pr_number"] <= 0
        or not request.get("launch_id")
        or not request.get("run_id")
        or not request.get("handoff_id")
    ):
        raise CampaignError("development continuation requires an owned PR and successor")
    context = request.get("continuation_context")
    if not isinstance(context, dict) or not context:
        raise CampaignError("development continuation requires frozen current constraints")
    raw_configuration = dataclasses.asdict(envelope)
    configuration = {
        key: raw_configuration[key]
        for key in ("profiles", "grants", "verify_commands", "checkpoint_interval_s")
    }
    configuration["policy"] = envelope.policy.to_dict()
    # Match the declared envelope's path and tuple serialization, preserving
    # unknown argv authority (None) separately from an explicit empty recipe.
    configuration = json.loads(json.dumps(configuration, sort_keys=True, default=str))
    binding = {
        "campaign_id": envelope.campaign_id,
        "unit_id": envelope.unit_id,
        "config_revision": envelope.config_revision,
        "configuration_digest": _hash(configuration),
        "continuation_context": context,
        "native_role": "dev",
        "handoff_id": request["handoff_id"],
        "capture_digest": handoff["capture_digest"],
        "workspace": str(Path(request["workspace"]).resolve()),
        "common_dir": str(Path(request["common_dir"]).resolve()),
        "branch": request["branch"],
        "pr_number": request["pr_number"],
        "source": request["source"],
        "source_roots": handoff["source_roots"],
        "fingerprint": handoff["fingerprint"],
        "task_digest": _hash(original.tasks[selector].to_dict()),
        "original_state_sha256": handoff["original_state_sha256"],
        "predecessor_receipt_sha256": handoff["predecessor_receipt_sha256"],
        "predecessor": {
            key: predecessor[key]
            for key in ("launch_id", "run_id", "run_dir", "attempt", "generation")
        },
        "successor": {
            "launch_id": request["launch_id"],
            "run_id": request["run_id"],
            "directory": str(envelope.directory.resolve()),
            "attempt": envelope.attempt,
            "generation": envelope.generation,
        },
        "remaining_dev_attempts": envelope.policy.limits.max_dev_attempts,
        "remaining_review_cycles": envelope.policy.limits.max_review_cycles,
    }
    # Freeze nested tuples and dictionaries in their persisted JSON form.
    binding = json.loads(json.dumps(binding, sort_keys=True))
    return {"schema_version": 2, "permit_id": _hash(binding), "binding": binding}


def _consumed_permit(request, envelope) -> dict[str, Any]:
    permit = prepare_permit(request, envelope)
    if request.get("continuation_permit") != permit or request.get("continuation_consumption") != {
        "schema_version": 1,
        "permit_id": permit["permit_id"],
        "successor_launch_id": request["launch_id"],
        "successor_run_id": request["run_id"],
        "state": "consumed",
    }:
        raise CampaignError("development continuation lacks its consumed exact-successor permit")
    from .campaign_store import CampaignStore
    from .campaign_transfer import retired
    from .campaign_usage import remaining
    from .campaign_worker_authority import continuation_context

    directory = envelope.directory.resolve()
    authority = request.get("continuation_authority")
    if (
        not isinstance(authority, str)
        or not Path(authority).is_absolute()
        or is_link_like(Path(authority))
        or Path(authority) != directory.parents[2]
        or directory != Path(authority) / envelope.campaign_id / "workers" / request["launch_id"]
    ):
        raise CampaignError("development continuation authority is outside its owned campaign")
    snapshot = CampaignStore(Path(authority)).load(envelope.campaign_id)
    unit = snapshot.units.get(envelope.unit_id)
    expected_claim = {
        "unit": envelope.unit_id,
        "launch_id": request["launch_id"],
        "run_id": request["run_id"],
        "run_dir": str(directory),
        "attempt": envelope.attempt,
        "generation": envelope.generation,
        "workspace": str(Path(request["workspace"]).resolve()),
        "branch": request["branch"],
    }
    handoff = unit.evidence.get("handoff", {}) if unit is not None else {}
    if (
        unit is None
        or snapshot.phase != "active"
        or retired(snapshot)
        or snapshot.generation != envelope.generation
        or snapshot.config_revision != envelope.config_revision
        or not unit.claim
        or unit.claim.quiescent
        or any(getattr(unit.claim, key) != value for key, value in expected_claim.items())
        or unit.bindings.get("pr", {}).get("number") != request["pr_number"]
        or unit.evidence.get("continuation_permits", {}).get(permit["permit_id"])
        != {"permit": permit, "consumption": request["continuation_consumption"]}
        or handoff.get("id") != request["handoff_id"]
        or handoff.get("phase") != "continuing"
        or handoff.get("predecessor") != request["native_predecessor"]
        or any(
            handoff.get("successor", {}).get(key) != value for key, value in expected_claim.items()
        )
        or (unit.hold and (unit.hold.kind != "handoff" or unit.hold.id != request["handoff_id"]))
    ):
        raise CampaignError("development continuation lost current campaign successor authority")
    if continuation_context(snapshot, unit) != permit["binding"]["continuation_context"]:
        raise CampaignError("development continuation current constraints changed")
    remaining_dev, remaining_review = remaining(snapshot, unit)
    if (
        unit.evidence.get("usage", {}).get("legacy_counters_unknown")
        or remaining_dev != permit["binding"]["remaining_dev_attempts"]
        or max(0, remaining_review) != permit["binding"]["remaining_review_cycles"]
    ):
        raise CampaignError("development continuation remaining allowance changed")
    return permit


def validate_resume(engine, task: StoryTask) -> bool:
    permit = getattr(engine, "_campaign_continuation_permit", None)
    development = isinstance(permit, DevelopmentPermit)
    if not development:
        permit = getattr(engine, "_campaign_review_permit", None)
    context = engine.campaign_execution
    if not isinstance(permit, ReviewPermit) or context is None:
        return False
    if (
        context.campaign_id != permit.campaign_id
        or context.unit_id != permit.unit_id
        or engine.state.run_id != permit.run_id
        or len(engine.state.tasks) != 1
        or task.phase != (Phase.DEV_VERIFY if development else Phase.REVIEW_VERIFY)
        or _hash(task.to_dict()) != permit.task_digest
        or _hash(engine.policy.to_dict()) != permit.policy_digest
    ):
        return False
    if isinstance(permit, DevelopmentPermit) and (
        context.generation != permit.generation
        or context.attempt != permit.attempt
        or context.config_revision != permit.config_revision
        or engine.workspace.root.resolve() != permit.workspace
        or verify.current_branch(permit.workspace) != permit.branch
        or runs.engine_liveness(permit.predecessor_directory) != "dead"
        or (engine.run_dir / "continuation-consumed.json").exists()
    ):
        return False
    if isinstance(permit, DevelopmentPermit):
        try:
            _consumed_permit(json.loads(permit.request_json), context)
        except (CampaignError, OSError, ValueError):
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
    if isinstance(getattr(engine, "_campaign_continuation_permit", None), DevelopmentPermit):
        return False
    if not validate_resume(engine, task):
        return False
    # Consume before calling hooks or spawning: the permit is not a general
    # recovery exemption after another session changes the task or candidate.
    engine._campaign_review_permit = None
    engine.journal.append("campaign-review-continuation", story_key=task.story_key)
    engine._review_and_commit(task, resumed_loop=True)
    return True


def continue_task(engine, task: StoryTask) -> bool:
    """Consume one retained dev entry, or preserve the existing review entry."""
    permit = getattr(engine, "_campaign_continuation_permit", None)
    if not isinstance(permit, DevelopmentPermit):
        return continue_review(engine, task)
    if not validate_resume(engine, task):
        return False
    identity = engine.state.run_dir_identity
    if identity is None:
        raise CampaignError("development continuation run identity is unavailable")
    descriptor = create_exclusive_confined(
        engine.run_dir / "continuation-consumed.json",
        confine_root=engine.run_dir,
        root_identity=recorded_root_identity(identity),
    )
    engine._campaign_continuation_permit = None
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump({"schema_version": 1, "permit_id": permit.authority_digest}, stream)
        stream.flush()
        os.fsync(stream.fileno())
    engine.journal.append("campaign-development-continuation", story_key=task.story_key)

    def validate() -> None:
        # Hooks run after entry and may change the candidate. Task counters
        # legitimately advance; executable authority and retained source do not.
        if (
            engine.workspace.root.resolve() != permit.workspace
            or verify.current_branch(permit.workspace) != permit.branch
            or _hash(engine.policy.to_dict()) != permit.policy_digest
            or runs.engine_liveness(permit.predecessor_directory) != "dead"
            or capture(
                permit.workspace,
                source_roots=permit.source_roots,
                exclude_paths=(engine.run_dir,),
            )
            != permit.fingerprint
        ):
            raise CampaignError("development continuation changed before native launch")
        _consumed_permit(json.loads(permit.request_json), engine.campaign_execution)

    engine._continue_campaign_development(task, prelaunch_validator=validate)
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
    return _compose(
        request,
        envelope,
        make_adapters=make_adapters,
        engine_cls=engine_cls,
        stories_engine_cls=stories_engine_cls,
        tickets_engine_cls=tickets_engine_cls,
        role="review",
    )


def compose_continuation(
    request: dict[str, Any],
    envelope: CampaignExecution,
    *,
    make_adapters: Callable,
    engine_cls=None,
    stories_engine_cls=None,
    tickets_engine_cls=None,
) -> runsetup.ComposedRun:
    """One campaign-owned entry; ordinary legacy ticket selection stays closed."""
    role = request.get("handoff_state", {}).get("native_role")
    if role not in {"dev", "review"}:
        raise CampaignError("campaign continuation requires an exact native role")
    return _compose(
        request,
        envelope,
        make_adapters=make_adapters,
        engine_cls=engine_cls,
        stories_engine_cls=stories_engine_cls,
        tickets_engine_cls=tickets_engine_cls,
        role=role,
    )


def _compose(
    request, envelope, *, make_adapters, engine_cls, stories_engine_cls, tickets_engine_cls, role
) -> runsetup.ComposedRun:
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
        or handoff.get("native_role") != role
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
        or interruption["role"] != role
        or interruption["kind"] not in {"quota", "transport", "canceled"}
        or any(
            reported.get(key) != interruption[key]
            for key in ("kind", "role", "session_id", "task_id")
        )
        or receipt.get("sessions") != sessions
        or task.phase
        not in (
            {Phase.REVIEW_RUNNING, Phase.REVIEW_VERIFY, Phase.ESCALATED}
            if role == "review"
            else {Phase.DEV_RUNNING, Phase.DEV_VERIFY, Phase.ESCALATED}
        )
        or task.worktree_path
        or task.implemented_review is not None
        or task.stopped_native
        or task.defer_reason is not None
    ):
        raise CampaignError("review continuation requires an exact interrupted ordinary review")
    permit = None
    if role == "dev":
        if (
            original.paused_cause is None
            or interruption["kind"] not in {"quota", "transport"}
            or original.paused_cause.kind
            != {
                "quota": PauseKind.QUOTA_WAIT,
                "transport": PauseKind.TRANSPORT_FAILURE,
            }.get(interruption["kind"])
            or original.paused_story_key != selector
        ):
            raise CampaignError("development continuation requires a typed eligible child pause")
        permit = _consumed_permit(request, envelope)
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
    selected = request.get("review_candidate" if role == "review" else "candidate") or {}
    adapter = pol.adapter.review if role == "review" else pol.adapter.dev
    if (
        (role == "review" and (not pol.review.enabled or pol.limits.max_review_cycles <= 0))
        or (role == "dev" and pol.limits.max_dev_attempts <= 0)
        or selected.get("profile") != adapter.name
        or selected.get("model", "") != adapter.model
        or selected.get("effort", "") != adapter.effort
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
    if role == "review":
        campaign_review_continuation(task)
    else:
        campaign_development_continuation(task)
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
    common_permit = (
        envelope.campaign_id,
        envelope.unit_id,
        state.run_id,
        _hash(task.to_dict()),
        fingerprint,
        roots,
        _hash(pol.to_dict()),
    )
    if role == "review":
        engine._campaign_review_permit = ReviewPermit(*common_permit)
    else:
        assert permit is not None
        engine._campaign_continuation_permit = DevelopmentPermit(
            *common_permit,
            permit["permit_id"],
            original_envelope.directory,
            workspace,
            request["branch"],
            envelope.generation,
            envelope.attempt,
            envelope.config_revision,
            json.dumps(request, sort_keys=True),
        )
    if role == "review":
        journal.append(
            "campaign-review-handoff",
            story_key=selector,
            predecessor=original.run_id,
            handoff_id=request["handoff_id"],
        )
    else:
        journal.append(
            "campaign-development-handoff",
            story_key=selector,
            predecessor=original.run_id,
            handoff_id=request["handoff_id"],
        )
    return runsetup.ComposedRun(engine, state.run_id, run_dir, state, journal)
