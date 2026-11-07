"""Prove an externally landed ready candidate without inventing phase history.

An observed merged flag is only the start. H and every checkpoint must survive
in fresh canonical history, the actual merge parents must already be covered by
H, and the merged tree must equal the fully verified/reviewed tree. Uncovered
integration receives an explicit post-merge qualification hold instead of being
credited with acceptance performed on another product.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

from . import verify
from .campaign_model import CampaignError
from .campaign_publication import PublicationController, PublicationRequirements
from .campaign_supervisor import Observation


@dataclass(frozen=True)
class ExternalMergeEvidence:
    repository: str
    unit_id: str
    head: str
    base: str
    target: str
    merge_commit: str
    canonical_head: str
    parents: tuple[str, ...]
    checkpoint_heads: tuple[str, ...]
    verification_launch: str
    review_launch: str
    source_revision: str
    graph_revision: int
    config_revision: int
    covered_tree: bool
    quiescent: bool
    schema_version: int = 1


def inspect(runtime, snapshot, unit, observation) -> Observation:
    candidate = runtime._candidate(snapshot, unit)
    controller = PublicationController(
        runtime.gateway(snapshot),
        clock=runtime.clock,
        requirements=PublicationRequirements.from_spec(snapshot.spec.github),
    )
    receipt = controller.merge_status(candidate.pr_number, expected_head=candidate.head)
    if receipt.state != "merged_pending_sync":
        return Observation(
            "waiting", reason=receipt.reason or receipt.state, retry_at=runtime.clock() + 5
        )
    canonical = controller.confirm_canonical(
        candidate,
        receipt,
        runtime.project,
        remote=snapshot.spec.remote,
        target=snapshot.spec.target,
    )
    if not verify.is_ancestor(runtime.project, candidate.head, canonical.canonical_head):
        return Observation(
            "confirmed",
            {
                "external_followup_hold": "external merge omitted the accepted H history",
                "merge": asdict(canonical),
            },
        )
    checkpoints = runtime._pusher(snapshot, unit).observations()
    heads = tuple(row.head for row in checkpoints if row.state != "superseded")
    if any(
        not verify.is_ancestor(runtime.project, head, canonical.canonical_head) for head in heads
    ):
        return Observation(
            "confirmed",
            {
                "external_followup_hold": "external merged history omitted a published or pending checkpoint",
                "merge": asdict(canonical),
            },
        )
    raw = verify.git_bytes(runtime.project, "show", "-s", "--format=%P", canonical.merge_commit)
    if raw.returncode:
        raise CampaignError("external merge parent observation unavailable")
    parents = tuple(raw.stdout.decode("ascii").split())
    covered = (
        bool(parents)
        and all(verify.is_ancestor(runtime.project, parent, candidate.head) for parent in parents)
        and verify.revision_tree_oid(runtime.project, canonical.merge_commit)
        == verify.revision_tree_oid(runtime.project, candidate.head)
    )
    if not covered:
        return Observation(
            "confirmed",
            {
                "external_followup_hold": "external merge contains integration not covered by H verification/review; qualify merged product or a linked follow-up PR",
                "merge": asdict(canonical),
            },
        )
    verification = unit.evidence.get("verification", {})
    review = unit.evidence.get("review", {})
    coordinates = review.get("validated", {}).get("coordinates", {})
    exact = (
        verification.get("outcome") == "verified"
        and verification.get("head") == candidate.head
        and bool(verification.get("launch_id"))
        and bool(review.get("launch_id"))
        and review.get("validated", {}).get("verdict") == "approved"
        and coordinates.get("head") == candidate.head
        and coordinates.get("base") == candidate.base
        and coordinates.get("source_revision", "") == candidate.source_revision
        and coordinates.get("graph_revision") == snapshot.graph_revision
        and coordinates.get("config_revision") == snapshot.config_revision
        and unit.claim is not None
        and unit.claim.quiescent
    )
    if not exact:
        return Observation(
            "confirmed",
            {
                "external_followup_hold": "externally merged candidate lacks current exact-H verification, full review or quiescence receipts",
                "merge": asdict(canonical),
            },
        )
    # This keeps the real merged/closed observation intact. Only lifecycle and
    # target movement already established by canonical Git proof are exempted.
    fresh = controller.inspect(candidate.pr_number)
    gate = controller.evaluate(
        candidate,
        runtime._publication_evidence(snapshot, unit),
        fresh,
        merge_mode=snapshot.spec.github.merge_mode,
        independent_families=snapshot.spec.github.require_independent_review,
        queue_evidence_context=snapshot.spec.github.candidate_check_context,
        canonical_merge=canonical,
    )
    if not gate.ready:
        return Observation(
            "confirmed",
            {"external_followup_hold": "; ".join(gate.reasons), "merge": asdict(canonical)},
        )
    proof = ExternalMergeEvidence(
        candidate.repository,
        unit.id,
        candidate.head,
        candidate.base,
        candidate.target,
        canonical.merge_commit,
        canonical.canonical_head,
        parents,
        heads,
        str(verification.get("launch_id", "")),
        str(review.get("launch_id", "")),
        candidate.source_revision,
        snapshot.graph_revision,
        snapshot.config_revision,
        True,
        True,
    )
    return Observation("confirmed", {"external_merge": asdict(proof), "merge": asdict(canonical)})
