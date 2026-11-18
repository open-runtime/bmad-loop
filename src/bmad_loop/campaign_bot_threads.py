"""Link configured GitHub bot findings to verified repairs and a fresh reviewer.

GitHub's resolveReviewThread mutation takes no expected head or comment revision.
The campaign can recommend a resolution and observe the maintainer's action. It
cannot atomically resolve only the reviewed comments, so it issues no mutation.
"""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path

from . import verify
from .campaign_acceptance import receipts_current
from .campaign_headless import provider_family
from .campaign_model import Hold, UnitState
from .campaign_transcripts import digest


def revision(thread, allowed):
    """Require complete, stable native bot/comment identities on every page."""
    connection = thread.get("comments") or {}
    comments = connection.get("nodes")
    if (
        not allowed
        or not thread.get("id")
        or not thread.get("path")
        or not isinstance(comments, list)
        or not comments
        or type(connection.get("totalCount")) is not int
        or len(comments) != connection["totalCount"]
        or (connection.get("pageInfo") or {}).get("hasNextPage") is not False
    ):
        return None
    ids = set()
    for comment in comments:
        if not isinstance(comment, dict):
            return None
        actor = comment.get("author") or {}
        if (
            not isinstance(actor, dict)
            or actor.get("__typename") != "Bot"
            or actor.get("id") not in allowed
            or not isinstance(comment.get("id"), str)
            or not comment["id"]
            or comment["id"] in ids
            or not isinstance(comment.get("body"), str)
            or not comment.get("updatedAt")
        ):
            return None
        ids.add(comment["id"])
    return digest([thread["id"], thread["path"], comments])


def record_repair(snapshot, unit, result):
    """Retain the exact accepted repair's native finding before repair cleanup."""
    allowed = snapshot.spec.github.verified_thread_bot_ids
    feedback = unit.evidence.get("remote_feedback") or {}
    origin = feedback.get("candidate") or {}
    repair = unit.evidence.get("repair_candidate") or {}
    accepted = unit.evidence.get("accepted_resolution") or {}
    pr = unit.bindings.get("pr")
    if (
        not allowed
        or not feedback.get("digest")
        or accepted.get("cause_id") != feedback["digest"]
        or not repair.get("receipt")
        or repair.get("resolution_id") != result["proposal_id"]
        or repair.get("head") != result["head"]
        or repair.get("parent") != result["parent_head"]
        or repair["head"] == repair["parent"]
        or origin.get("head") != repair["parent"]
        or origin.get("unit_id") != unit.id
        or origin.get("repository") != snapshot.repository
        or not isinstance(pr, dict)
        or origin.get("pr_number") != pr.get("number")
        or not result.get("recovery_push_confirmed")
    ):
        return
    for finding in feedback.get("defects", ()):
        if finding.get("kind") != "review_thread" or finding.get("path") not in repair.get(
            "changed_paths", ()
        ):
            continue
        fingerprint = revision(finding, allowed)
        if fingerprint:
            unit.evidence.setdefault("bot_thread_repairs", {})[finding["id"]] = {
                "id": finding["id"],
                "revision": fingerprint,
                "path": finding["path"],
                "original": origin,
                "fix_head": repair["head"],
                "repair_launch": repair["receipt"],
            }


def review_context(unit):
    repairs = unit.evidence.get("bot_thread_repairs")
    if not repairs:
        return {}
    return {
        "bot_thread_repairs": list(repairs.values()),
        "bot_thread_review_instruction": (
            "For each listed repair inspect the original finding, complete current conversation, "
            "actual fix and current code. If a successful recorded verification command exercises "
            "the reported behavior, include a findings row with id, revision, fix_head, resolved "
            "true, verification_launch_id, command_index and a reason explaining how that command "
            "and fix address this particular finding. Leave unresolved findings explicit."
        ),
    }


