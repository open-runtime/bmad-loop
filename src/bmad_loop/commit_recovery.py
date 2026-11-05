"""Explicit retry of a failed commit window, retaining completed acceptance.

The operator's unchanged-HEAD attestation must agree with independent source
evidence from final verification. Existing accepted development, review and verify
records remain required. All evidence is checked again at commit-window entry.
"""

from __future__ import annotations

import json
import math
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from . import accepted_review, bmadconfig, frontmatter, policy, ticketstore, verify
from .journal import Journal, load_state, save_state, state_lock
from .model import Phase, RunState, StoryTask


def _require(condition: bool, reason: str) -> None:
    if not condition:
        raise ValueError(reason)


def _gate_policy(snapshot: dict[str, Any]) -> dict[str, Any]:
    keys = ("verify", "review", "gates", "workflows", "plugins", "tickets", "operator")
    return json.loads(json.dumps({key: snapshot.get(key) for key in keys}))


def source_snapshot(root: Path) -> dict[str, Any]:
    """Observe a review verifier's source without changing normal verification.

    An unavailable identity is recorded explicitly, not mistaken for a clean
    tree. It prevents commit-only recovery; it does not discard verifier output.
    """
    try:
        head = verify.rev_parse_head(root)
        return {
            "head": head,
            "tree": verify.revision_tree_oid(root, head),
            "clean": not verify.dirty_paths(root),
        }
    except (OSError, verify.GitError) as exc:
        return {"error": type(exc).__name__}


