"""Owner-authorized native GitHub publication with durable single submissions.

Normal and administrative submissions are separate recorded attempts. An
uncertain command is observed, never promoted into an administrative retry.
The CLI's success is not merge proof: only GitHub readback confirms delivery.
"""

from __future__ import annotations

import dataclasses
import json
import shutil
from pathlib import Path

from .campaign_github import GithubError, MutationIntent
from .campaign_publication import Candidate
from .childrun import run_argv
from .platform_util import atomic_write_text, file_lock, is_link_like


def prepare(
    controller,
    candidate,
    evidence,
    *,
    action_id,
    generation,
    independent_families=True,
    executable="gh",
):
    gate = controller.evaluate(
        candidate,
        evidence,
        controller.inspect(candidate.pr_number),
        merge_mode="direct-authorized",
        independent_families=independent_families,
    )
    if not gate.ready:
        raise GithubError("Publication held: " + "; ".join(gate.reasons))
    binary = shutil.which(executable)
    if not binary:
        raise GithubError("Native GitHub merge executable is unavailable")
    return MutationIntent(
        action_id,
        "native_merge",
        "COMMAND",
        "",
        {
            "candidate": dataclasses.asdict(candidate),
            "binary": str(Path(binary).resolve()),
            "allow_admin_merge": True,
            "merge_method": "merge",
        },
        generation=generation,
        coordinates={**dataclasses.asdict(candidate), "rules_fingerprint": gate.rules_fingerprint},
    )


def _load(path):
    if is_link_like(path) or not path.is_file() or path.stat().st_size > 1024 * 1024:
        raise GithubError("Native merge execution record is missing, redirected or oversized")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise GithubError("Native merge execution record is invalid")
    return value


def _save(path, value):
    atomic_write_text(path, json.dumps(value, sort_keys=True), follow_symlinks=False)


def policy_refusal(result) -> bool:
    """Recognize only gh's definitive refusal before a merge mutation.

    Generic transport/GraphQL errors and arbitrary nonzero exits are uncertain.
    This conservative classifier cannot authorize --admin after a timeout.
    """
    if result.timed_out or result.interrupted or result.returncode != 1:
        return False
    text = (str(result.stdout) + "\n" + str(result.stderr)).lower()
    return "is not mergeable: the base branch policy prohibits the merge" in text


