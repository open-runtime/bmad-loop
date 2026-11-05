"""Explicit, head-bound recovery of previously accepted ticket implementation.

An operator supplies the implementation commit; it is never inferred from agent
prose. Historical acceptance, surviving source content and a quiescent tree are
all required. This authorizes an owed primary review, not acceptance of HEAD.
"""

from __future__ import annotations

import hashlib
import json
import re
import stat
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import bmadconfig, devcontract, frontmatter, policy, ticketstore, tickettree, verify
from .journal import Journal, load_state, save_state, state_lock
from .model import Phase, RunState, SessionRecord, StoryTask
from .platform_util import session_task_id

if TYPE_CHECKING:
    from .runs import RearmOutcome


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def _plan_digest(plan: Path) -> str:
    """Bind all plan semantics and its verbatim body except the review status.

    The only write recovery makes is the maintained in-review transition. Its
    idempotent replay must survive a host loss between that write and dispatch.
    """
    return _plan_content_digest(plan.read_bytes())


def _plan_content_digest(raw: bytes) -> str:
    text = raw.decode("utf-8")
    split = frontmatter._split_frontmatter(text)
    if split is None or frontmatter.frontmatter_fault(text):
        raise ValueError("the bound plan has invalid frontmatter")
    fields = frontmatter.parse_frontmatter(text)
    fields.pop("status", None)
    return _digest([fields, split[2]])


def _ledger_evidence(path: Path) -> dict[str, Any]:
    """Bind even ignored ledger bytes; check dirt in the actual owning repo."""
    if path.is_symlink():
        raise ValueError("the deferred-work ledger is a symlink")
    resolved = path.resolve()
    parent = resolved.parent
    while not parent.exists() and parent != parent.parent:
        parent = parent.parent
    owner = verify.git_toplevel(parent)
    if owner is not None and not verify.path_clean(owner, resolved.relative_to(owner).as_posix()):
        raise ValueError("uncommitted ledger work must be preserved in its owning repository first")
    parent_stat = parent.stat()
    evidence: dict[str, Any] = {
        "path": str(path),
        "resolved": str(resolved),
        "owner": str(owner) if owner else None,
        "parent": [str(parent), parent_stat.st_dev, parent_stat.st_ino],
    }
    try:
        before = path.lstat()
    except FileNotFoundError:
        evidence["exists"] = False
        return evidence
    if not stat.S_ISREG(before.st_mode):
        raise ValueError("the deferred-work ledger is not a regular file")
    data = path.read_bytes()
    after = path.lstat()

    def identity(value):
        return (value.st_dev, value.st_ino, value.st_mode, value.st_size, value.st_mtime_ns)

    if identity(before) != identity(after):
        raise ValueError("the deferred-work ledger changed while being read")
    evidence.update(
        exists=True,
        identity=[before.st_dev, before.st_ino, before.st_mode],
        sha256=hashlib.sha256(data).hexdigest(),
    )
    return evidence


def _accepted(task: StoryTask) -> SessionRecord | None:
    index = task.accepted_dev_session_index
    if type(index) is not int or not 0 <= index < len(task.sessions):
        return None
    return task.sessions[index]


def _accepted_proceeded(state: RunState, task: StoryTask, run_dir: Path) -> bool:
    """Pair the decision with its exact session-end, not a reused attempt number."""
    accepted = _accepted(task)
    if accepted is None:
        return False
    current = ""
    ended = False
    bound = None
    entries = Journal(run_dir, pin=state).entries()
    if any(
        not isinstance(entry, dict) or entry.get("kind") == "journal-line-unreadable"
        for entry in entries
    ):
        return False
    for entry in entries:
        kind = entry.get("kind")
        task_id = str(entry.get("task_id", ""))
        if kind == "session-start" and task_id.startswith(f"{task.story_key}-dev-"):
            current, ended, bound = task_id, False, None
        elif kind == "session-bound" and task_id == current:
            bound = entry.get("session_id")
        elif kind == "session-end" and task_id == current:
            ended = entry.get("status") == "completed"
        elif kind == "dev-decision" and entry.get("story_key") == task.story_key:
            if current == accepted.task_id and ended:
                return (
                    bool(accepted.session_id)
                    and bound == accepted.session_id
                    and entry.get("action") == "proceed"
                    and entry.get("session_status") == "completed"
                    and entry.get("produced_work") is True
                    and not entry.get("env_fault")
                )
            ended = False
    return False