def reviewed(runtime, snapshot, unit, candidate, threads):
    """Validate exact repair, independent current-head assessment and test receipt."""
    links = unit.evidence.get("bot_thread_repairs") or {}
    if not links or not snapshot.spec.github.verified_thread_bot_ids:
        return {}
    evidence = runtime._publication_evidence(snapshot, unit)
    review = unit.evidence.get("review") or {}
    verifier = unit.evidence.get("verification") or {}
    writers = set(evidence.developer_families) | {evidence.developer_family}
    writers.discard("")
    if (
        not writers
        or not evidence.reviewer_family
        or evidence.reviewer_family != provider_family(evidence.observed_model)
        or evidence.reviewer_family in writers
        or evidence.attribution_incomplete
        or not evidence.review_approved
        or not evidence.review_receipt
        or not evidence.verified
        or not evidence.writer_quiescent
        or not evidence.clean
        or not verifier.get("launch_id")
        or not receipts_current(verifier, review, candidate)
    ):
        return {}
    approved = {}
    findings = review.get("artifact", {}).get("findings", [])
    for thread in threads:
        link = links.get(thread.get("id"))
        if (
            not link
            or revision(thread, snapshot.spec.github.verified_thread_bot_ids) != link["revision"]
        ):
            continue
        origin = link["original"]
        if any(
            origin.get(key) != getattr(candidate, key)
            for key in ("repository", "pr_number", "target", "unit_id")
        ):
            continue
        matching = [
            row for row in findings if isinstance(row, dict) and row.get("id") == link["id"]
        ]
        if len(matching) != 1:
            continue
        finding = matching[0]
        commands = verifier.get("commands", ())
        index = finding.get("command_index")
        if (
            finding.get("revision") != link["revision"]
            or finding.get("fix_head") != link["fix_head"]
            or finding.get("resolved") is not True
            or not isinstance(finding.get("reason"), str)
            or not finding["reason"].strip()
            or finding.get("verification_launch_id") != verifier["launch_id"]
            or type(index) is not int
            or not 0 <= index < len(commands)
            or commands[index].get("returncode") != 0
            or any(commands[index].get(key) for key in ("timed_out", "interrupted", "spawn_error"))
            or not verify.is_ancestor(
                Path(unit.bindings["workspace"]), link["fix_head"], candidate.head
            )
        ):
            continue
        approved[link["id"]] = {
            "id": link["id"],
            "revision": link["revision"],
            "fix_head": link["fix_head"],
            "review_launch": evidence.review_receipt,
            "verification_launch": verifier["launch_id"],
            "command_index": index,
            "reason": finding["reason"],
        }
    return approved


def disposition(runtime, snapshot, unit, candidate, observation, feedback, *, now):
    pr = observation.pr
    credited = unit.evidence.get("bot_thread_dispositions", ())
    if (
        (not unit.evidence.get("bot_thread_repairs") and not credited)
        or observation.base_head != candidate.base
        or observation.repository != candidate.repository
        or pr.get("number") != candidate.pr_number
        or pr.get("head", {}).get("sha") != candidate.head
        or pr.get("base", {}).get("ref") != candidate.target
        or pr.get("base", {}).get("repo", {}).get("full_name") != candidate.repository
        or pr.get("state") != "open"
        or pr.get("draft") is not False
        or pr.get("merged") is not False
        or not observation.rules.complete
        or not 0 <= now - observation.observed_at <= 30
    ):
        return None
    from .campaign_native_review_flow import accepted as pair_accepted
    from .campaign_review_freshness import current

    if not current(snapshot, unit) or not pair_accepted(snapshot, unit):
        return None
    threads = {row["id"]: row for row in observation.threads if row.get("id")}
    for proof in credited:
        if proof.get("superseded_by_feedback"):
            continue
        thread = threads.get(proof["id"], {})
        if revision(thread, snapshot.spec.github.verified_thread_bot_ids) == proof["revision"]:
            continue
        if unit.evidence["review"].get("launch_id") == proof["review_launch"]:
            return {"acceptance_stale": True}
        if thread.get("isResolved") is not False:
            # GitHub can display a resolved conversation with a newly appended
            # objection. An old resolution cannot establish its disposition.
            return {
                "bot_thread_objection": proof["id"],
                "candidate": asdict(candidate),
            }
    local = {
        row["id"]: row
        for row in unit.evidence.get("required_findings", [])
        if isinstance(row, dict) and row.get("kind") == "review_thread" and row.get("id")
    }
    if not local:
        return None
    approved = reviewed(runtime, snapshot, unit, candidate, observation.threads)
    local = {
        key: row
        for key, row in local.items()
        if key in approved
        and revision(row, snapshot.spec.github.verified_thread_bot_ids) == approved[key]["revision"]
    }
    if not local:
        return None
    closed = [
        approved[t["id"]]
        for t in observation.threads
        if t.get("id") in local and t.get("isResolved") is True
    ]
    if closed:
        return {"bot_threads_closed": closed, "candidate": asdict(candidate)}
    if feedback and all(
        row["kind"] == "review_thread" and row["id"] in local for row in feedback.defects
    ):
        return {
            "bot_thread_wait": {
                "candidate": asdict(candidate),
                "feedback": asdict(feedback),
                "findings": [approved[row["id"]] for row in feedback.defects],
            }
        }
    return None