def execute(
    intent,
    project,
    directory,
    controller,
    *,
    evidence,
    generation,
    independent_families=True,
    reconcile=False,
    runner=run_argv,
    timeout_s=120,
    evidence_provider=None,
):
    if (
        intent.kind != "native_merge"
        or intent.state != "sent_unknown"
        or intent.generation != generation
    ):
        raise GithubError("Native merge requires a persisted current-generation intent")
    candidate = Candidate(**intent.payload["candidate"])
    if (
        not controller.requirements.allow_admin_merge
        or controller.requirements.repository != candidate.repository
        or controller.gateway.repository != candidate.repository
        or intent.payload.get("allow_admin_merge") is not True
        or intent.payload.get("merge_method") != "merge"
        or not intent.coordinates
        or any(
            intent.coordinates.get(key) != value
            for key, value in dataclasses.asdict(candidate).items()
        )
    ):
        raise GithubError("Native merge repository/candidate authorization changed")
    directory = Path(directory)
    if directory.resolve().is_relative_to(Path(project).resolve()):
        raise GithubError("Native merge records must be outside the editing checkout")
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "submission.json"

    def merged(record):
        observed = controller.merge_status(candidate.pr_number, expected_head=candidate.head)
        if observed.state == "merged_pending_sync":
            return dataclasses.replace(
                intent,
                state="confirmed",
                error="",
                receipt={
                    "status": "merged",
                    "details": {"sha": observed.merge_commit},
                    "native_executor": True,
                    "execution": record,
                },
            )
        if observed.state == "held":
            return dataclasses.replace(intent, error=observed.reason)
        return None

    # Holding this per-intent lock never holds the coordinator lock or coding
    # capacity. The normal executor owns a bounded subprocess, not a shell.
    with file_lock(directory / "submission.lock"):
        record = _load(path) if path.exists() else None
        if record and record.get("payload") != intent.payload:
            raise GithubError("Native merge execution identity changed")
        if observed := merged(record):
            return observed
        if record and record.get("phase") in {
            "normal-submitted",
            "admin-submitted",
            "acknowledged",
        }:
            return dataclasses.replace(
                intent,
                error="Native merge outcome uncertain; awaiting GitHub readback",
                receipt={"execution": record},
            )
        if record is None and reconcile:
            return dataclasses.replace(
                intent, error="Native merge submission record unavailable; replacement forbidden"
            )
        if record is None:
            record = {"payload": intent.payload, "attempts": [], "phase": "prepared"}
            _save(path, record)
        if record.get("phase") not in {"prepared", "normal-refused"}:
            raise GithubError("Native merge record has an unknown submission phase")
        if evidence_provider is not None:
            evidence = evidence_provider()
        observation = controller.inspect(candidate.pr_number)
        gate = controller.evaluate(
            candidate,
            evidence,
            observation,
            merge_mode="direct-authorized",
            independent_families=independent_families,
        )
        if observation.base_head != candidate.base:
            # Both phases are positively unsent (or definitively refused).
            # Retire this candidate so normal integration/review can resume.
            return dataclasses.replace(
                intent,
                state="rejected",
                error="Canonical target moved before native merge",
                receipt={
                    "submission": "not-submitted",
                    "definitive": True,
                    "needs_integration": True,
                    "base": observation.base_head,
                    "execution": record,
                },
            )
        if not gate.ready:
            return dataclasses.replace(
                intent,
                error="Publication changed before native merge: " + "; ".join(gate.reasons),
                receipt={"submission": "not-submitted", "definitive": True},
            )
        mode = "admin" if record["phase"] == "normal-refused" else "normal"
        # No command is in flight in these phases. Newly observed rules may
        # safely replace older rules only after the complete gate passes them.
        record["rules_fingerprint"] = gate.rules_fingerprint
        argv = [
            intent.payload["binary"],
            "pr",
            "merge",
            str(candidate.pr_number),
            "--repo",
            candidate.repository,
            "--merge",
            "--match-head-commit",
            candidate.head,
        ]
        if mode == "admin":
            argv.append("--admin")
        record["phase"] = mode + "-submitted"
        _save(path, record)
        try:
            result = runner(argv, cwd=project, timeout=timeout_s, encoding="utf-8")
        except (OSError, ValueError) as exc:
            return dataclasses.replace(
                intent, error=f"Native merge outcome uncertain: {type(exc).__name__}"
            )
        attempt = {
            "mode": mode,
            "returncode": result.returncode,
            "timed_out": result.timed_out,
            "interrupted": result.interrupted,
            "stdout": str(result.stdout)[-16000:],
            "stderr": str(result.stderr)[-16000:],
        }
        record["attempts"].append(attempt)
        if mode == "normal" and policy_refusal(result):
            record["phase"] = "normal-refused"
        elif not result.timed_out and not result.interrupted and result.returncode == 0:
            record["phase"] = "acknowledged"
        _save(path, record)
        if observed := merged(record):
            return observed
    if record["phase"] == "normal-refused":
        # Reenter through fresh evidence/rules/holds before the separately
        # recorded administrative attempt. A crash here resumes the same phase.
        return execute(
            intent,
            project,
            directory,
            controller,
            evidence=evidence,
            generation=generation,
            independent_families=independent_families,
            runner=runner,
            timeout_s=timeout_s,
            evidence_provider=evidence_provider,
        )
    return dataclasses.replace(
        intent,
        error="Native merge awaits independent GitHub confirmation",
        receipt={"execution": record},
    )