def _timestamp(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError("verification source timestamp is missing or invalid")
    return float(value)


def _verified_source(
    *,
    head: str,
    root: Path,
    run_dir: Path,
    key: str,
    review_task: str,
    checks: list[dict[str, Any]],
    following: list[dict[str, Any]],
    failure: dict[str, Any],
) -> dict[str, Any]:
    """Bind recovery to the final verifier, never solely to an operator SHA."""
    identity = {"head": head, "tree": verify.revision_tree_oid(root, head), "clean": True}
    if any("verification_source" in row for row in checks):
        expected = {"before": identity, "after": identity}
        _require(
            all(row.get("verification_source") == expected for row in checks),
            "verification source is missing, changed, dirty or belongs to another HEAD",
        )
        return {"kind": "review-verification", **identity}

    # Older engines did not record verifier identities. The sync-with-main
    # pre_commit hook did independently record its HEAD without changing source.
    # Admit that legacy record only when the matching successful hook ran after
    # this final verification and before this commit failure. A later main sync,
    # stale chain, unrelated task or merely attesting a new HEAD cannot qualify.
    try:
        document = json.loads((run_dir / "sync-with-main.json").read_text(encoding="utf-8"))
        chain = document["chains"][key]
        recorded = datetime.fromisoformat(chain["recorded_at"])
        _require(recorded.utcoffset() is not None, "verification source timestamp lacks a timezone")
        recorded_at = recorded.timestamp()
        _require(chain["chain_head"] == head, "verification source names another HEAD")
    except (OSError, ValueError, TypeError, KeyError, OverflowError) as exc:
        raise ValueError(f"independent verification source is unavailable: {exc}") from exc
    verified_at = _timestamp(checks[-1].get("ts"))
    failed_at = _timestamp(failure.get("ts"))
    hooks = [
        row
        for row in following
        if row.get("kind") == "plugin-hook"
        and row.get("plugin") == "sync-with-main"
        and row.get("stage") == "pre_commit"
        and row.get("rc") == 0
        and row.get("log_task") == review_task
    ]
    # The hook's ISO record has whole-second precision; journal timestamps have
    # fractions. Compare the same second while retaining exact journal ordering.
    _require(
        any(
            verified_at <= _timestamp(row.get("ts")) <= failed_at
            and math.floor(recorded_at) == math.floor(_timestamp(row.get("ts")))
            for row in hooks
        ),
        "verification source has no matching hook between final verification and failure",
    )
    return {"kind": "legacy-pre-commit", **identity, "recorded_at": recorded_at}


def evidence(
    state: RunState,
    task: StoryTask,
    head: str,
    *,
    run_dir: Path,
    project_root: Path,
    resuming: bool = False,
) -> dict[str, Any]:
    """Read the exact acceptance being retained; never change source or state."""
    from . import runs

    key = task.story_key
    _require(state.source == "tickets" and state.run_type == "story", "requires a ticket run")
    _require(not state.finished, "the run is finished")
    _require(resuming or state.stopped, "the run must be stopped")
    _require(
        resuming
        or (runs.escalation_resolvable(state) and runs.escalation_story_admitted(state, key)),
        "another pause must be resolved before retrying finalization",
    )
    _require(
        task.phase == (Phase.COMMITTING if resuming else Phase.ESCALATED),
        "the task is not at a failed finalization boundary",
    )
    _require(
        state.tasks.get(key) is task and next(reversed(state.tasks)) == key,
        "a later task exists or this task does not belong to the run",
    )
    _require(not task.worktree_path and not task.branch, "requires an in-place ticket")
    _require(
        not any(
            (
                task.restore_patch,
                task.adopt_pending,
                task.operator_actions,
                task.plan_review_owed,
                task.plan_checkpoint_pending,
                task.reverify_from,
                task.reverify_replayed,
                task.implemented_review,
                task.dw_ids,
            )
        ),
        "another recovery, review or operator obligation is pending",
    )
    _require(not task.followup_review_recommended, "a follow-up review is still owed")
    _require(
        bool(re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", head)),
        "attest the full unchanged reviewed HEAD",
    )
    pol = policy.load(project_root / policy.POLICY_FILE)
    gate_policy = _gate_policy(pol.to_dict())
    if not resuming:
        _require(
            bool(state.policy_snapshot) and _gate_policy(state.policy_snapshot) == gate_policy,
            "verification or gate policy changed since the recorded run",
        )
    _require(pol.scm.isolation == "none", "requires live in-place policy")
    conflict = ticketstore.store_conflict(
        project_root,
        state.source,
        pol.scm.isolation,
        recorded=runs.live_store_topology(state, project_root) or None,
        initiative=state.initiative if state.parent_initiative is not None else None,
    )
    _require(conflict is None, conflict or "the ticket store changed")
    paths = runs.paths_for_state(bmadconfig.load_paths(project_root, want_tickets=True), state)
    root = paths.repo_root.resolve()
    _require(
        runs.rebase_recorded_code_root(state, project_root).resolve() == root,
        "the recorded code root changed",
    )
    _require(verify.rev_parse_head(root) == head, "HEAD changed from the attested reviewed commit")
    _require(not verify.dirty_paths(root), "the attested checkout must be clean")
    _require(task.ticket is not None and bool(task.spec_file), "no bound ticket plan")
    assert task.ticket is not None
    plan = runs.live_spec_path(task, state, project_root).resolve(strict=True)
    bound = runs.rebase_recorded_project_path(Path(task.ticket.plan), state, project_root)
    _require(
        plan == bound.resolve(strict=True) and plan.is_relative_to(root),
        "the bound ticket plan changed or is outside the code checkout",
    )
    rel = plan.relative_to(root).as_posix()
    _require(
        rel in verify.revision_blob_oids(root, head, (rel,)),
        "the ticket plan must be tracked in the attested HEAD",
    )
    fm = frontmatter.read_frontmatter(plan)
    _require(
        task.ticket.ref == key and str(fm.get("ticket")) == str(task.ticket.id),
        "the plan names another ticket",
    )
    _require(
        frontmatter.status_of(fm) == "built", "the plan is not built and awaiting finalization"
    )
    _require(
        frontmatter.auto_dev_baseline_of(fm) == task.baseline_commit, "the plan baseline changed"
    )

    index = task.accepted_dev_session_index
    _require(
        type(index) is int and 0 <= index < len(task.sessions), "no recorded accepted development"
    )
    assert isinstance(index, int)
    accepted = task.sessions[index]
    review = task.sessions[-1]
    for record, role in ((accepted, "dev"), (review, "review")):
        result = record.result_json
        _require(
            record.role == role
            and record.status == "completed"
            and not record.label
            and bool(record.session_id)
            and isinstance(result, dict),
            f"no completed {role} acceptance",
        )
        assert isinstance(result, dict)
        _require(
            result.get("workflow") == "auto-dev"
            and result.get("story_key") == key
            and result.get("status") == "built"
            and not result.get("escalations")
            and not result.get("park_asserted")
            and not result.get("artifact_only")
            and (role != "review" or not result.get("followup_review_recommended")),
            f"the {role} result is not accepted built work",
        )
        claimed = runs.rebase_recorded_project_path(
            verify.resolve_spec_path(str(result.get("spec_file", "")), paths), state, project_root
        )
        _require(
            claimed.resolve() == plan and result.get("baseline_commit") == task.baseline_commit,
            f"the {role} acceptance belongs to another plan or baseline",
        )
    _require(index < len(task.sessions) - 1, "the accepted work has no subsequent review")
    _require(
        accepted_review._accepted_proceeded(state, task, run_dir),
        "no successful decision belongs to the accepted development",
    )

    rows = Journal(run_dir, pin=state).entries()
    _require(
        all(isinstance(r, dict) and r.get("kind") != "journal-line-unreadable" for r in rows),
        "the acceptance journal cannot be read completely",
    )
    failures = [
        i
        for i, row in enumerate(rows)
        if row.get("kind") == "story-escalated" and row.get("story_key") == key
    ]
    _require(bool(failures), "no recorded finalization commit failure")
    end = failures[-1]
    failure = rows[end]
    _require(
        str(failure.get("reason", "")).startswith("commit failed:"),
        "the latest escalation is not a commit failure",
    )
    reviews = [
        i
        for i, row in enumerate(rows[:end])
        if row.get("kind") == "review-result" and row.get("story_key") == key
    ]
    _require(bool(reviews), "no completed review result")
    start = reviews[-1]
    review_row = rows[start]
    _require(
        review_row.get("log_task") == review.task_id
        and review_row.get("status") == "built"
        and review_row.get("cycle") == task.review_cycle
        and not review_row.get("followup_review_recommended"),
        "the latest review did not converge on this attempt",
    )
    _require(
        any(
            row.get("kind") == "session-end"
            and row.get("task_id") == review.task_id
            and row.get("status") == "completed"
            for row in rows[:start]
        ),
        "the review session did not complete",
    )
    bindings = [
        row
        for row in rows[:start]
        if row.get("kind") == "session-bound" and row.get("task_id") == review.task_id
    ]
    _require(
        bool(bindings) and bindings[-1].get("session_id") == review.session_id,
        "the completed review has no matching native binding",
    )
    _require(
        not any(
            row.get("kind") == "session-start" and str(row.get("task_id", "")).startswith(key + "-")
            for row in rows[start + 1 :]
        ),
        "another session ran after this review",
    )
    checks = [
        row
        for row in rows[start + 1 : end]
        if row.get("kind") == "verify-command-result" and row.get("story_key") == key
    ]
    commands = list(pol.verify.commands)
    _require(
        bool(commands) and len(checks) == len(commands),
        "the final verification sequence is incomplete",
    )
    sequence = checks[0].get("verification_sequence")
    _require(
        type(sequence) is int
        and all(
            row.get("verification_stage") == "review"
            and row.get("verification_sequence") == sequence
            and row.get("attempt") == task.attempt
            and row.get("command_index") == i
            and row.get("command") == command
            and row.get("returncode") == 0
            and not row.get("spawn_error")
            and not row.get("capture_error")
            for i, (row, command) in enumerate(zip(checks, commands, strict=True))
        ),
        "the final verification sequence failed or changed",
    )
    _require(
        not any(
            row.get("kind") == "review-verify-failed" and row.get("story_key") == key
            for row in rows[start + 1 :]
        ),
        "a verification failure remains after the review",
    )
    source = _verified_source(
        head=head,
        root=root,
        run_dir=run_dir,
        key=key,
        review_task=review.task_id,
        checks=checks,
        following=rows[rows.index(checks[-1]) + 1 : end],
        failure=failure,
    )
    return {
        "version": 1,
        "head": head,
        "baseline": task.baseline_commit,
        "project": str(project_root.resolve()),
        "code_root": str(root),
        "plan": str(plan),
        "accepted": accepted.to_dict(),
        "review": review.to_dict(),
        "commands": commands,
        "verification_sequence": sequence,
        "verification_source": source,
        "gate_policy": gate_policy,
        "escalation_time": failure.get("ts"),
    }


def arm(
    run_dir: Path,
    story_key: str,
    head: str,
    *,
    project_root: Path,
    expected_evidence: dict[str, Any] | None = None,
) -> None:
    """Re-enter COMMITTING under the normal run lock without inventing acceptance."""
    from . import runs

    with state_lock(run_dir):
        state = load_state(run_dir)
        if runs.engine_liveness(run_dir) != "dead":
            raise runs.RearmError("the engine must be demonstrably stopped")
        task = state.tasks.get(story_key)
        if task is None:
            raise runs.RearmError("no task for the named story")
        try:
            observed = evidence(state, task, head, run_dir=run_dir, project_root=project_root)
            _require(
                expected_evidence is None or observed == expected_evidence,
                "the finalization evidence changed during confirmation",
            )
        except (
            ValueError,
            OSError,
            bmadconfig.BmadConfigError,
            policy.PolicyError,
            verify.GitError,
        ) as exc:
            raise runs.RearmError(str(exc)) from exc
        journal = Journal(run_dir, pin=state)
        runs.reconcile_root_identities(state, run_dir, journal, project_root)
        # Explicit recovery assignment, as in adopt_escalated_branch; this is not
        # an ordinary ESCALATED transition, and no acceptance counters are reset.
        task.phase = Phase.COMMITTING
        task.commit_retry = observed
        save_state(run_dir, state)
        journal.append("commit-retry-armed", story_key=story_key, head=head)


def validate_entry(state: RunState, task: StoryTask, *, run_dir: Path, project_root: Path) -> None:
    """Reject changes made between the explicit attestation and commit entry."""
    expected = task.commit_retry
    _require(isinstance(expected, dict), "missing commit retry attestation")
    assert isinstance(expected, dict)
    observed = evidence(
        state,
        task,
        str(expected.get("head", "")),
        run_dir=run_dir,
        project_root=project_root,
        resuming=True,
    )
    _require(observed == expected, "the finalization evidence changed before resume")
