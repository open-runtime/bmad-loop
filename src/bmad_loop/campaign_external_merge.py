"""Prove an externally landed ready candidate without inventing phase history.

An observed merged flag is only the start. H and every checkpoint must survive
in the actual merge and fresh canonical history. A covered tree retains its
existing acceptance; uncovered integration carries explicit landing debt through
verification and independent review before canonical tracking can complete.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

from . import verify
from .campaign_model import CampaignError
from .campaign_publication import PublicationController, PublicationRequirements
from .campaign_supervisor import Observation


class ExternalLandingStale(CampaignError):
    """A completed observation needs fresh authority before it can be adopted."""


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


@dataclass(frozen=True)
class ExternalLandingEvidence(ExternalMergeEvidence):
    """Canonical external merge identity whose actual product still needs acceptance."""

    schema_version: int = 2
    merge_tree: str = ""
    pr_number: int = 0


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
    if not all(
        verify.is_ancestor(runtime.project, candidate.head, revision)
        for revision in (canonical.merge_commit, canonical.canonical_head)
    ):
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
        not verify.is_ancestor(runtime.project, head, revision)
        for head in heads
        for revision in (canonical.merge_commit, canonical.canonical_head)
    ):
        return Observation(
            "confirmed",
            {
                "external_followup_hold": "external merged history omitted a published or pending checkpoint",
                "merge": asdict(canonical),
            },
        )
    from .campaign_landing_acceptance import assess

    landing = assess(runtime.project, candidate, canonical)
    parents = tuple(landing["merge_parents"])
    covered = landing["state"] == "covered"
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
    if fresh.base_head != canonical.canonical_head:
        return Observation(
            "waiting",
            reason="Canonical target moved during external merge observation; reobserve",
            retry_at=runtime.clock() + 5,
        )
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
        covered,
        True,
    )
    if not covered:
        proof = ExternalLandingEvidence(
            **{
                **asdict(proof),
                "schema_version": 2,
                "merge_tree": landing["merge_tree"],
                "pr_number": candidate.pr_number,
            }
        )
        return Observation(
            "confirmed", {"external_landing": asdict(proof), "merge": asdict(canonical)}
        )
    return Observation("confirmed", {"external_merge": asdict(proof), "merge": asdict(canonical)})