def _evidence(
    state: RunState,
    task: StoryTask,
    story_key: str,
    implementation_commit: str,
    *,
    run_dir: Path,
    project_root: Path,
    require_stopped: bool,
    source_generation: int | None = None,
) -> dict[str, Any]:
    from . import runs

    def require(condition: bool, why: str) -> None:
        if not condition:
            raise ValueError(why)

    require(state.source == "tickets", "implemented review requires a tickets run")
    require(not state.finished, "the run is finished")
    require(not require_stopped or state.stopped, "stop the run before recovering its review")
    require(not task.worktree_path, "implemented review requires an in-place ticket")
    require(
        task.phase
        in (
            (Phase.DEV_RUNNING, Phase.DEV_VERIFY)
            if require_stopped
            else (Phase.DEV_RUNNING, Phase.DEV_VERIFY, Phase.REVIEW_RUNNING, Phase.REVIEW_VERIFY)
        ),
        "the task is not at a recoverable dev boundary",
    )
    require(
        bool(state.tasks) and next(reversed(state.tasks)) == story_key,
        "a later task already exists in this run",
    )
    require(
        state.tasks.get(story_key) is task and task.story_key == story_key,
        "the task does not belong to this run",
    )
    require(
        not task.plan_review_owed and not task.plan_checkpoint_pending,
        "a planning checkpoint is still owed",
    )
    require(
        not task.restore_patch and not task.operator_actions and not task.adopt_pending,
        "another recovery or operator obligation is pending",
    )
    require(
        Path(state.project).resolve() == project_root.resolve(),
        "the recorded project differs from this checkout",
    )
    live_policy = policy.load(project_root / policy.POLICY_FILE)
    require(live_policy.scm.isolation == "none", "implemented review requires live in-place policy")
    store_conflict = ticketstore.store_conflict(
        project_root,
        state.source,
        live_policy.scm.isolation,
        recorded=runs.live_store_topology(state, project_root) or None,
        initiative=state.initiative if state.parent_initiative is not None else None,
    )
    require(store_conflict is None, store_conflict or "the ticket store cannot be verified")
    paths = runs.paths_for_state(bmadconfig.load_paths(project_root, want_tickets=True), state)
    topology = ticketstore.assess_store(
        paths,
        live_policy.scm.isolation,
        chained=state.parent_initiative is not None,
    )
    require(
        isinstance(topology, ticketstore.StoreTopology), "the ticket store cannot be classified"
    )
    assert isinstance(topology, ticketstore.StoreTopology)
    # Compare the exact assessment being pinned too: configuration may have
    # changed since store_conflict's read, and an old plan can still exist.
    assessed_conflict = ticketstore.store_topology_conflict(
        topology,
        recorded=runs.live_store_topology(state, project_root) or None,
    )
    require(assessed_conflict is None, assessed_conflict or "the ticket store changed")
    root = paths.repo_root.resolve()
    require(state.code_root.resolve() == root, "the recorded code root changed")
    require(task.ticket is not None and task.spec_file is not None, "no bound ticket plan")
    assert task.ticket is not None
    plan = runs.task_spec_path(task, state).resolve(strict=True)
    require(plan == Path(task.ticket.plan).resolve(strict=True), "the bound plan changed")
    require(plan.is_relative_to(root), "the plan is outside the code checkout")
    pin, folder = runs.tree_pin(state, paths)
    try:
        found = tickettree.TicketTree(project_root, pinned_initiative=pin, folder=folder).find(
            task.ticket.ref
        )
    except tickettree.TicketsError as exc:
        raise ValueError(f"the live ticket reference cannot be verified: {exc}") from exc
    require(
        found.row.ref == task.ticket.ref
        and found.row.id == str(task.ticket.id)
        and Path(found.plan).resolve(strict=True) == plan,
        "the live ticket reference no longer names the bound plan",
    )
    fm = frontmatter.parse_frontmatter(plan.read_bytes().decode("utf-8"))
    require(str(fm.get("ticket")) == str(task.ticket.id), "the plan names another ticket")
    require(task.ticket.ref == story_key, "the ticket binding names another story")
    require(
        frontmatter.status_of(fm) in {"in-progress", "in-review", "built"},
        "the plan is not an implemented ticket awaiting review",
    )
    require(
        frontmatter.auto_dev_baseline_of(fm) == task.baseline_commit,
        "the plan does not name the current task baseline",
    )
    accepted = _accepted(task)
    require(accepted is not None, "no recorded accepted dev session")
    assert accepted is not None
    require(isinstance(accepted.result_json, dict), "the accepted result is not an object")
    result = accepted.result_json
    assert isinstance(result, dict)
    require(
        accepted.role == "dev" and not accepted.label and accepted.status == "completed",
        "the accepted pointer does not name a completed unlabelled dev",
    )
    require(
        bool(re.fullmatch(re.escape(story_key) + r"-dev-\d+(?:-g\d+)?", accepted.task_id)),
        "the accepted session belongs to another story",
    )
    require(
        result.get("workflow") == "auto-dev"
        and result.get("story_key") == story_key
        and result.get("status") == "built"
        and not result.get("artifact_only")
        and not result.get("park_asserted")
        and not result.get("escalations"),
        "the accepted result is not this story's built implementation",
    )
    claimed_plan = verify.resolve_spec_path(str(result.get("spec_file", "")), paths)
    require(claimed_plan.resolve() == plan, "the accepted result names another plan")
    require(
        _accepted_proceeded(state, task, run_dir),
        "no successful dev decision is attributable to the accepted session",
    )
    original = str(result.get("baseline_commit", ""))
    baseline = task.baseline_commit or ""
    generation = task.generation if source_generation is None else source_generation
    latest = next(
        (record for record in reversed(task.sessions) if record.role == "dev" and not record.label),
        None,
    )
    require(
        latest is not None
        and latest.status == "completed"
        and isinstance(latest.result_json, dict),
        "the latest dev has no completed result to hand to review",
    )
    assert latest is not None and isinstance(latest.result_json, dict)
    latest_result = latest.result_json
    suffix = f"-g{generation}" if generation else ""
    match = re.fullmatch(re.escape(story_key) + r"-dev-(\d+)" + re.escape(suffix), latest.task_id)
    require(
        match is not None and int(match[1]) <= task.attempt,
        "the latest dev does not belong to the current generation and attempt",
    )
    require(
        latest_result.get("workflow") == "auto-dev"
        and latest_result.get("story_key") == story_key
        and latest_result.get("status") == "built"
        and latest_result.get("baseline_commit") == baseline
        and not latest_result.get("escalations")
        and not latest_result.get("park_asserted")
        and not latest_result.get("artifact_only")
        and verify.resolve_spec_path(str(latest_result.get("spec_file", "")), paths).resolve()
        == plan,
        "the latest dev result does not validate this plan and baseline",
    )
    require(
        bool(re.fullmatch(r"[0-9a-f]{40}", implementation_commit)),
        "supply the full 40-character implementation commit",
    )
    require(
        verify.rev_parse_revision(root, implementation_commit) == implementation_commit,
        "the implementation object is not a commit",
    )
    head = verify.rev_parse_head(root)
    require(
        original != implementation_commit
        and verify.is_ancestor(root, original, implementation_commit),
        "the implementation does not follow the accepted dev baseline",
    )
    require(
        verify.is_ancestor(root, implementation_commit, baseline)
        and verify.is_ancestor(root, baseline, head),
        "the implementation is not retained in the current baseline and HEAD",
    )
    excluded = {plan.relative_to(root).as_posix()}
    ledger = paths.deferred_work.resolve()
    if ledger.is_relative_to(root):
        excluded.add(ledger.relative_to(root).as_posix())
    product_paths = verify.committed_changed_paths(root, original, implementation_commit) - excluded
    require(bool(product_paths), "the implementation commit contains only plan or ledger changes")
    for revision in {baseline, head}:
        require(
            not (
                verify.committed_changed_paths(root, implementation_commit, revision)
                & product_paths
            ),
            "the recorded implementation is no longer present in the retained source",
        )
    dirty = verify.dirty_paths(root)
    # dirty_paths names only rename/copy destinations. A destination equal to
    # the plan must not conceal an uncommitted source removal from this proof.
    require(
        not any("R" in status or "C" in status for status in dirty.values()),
        "uncommitted renames or copies must be preserved first",
    )
    require(
        not (set(dirty) - {plan.relative_to(root).as_posix()}),
        "uncommitted source or ledger outside the bound plan must be preserved first",
    )
    require(
        live_policy.review.enabled and live_policy.limits.max_review_cycles > 0,
        "implemented review requires an enabled primary review and a review budget",
    )
    return {
        "version": 1,
        "store_topology": topology.to_dict(),
        "active_initiative": paths.active_initiative,
        "implementation_commit": implementation_commit,
        "head": head,
        "baseline": baseline,
        "original_baseline": original,
        "source_generation": generation,
        "source_attempt": task.attempt,
        "accepted_dev_session_index": task.accepted_dev_session_index,
        "accepted_task_id": accepted.task_id,
        "accepted_native_id": accepted.session_id,
        "accepted_digest": _digest(accepted.to_dict()),
        "latest_task_id": latest.task_id,
        "latest_digest": _digest(latest.to_dict()),
        "plan_digest": _plan_digest(plan),
        "ledger": _ledger_evidence(paths.deferred_work),
        "story_key": story_key,
    }