def bind_new_feedback(unit, feedback):
    """A freshly bound, open objection takes ownership of its old resolution."""
    open_ids = {row["id"] for row in feedback.defects if row["kind"] == "review_thread"}
    for row in unit.evidence.get("bot_thread_dispositions", ()):
        if row["id"] in open_ids:
            row.setdefault("superseded_by_feedback", feedback.digest)


def changed_while_waiting(allowed, hold, observation):
    """Re-review concurrent edits even when somebody has already resolved the thread."""
    previous = hold.evidence.get("bot_thread_wait")
    if not previous:
        return False
    current = {t["id"]: t for t in observation.threads if t.get("id")}
    return observation.pr.get("head", {}).get("sha") != hold.evidence["candidate"]["head"] or any(
        revision(current.get(row["id"], {}), allowed) != row["revision"] for row in previous
    )


def apply(snapshot, unit, data, action_id):
    candidate = data.get("candidate") or data["bot_thread_wait"]["candidate"]
    if (
        unit.hold
        or unit.state != UnitState.READY_TO_PUBLISH
        or not unit.claim
        or not unit.claim.quiescent
        or unit.evidence.get("integrated", {}).get("head") != candidate["head"]
        or unit.evidence.get("integrated", {}).get("base") != candidate["base"]
        or unit.bindings.get("pr", {}).get("number") != candidate["pr_number"]
        or candidate["unit_id"] != unit.id
        or candidate["repository"] != snapshot.repository
        or str(snapshot.config_revision) != candidate["config_revision"]
        or candidate["graph_revision"] != snapshot.graph_revision
        or candidate["source_revision"] != unit.bindings.get("source_revision", "")
    ):
        return
    proofs = data.get("bot_threads_closed") or (data.get("bot_thread_wait") or {}).get(
        "findings", ()
    )
    if any(
        row["review_launch"] != unit.evidence.get("review", {}).get("launch_id")
        or row["verification_launch"] != unit.evidence.get("verification", {}).get("launch_id")
        for row in proofs
    ):
        return
    if data.get("bot_thread_objection"):
        unit.hold = Hold(
            "external_wait",
            f"Review thread {data['bot_thread_objection']} changed after its confirmed resolution. "
            "A maintainer must reopen the updated conversation on GitHub for fresh assessment.",
            id=action_id,
            actor="github",
            evidence=data,
        )
        return
    if "bot_threads_closed" in data:
        links = unit.evidence.get("bot_thread_repairs", {})
        bound = {row["id"]: row for row in data["bot_threads_closed"]}
        retained = []
        for finding in unit.evidence.get("required_findings", ()):
            key = finding.get("id") if isinstance(finding, dict) else None
            proof = bound.get(key)
            if (
                not proof
                or links.get(key, {}).get("revision") != proof["revision"]
                or finding.get("kind") != "review_thread"
                or revision(finding, snapshot.spec.github.verified_thread_bot_ids)
                != proof["revision"]
            ):
                retained.append(finding)
                continue
            links.pop(key)
            unit.evidence.setdefault("bot_thread_dispositions", []).append(
                {**proof, "candidate": data["candidate"], "observed_resolved": True}
            )
        unit.evidence["required_findings"] = retained
        return
    wait = data["bot_thread_wait"]
    ids = ", ".join(row["id"] for row in wait["findings"])
    unit.hold = Hold(
        "external_wait",
        f"Maintainer: inspect and resolve verified bot thread(s) {ids} on GitHub. "
        "The campaign will recheck the full conversation and current head.",
        id=action_id,
        actor="github",
        evidence={
            "candidate": wait["candidate"],
            "feedback": wait["feedback"],
            "bot_thread_wait": wait["findings"],
        },
    )