def review_implemented_refusal(
    state: RunState,
    task: StoryTask,
    story_key: str,
    implementation_commit: str,
    *,
    run_dir: Path,
    project_root: Path,
) -> str | None:
    """Explain why explicit recovery cannot attribute and retain this implementation."""
    try:
        _evidence(
            state,
            task,
            story_key,
            implementation_commit,
            run_dir=run_dir,
            project_root=project_root,
            require_stopped=True,
        )
    except (
        ValueError,
        OSError,
        bmadconfig.BmadConfigError,
        verify.GitError,
        policy.PolicyError,
    ) as exc:
        return str(exc)
    return None


def rearm_for_implemented_review(
    run_dir: Path,
    story_key: str,
    implementation_commit: str,
    *,
    project_root: Path,
    expected_evidence: dict[str, Any] | None = None,
) -> RearmOutcome:
    """Persist the operator's attestation under the run lock; never write source.

    The next engine prepares the plan through TicketTree.mark and runs its normal
    primary and review verification. The accepted index, sessions and baseline
    remain historical facts, not a synthetic current-generation acceptance.
    """
    from . import runs

    with state_lock(run_dir):
        state = load_state(run_dir)
        if runs.engine_liveness(run_dir) != "dead":
            raise runs.RearmError("the engine is not demonstrably stopped")
        task = state.tasks.get(story_key)
        if task is None:
            raise runs.RearmError("no task for the named story")
        try:
            evidence = _evidence(
                state,
                task,
                story_key,
                implementation_commit,
                run_dir=run_dir,
                project_root=project_root,
                require_stopped=True,
            )
            if expected_evidence is not None and evidence != expected_evidence:
                raise ValueError("the recovery evidence changed during confirmation")
        except (
            ValueError,
            OSError,
            bmadconfig.BmadConfigError,
            verify.GitError,
            policy.PolicyError,
        ) as exc:
            raise runs.RearmError(str(exc)) from exc
        journal = Journal(run_dir, pin=state)
        runs.reconcile_root_identities(state, run_dir, journal, project_root)
        # Explicit recovery transition, like rearm_for_reverify. No completed
        # session is invented and no earlier accepted pointer is relabelled.
        task.phase = Phase.DEV_VERIFY
        task.generation += 1
        task.review_cycle = task.review_usage_relaunches = task.followup_reviews_spent = 0
        task.review_unlaunched_cycles = 0
        task.reverify_from = task.reverify_replayed = ""
        task.rearmed = False
        task.defer_reason = task.env_fault_site = None
        task.implemented_review = evidence
        save_state(run_dir, state)
        journal.append(
            "implemented-review-armed",
            story_key=story_key,
            commit=implementation_commit,
            head=evidence["head"],
            task_id=evidence["accepted_task_id"],
        )
    return runs.RearmOutcome(story_key, (), False)


def prepared_plan_evidence(raw: bytes, original_digest: str) -> dict[str, str]:
    """Pin both exact controller forms before the normal marker-removal write."""
    prepared = devcontract.without_auto_run_result(raw.decode("utf-8")).encode("utf-8")
    return {
        "original_digest": original_digest,
        "source_sha256": hashlib.sha256(raw).hexdigest(),
        "prepared_sha256": hashlib.sha256(prepared).hexdigest(),
        "prepared_digest": _plan_content_digest(prepared),
    }


def pending_review_refusal(
    state: RunState,
    task: StoryTask,
    run_dir: Path,
    project_root: Path,
    *,
    require_prepared: bool = False,
) -> str | None:
    """Recheck source, topology and result authority through first native dispatch.

    Preparation records exact before/after plan forms before the controller's
    marker removal. Replay accepts only those two forms; launch requires the
    prepared form. Neither arbitrary marker edits nor other plan edits qualify.
    """
    held = task.implemented_review
    if not isinstance(held, dict) or held.get("version") != 1:
        return "missing or unsupported implemented-review attestation"
    if (
        type(held.get("source_generation")) is not int
        or task.generation != held["source_generation"] + 1
    ):
        return "the recovery generation changed after the attestation"
    try:
        current = _evidence(
            state,
            task,
            task.story_key,
            str(held.get("implementation_commit", "")),
            run_dir=run_dir,
            project_root=project_root,
            require_stopped=False,
            source_generation=held["source_generation"],
        )
        expected = dict(held)
        prepared = expected.pop("prepared_plan", None)
        if prepared is not None:
            from .runs import task_spec_path

            if not isinstance(prepared, dict) or prepared.get("original_digest") != held.get(
                "plan_digest"
            ):
                return "invalid controller plan preparation"
            plan_sha = hashlib.sha256(task_spec_path(task, state).read_bytes()).hexdigest()
            if plan_sha == prepared.get("prepared_sha256"):
                expected["plan_digest"] = prepared.get("prepared_digest")
            elif require_prepared or plan_sha != prepared.get("source_sha256"):
                return "the bound plan changed after review launch preparation"
        elif require_prepared:
            return "missing controller plan preparation"
        if current != expected:
            return "the source, plan or accepted-session evidence changed after recovery was armed"
    except (
        ValueError,
        OSError,
        bmadconfig.BmadConfigError,
        verify.GitError,
        policy.PolicyError,
    ) as exc:
        return str(exc)
    return None


def primary_was_bound(state: RunState, task: StoryTask, run_dir: Path) -> bool:
    """Whether this recovery actually dispatched a native primary before a crash."""
    entries = Journal(run_dir, pin=state).entries()
    if any(not isinstance(e, dict) or e.get("kind") == "journal-line-unreadable" for e in entries):
        return False
    pattern = re.escape(task.story_key) + r"-review-\d+-g" + str(task.generation)
    return any(
        e.get("kind") == "session-bound"
        and e.get("session_id")
        and re.fullmatch(pattern, str(e.get("task_id", "")))
        for e in entries
    )


def review_reservation_unlaunched(state: RunState, task: StoryTask, run_dir: Path) -> bool:
    """Refund only an exact reserved identity with no launch or result evidence."""
    if task.review_cycle <= 0:
        return False
    task_id = session_task_id(task.story_key, "review", task.review_cycle, task.generation)
    if any(record.task_id == task_id for record in task.sessions):
        return False
    entries = Journal(run_dir, pin=state).entries()
    return not any(
        not isinstance(entry, dict)
        or entry.get("kind") == "journal-line-unreadable"
        or entry.get("task_id") == task_id
        for entry in entries
    )
