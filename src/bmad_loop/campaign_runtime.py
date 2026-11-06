"""Production campaign effects behind the single-writer supervisor.

Every method either proposes a durable action, executes one bounded effect, or
applies independently checked evidence. Coding sessions and verification never
run in the supervisor interpreter. External observations remain explicit debt.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import shutil
import threading
import time
import uuid
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import policy as policy_mod
from . import runs, runsetup, verify
from .adapters.profile import get_profile
from .campaign_capacity import observe as observe_capacity
from .campaign_discovery import (
    CoverageReceipt,
    OverlapDisposition,
    RepositoryDiscovery,
    validate_overlap,
)
from .campaign_execution import CampaignExecution
from .campaign_github import GhTransport, GithubGateway, MutationIntent
from .campaign_model import (
    CampaignError,
    CampaignSnapshot,
    CampaignSpec,
    CampaignUnit,
    Hold,
    RoleCandidate,
    SourceSelector,
    UnitSpec,
    UnitState,
    WorkerClaim,
    scopes_overlap,
)
from .campaign_planning import StageCoordinates, execution_spec, stage_packet, validate_result
from .campaign_preflight import repository_name, resolve_roles
from .campaign_publication import (
    Candidate,
    PublicationController,
    PublicationEvidence,
    PublicationRequirements,
)
from .campaign_recovery import CheckpointPusher
from .campaign_scheduler import CapacityObservation, admit, select_role
from .campaign_sources import import_sources, prepare_source_view
from .campaign_store import CampaignStore, add_action, add_event, transition
from .campaign_supervisor import Observation, Proposal
from .campaign_tracking import HierarchyCoordinator
from .campaign_workers import (
    allocate_worktree,
    common_directory,
    git,
    observe,
    prepare_launch,
    spawn,
)
from .journal import Journal
from .model import RunState
from .platform_util import atomic_write_text, root_identity_record, safe_segment


def store_for(project: Path) -> CampaignStore:
    common = common_directory(project)
    key = hashlib.sha256(str(common).encode()).hexdigest()[:24]
    return CampaignStore(runs.state_root() / "campaigns" / key)


def create(project: Path, spec: CampaignSpec) -> tuple[CampaignStore, CampaignSnapshot]:
    project = project.resolve()
    store = store_for(project)
    common = common_directory(project)
    remote_url = git(project, "remote", "get-url", spec.remote)
    repository = spec.github.repository or repository_name(remote_url)
    from .campaign_source_freshness import require as require_current_sources

    source_freshness = require_current_sources(project, spec)
    graph = import_sources(project, spec)
    campaign_id = "campaign-" + time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:8]
    pol = policy_mod.load(project / policy_mod.POLICY_FILE)
    spec = resolve_roles(spec, pol)
    from .adapters.multiplexer import _select, configure_multiplexer
    from .campaign_reflection_flow import reflection_policy

    pol = reflection_policy(pol, spec.paradev.reflect)
    configure_multiplexer(pol.mux.backend or None, origin=project)
    resolved_backend = _select()[1]
    if spec.paradev.reflect and resolved_backend != "paradev":
        raise CampaignError("ambient transport override conflicts with native reflection")
    pol = dataclasses.replace(pol, mux=dataclasses.replace(pol.mux, backend=resolved_backend))
    profiles = runsetup.resolve_profiles(pol, project)
    for role in set(dict(spec.roles)) | {
        "planning",
        "planning_review",
        "triage",
        "escalation",
        "decision_review",
        "conflict_repair",
        "publication_review",
    }:
        for candidate in spec.candidates(role):
            if candidate.profile:
                profiles["campaign:" + candidate.profile] = get_profile(candidate.profile, project)
    from .cli import _launch_plugin_grants

    grants = _launch_plugin_grants(project)
    template = CampaignExecution(
        campaign_id,
        "configuration",
        0,
        0,
        1,
        store.directory(campaign_id) / "configuration",
        pol,
        profiles,
        grants,
    )
    # Claim the snapshot directory before writing the separate immutable envelope.
    snapshot = CampaignSnapshot(
        campaign_id,
        repository,
        spec,
        units={unit.id: CampaignUnit(unit) for unit in graph.units},
        common_dir=str(common),
        root=str(project),
        remote_url=remote_url,
        config_snapshot={"policy": pol.to_dict(), "configuration_revision": 1},
    )
    snapshot.bindings["source_observations"] = graph.observations
    if spec.reasoning.enabled:
        from .campaign_headless import load_capabilities

        if not spec.reasoning.capabilities:
            raise CampaignError("reasoning requires a qualified capabilities manifest")
        manifest = Path(spec.reasoning.capabilities).expanduser()
        if not manifest.is_absolute():
            manifest = project / manifest
        snapshot.config_snapshot["reasoning_capabilities"] = load_capabilities(manifest)
    snapshot.bindings["source_freshness"] = dataclasses.asdict(source_freshness)
    ownership = dict(spec.ownership)
    unknown = set(ownership) - set(snapshot.units)
    if unknown:
        raise CampaignError(f"ownership names unknown units: {sorted(unknown)}")
    for key, unit in snapshot.units.items():
        if key in ownership:
            unit.spec = dataclasses.replace(unit.spec, scopes=ownership[key])
        original = graph.observations.get(key, {})
        from .campaign_existing import terminal_status

        terminal = terminal_status(original)
        if terminal == "dropped":
            unit.hold = Hold(
                "source_dropped",
                "native source is dropped; current canonical decision must be reconciled before cancellation or dependency release",
                id="source-dropped:" + key,
                actor="source",
            )
        elif original.get("hitl") and terminal != "done":
            unit.hold = Hold(
                "human_only",
                "source HITL requires its configured human action",
                id="source-hitl:" + key,
                actor="source",
            )
        elif original.get("blocked_reason"):
            unit.hold = Hold(
                "source_blocked",
                str(original["blocked_reason"]),
                id="source-blocked:" + key,
                actor="source",
            )
    if spec.planning.enabled:
        plan_id = f"campaign:{campaign_id}:planning"
        plan = UnitSpec(
            plan_id,
            "campaign-plan",
            "planning",
            "Campaign planning",
            kind="planning",
            acceptance=(
                "Review and deliver requirements, architecture and executable source plans.",
            ),
            scopes=("resource:campaign-plan",),
            order=-1,
        )
        for unit in snapshot.units.values():
            unit.spec = dataclasses.replace(unit.spec, after=(*unit.spec.after, plan_id))
        snapshot.units[plan_id] = CampaignUnit(plan)
    snapshot.spec = dataclasses.replace(
        spec, units=tuple(unit.spec for unit in snapshot.units.values())
    )
    add_event(snapshot, "campaign-created", selected_units=len(snapshot.units))
    snapshot = store.create(snapshot)
    try:
        path, digest = template.save()
        # The policy itself is already frozen. These stable envelope coordinates
        # are recoverable metadata, not a new operational configuration.
        snapshot.bindings["execution_template"] = {"path": str(path), "digest": digest}
        snapshot = store.save(snapshot, snapshot.revision)
    except BaseException:
        # Keep the allocated authority visible; never delete a half-created
        # campaign and silently permit another owner to ignore it.
        raise
    return store, snapshot


class CampaignRuntime:
    def __init__(
        self,
        project: Path,
        store: CampaignStore,
        *,
        gateway: GithubGateway | None = None,
        clock=time.time,
        boundary_clock=time.time,
        capacity_observer=observe_capacity,
        evidence_providers=None,
        adoption_resolver=None,
    ):
        self.project, self.store, self.clock = project.resolve(), store, clock
        self.boundary_clock = boundary_clock
        self._gateway = gateway
        from .campaign_adoption import load_provider

        self.adoption_resolver = adoption_resolver or load_provider
        self.capacity_observer = capacity_observer
        self.evidence_providers = evidence_providers or {}
        self._publication_lock = threading.RLock()

    def gateway(self, snapshot: CampaignSnapshot) -> GithubGateway:
        return self._gateway or GithubGateway(GhTransport(), snapshot.repository)

    @staticmethod
    def _set_hold(unit, hold: Hold) -> None:
        from .campaign_hold import PROTECTED_HOLDS, defer_failure

        if unit.hold and unit.hold.kind in PROTECTED_HOLDS:
            defer_failure(unit, hold)
        else:
            unit.hold = hold

    def template(self, snapshot: CampaignSnapshot) -> CampaignExecution:
        binding = snapshot.bindings.get("execution_template")
        if not binding:
            raise CampaignError("campaign configuration envelope is incomplete")
        value = CampaignExecution.load(Path(binding["path"]), binding["digest"])
        if value.campaign_id != snapshot.id or value.config_revision != snapshot.config_revision:
            raise CampaignError("campaign configuration binding mismatch")
        return value

    def source(self, snapshot: CampaignSnapshot, unit: CampaignUnit) -> SourceSelector:
        if unit.spec.kind == "tracking":
            return SourceSelector("explicit", id=unit.spec.source)
        found = next(
            (source for source in snapshot.spec.sources if source.id == unit.spec.source), None
        )
        if found is not None:
            return found
        if unit.spec.kind == "planning":
            return SourceSelector("explicit", id=unit.spec.source)
        raise CampaignError(f"unit {unit.id} has no configured BMAD source selector")

    def _pending(self, snapshot: CampaignSnapshot, unit: str, kind: str | None = None) -> bool:
        return any(
            action.unit == unit
            and action.state in {"prepared", "sent_unknown"}
            and (kind is None or action.kind == kind)
            for action in snapshot.outbox
        )

    @staticmethod
    def _writes(payload: dict[str, Any]) -> bool:
        if payload.get("kind") in {"verify", "queue_verify"}:
            return False
        if payload.get("kind") != "stage":
            return True
        return not payload.get(
            "read_only",
            payload.get("stage")
            in {
                "planning_review",
                "publication_review",
                "admission_review",
                "triage",
                "decision_review",
                "escalation",
            },
        )

    def _role(
        self,
        snapshot: CampaignSnapshot,
        role: str,
        *,
        developer_family: str = "",
        developer_families: tuple[str, ...] = (),
        provisional: tuple[tuple[str, RoleCandidate], ...] = (),
        allowed_candidates: tuple[RoleCandidate, ...] | None = None,
    ) -> RoleCandidate | None:
        rows = snapshot.bindings.get("capacity", {})
        capacities = {
            (row["profile"], row["model"]): CapacityObservation(**row["capacity"])
            for row in rows.get("observations", [])
        }
        from .campaign_scheduler import route_unavailable

        for identity, observation in capacities.items():
            # Older/injected observers inherit the observation batch's bounded
            # lifetime, never a fresh timestamp on every scheduler pass.
            stamped = dataclasses.replace(
                observation,
                observed_at=(
                    observation.observed_at
                    if observation.observed_at is not None
                    else rows.get("observed_at", 0)
                ),
                expires_at=(
                    observation.expires_at
                    if observation.expires_at is not None
                    else rows.get("observed_at", 0) + 120
                ),
            )
            reason = route_unavailable(stamped, now=self.clock())
            capacities[identity] = (
                dataclasses.replace(stamped, available=False, reason=reason) if reason else stamped
            )
        unobserved = Counter((candidate.profile, candidate.model) for _, candidate in provisional)
        observed_at = rows.get("observed_at", 0)
        for unit in snapshot.units.values():
            claim = unit.claim
            if not claim or claim.quiescent:
                continue
            launch = next(
                (action for action in snapshot.outbox if action.id == claim.launch_id), None
            )
            candidate = launch.payload.get("candidate") if launch else None
            review_candidate = launch.payload.get("review_candidate") if launch else None
            handoff = unit.evidence.get("handoff", {})
            review_only = (
                handoff.get("phase") == "continuing" and handoff.get("native_role") == "review"
            )
            # A configured legacy review can start later in this same child.
            # Reserve its advertised provider slot until the child is drained;
            # unknown provider concurrency continues to use campaign limits.
            if review_candidate:
                unobserved[(review_candidate["profile"], review_candidate.get("model", ""))] += 1
            if (
                candidate
                and not review_only
                and (claim.started_at is None or claim.started_at >= observed_at)
                and (
                    not review_candidate
                    or (candidate["profile"], candidate.get("model", ""))
                    != (review_candidate["profile"], review_candidate.get("model", ""))
                )
            ):
                unobserved[(candidate["profile"], candidate.get("model", ""))] += 1
        for identity, count in unobserved.items():
            if identity in capacities:
                capacities[identity] = dataclasses.replace(
                    capacities[identity], active=capacities[identity].active + count
                )
        for job in snapshot.jobs.values():
            claim = job.get("claim")
            candidate = job.get("selection")
            if (
                claim
                and not claim["quiescent"]
                and candidate
                and claim.get("started_at", 0) >= observed_at
            ):
                identity = (candidate["profile"], candidate["model"])
                if identity in capacities:
                    capacities[identity] = dataclasses.replace(
                        capacities[identity], active=capacities[identity].active + 1
                    )
        if (
            role == "publication_review"
            and snapshot.spec.github.require_independent_review
            and (developer_family or developer_families)
        ):
            for candidate in snapshot.spec.candidates(role):
                from .campaign_headless import provider_family

                family = candidate.provider_family or provider_family(candidate.model)
                key = (candidate.profile, candidate.model)
                if family in {developer_family, *developer_families} and key in capacities:
                    capacities[key] = dataclasses.replace(
                        capacities[key],
                        available=False,
                        reason="publication review requires a different provider family",
                    )
        active_role = sum(
            bool(unit.claim and not unit.claim.quiescent and unit.claim.role == role)
            for unit in snapshot.units.values()
        )
        if role == "execution_review":
            active_role += sum(
                bool(
                    unit.claim
                    and not unit.claim.quiescent
                    and any(
                        action.id == unit.claim.launch_id and action.payload.get("review_candidate")
                        for action in snapshot.outbox
                    )
                )
                for unit in snapshot.units.values()
            )
        active_role += sum(
            bool(
                job.get("claim")
                and not job["claim"]["quiescent"]
                and job["request"]["role"] == role
            )
            for job in snapshot.jobs.values()
        )
        if (
            active_role + sum(saved_role == role for saved_role, _ in provisional)
            >= snapshot.spec.role(role).max_sessions
        ):
            return None
        if snapshot.spec.resources.budget_usd is not None:
            spent = snapshot.bindings.get("spend_usd", 0)
            if spent is None or spent >= snapshot.spec.resources.budget_usd:
                return None
        selected, _ = select_role(
            snapshot.spec,
            role,
            capacities,
            selections={
                tuple(key.split("\0")): count
                for key, count in snapshot.bindings.get("role_selections", {}).items()
            },
            spent_usd=snapshot.bindings.get("role_spend", {}).get(role, 0),
            allowed_candidates=allowed_candidates,
        )
        if selected is not None:
            from .campaign_reasoning_results import preferred_candidate

            eligible = tuple(
                candidate
                for candidate in snapshot.spec.candidates(role)
                if (allowed_candidates is None or candidate in allowed_candidates)
                and not route_unavailable(
                    capacities.get((candidate.profile, candidate.model)), now=self.clock()
                )
            )
            selected = preferred_candidate(snapshot, role, eligible) or selected
        return selected

    def _units_settled(self, snapshot) -> bool:
        from .campaign_runtime_intake import optional_canceled, settled

        return (
            bool(snapshot.units)
            and all(
                unit.state == UnitState.DELIVERED or optional_canceled(unit)
                for unit in snapshot.units.values()
            )
            and settled(snapshot)
        )

    def completion_ready(self, snapshot) -> bool:
        from .campaign_reasoning_flow import outstanding

        if outstanding(snapshot):
            return False
        return snapshot.bindings.get(
            "hierarchy_finalized_graph"
        ) == snapshot.graph_revision and snapshot.bindings.get("intake", {}).get(
            "observed_at", 0
        ) >= snapshot.bindings.get(
            "last_delivery_at", 0
        )

    def reconciliation_observations(self, snapshot) -> tuple[Proposal, ...]:
        proposals = [
            Proposal(kind, "")
            for kind in ("inventory", "capacity", "intake")
            if not self._pending(snapshot, "", kind)
        ]
        proposals.extend(
            proposal
            for proposal in self.observe_workers(snapshot)
            if proposal.kind == "observe-worker"
        )
        return tuple(proposals)

    def prepare_resume(self, snapshot: CampaignSnapshot) -> bool:
        from .campaign_attribution import restore

        changed = restore(snapshot)
        for unit in snapshot.units.values():
            hold = unit.hold
            if (
                hold
                and hold.kind == "approval"
                and hold.actor == "campaign"
                and hold.evidence.get("dequeue_confirmed")
            ):
                candidate = hold.evidence.get("queue", {}).get("candidate")
                if candidate:
                    unit.hold = dataclasses.replace(
                        hold,
                        kind="remote_wait",
                        actor="github",
                        evidence={**hold.evidence, "candidate": candidate},
                    )
                    add_event(snapshot, "legacy-queue-wait-restored", unit=unit.id)
                    changed = True
        return changed

    def proposals(self, snapshot: CampaignSnapshot) -> tuple[Proposal, ...]:
        if self._units_settled(snapshot) and self.completion_ready(snapshot):
            # Let already-issued observations/metadata drain. Re-admitting a
            # periodic observation here can permanently outrun terminality on a
            # slow state store, even after the final intake watermark is fresh.
            return tuple(
                Proposal("lifecycle", key)
                for key, unit in snapshot.units.items()
                if snapshot.spec.paradev.reflect
                and unit.state == UnitState.DELIVERED
                and unit.bindings.get("native")
                and "lifecycle" not in unit.bindings
                and not self._pending(snapshot, key, "lifecycle")
            )
        proposals: list[Proposal] = []
        now = self.clock()
        inventory = snapshot.bindings.get("inventory")
        if not inventory and not self._pending(snapshot, "", "inventory"):
            return (Proposal("inventory", ""),)
        if (
            inventory
            and not inventory.get("complete")
            and not self._pending(snapshot, "", "inventory")
        ):
            if now - inventory.get("observed_at", 0) > 15:
                return (Proposal("inventory", ""),)
        if not inventory or not inventory.get("complete"):
            return ()
        if self._pending(snapshot, "", "inventory"):
            return ()
        reviewing_inventory = any(
            unit.claim
            and not unit.claim.quiescent
            and unit.claim.resource in {"planning", "review", "resolution"}
            for unit in snapshot.units.values()
        )
        if now - inventory.get("observed_at", 0) > 120 and not reviewing_inventory:
            return (Proposal("inventory", ""),)
        if (
            any(
                unit.hold and unit.hold.kind == "source_blocked" for unit in snapshot.units.values()
            )
            and now - snapshot.bindings.get("source_refresh_at", 0) > 30
            and not self._pending(snapshot, "", "source-observation")
        ):
            proposals.append(Proposal("source-observation", ""))
        capacity = snapshot.bindings.get("capacity", {})
        if now - capacity.get("observed_at", 0) > 30 and not self._pending(
            snapshot, "", "capacity"
        ):
            proposals.append(Proposal("capacity", ""))
        intake = snapshot.bindings.get("intake", {})
        if (
            now - intake.get("observed_at", 0) > 30
            or (
                self._units_settled(snapshot)
                and intake.get("observed_at", 0) < snapshot.bindings.get("last_delivery_at", 0)
            )
        ) and not self._pending(snapshot, "", "intake"):
            proposals.append(Proposal("intake", ""))
        admission = admit(snapshot, now=now)
        selected = set(admission.units)
        reserved = Counter(
            claim.resource
            for row in snapshot.units.values()
            if (claim := row.claim) is not None and not claim.quiescent
        )
        limits = {
            "execution": snapshot.spec.max_workers,
            "planning": snapshot.spec.resources.max_sessions,
            "review": snapshot.spec.max_review_sessions,
            "verify": snapshot.spec.resources.verify_slots,
            "resolution": snapshot.spec.resolution.max_sessions,
        }

        def reserve(resource: str) -> bool:
            if reserved[resource] >= limits[resource]:
                return False
            if (
                resource != "verify"
                and sum(count for name, count in reserved.items() if name != "verify")
                >= snapshot.spec.resources.max_sessions
            ):
                return False
            reserved[resource] += 1
            return True

        from .campaign_scheduler import delegates_tracking_slot

        workspace_count = sum(
            bool(unit.bindings.get("workspace")) or unit.state == UnitState.PLANNING
            for unit in snapshot.units.values()
            if unit.state not in {UnitState.DELIVERED, UnitState.CANCELED}
            and not delegates_tracking_slot(snapshot, unit)
        )
        tracking_reservations = sum(
            row.spec.kind == "tracking"
            and row.state == UnitState.PENDING
            and not row.bindings.get("workspace")
            for row in snapshot.units.values()
        )
        from .campaign_scheduler import allocation_order

        allocation_reservations = [
            row.id
            for row in snapshot.units.values()
            if (row.bindings.get("workspace") or row.state == UnitState.PLANNING)
            and row.state not in {UnitState.DELIVERED, UnitState.CANCELED}
            and not delegates_tracking_slot(snapshot, row)
        ]
        for key in allocation_order(snapshot):
            unit = snapshot.units[key]
            adoption = unit.bindings.get("adoption")
            if adoption and adoption["state"] in {
                "retire_pending",
                "draft_pending",
                "draft_confirmed",
            }:
                provisional_hold = (
                    adoption.get("provisional", {}).get("hold_id")
                    if adoption.get("provisional")
                    else ""
                )
                if (
                    (
                        not unit.hold
                        or unit.hold.kind in {"migration", "overlap", "admission"}
                        or (
                            unit.hold.id == provisional_hold
                            and unit.hold.kind
                            not in {"operator", "owner", "owner_stop", "human_action", "human_only"}
                        )
                    )
                    and not self._pending(snapshot, key)
                    and (not unit.claim or unit.claim.quiescent)
                ):
                    if adoption["state"] == "retire_pending":
                        proposals.append(Proposal("adopt-retire", key))
                    elif adoption["state"] == "draft_pending":
                        proposals.append(Proposal("adopt-draft", key))
                    elif workspace_count < snapshot.spec.max_in_flight_units:
                        proposals.append(Proposal("prepare-unit", key, {"adoption": True}))
                        workspace_count += 1
                continue
            release = unit.bindings.get("operator_release")
            if (
                release
                and unit.hold
                and unit.hold.kind == "operator"
                and not self._pending(snapshot, key, "release-observation")
            ):
                proposals.append(Proposal("release-observation", key, release))
            if unit.evidence.get("stage_sync_debt"):
                projecting = any(
                    row.unit == key
                    and row.kind == "github"
                    and row.state in {"prepared", "sent_unknown"}
                    for row in snapshot.outbox
                )
                if (
                    not projecting
                    and not (unit.hold and unit.hold.kind == "operator")
                    and not self._pending(snapshot, key, "stage-boundary-after")
                ):
                    proposals.append(Proposal("stage-boundary-after", key))
                continue
            retained = unit.evidence.get("retained_finalization_pending")
            if (
                retained
                and not unit.hold
                and unit.state == UnitState.READY
                and not self._pending(snapshot, key)
                and (not unit.claim or unit.claim.quiescent)
                and reserve("execution")
            ):
                proposals.append(
                    Proposal(
                        "launch",
                        key,
                        {
                            "kind": "recovery",
                            "stage": "retained_finalization",
                            "original_launch": retained["original_launch"],
                            "recovery_action": retained["recovery_action"],
                            "quiescence_receipt": retained["quiescence_receipt"],
                            "quiescence_head": unit.bindings["head"],
                        },
                    )
                )
                continue
            if (
                unit.hold
                and unit.hold.kind == "recovery_required"
                and unit.claim
                and unit.claim.quiescent
                and not self._pending(snapshot, key)
                and reserve("execution")
            ):
                proposals.append(
                    Proposal(
                        "launch",
                        key,
                        {
                            "kind": "recovery",
                            "stage": "internal_recovery",
                            "original_launch": unit.claim.launch_id,
                            "quiescence_receipt": unit.claim.launch_id,
                        },
                    )
                )
                continue
            if (
                unit.hold
                and unit.hold.kind in {"remote_wait", "external_wait"}
                and unit.hold.actor == "github"
                and (unit.retry_at is None or now >= unit.retry_at)
                and not self._pending(snapshot, key)
                and (not unit.claim or unit.claim.quiescent)
            ):
                proposals.append(Proposal("remote-wait", key, {"hold_id": unit.hold.id}))
                continue
            if (
                unit.hold
                or self._pending(snapshot, key)
                or (unit.claim and not unit.claim.quiescent)
            ):
                continue
            from .campaign_handoff_flow import resume_proposal

            continuation = resume_proposal(snapshot, unit, runtime=self)
            if continuation is not None:
                resource = (
                    "execution" if continuation.payload["kind"] == "execution" else "planning"
                )
                if continuation.payload.get("role") == "publication_review":
                    resource = "review"
                if reserve(resource):
                    proposals.append(continuation)
                continue
            if unit.spec.kind == "planning" and (
                unit.state == UnitState.RUNNING
                or (unit.state == UnitState.READY and key in selected)
            ):
                from .campaign_runtime_intake import owns_stage, ready_for_planning, stage_proposal

                if owns_stage(unit):
                    intake_stage = stage_proposal(snapshot, key, self._role(snapshot, "triage"))
                    if intake_stage is not None and reserve("planning"):
                        proposals.append(intake_stage)
                    continue
                if not ready_for_planning(snapshot, unit):
                    continue
                from .campaign_planning_flow import next_stage

                step = next_stage(snapshot, unit)
                if step:
                    candidate = self._role(snapshot, step.role)
                    if candidate and reserve("planning"):
                        proposals.append(
                            Proposal(
                                "launch",
                                key,
                                {
                                    "kind": "stage",
                                    "role": step.role,
                                    "candidate": dataclasses.asdict(candidate),
                                    "stage": step.stage,
                                },
                            )
                        )
                elif unit.evidence.get("planning_flow", {}).get("state") == "approved":
                    proposals.append(Proposal("planning-complete", key))
                continue
            if unit.state == UnitState.PENDING:
                from .campaign_existing import terminal_status

                if terminal_status(
                    snapshot.bindings.get("source_observations", {}).get(unit.id, {})
                ) == "done" and not unit.evidence.get("prior_delivery_checked"):
                    proposals.append(Proposal("reconcile-existing", key))
                    continue
                reservation = 0 if unit.spec.kind == "tracking" else tracking_reservations
                if (
                    workspace_count + reservation < snapshot.spec.max_in_flight_units
                    and all(
                        snapshot.units[key].state == UnitState.DELIVERED for key in unit.spec.after
                    )
                    and not any(
                        scopes_overlap(unit.spec.scopes, snapshot.units[other].spec.scopes)
                        for other in allocation_reservations
                        if other != key
                    )
                ):
                    proposals.append(Proposal("prepare-unit", key))
                    allocation_reservations.append(key)
                    workspace_count += 1
                    if unit.spec.kind == "tracking":
                        tracking_reservations -= 1
            elif unit.state == UnitState.DRAFT:
                if not unit.bindings.get("hierarchy_ready"):
                    proposals.append(Proposal("hierarchy", key))
                elif not unit.bindings.get("pr"):
                    proposals.append(Proposal("prepare-pr", key))
                else:
                    proposals.append(Proposal("ready", key))
            elif unit.state == UnitState.READY and key in selected:
                from .campaign_existing import requires_existing

                if requires_existing(snapshot, unit):
                    if reserve("execution"):
                        proposals.append(Proposal("launch", key, {"kind": "verify_existing"}))
                elif unit.spec.kind == "tracking":
                    if reserve("execution"):
                        proposals.append(Proposal("launch", key, {"kind": "tracking"}))
                elif unit.spec.kind in {"planning", "closure"}:
                    role = "planning" if unit.spec.kind == "planning" else "planning_review"
                    candidate = self._role(snapshot, role)
                    if candidate and reserve("planning"):
                        proposals.append(
                            Proposal(
                                "launch",
                                key,
                                {
                                    "kind": "stage",
                                    "role": role,
                                    "candidate": dataclasses.asdict(candidate),
                                    "stage": unit.spec.kind,
                                },
                            )
                        )
                else:
                    if not unit.evidence.get("admission"):
                        candidate = self._role(snapshot, "triage")
                        if candidate and reserve("planning"):
                            proposals.append(
                                Proposal(
                                    "launch",
                                    key,
                                    {
                                        "kind": "stage",
                                        "role": "triage",
                                        "stage": "admission_review",
                                        "candidate": dataclasses.asdict(candidate),
                                    },
                                )
                            )
                    elif "execution" in dict(snapshot.spec.roles):
                        candidate = self._role(snapshot, "execution")
                        if candidate and reserve("execution"):
                            proposals.append(
                                Proposal(
                                    "launch",
                                    key,
                                    {
                                        "kind": "execution",
                                        "role": "execution",
                                        "candidate": dataclasses.asdict(candidate),
                                    },
                                )
                            )
                    elif reserve("execution"):
                        proposals.append(Proposal("launch", key, {"kind": "execution"}))
            elif unit.state == UnitState.LOCALLY_COMPLETE:
                if reserve("execution"):
                    proposals.append(Proposal("launch", key, {"kind": "integrate"}))
            elif unit.state == UnitState.SYNCING and unit.evidence.get("integrated"):
                if unit.evidence.get("pushed") != unit.evidence["integrated"]["head"]:
                    proposals.append(
                        Proposal("push", key, {"head": unit.evidence["integrated"]["head"]})
                    )
                elif reserve("verify"):
                    proposals.append(Proposal("launch", key, {"kind": "verify"}))
            elif unit.state == UnitState.SYNCING and not unit.evidence.get("integrated"):
                if reserve("execution"):
                    proposals.append(Proposal("launch", key, {"kind": "integrate"}))
            elif unit.state == UnitState.REVIEWING and not unit.evidence.get("review"):
                candidate = self._role(
                    snapshot,
                    "publication_review",
                    developer_family=unit.evidence.get("developer_family", ""),
                    developer_families=tuple(unit.evidence.get("developer_families", [])),
                )
                if candidate and reserve("review"):
                    proposals.append(
                        Proposal(
                            "launch",
                            key,
                            {
                                "kind": "stage",
                                "role": "publication_review",
                                "candidate": dataclasses.asdict(candidate),
                                "stage": "publication_review",
                            },
                        )
                    )
            elif unit.state == UnitState.READY_TO_PUBLISH:
                if not unit.bindings.get("ready_for_review"):
                    proposals.append(Proposal("prepare-ready", key))
                else:
                    from .campaign_publication_flow import proposals as publication_proposals

                    proposals.extend(publication_proposals(self, snapshot, unit, reserve))
            elif unit.state == UnitState.MERGE_REQUESTED:
                if snapshot.spec.github.merge_mode == "queue":
                    from .campaign_runtime_queue import proposals as queue_proposals

                    proposals.extend(queue_proposals(self, snapshot, unit, reserve))
                else:
                    proposals.append(Proposal("observe-merge", key))
            elif unit.state == UnitState.MERGED_PENDING_SYNC:
                proposals.append(Proposal("sync-tracking", key))
        if snapshot.spec.paradev.reflect:
            for key, unit in snapshot.units.items():
                if (
                    unit.state == UnitState.DELIVERED
                    and unit.bindings.get("native")
                    and "lifecycle" not in unit.bindings
                    and not self._pending(snapshot, key, "lifecycle")
                ):
                    proposals.append(Proposal("lifecycle", key))
        from .campaign_resolution_flow import proposals as resolution_proposals

        resolution_candidates = {
            role: candidate
            for role in ("decision_review", "escalation", "conflict_repair")
            if (candidate := self._role(snapshot, role)) is not None
        }
        for proposal in resolution_proposals(
            snapshot, self.project, resolution_candidates, now=now
        ):
            if self._pending(snapshot, proposal.unit):
                continue
            if proposal.kind == "launch" and not reserve(
                "verify" if proposal.payload.get("kind") == "verify" else "resolution"
            ):
                continue
            proposals.append(proposal)
        from .campaign_reasoning_plan import defer_intake
        from .campaign_runtime_intake import proposals as intake_proposals

        defer_triage = defer_intake(snapshot, now=self.clock())
        proposals.extend(
            proposal
            for proposal in intake_proposals(snapshot, self._role(snapshot, "triage"))
            if not defer_triage or proposal.kind != "triage"
        )
        if (
            self._units_settled(snapshot)
            and not self.completion_ready(snapshot)
            and not self._pending(snapshot, "", "intake")
            and intake.get("observed_at", 0) >= snapshot.bindings.get("last_delivery_at", 0)
            and not any(
                action.kind == "hierarchy-close" and action.state in {"prepared", "sent_unknown"}
                for action in snapshot.outbox
            )
        ):
            proposals.append(Proposal("hierarchy-close", next(reversed(snapshot.units))))
        admitted = []
        role_reservations = []
        writer_reservations = []
        for proposal in proposals:
            resuming_review = False
            if proposal.kind == "launch":
                unit = snapshot.units[proposal.unit]
                handoff = unit.evidence.get("handoff", {})
                resuming_review = (
                    bool(handoff.get("id"))
                    and proposal.payload.get("handoff_id") == handoff["id"]
                    and handoff.get("native_role") == "review"
                    and proposal.payload.get("kind") == "execution"
                )
                from .campaign_usage import admission_reason

                reason = admission_reason(snapshot, unit, proposal.payload)
                if reason:
                    admitted.append(Proposal("usage-hold", unit.id, {"reason": reason}))
                    continue
                from .campaign_scheduler import writer_available

                writes = self._writes(proposal.payload)
                if writes and not writer_available(
                    snapshot, unit.id, provisional=tuple(writer_reservations)
                ):
                    continue
                if writes and unit.bindings.get("ready_for_review"):
                    admitted.append(Proposal("prepare-draft", unit.id))
                    continue
            if (
                proposal.kind == "launch"
                and proposal.payload.get("candidate")
                and not resuming_review
            ):
                unit = snapshot.units[proposal.unit]
                role = proposal.payload["role"]
                candidate = self._role(
                    snapshot,
                    role,
                    developer_family=unit.evidence.get("developer_family", ""),
                    developer_families=tuple(unit.evidence.get("developer_families", [])),
                    provisional=tuple(role_reservations),
                    allowed_candidates=(
                        (
                            RoleCandidate(
                                **{
                                    **proposal.payload["candidate"],
                                    "capacity_command": tuple(
                                        proposal.payload["candidate"].get("capacity_command", ())
                                    ),
                                }
                            ),
                        )
                        if proposal.payload.get("handoff_id")
                        else None
                    ),
                )
                if candidate is None:
                    continue
                proposal = dataclasses.replace(
                    proposal,
                    payload={**proposal.payload, "candidate": dataclasses.asdict(candidate)},
                )
                role_reservations.append((role, candidate))
            if (
                proposal.kind == "launch"
                and proposal.payload.get("kind") == "execution"
                and "execution_review" in dict(snapshot.spec.roles)
            ):
                fixed_review = (
                    proposal.payload.get("review_candidate")
                    if proposal.payload.get("handoff_id")
                    else None
                )
                allowed_review = (
                    (
                        RoleCandidate(
                            **{
                                **fixed_review,
                                "capacity_command": tuple(fixed_review.get("capacity_command", ())),
                            }
                        ),
                    )
                    if fixed_review
                    else None
                )
                review_candidate = self._role(
                    snapshot,
                    "execution_review",
                    provisional=tuple(role_reservations),
                    allowed_candidates=allowed_review,
                )
                if review_candidate is None:
                    continue
                proposal = dataclasses.replace(
                    proposal,
                    payload={
                        **proposal.payload,
                        "review_candidate": dataclasses.asdict(review_candidate),
                    },
                )
                role_reservations.append(("execution_review", review_candidate))
            if proposal.kind == "launch" and self._writes(proposal.payload):
                writer_reservations.append(proposal.unit)
            admitted.append(proposal)
        from .campaign_boundary_sync import before_proposal

        return tuple(before_proposal(self, snapshot, proposal) for proposal in admitted)

    def observe_workers(self, snapshot: CampaignSnapshot) -> tuple[Proposal, ...]:
        from .campaign_handoff_flow import proposals as handoff_proposals
        from .campaign_reasoning_flow import proposals as reasoning_proposals
        from .campaign_reasoning_plan import proposals as semantic_proposals

        proposals = (
            list(reasoning_proposals(self, snapshot))
            + list(semantic_proposals(self, snapshot))
            + list(handoff_proposals(self, snapshot))
        )
        if (
            snapshot.spec.communication.mode == "observe"
            and any(unit.claim and not unit.claim.quiescent for unit in snapshot.units.values())
            and self.clock() - snapshot.bindings.get("communication", {}).get("observed_at", 0)
            >= 15
            and not self._pending(snapshot, "", "communication")
        ):
            proposals.append(Proposal("communication", ""))
        for key, unit in snapshot.units.items():
            claim = unit.claim
            reflected = unit.bindings.get("reflection", {})
            if (
                snapshot.spec.paradev.reflect
                and unit.bindings.get("native")
                and not self._pending(snapshot, key, "reflection")
                and (
                    reflected.get("fingerprint") != self._reflection_fingerprint(unit)
                    or (
                        claim
                        and not claim.quiescent
                        and self.clock() - reflected.get("observed_at", 0) >= 5
                    )
                )
            ):
                proposals.append(Proposal("reflection", key))
            if not claim or claim.quiescent:
                continue
            launch = next((row for row in snapshot.outbox if row.id == claim.launch_id), None)
            if launch is None or launch.state != "confirmed":
                # Adopt the real startup binding first and persist RUNNING.
                # A fast child may finish before a lost spawn reply is settled.
                continue
            if snapshot.bindings.get("terminate_requested") and not self._pending(
                snapshot, key, "terminate-worker"
            ):
                if not unit.bindings.get("stop_requested_for") == claim.launch_id:
                    proposals.append(
                        Proposal("terminate-worker", key, {"launch_id": claim.launch_id})
                    )
            if not self._pending(snapshot, key, "observe-worker"):
                proposals.append(Proposal("observe-worker", key, {"launch_id": claim.launch_id}))
            writable_session = launch.payload.get("kind") == "execution" or (
                launch.payload.get("kind") == "stage"
                and not launch.payload.get(
                    "read_only",
                    launch.payload.get("stage")
                    in {
                        "publication_review",
                        "planning_review",
                        "decision_review",
                        "escalation",
                        "admission_review",
                    },
                )
            )
            if (
                writable_session
                and not self._pending(snapshot, key, "checkpoint-progress")
                and (
                    self.clock() - unit.bindings.get("checkpoint_observed_at", 0)
                    >= min(5, snapshot.spec.checkpoints.target_interval_s)
                )
            ):
                proposals.append(Proposal("checkpoint-progress", key))
            progress = unit.evidence.get("checkpoint_progress", {})
            if (
                snapshot.spec.checkpoints.push
                and not unit.hold
                and writable_session
                and progress.get("state") == "observed"
                and progress.get("launch_id") == claim.launch_id
                and progress.get("head") != unit.bindings.get("last_pushed_head")
                and unit.bindings.get("workspace")
                and not self._pending(snapshot, key, "checkpoint")
                and self.clock() - unit.bindings.get("last_checkpoint_at", 0)
                >= snapshot.spec.checkpoints.target_interval_s
            ):
                proposals.append(
                    Proposal(
                        "checkpoint", key, {"launch_id": claim.launch_id, "progress": progress}
                    )
                )
        return tuple(proposals)

    def control(self, snapshot: CampaignSnapshot, command: dict[str, Any]) -> None:
        kind, key = command["kind"], command.get("unit", "")
        if kind == "authorize-scope":
            from .campaign_scope_authority import apply

            apply(snapshot, command)
            return
        if kind == "handoff":
            from .campaign_handoff_flow import control

            control(self, snapshot, command)
            return
        if kind == "prepare-effect":
            action = command["action"]
            if action["kind"].startswith("reasoning-"):
                from .campaign_reasoning_flow import prepare_effect

                owned = next(row for row in snapshot.outbox if row.id == action["id"])
                prepare_effect(self, snapshot, owned)
                return
            unit = snapshot.units.get(action["unit"])
            if unit is None:
                return
            if action["kind"] == "prepare-unit" and unit.state == UnitState.PENDING:
                transition(snapshot, unit.id, UnitState.PLANNING)
            elif (
                action["kind"] == "prepare-unit"
                and action["payload"].get("adoption")
                and unit.state in {UnitState.DRAFT, UnitState.READY}
            ):
                transition(snapshot, unit.id, UnitState.PLANNING)
            elif action["kind"] == "launch":
                payload = action["payload"]
                if unit.claim and not unit.claim.quiescent:
                    raise CampaignError("cannot launch over a live or uncertain worker")
                from .campaign_scheduler import writer_available

                if self._writes(payload) and not writer_available(snapshot, unit.id):
                    current = next(row for row in snapshot.outbox if row.id == action["id"])
                    current.state = "rejected"
                    current.error = "another actual writer owns overlapping scope"
                    self._set_hold(unit, Hold("ownership_uncertain", current.error, id=current.id))
                    return
                from .campaign_usage import admission_reason

                reason = admission_reason(snapshot, unit, payload)
                if reason:
                    current = next(row for row in snapshot.outbox if row.id == action["id"])
                    current.state = "rejected"
                    current.error = reason
                    self._set_hold(unit, Hold("lifetime_budget", reason, id=current.id))
                    return
                from .campaign_resolution_flow import prepare as prepare_resolution

                prepare_resolution(snapshot, unit.id, payload)
                from .campaign_resolution_delivery import prepare as prepare_resolution_effect

                prepare_resolution_effect(unit, payload)
                if payload.get("role") == "conflict_repair":
                    base = unit.evidence.get("integration_conflict", {}).get("base")
                    if base:
                        payload["checkpoint_preserved_bases"] = [base]
                        next(row for row in snapshot.outbox if row.id == action["id"]).payload[
                            "checkpoint_preserved_bases"
                        ] = [base]
                if payload.get("result_contract") == "repair-v1":
                    extra = unit.evidence.get("accepted_resolution", {}).get("affected_paths", [])
                    payload["checkpoint_extra_paths"] = extra
                    next(row for row in snapshot.outbox if row.id == action["id"]).payload[
                        "checkpoint_extra_paths"
                    ] = extra
                if self._writes(payload) and payload.get("kind") in {"execution", "stage"}:
                    from .campaign_checkpoint import seal as seal_checkpoint

                    scope_request = {
                        **payload,
                        "unit": dataclasses.asdict(unit.spec),
                        "source": unit.bindings.get(
                            "effective_source", dataclasses.asdict(self.source(snapshot, unit))
                        ),
                        "sources": unit.bindings.get("effective_sources", []),
                    }
                    capsule = seal_checkpoint(
                        Path(unit.bindings["workspace"]),
                        scope_request,
                        baseline=unit.bindings["head"],
                    )
                    payload.update(capsule)
                    next(row for row in snapshot.outbox if row.id == action["id"]).payload.update(
                        capsule
                    )
                from .campaign_runtime_intake import prepare_stage as prepare_intake_stage

                prepare_intake_stage(snapshot, unit.id, payload, launch_id=action["id"])
                if (
                    unit.spec.kind == "planning"
                    and payload.get("stage") in snapshot.spec.planning.stages
                ):
                    from .campaign_planning_flow import prepare_stage

                    prepare_stage(snapshot, unit.id, payload["stage"], launch_id=action["id"])
                unit.attempt += 1
                if payload.get("candidate"):
                    candidate = payload["candidate"]
                    identity = candidate["profile"] + "\0" + candidate.get("model", "")
                    selections = snapshot.bindings.setdefault("role_selections", {})
                    selections[identity] = selections.get(identity, 0) + 1
                if payload.get("review_candidate"):
                    candidate = payload["review_candidate"]
                    identity = candidate["profile"] + "\0" + candidate.get("model", "")
                    selections = snapshot.bindings.setdefault("role_selections", {})
                    selections[identity] = selections.get(identity, 0) + 1
                resource = (
                    "verify"
                    if payload["kind"] in {"verify", "queue_verify"}
                    else (
                        "review"
                        if payload.get("role") == "publication_review"
                        else (
                            "resolution"
                            if payload.get("result_contract")
                            in {"decision-v1", "resolution-v1", "repair-v1"}
                            else "planning" if payload["kind"] == "stage" else "execution"
                        )
                    )
                )
                unit.claim = WorkerClaim(
                    unit.id,
                    unit.attempt,
                    snapshot.generation,
                    action["id"],
                    workspace=payload.get("verification_workspace", unit.bindings["workspace"]),
                    branch=unit.bindings["branch"],
                    run_id=f"{snapshot.id}-{safe_segment(unit.id)[-24:]}-{unit.attempt}",
                    role=payload.get("role", payload["kind"]),
                    resource=resource,
                    writes=self._writes(payload),
                )
                unit.claim.run_dir = str(
                    self.store.directory(snapshot.id) / "workers" / action["id"]
                )
                from .campaign_handoff_flow import adopt_launch

                adopt_launch(snapshot, unit, payload, unit.claim)
                from .campaign_scheduler import epic_key

                snapshot.last_epic = epic_key(
                    unit.spec.source, unit.spec.initiative, unit.spec.epic
                )
                if unit.state == UnitState.READY:
                    transition(snapshot, unit.id, UnitState.CLAIMED)
                elif unit.state == UnitState.LOCALLY_COMPLETE:
                    transition(snapshot, unit.id, UnitState.SYNCING)
                elif unit.state == UnitState.SYNCING and payload["kind"] == "verify":
                    transition(snapshot, unit.id, UnitState.VERIFYING)
        elif kind in {"stop", "terminate-workers"}:
            snapshot.phase = "draining"
            for pending in snapshot.outbox:
                if pending.kind != "launch" or pending.state != "prepared":
                    continue
                pending.state = "rejected"
                pending.error = "campaign drained before launch submission"
                waiting = snapshot.units[pending.unit]
                if waiting.claim and waiting.claim.launch_id == pending.id:
                    waiting.claim.quiescent = True
                    waiting.claim.status = "not_launched"
                    if waiting.state == UnitState.CLAIMED:
                        transition(snapshot, waiting.id, UnitState.READY)
                    elif waiting.state == UnitState.VERIFYING:
                        transition(snapshot, waiting.id, UnitState.SYNCING)
            if (
                kind == "terminate-workers"
                or command.get("payload", {}).get("terminate_workers") is True
            ):
                snapshot.bindings["terminate_requested"] = True
        elif kind == "hold":
            unit = snapshot.units[key]
            from . import campaign_hold

            if unit.hold and unit.hold.kind != "operator":
                unit.evidence["operator_prior_hold"] = dataclasses.asdict(unit.hold)

            campaign_hold.write(
                campaign_hold.channel(self.store.directory(snapshot.id), key),
                campaign=snapshot.id,
                unit=key,
                hold=command["id"],
            )
            unit.bindings.pop("operator_release", None)
            unit.hold = Hold(
                "operator",
                command.get("reason", "operator hold"),
                id=command["id"],
                actor="operator",
                created_at=self.clock(),
            )
        elif kind == "release":
            unit = snapshot.units[key]
            if unit.hold and unit.hold.kind != "operator":
                raise CampaignError("release clears only an explicit operator hold")
            if unit.claim and not unit.claim.quiescent:
                raise CampaignError("operator takeover requires worker quiescence before release")
            if unit.hold is not None:
                unit.bindings["operator_release"] = {
                    "hold_id": unit.hold.id,
                    "command_id": command["id"],
                }
        elif kind == "reconcile":
            snapshot.bindings.pop("inventory", None)
        elif kind == "adopt":
            from .campaign_adoption import begin

            begin(snapshot, key, command["payload"]["adoption"], command_id=command["id"])
        else:
            raise CampaignError(f"unsupported campaign control: {kind}")

    def perform(self, snapshot: CampaignSnapshot, action) -> Observation:
        if action.generation != snapshot.generation:
            return Observation("rejected", reason="stale effect generation")
        if action.kind.startswith("handoff-"):
            from .campaign_handoff_flow import perform

            return perform(self, snapshot, action)
        if action.kind == "semantic-plan":
            from .campaign_reasoning_plan import perform

            return perform(self, snapshot, action)
        if action.kind.startswith("reasoning-"):
            from .campaign_reasoning_flow import perform

            return perform(self, snapshot, action)
        if action.unit in snapshot.units and action.kind not in {
            "launch",
            "observe-worker",
            "checkpoint-progress",
            "reflection",
            "release-observation",
            "stage-boundary-after",
            "terminate-worker",
            "decision-project",
        }:
            from . import campaign_hold

            current = self.store.load(snapshot.id)
            current_hold = current.units[action.unit].hold
            if (
                current_hold is not None and current_hold.kind == "operator"
            ) or campaign_hold.active(
                campaign_hold.channel(self.store.directory(snapshot.id), action.unit),
                campaign=snapshot.id,
                unit=action.unit,
            ):
                if action.kind == "github" and action.payload.get("purpose") == "decision-summary":
                    return Observation(
                        "waiting",
                        reason="operator hold delays decision summary publication",
                        retry_at=self.clock() + 5,
                    )
                return Observation("confirmed", {"operator_suppressed": True})
        handler = getattr(self, "_perform_" + action.kind.replace("-", "_"), None)
        if handler is None:
            return Observation("rejected", reason=f"unsupported campaign effect: {action.kind}")
        return handler(snapshot, action)

    def reconcile_action(self, snapshot: CampaignSnapshot, action) -> Observation:
        if action.kind.startswith("handoff-"):
            from .campaign_handoff_flow import perform

            return perform(self, snapshot, action)
        if action.kind == "semantic-plan":
            from .campaign_reasoning_plan import perform

            return perform(self, snapshot, action)
        if action.kind.startswith("reasoning-"):
            from .campaign_reasoning_flow import perform

            return perform(self, snapshot, action, reconcile=True)
        if action.kind.startswith("github"):
            intent = MutationIntent.from_dict(
                action.receipt.get("intent") or action.payload["intent"]
            )
            gateway = self.gateway(snapshot)
            if intent.kind in {"project_merge", "review_request"}:
                from .campaign_publication_flow import execute_special

                category = (
                    "merge-submissions" if intent.kind == "project_merge" else "review-submissions"
                )
                submitted = (
                    self.store.directory(snapshot.id)
                    / category
                    / (safe_segment(action.id) + ".json")
                )
                if not submitted.exists():
                    return self._perform_github(snapshot, action)
                return execute_special(self, snapshot, action, intent, reconcile=True)
            if intent.kind == "merge":
                submitted = (
                    self.store.directory(snapshot.id)
                    / "merge-submissions"
                    / (safe_segment(action.id) + ".json")
                )
                if not submitted.exists():
                    return self._perform_github(snapshot, action)
                with self._publication_lock:
                    current = self.store.load(snapshot.id)
                    unit = current.units[action.unit]
                    from . import campaign_hold

                    held = bool(unit.hold) or campaign_hold.active(
                        campaign_hold.channel(self.store.directory(snapshot.id), action.unit),
                        campaign=snapshot.id,
                        unit=action.unit,
                    )
                    adopted = dataclasses.replace(
                        intent, generation=current.generation, state="sent_unknown"
                    )
                    result = PublicationController(
                        gateway, requirements=PublicationRequirements.from_spec(current.spec.github)
                    ).reconcile_request(
                        adopted,
                        self._candidate(current, unit),
                        self._publication_evidence(current, unit),
                        generation=current.generation,
                        allow_retry=action.payload.get("retry_authorized", False) and not held,
                        merge_mode=current.spec.github.merge_mode,
                        merge_method=current.spec.github.merge_method,
                        independent_families=current.spec.github.require_independent_review,
                        queue_evidence_context=current.spec.github.candidate_check_context,
                    )
                return Observation(
                    "confirmed" if result.state == "confirmed" else "unknown",
                    {"intent": result.to_dict()},
                    result.error,
                )
            result = (
                gateway.reconcile_create(intent)
                if intent.kind in {"issue", "milestone", "pr", "comment", "queue_check"}
                else gateway.reconcile_update(
                    dataclasses.replace(intent, generation=snapshot.generation),
                    allow_retry=action.payload.get("retry_authorized", False),
                    generation=snapshot.generation,
                )
            )
            return Observation(
                "confirmed" if result.state == "confirmed" else "unknown",
                {"intent": result.to_dict()},
                result.error,
            )
        if action.kind == "launch":
            claim = snapshot.units[action.unit].claim
            if claim is None:
                return Observation("unknown", reason="launch ownership missing")
            directory = self.store.directory(snapshot.id) / "workers" / action.id
            startup = directory / "startup.json"
            if not startup.exists():
                return Observation(
                    "unknown", reason="launch acknowledgement not observed; replacement forbidden"
                )
            data = json.loads(startup.read_text(encoding="utf-8"))
            if data.get("launch_id") != action.id:
                return Observation("rejected", reason="launch binding mismatch")
            return Observation("confirmed", {"launched": True, "directory": str(directory)})
        if action.kind in {"push", "checkpoint"}:
            unit = snapshot.units[action.unit]
            pusher = self._pusher(snapshot, unit)
            if action.kind == "checkpoint":
                return self._reconcile_checkpoint(snapshot, action)
            row = pusher.reconcile(action.id)
            return Observation(
                "confirmed" if row.state == "confirmed" else "unknown", dataclasses.asdict(row)
            )
        # These effects are read-only, pure local state observations, or have
        # action-bound create/adopt logic. Re-running them does not launch twice.
        if action.kind in {
            "inventory",
            "source-observation",
            "reconcile-existing",
            "capacity",
            "intake",
            "hierarchy",
            "hierarchy-close",
            "prepare-pr",
            "prepare-ready",
            "prepare-review-requests",
            "pr-head-checkout",
            "pr-head-publish",
            "prepare-draft",
            "ready",
            "prepare-unit",
            "adopt-draft",
            "adopt-retire",
            "observe-worker",
            "checkpoint-progress",
            "release-observation",
            "stage-boundary-before",
            "stage-boundary-after",
            "prepare-merge",
            "observe-merge",
            "queue-observe",
            "queue-checkout",
            "queue-publish",
            "queue-dequeue",
            "sync-tracking",
            "reflection",
            "communication",
            "lifecycle",
            "planning-complete",
            "intake-control",
            "intake-cancel",
            "intake-note",
            "intake-refresh",
            "intake-cancel-issue",
            "intake-completion-note",
            "usage-hold",
            "resolution-context",
            "remote-wait",
            "decision-project",
            "resolution-backoff",
            "resolution-reprobe",
            "resolution-accept",
            "resolution-finish",
            "resolution-exhausted",
            "terminate-worker",
        }:
            return self.perform(dataclasses.replace(snapshot, generation=action.generation), action)
        return Observation("unknown", reason="effect requires explicit reconciliation")

    def authorize_backup_resume(self, snapshot) -> None:
        """Capture CLI resume intent; re-arm authority is saved under coordinator lock."""
        self._backup_resume_epoch = uuid.uuid4().hex
        self._backup_resume_actions = {
            row.id
            for row in snapshot.outbox
            if row.kind == "checkpoint"
            and row.state in {"prepared", "sent_unknown"}
            and row.receipt.get("backup_failures", 0)
            >= snapshot.spec.checkpoints.max_backup_failures
        }

    def prepare_reconciliation(self, snapshot, action) -> None:
        if action.kind == "checkpoint":
            epoch = getattr(self, "_backup_resume_epoch", "")
            if (
                not epoch
                or action.id not in getattr(self, "_backup_resume_actions", set())
                or action.receipt.get("backup_retry_epoch") == epoch
            ):
                return
            unit = snapshot.units[action.unit]
            from . import campaign_hold

            if (
                (unit.hold and unit.hold.kind != "technical_failure")
                or snapshot.phase not in {"active", "stopped"}
                or campaign_hold.active(
                    campaign_hold.channel(self.store.directory(snapshot.id), unit.id),
                    campaign=snapshot.id,
                    unit=unit.id,
                )
            ):
                return
            try:
                prior = next(
                    (
                        row
                        for row in self._pusher(snapshot, unit).observations()
                        if row.action_id == action.id
                    ),
                    None,
                )
            except Exception as exc:
                add_event(
                    snapshot,
                    "checkpoint-rearm-unavailable",
                    unit=unit.id,
                    action=action.id,
                    reason=str(exc),
                )
                return
            if prior is not None and prior.state != "prepared":
                return
            failures = action.receipt.get("backup_failures", 0)
            if failures < snapshot.spec.checkpoints.max_backup_failures:
                return
            history = list(action.receipt.get("backup_retry_history", []))
            history.append(
                {
                    "epoch": action.receipt.get("backup_retry_epoch", "initial"),
                    "failures": failures,
                    "generation": snapshot.generation,
                }
            )
            action.receipt.update(
                backup_retry_epoch=epoch,
                backup_failures=0,
                backup_retry_exhausted=False,
                backup_retry_history=history,
            )
            action.next_retry_at = 0
            add_event(
                snapshot,
                "checkpoint-write-budget-rearmed",
                unit=unit.id,
                action=action.id,
                epoch=epoch,
                prior_failures=failures,
            )
            return
        if not action.kind.startswith("github"):
            return
        intent = action.receipt.get("intent") or action.payload["intent"]
        if intent["kind"] in {"project_merge", "review_request"}:
            # Their reconciliation protocols are observation-only. Never grant
            # generic update retry permission to another outbound submission.
            return
        if intent["kind"] in {"issue", "milestone", "pr", "comment", "queue_check"}:
            return
        unit = snapshot.units[action.unit]
        if action.payload.get("purpose") != "merge":
            window = action.payload.get("retry_window_at", self.clock())
            if self.clock() - window >= 15:
                action.payload["update_retries"] = 0
                window = self.clock()
            action.payload["retry_window_at"] = window
            attempts = action.payload.get("update_retries", 0)
            action.payload["retry_authorized"] = attempts < 3 and not unit.hold
            if action.payload["retry_authorized"]:
                action.payload["update_retries"] = attempts + 1
                add_event(
                    snapshot,
                    "metadata-retry-authorized",
                    unit=unit.id,
                    action=action.id,
                    attempt=attempts + 1,
                    original_generation=action.generation,
                )
            return
        attempts = unit.evidence.get("publication_retries", 0)
        limit = self.template(snapshot).policy.limits.max_review_cycles
        has_request = bool((intent.get("receipt") or {}).get("details", {}).get("uuid"))
        action.payload["retry_authorized"] = not has_request and attempts < limit and not unit.hold
        if action.payload["retry_authorized"]:
            unit.evidence["publication_retries"] = attempts + 1
            add_event(
                snapshot,
                "merge-retry-authorized",
                unit=unit.id,
                action=action.id,
                attempt=attempts + 1,
                original_generation=action.generation,
            )

    def _native_mux(self, snapshot):
        from .adapters.paradev_backend import ParadevMultiplexer

        return ParadevMultiplexer(state_dir=self.store.directory(snapshot.id) / "native-transport")

    @staticmethod
    def _reflection_fingerprint(unit) -> str:
        return hashlib.sha256(
            json.dumps(
                {
                    "state": str(unit.state),
                    "hold": dataclasses.asdict(unit.hold) if unit.hold else None,
                    "claim": dataclasses.asdict(unit.claim) if unit.claim else None,
                    "pr": unit.bindings.get("pr"),
                },
                sort_keys=True,
            ).encode()
        ).hexdigest()

    def _perform_communication(self, snapshot, action) -> Observation:
        from .campaign_reflection_flow import observe_communication

        observer = self._hcom_observer(snapshot)
        if observer is None:
            return Observation("confirmed", {"mode": "off", "observed_at": self.clock()})
        result = observe_communication(snapshot, observer=observer)
        return Observation("confirmed", {**result, "observed_at": self.clock()})

    def _perform_reflection(self, snapshot, action) -> Observation:
        from .campaign_paradev import ParadevWorkspaceProvider
        from .campaign_reflection_flow import reflect

        unit = snapshot.units[action.unit]
        provider = ParadevWorkspaceProvider(
            project_id=snapshot.spec.paradev.project_id,
            allowed_root=Path(snapshot.spec.paradev.worktree_root),
            repository=self.project,
            state_dir=self.store.directory(snapshot.id) / "reflection",
        )
        mux = self._native_mux(snapshot)
        result = reflect(snapshot, unit, unit.claim, provider=provider, mux=mux)
        bridge = self._lifecycle_bridge(snapshot, mux=mux, provider=provider)
        try:
            result["lifecycle"] = bridge.observe(
                snapshot, unit, observer=self._hcom_observer(snapshot), projection=result["stage"]
            )
        except Exception as exc:
            result["lifecycle"] = {
                "state": "unknown",
                "workspace_retained": True,
                "detail": "Lifecycle observation unavailable: " + type(exc).__name__,
            }
        result["observed_at"] = self.clock()
        result["fingerprint"] = self._reflection_fingerprint(unit)
        payload = json.dumps(result, sort_keys=True)
        history = self.store.directory(snapshot.id) / "reflection-history" / safe_segment(unit.id)
        history.mkdir(parents=True, exist_ok=True)
        path = history / (hashlib.sha256(payload.encode()).hexdigest() + ".json")
        atomic_write_text(path, payload, follow_symlinks=False)
        result["history_path"] = str(path)
        return Observation("confirmed", result)

    def _hcom_observer(self, snapshot):
        if snapshot.spec.communication.mode == "off":
            return None
        from .campaign_communication import HcomObserver

        name = os.environ.get("HCOM_INSTANCE_NAME") or os.environ.get("HCOM_NAME") or "bmad"
        return HcomObserver(room=Path(snapshot.spec.communication.room), observer_name=name)

    def _lifecycle_bridge(self, snapshot, *, mux=None, provider=None):
        from .campaign_lifecycle import CampaignLifecycle
        from .campaign_paradev import ParadevWorkspaceProvider

        provider = provider or ParadevWorkspaceProvider(
            project_id=snapshot.spec.paradev.project_id,
            allowed_root=Path(snapshot.spec.paradev.worktree_root),
            repository=self.project,
            state_dir=self.store.directory(snapshot.id) / "reflection",
        )
        return CampaignLifecycle(
            self.store.directory(snapshot.id) / "lifecycle",
            mux=mux or self._native_mux(snapshot),
            provider=provider,
        )

    def _perform_lifecycle(self, snapshot, action) -> Observation:
        from .campaign_lifecycle import LifecyclePolicy

        try:
            result = self._lifecycle_bridge(snapshot).retire(
                snapshot,
                snapshot.units[action.unit],
                action.id,
                policy=LifecyclePolicy(retention=snapshot.spec.paradev.retention),
            )
        except Exception as exc:
            result = {
                "state": "retained",
                "detail": "Lifecycle retirement unqualified: " + str(exc),
                "workspace_retained": True,
                "worktree_retained": True,
                "git_data_retained": True,
            }
        return Observation("confirmed", result)

    def _pusher(self, snapshot: CampaignSnapshot, unit: CampaignUnit) -> CheckpointPusher:
        return CheckpointPusher(
            Path(unit.bindings["workspace"]),
            self.store.directory(snapshot.id) / "publication",
            remote=snapshot.spec.remote,
            branch=unit.bindings["branch"],
        )

    def _source_publisher(self, snapshot, *, project: Path | None = None):
        from .campaign_source_publication import SourcePublication

        return SourcePublication(
            project or self.project,
            self.store.directory(snapshot.id) / "source-publication",
            remote=snapshot.spec.remote,
            target=snapshot.spec.target,
        )

    def _operation_journal(self, snapshot, action) -> Journal:
        directory = self.store.directory(snapshot.id) / "tracking" / action.id
        directory.mkdir(parents=True, exist_ok=True)
        pin = RunState(
            action.id, str(self.project), "", run_dir_identity=root_identity_record(directory)
        )
        return Journal(directory, pin=pin)

    def _perform_inventory(self, snapshot, action) -> Observation:
        mapped = tuple(
            sorted(
                {
                    unit.bindings["pr"]["number"]
                    for unit in snapshot.units.values()
                    if unit.bindings.get("pr")
                }
            )
        )
        since = (
            (
                datetime.fromtimestamp(self.clock(), timezone.utc)
                - timedelta(days=snapshot.spec.github.recent_days)
            )
            .isoformat()
            .replace("+00:00", "Z")
        )
        scope_sets = [
            tuple(
                path.replace("\\", "/")
                for path in (*unit.spec.scopes, *unit.spec.paths)
                if path and not Path(path).is_absolute() and ":" not in path
            )
            for unit in snapshot.units.values()
        ]
        relevant_paths = (
            tuple(sorted({path for paths in scope_sets for path in paths}))
            if all(scope_sets)
            else ()
        )
        receipt = RepositoryDiscovery(
            self.gateway(snapshot),
            self.project,
            cache=self.__dict__.setdefault("_discovery_objects", {}),
        ).collect(
            remote=snapshot.spec.remote,
            recent_since=since,
            mapped_prs=mapped,
            detailed_prs=None,
            archive_roots=tuple(Path(path) for path in snapshot.spec.archive_roots),
            target=snapshot.spec.target,
            relevant_paths=relevant_paths,
        )
        result = {**receipt.to_dict(), "digest": receipt.digest}
        from .campaign_adoption import AdoptionError, guard_owned

        adoption_fences = {}
        for key, unit in snapshot.units.items():
            if (
                unit.bindings.get("adoption", {}).get("state") != "owned"
                or unit.state
                in {
                    UnitState.MERGE_REQUESTED,
                    UnitState.MERGED_PENDING_SYNC,
                    UnitState.DELIVERED,
                    UnitState.CANCELED,
                }
                or (unit.claim and not unit.claim.quiescent)
            ):
                continue
            try:
                guard_owned(self, snapshot, key)
                adoption_fences[key] = {"valid": True}
            except AdoptionError as exc:
                adoption_fences[key] = {"valid": False, "reason": str(exc)}
        result["adoption_fences"] = adoption_fences
        if snapshot.spec.paradev.project_id:
            from .campaign_native_inventory import native_inventory

            observed = native_inventory(
                self.project,
                snapshot.spec,
                gateway=self.gateway(snapshot),
                campaign_root=self.store.root,
            )
            result["external_ownership"] = dataclasses.asdict(observed)
            if not observed.complete:
                receipt = dataclasses.replace(
                    receipt,
                    complete=False,
                    unavailable=(*receipt.unavailable, *observed.unavailable, *observed.truncated),
                )
                result.update(receipt.to_dict(), digest=receipt.digest)
        return Observation("confirmed", result)

    def _perform_source_observation(self, snapshot, action) -> Observation:
        imported = import_sources(self.project, dataclasses.replace(snapshot.spec, units=()))
        return Observation(
            "confirmed", {"observations": imported.observations, "observed_at": self.clock()}
        )

    def _perform_reconcile_existing(self, snapshot, action) -> Observation:
        from .campaign_existing import ExistingDeliveryHeld, prior_delivery

        try:
            proof = prior_delivery(self, snapshot, snapshot.units[action.unit])
        except ExistingDeliveryHeld as exc:
            return Observation("confirmed", {"existing_hold": str(exc)})
        return Observation(
            "confirmed", {"proof": dataclasses.asdict(proof) if proof is not None else None}
        )

    def _perform_capacity(self, snapshot, action) -> Observation:
        observed_at = self.clock()
        observations = []
        seen = set()
        for role in set(dict(snapshot.spec.roles)) | {
            "planning",
            "planning_review",
            "triage",
            "escalation",
            "decision_review",
            "conflict_repair",
            "publication_review",
        }:
            for candidate in snapshot.spec.candidates(role):
                identity = (candidate.profile, candidate.model)
                if identity in seen:
                    continue
                seen.add(identity)
                result = self.capacity_observer(candidate, self.project)
                observations.append(
                    {
                        "profile": candidate.profile,
                        "model": candidate.model,
                        "capacity": dataclasses.asdict(result),
                    }
                )
        return Observation("confirmed", {"observations": observations, "observed_at": observed_at})

    def _perform_intake(self, snapshot, action) -> Observation:
        from .campaign_runtime_intake import collect

        return Observation(
            "confirmed",
            collect(
                snapshot,
                self.gateway(snapshot),
                now=self.clock(),
                prior_projections=tuple(
                    prior
                    for prior in self.store.list()
                    if prior.id != snapshot.id
                    and prior.repository == snapshot.repository
                    and prior.common_dir == snapshot.common_dir
                ),
            ),
        )

    def _perform_intake_control(self, snapshot, action) -> Observation:
        return Observation("confirmed", action.payload)

    def _perform_intake_note(self, snapshot, action) -> Observation:
        from .campaign_runtime_intake import disposition_intent

        intent = disposition_intent(
            snapshot, action.unit, self.gateway(snapshot), action_id=action.id + "-note"
        )
        return Observation("confirmed", {"intent": intent.to_dict(), "purpose": "intake-note"})

    def _perform_intake_completion_note(self, snapshot, action) -> Observation:
        from .campaign_runtime_intake import completion_intent

        intent = completion_intent(
            snapshot, action.unit, self.gateway(snapshot), action_id=action.id + "-completed"
        )
        return Observation(
            "confirmed", {"intent": intent.to_dict(), "purpose": "intake-completion-note"}
        )

    def _perform_intake_cancel(self, snapshot, action) -> Observation:
        from .campaign_runtime_intake import cancellation_intent

        intent = cancellation_intent(
            snapshot, action.unit, self.gateway(snapshot), action_id=action.id + "-cancel"
        )
        return Observation("confirmed", {"intent": intent.to_dict(), "purpose": "intake-cancel"})

    def _perform_intake_cancel_issue(self, snapshot, action) -> Observation:
        from .campaign_runtime_intake import issue_cancellation_intent

        intent = issue_cancellation_intent(
            snapshot, action.unit, self.gateway(snapshot), action_id=action.id + "-issue"
        )
        return Observation(
            "confirmed", {"intent": intent.to_dict(), "purpose": "intake-cancel-issue"}
        )

    def _perform_intake_refresh(self, snapshot, action) -> Observation:
        from .campaign_intake_amendment import AmendmentUncertain, perform

        try:
            return Observation("confirmed", perform(self, snapshot, action))
        except AmendmentUncertain as exc:
            return Observation("unknown", reason=str(exc), retry_at=self.clock() + 5)
        except CampaignError as exc:
            return Observation("rejected", reason=str(exc))

    def _perform_prepare_unit(self, snapshot, action) -> Observation:
        unit = snapshot.units[action.unit]
        if action.payload.get("adoption"):
            from .campaign_adoption import AdoptionError, AdoptionUncertain, prepare

            try:
                return Observation("confirmed", prepare(self, snapshot, action))
            except AdoptionUncertain as exc:
                return Observation("unknown", reason=str(exc), retry_at=self.clock() + 5)
            except AdoptionError as exc:
                return Observation("rejected", reason=str(exc))
        branch = f"bmad/{snapshot.id}/{safe_segment(unit.id)}"
        root = Path(
            snapshot.spec.paradev.worktree_root
            if snapshot.spec.paradev.reflect
            else snapshot.spec.worktree_root
        )
        if not root.is_absolute():
            raise CampaignError("explicit absolute worktree root required")
        state_dir = self.store.directory(snapshot.id) / "allocations" / action.id
        state_dir.mkdir(parents=True, exist_ok=True)
        intent = state_dir / "intent.json"
        if not intent.exists():
            atomic_write_text(
                intent, json.dumps({"unit": unit.id, "branch": branch, "action": action.id})
            )
        observation_ref = f"refs/bmad-campaign/{snapshot.id}/allocation/{action.id}"
        if not verify.ref_exists_checked(self.project, observation_ref):
            git(
                self.project,
                "fetch",
                "--no-tags",
                snapshot.spec.remote,
                f"refs/heads/{snapshot.spec.target}:{observation_ref}",
            )
        base = verify.rev_parse_revision(self.project, observation_ref)
        if snapshot.spec.paradev.reflect:
            from .campaign_paradev import ParadevWorkspaceProvider
            from .campaign_reflection_flow import group_identity

            provider = ParadevWorkspaceProvider(
                project_id=snapshot.spec.paradev.project_id,
                allowed_root=root,
                repository=self.project,
                state_dir=self.store.directory(snapshot.id) / "reflection",
            )
            binding = provider.allocate(
                unit.id,
                branch,
                unit.spec.title,
                action_id=action.id,
                target=snapshot.spec.target,
                group=group_identity(unit.spec, snapshot.spec.paradev.group_by),
            )
            workspace = Path(binding.path)
            native = dataclasses.asdict(binding)
        else:
            workspace = root / safe_segment(unit.id + "-" + action.id[:8])
            workspace = allocate_worktree(
                self.project,
                root,
                branch=branch,
                unit_id=workspace.name,
                base=base,
                allocation_dir=state_dir,
            )
            native = {}
        self._seed_checkout(workspace)
        source = self.source(snapshot, unit)
        canonical_binding = None
        initial_source = None
        if unit.spec.kind == "planning":
            source = next((row for row in snapshot.spec.sources if row.kind == "tickets"), source)
        source_marker = state_dir / "source.json"
        if source.kind != "explicit" and (not source_marker.exists() or source.kind == "tickets"):
            prepared = prepare_source_view(
                self.project, workspace, source, authority_dir=state_dir / "source-views"
            )
            atomic_write_text(
                source_marker,
                json.dumps(
                    {
                        "output_folder": str(prepared.paths.output_folder),
                        "effective_source": dataclasses.asdict(prepared.selector),
                        "private_paths": [str(path) for path in prepared.private_paths],
                    }
                ),
            )
        source_binding = (
            json.loads(source_marker.read_text(encoding="utf-8")) if source_marker.exists() else {}
        )
        planning_manifest = ""
        effective_sources = []
        private_paths = list(source_binding.get("private_paths", []))
        if unit.spec.kind == "planning":
            from .campaign_planning_publication import capture

            pairs = []
            for configured in snapshot.spec.sources:
                if configured.id == source.id and source_binding.get("effective_source"):
                    effective = SourceSelector(**source_binding["effective_source"])
                else:
                    prepared = prepare_source_view(
                        self.project,
                        workspace,
                        configured,
                        authority_dir=state_dir / "source-views",
                    )
                    effective = prepared.selector
                    private_paths.extend(str(path) for path in prepared.private_paths)
                pairs.append((configured, effective))
                effective_sources.append(dataclasses.asdict(effective))
            manifest_path = state_dir / "planning-preimages.json"
            capture(
                manifest_path,
                project=self.project,
                workspace=workspace,
                sources=tuple(pairs),
                source_authority=state_dir / "source-views",
            )
            planning_manifest = str(manifest_path)
        if unit.spec.kind not in {"planning", "tracking"}:
            effective = SourceSelector(
                **source_binding.get("effective_source", dataclasses.asdict(source))
            )
            observed_source = self._source_publisher(snapshot, project=workspace).capture(
                effective, unit.spec
            )
            canonical_binding = (
                observed_source
                if observed_source.versioning == "code-repo"
                else self._source_publisher(snapshot).capture(source, unit.spec)
            ).to_dict()
            if observed_source.path:
                initial_source = {
                    "path": Path(observed_source.path).relative_to(workspace).as_posix(),
                    "sha256": observed_source.revision,
                }
        # This product specification must be tracked even when .bmad-loop is
        # ignored. Use a repository-owned docs path instead of force-adding logs.
        spec_path = workspace / "docs" / "bmad-campaign" / f"{safe_segment(unit.id)}.md"
        spec_path.parent.mkdir(parents=True, exist_ok=True)
        from .campaign_product_input import collect as selected_product_input

        try:
            native_input = selected_product_input(
                workspace,
                SourceSelector(
                    **source_binding.get("effective_source", dataclasses.asdict(source))
                ),
                unit.spec,
            )
            specification = execution_spec(
                unit.spec,
                campaign_id=snapshot.id,
                graph_revision=snapshot.graph_revision,
                source=snapshot.bindings.get("source_observations", {}).get(unit.id, {}),
                native_input=native_input,
            )
        except CampaignError as exc:
            return Observation("rejected", reason=str(exc))
        if unit.spec.kind == "planning" and snapshot.spec.planning.brief:
            brief = Path(snapshot.spec.planning.brief)
            if not brief.is_absolute():
                brief = self.project / brief
            if brief.is_file() and brief.stat().st_size <= 1024 * 1024 and not brief.is_symlink():
                specification += "\nApproved brief\n\n" + brief.read_text(encoding="utf-8")
            else:
                specification += "\nApproved brief\n\n" + snapshot.spec.planning.brief
        from .campaign_runtime_intake import baseline as intake_baseline

        specification += "\n" + intake_baseline(snapshot, unit.id)
        spec_path.write_text(specification, encoding="utf-8")
        verify.commit_paths(
            workspace, f"docs: specify campaign unit {unit.spec.title}", [spec_path]
        )
        head = verify.rev_parse_head(workspace)
        return Observation(
            "confirmed",
            {
                "workspace": str(workspace),
                "branch": branch,
                "base": base,
                "head": head,
                "native": native,
                "effective_source": source_binding.get(
                    "effective_source", dataclasses.asdict(source)
                ),
                "private_paths": list(dict.fromkeys(private_paths)),
                "canonical_source": canonical_binding,
                "canonical_selector": dataclasses.asdict(self.source(snapshot, unit)),
                "source_authority_root": str(
                    self.store.directory(snapshot.id) / "source-publication"
                ),
                "initial_source": initial_source,
                "planning_manifest": planning_manifest,
                "effective_sources": effective_sources,
                "spec_path": str(spec_path.relative_to(workspace)),
            },
        )

    def _seed_checkout(self, workspace: Path) -> None:
        for relative in (
            ".bmad-loop/policy.toml",
            ".bmad-loop/profiles",
            "_bmad",
            ".agents/skills",
            ".claude/skills",
        ):
            source, destination = self.project / relative, workspace / relative
            if not source.exists() or destination.exists():
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            if source.is_dir():
                shutil.copytree(
                    source,
                    destination,
                    ignore=shutil.ignore_patterns(".git", "node_modules", "runs"),
                )
            else:
                shutil.copyfile(source, destination)

    def _perform_hierarchy(self, snapshot, action) -> Observation:
        receipts = {
            row.payload["tracking_key"]: MutationIntent.from_dict(row.receipt["intent"])
            for row in snapshot.outbox
            if row.payload.get("tracking_key") and row.receipt.get("intent")
        }
        result = HierarchyCoordinator(self.gateway(snapshot)).step(
            snapshot,
            action.unit,
            source_metadata=snapshot.bindings.get("source_observations", {}),
            receipts=receipts,
        )
        from .campaign_tracking_delta import observation

        return Observation("confirmed", observation(snapshot, result, action.unit))

    def _perform_hierarchy_close(self, snapshot, action) -> Observation:
        if not self._units_settled(snapshot):
            return Observation("confirmed", {"stale_scope": True})
        receipts = {
            row.payload["tracking_key"]: MutationIntent.from_dict(row.receipt["intent"])
            for row in snapshot.outbox
            if row.payload.get("tracking_key") and row.receipt.get("intent")
        }
        result = HierarchyCoordinator(self.gateway(snapshot)).close_parents(
            snapshot, receipts=receipts
        )
        from .campaign_tracking_delta import observation

        return Observation("confirmed", observation(snapshot, result))

    def _perform_prepare_pr(self, snapshot, action) -> Observation:
        unit = snapshot.units[action.unit]
        pusher = self._pusher(snapshot, unit)
        head = verify.rev_parse_head(Path(unit.bindings["workspace"]))
        receipt = pusher.push(action.id + "-initial", head)
        if receipt.state != "confirmed":
            return Observation("unknown", reason="initial branch push requires reconciliation")
        issue = unit.bindings["issue"]
        intent = self.gateway(snapshot).prepare_create(
            kind="pr",
            action_id=action.id + "-create",
            campaign_id=snapshot.id,
            unit_id=unit.id,
            title=unit.spec.title,
            body=f"Implement campaign unit {unit.id}.\n\nReferences {issue['url']}\n\nSource: {unit.bindings['spec_path']}",
            branch=unit.bindings["branch"],
            target=snapshot.spec.target,
            generation=snapshot.generation,
        )
        return Observation("confirmed", {"intent": intent.to_dict(), "purpose": "pr"})

    def _perform_ready(self, snapshot, action) -> Observation:
        return Observation("confirmed")

    def _perform_stage_boundary_before(self, snapshot, action) -> Observation:
        from .campaign_boundary_sync import before_effect

        return before_effect(self, snapshot, action)

    def _perform_stage_boundary_after(self, snapshot, action) -> Observation:
        from .campaign_boundary_sync import after_effect

        return after_effect(self, snapshot, action)

    def _perform_adopt_draft(self, snapshot, action) -> Observation:
        from .campaign_adoption import AdoptionError, perform_draft

        try:
            return Observation("confirmed", perform_draft(self, snapshot, action))
        except AdoptionError as exc:
            return Observation("rejected", reason=str(exc))

    def _perform_adopt_retire(self, snapshot, action) -> Observation:
        from .campaign_adoption import AdoptionError, perform_retire

        try:
            return Observation("confirmed", perform_retire(self, snapshot, action))
        except AdoptionError as exc:
            return Observation("rejected", reason=str(exc))

    def _perform_release_observation(self, snapshot, action) -> Observation:
        from .campaign_takeover import observe as observe_release

        try:
            return Observation("confirmed", observe_release(self, snapshot, action))
        except CampaignError as exc:
            return Observation(
                "confirmed", {"release_refused": str(exc), "hold_id": action.payload["hold_id"]}
            )

    def _perform_usage_hold(self, snapshot, action) -> Observation:
        return Observation("confirmed", action.payload)

    def _perform_planning_complete(self, snapshot, action) -> Observation:
        return Observation("confirmed")

    def _perform_resolution_context(self, snapshot, action) -> Observation:
        from .campaign_decision_sources import github_provider
        from .campaign_resolution_flow import collect_context
        from .campaign_runtime_decisions import MappedEvidence
        from .campaign_runtime_decisions import collect as collect_mapped

        current = self.store.load(snapshot.id)
        unit = current.units[action.unit]
        if (
            not unit.hold
            or (unit.claim and not unit.claim.quiescent)
            or (action.payload.get("hold_id") and unit.hold.id != action.payload["hold_id"])
            or (action.payload.get("head") and unit.bindings.get("head") != action.payload["head"])
        ):
            return Observation("confirmed", {"stale_context": True})
        mapped = (
            collect_mapped(self, current, unit)
            if action.payload["mode"] == "decision"
            else MappedEvidence()
        )
        result = collect_context(
            self.project,
            current,
            action.unit,
            action.payload["mode"],
            providers={
                "github": github_provider(self.gateway(snapshot)),
                **self.evidence_providers,
            },
            mapped_sources=mapped.sources,
            facts=mapped.facts,
            mapped_unavailable=mapped.unavailable,
        )
        result["observed_at"] = self.clock()
        return Observation("confirmed", result)

    def _perform_decision_project(self, snapshot, action) -> Observation:
        from .campaign_runtime_decisions import project

        return Observation("confirmed", project(self, snapshot, action))

    def _perform_remote_wait(self, snapshot, action) -> Observation:
        from .campaign_remote_waits import observe

        data = observe(self, snapshot, action)
        return Observation("confirmed", data)

    def _perform_resolution_backoff(self, snapshot, action) -> Observation:
        return Observation("confirmed")

    def _perform_resolution_reprobe(self, snapshot, action) -> Observation:
        unit = snapshot.units[action.unit]
        role = unit.claim.role if unit.claim else "escalation"
        available = self._role(snapshot, role)
        return Observation("confirmed", {"available": available is not None})

    def _perform_resolution_finish(self, snapshot, action) -> Observation:
        from .campaign_resolution_delivery import finish

        return finish(self, snapshot, snapshot.units[action.unit], action)

    def _perform_resolution_exhausted(self, snapshot, action) -> Observation:
        return Observation("confirmed", dict(action.payload))

    def _perform_terminate_worker(self, snapshot, action) -> Observation:
        unit = snapshot.units[action.unit]
        claim = unit.claim
        if claim is None or claim.launch_id != action.payload["launch_id"]:
            return Observation("rejected", reason="stop claim binding changed")
        directory = self.store.directory(snapshot.id) / "workers" / claim.launch_id
        startup = directory / "startup.json"
        if not startup.exists():
            return Observation("unknown", reason="cannot stop an unacknowledged worker launch")
        binding = json.loads(startup.read_text(encoding="utf-8"))
        if (
            binding.get("launch_id") != claim.launch_id
            or binding.get("generation") != claim.generation
        ):
            return Observation(
                "rejected", reason="stop would address a different worker generation"
            )
        request = json.loads((directory / "request.json").read_text(encoding="utf-8"))
        targets = [directory]
        if request["kind"] == "execution":
            targets.append(runs.run_dir_for(Path(claim.workspace), claim.run_id))
        elif request["kind"] == "stage":
            targets.append(directory / claim.run_id)
        elif request["kind"] == "recovery":
            from .campaign_finalization import continuation_run_id

            retained_id = continuation_run_id(request)
            if retained_id:
                targets.append(runs.run_dir_for(Path(claim.workspace), retained_id))
        for target in targets:
            if target.exists():
                atomic_write_text(
                    target / runs.STOP_REQUEST_FILE,
                    json.dumps(
                        {"mode": "hard", "campaign": snapshot.id, "launch_id": claim.launch_id}
                    ),
                    follow_symlinks=False,
                )
        return Observation("confirmed", {"launch_id": claim.launch_id})

    def _perform_github(self, snapshot, action) -> Observation:
        intent = MutationIntent.from_dict(action.payload["intent"])
        if intent.kind not in {"merge", "project_merge"}:
            return self._perform_github_submission(snapshot, action)
        # CI/model/test waits happen in independent prepare effects. Serialize
        # only the last fresh gate and bounded request, never their wait period.
        with self._publication_lock:
            return self._perform_github_submission(snapshot, action)

    def _perform_github_submission(self, snapshot, action) -> Observation:
        intent = MutationIntent.from_dict(action.payload["intent"])
        if action.payload.get("purpose") == "adoption-draft":
            from .campaign_adoption import guard_draft_submission

            guard_draft_submission(self, self.store.load(snapshot.id), action.unit, intent)
        if action.payload.get("purpose") == "adoption-retire":
            from .campaign_adoption import guard_retire_submission

            guard_retire_submission(self, self.store.load(snapshot.id), action.unit, intent)
        if intent.kind in {"merge", "project_merge"}:
            current = self.store.load(snapshot.id)
            from . import campaign_hold

            if current.units[action.unit].hold or campaign_hold.active(
                campaign_hold.channel(self.store.directory(snapshot.id), action.unit),
                campaign=snapshot.id,
                unit=action.unit,
            ):
                return Observation(
                    "waiting",
                    reason="operator hold prevents merge submission",
                    retry_at=self.clock() + 1,
                )
            if current.spec.github.merge_mode != "queue":
                submitted = self.store.directory(snapshot.id) / "merge-submissions"
                if any(
                    row.id != action.unit and row.state == UnitState.MERGE_REQUESTED
                    for row in current.units.values()
                ) or any(
                    pending.id != action.id
                    and pending.payload.get("purpose") == "merge"
                    and pending.state == "sent_unknown"
                    and (submitted / (safe_segment(pending.id) + ".json")).exists()
                    for pending in current.outbox
                ):
                    return Observation(
                        "waiting",
                        reason="an issued direct merge awaits canonical reconciliation",
                        retry_at=self.clock() + 1,
                    )
            if current.phase != "active":
                return Observation(
                    "confirmed", {"intent": intent.to_dict(), "canceled_before_write": True}
                )
            unit = current.units[action.unit]
            candidate = self._candidate(current, unit)
            coordinates = intent.coordinates or {}
            if any(
                coordinates.get(key) != value
                for key, value in dataclasses.asdict(candidate).items()
            ):
                return Observation(
                    "confirmed",
                    {
                        "intent": intent.to_dict(),
                        "canceled_before_write": True,
                        "needs_integration": True,
                    },
                )
            checked = self._perform_prepare_merge(
                current, dataclasses.replace(action, id=action.id.removesuffix("-merge"))
            )
            if checked.state != "confirmed":
                return checked
            if not checked.data.get("intent"):
                return Observation(
                    "confirmed",
                    {**checked.data, "intent": intent.to_dict(), "canceled_before_write": True},
                )
            refreshed = MutationIntent.from_dict(checked.data["intent"])
            if refreshed.payload != intent.payload:
                return Observation(
                    "rejected", reason="final merge contract changed before submission"
                )
            intent = dataclasses.replace(refreshed, action_id=intent.action_id)
        if action.payload.get("purpose") == "parent-tracking":
            current = self.store.load(snapshot.id)
            if action.payload.get(
                "scope_revision"
            ) != current.graph_revision or not self._units_settled(current):
                return Observation(
                    "confirmed", {"intent": intent.to_dict(), "canceled_before_write": True}
                )
        if intent.state == "confirmed":
            return Observation("confirmed", {"intent": intent.to_dict()})
        if intent.kind in {"merge", "project_merge", "review_request"}:
            category = (
                "review-submissions" if intent.kind == "review_request" else "merge-submissions"
            )
            submitted = self.store.directory(snapshot.id) / category
            submitted.mkdir(parents=True, exist_ok=True)
            atomic_write_text(
                submitted / (safe_segment(action.id) + ".json"),
                json.dumps({"action_id": action.id, "coordinates": intent.coordinates}),
                follow_symlinks=False,
            )
        if intent.kind in {"project_merge", "review_request"}:
            from .campaign_publication_flow import execute_special

            return execute_special(self, snapshot, action, intent)
        result = self.gateway(snapshot).execute(
            dataclasses.replace(intent, state="sent_unknown"), generation=snapshot.generation
        )
        return Observation(
            "confirmed" if result.state == "confirmed" else "unknown",
            {"intent": result.to_dict()},
            result.error,
        )

    def _perform_push(self, snapshot, action) -> Observation:
        unit = snapshot.units[action.unit]
        row = self._pusher(snapshot, unit).push(action.id, action.payload["head"])
        return Observation(
            "confirmed" if row.state == "confirmed" else "unknown", dataclasses.asdict(row)
        )

    def _perform_checkpoint(self, snapshot, action) -> Observation:
        unit = snapshot.units[action.unit]
        from . import campaign_hold
        from .campaign_checkpoint import read as read_checkpoint
        from .campaign_checkpoint import request as checkpoint_request

        if campaign_hold.active(
            campaign_hold.channel(self.store.directory(snapshot.id), unit.id),
            campaign=snapshot.id,
            unit=unit.id,
        ):
            return self._checkpoint_wait(snapshot, action, "operator hold delays checkpoint backup")
        launch_id = action.payload.get("launch_id")
        if not launch_id or not action.payload.get("progress"):
            return Observation(
                "rejected", reason="checkpoint requires an immutable worker progress receipt"
            )
        if (
            action.receipt.get("backup_failures", 0)
            >= snapshot.spec.checkpoints.max_backup_failures
        ):
            return self._checkpoint_wait(
                snapshot, action, "backup write budget exhausted; no new push is authorized"
            )
        directory, expected = checkpoint_request(self, snapshot, unit, launch_id)
        progress = read_checkpoint(
            directory, expected, now=time.time(), max_age_s=None, receipt=action.payload["progress"]
        )
        pusher = self._pusher(snapshot, unit)
        prior = next((row for row in pusher.observations() if row.action_id == action.id), None)
        if prior is not None:
            # This durable branch intent was written after progress validation.
            # Even prepared writes retain that exact SHA across worker progress.
            head = prior.head
            coalescing = "frozen_branch_intent"
        else:
            try:
                latest = read_checkpoint(directory, expected, now=time.time(), max_age_s=None)
            except (OSError, ValueError, KeyError, CampaignError) as exc:
                return self._checkpoint_wait(
                    snapshot,
                    action,
                    str(exc),
                    {"head": progress["head"], "coalescing": "observation_failed"},
                )
            if not 0 <= time.time() - latest["observed_at"] <= 30:
                coalescing = "skipped_stale_observation"
            elif verify.is_ancestor(Path(expected["workspace"]), progress["head"], latest["head"]):
                progress = latest
                coalescing = "latest_valid_descendant"
            elif verify.is_ancestor(Path(expected["workspace"]), latest["head"], progress["head"]):
                coalescing = "sealed_receipt_is_newer"
            else:
                return self._checkpoint_wait(
                    snapshot,
                    action,
                    "checkpoint progress no longer preserves its sealed lineage",
                    {"head": progress["head"], "coalescing": "lineage_failed"},
                )
            head = progress["head"]
        if unit.claim:
            request = (
                self.store.directory(snapshot.id) / "workers" / launch_id / "recovery-request.json"
            )
            if request.exists():
                return Observation(
                    "confirmed",
                    {
                        "head": head,
                        "skipped": True,
                        "reason": "child handed candidate to append-only recovery",
                    },
                )
        try:
            row = pusher.push(action.id, head)
        except Exception as exc:
            return self._checkpoint_wait(
                snapshot, action, str(exc), {"head": head, "coalescing": coalescing}
            )
        return (
            self._checkpoint_done(action, {**dataclasses.asdict(row), "coalescing": coalescing})
            if row.state == "confirmed"
            else self._checkpoint_wait(
                snapshot,
                action,
                "checkpoint push requires exact remote readback",
                {**dataclasses.asdict(row), "coalescing": coalescing},
            )
        )

    def _checkpoint_wait(self, snapshot, action, reason, data=None) -> Observation:
        failures = min(
            action.receipt.get("backup_failures", 0) + 1,
            snapshot.spec.checkpoints.max_backup_failures,
        )
        exhausted = failures >= snapshot.spec.checkpoints.max_backup_failures
        return Observation(
            "unknown",
            {
                **self._checkpoint_history(action),
                **(data or {}),
                "backup_failures": failures,
                "backup_total_failures": action.receipt.get(
                    "backup_total_failures", action.receipt.get("backup_failures", 0)
                )
                + 1,
                "backup_retry_exhausted": exhausted,
            },
            reason
            + (
                "; bounded backup attempts exhausted; exact-SHA readback remains pending"
                if exhausted
                else ""
            ),
            self.clock() + (300 if exhausted else min(60, 2**failures)),
        )

    def _checkpoint_history(self, action) -> dict:
        return {
            key: value
            for key, value in action.receipt.items()
            if key
            in {
                "backup_failures",
                "backup_total_failures",
                "backup_retry_epoch",
                "backup_retry_history",
            }
        }

    def _checkpoint_done(self, action, data) -> Observation:
        return Observation(
            "confirmed",
            {**self._checkpoint_history(action), **data, "backup_retry_exhausted": False},
        )

    def _reconcile_checkpoint(self, snapshot, action) -> Observation:
        unit = snapshot.units[action.unit]
        pusher = self._pusher(snapshot, unit)
        # Absence of a durable branch intent proves no Git push was issued.
        # Re-enter only the original sealed receipt, never a later worker HEAD.
        try:
            prior = next((row for row in pusher.observations() if row.action_id == action.id), None)
        except Exception as exc:
            return self._checkpoint_wait(snapshot, action, str(exc))
        if prior is None:
            if (
                action.receipt.get("backup_failures", 0)
                >= snapshot.spec.checkpoints.max_backup_failures
            ):
                from .campaign_checkpoint import read as read_checkpoint
                from .campaign_checkpoint import request as checkpoint_request

                try:
                    directory, expected = checkpoint_request(
                        self, snapshot, unit, action.payload["launch_id"]
                    )
                    sealed = read_checkpoint(
                        directory,
                        expected,
                        now=time.time(),
                        max_age_s=None,
                        receipt=action.payload["progress"],
                    )
                    observed = pusher.observe_existing(action.id, sealed["head"])
                except Exception as exc:
                    return self._checkpoint_wait(
                        snapshot, action, "exhausted backup readback unavailable: " + str(exc)
                    )
                if observed.state == "confirmed":
                    return self._checkpoint_done(action, dataclasses.asdict(observed))
                return self._checkpoint_wait(
                    snapshot,
                    action,
                    "backup write budget exhausted; exact sealed-head readback remains pending",
                    dataclasses.asdict(observed),
                )
            return self._perform_checkpoint(snapshot, action)
        try:
            row = pusher.reconcile(action.id)
        except Exception as exc:
            return self._checkpoint_wait(snapshot, action, str(exc))
        if row.state == "confirmed":
            return self._checkpoint_done(action, dataclasses.asdict(row))
        if prior.state == "prepared":
            if (
                action.receipt.get("backup_failures", 0)
                >= snapshot.spec.checkpoints.max_backup_failures
            ):
                return self._checkpoint_wait(
                    snapshot,
                    action,
                    "backup write budget exhausted; no new push is authorized",
                    dataclasses.asdict(row),
                )
            return self._perform_checkpoint(snapshot, action)
        return self._checkpoint_wait(
            snapshot,
            action,
            "issued checkpoint remains uncertain; replacement forbidden",
            dataclasses.asdict(row),
        )

    def _perform_checkpoint_progress(self, snapshot, action) -> Observation:
        from .campaign_checkpoint import status as checkpoint_status

        return Observation(
            "confirmed", checkpoint_status(self, snapshot, snapshot.units[action.unit])
        )

    def _coordinates(self, snapshot, unit, stage: str) -> StageCoordinates:
        integrated = unit.evidence.get("integrated", {})
        return StageCoordinates(
            snapshot.id,
            unit.id,
            stage,
            snapshot.graph_revision,
            snapshot.config_revision,
            integrated.get("base", unit.bindings["base"]),
            verify.rev_parse_head(Path(unit.bindings["workspace"])),
            snapshot.bindings.get("inventory", {}).get("digest", ""),
            unit.bindings.get("source_revision", ""),
        )

    def _perform_launch(self, snapshot, action) -> Observation:
        unit = snapshot.units[action.unit]
        from . import campaign_hold

        if campaign_hold.active(
            campaign_hold.channel(self.store.directory(snapshot.id), unit.id),
            campaign=snapshot.id,
            unit=unit.id,
        ):
            return Observation("confirmed", {"not_launched": True})
        claim = unit.claim
        if not claim or claim.launch_id != action.id or claim.generation != action.generation:
            return Observation("rejected", reason="launch does not own the saved worker claim")
        from .campaign_adoption import AdoptionError, guard_owned

        try:
            guard_owned(self, snapshot, unit.id)
        except AdoptionError as exc:
            return Observation("confirmed", {"not_launched": True, "adoption_refused": str(exc)})
        if action.payload.get("result_contract") == "repair-v1" and unit.evidence.get(
            "remote_feedback"
        ):
            from .campaign_remote_feedback import repair_current

            if not repair_current(
                self.gateway(snapshot),
                unit.evidence["remote_feedback"],
                now=time.time(),
                requirements=PublicationRequirements.from_spec(snapshot.spec.github),
            ):
                return Observation(
                    "confirmed", {"not_launched": True, "remote_feedback_changed": True}
                )
        template = self.template(snapshot)
        from .adapters.multiplexer import configure_multiplexer

        configure_multiplexer(template.policy.mux.backend or None, origin=self.project)
        directory = self.store.directory(snapshot.id) / "workers" / action.id
        envelope = dataclasses.replace(
            template,
            unit_id=unit.id,
            attempt=claim.attempt,
            generation=claim.generation,
            directory=directory,
            checkpoint_interval_s=float(snapshot.spec.checkpoints.target_interval_s),
        )
        if action.payload["kind"] == "execution":
            from .campaign_usage import remaining

            remaining_dev, remaining_review = remaining(snapshot, unit)
            handoff = unit.evidence.get("handoff", {})
            resuming_review = (
                action.payload.get("handoff_id") == handoff.get("id")
                and bool(handoff.get("id"))
                and handoff.get("native_role") == "review"
            )
            if (not resuming_review and remaining_dev <= 0) or (
                resuming_review and remaining_review <= 0
            ):
                return Observation(
                    "rejected", reason="campaign lifetime child attempt budget exhausted"
                )
            envelope = dataclasses.replace(
                envelope,
                policy=dataclasses.replace(
                    template.policy,
                    limits=dataclasses.replace(
                        template.policy.limits,
                        max_dev_attempts=remaining_dev,
                        max_review_cycles=max(0, remaining_review),
                    ),
                ),
            )
            if action.payload.get("candidate"):
                candidate = action.payload["candidate"]
                selection = dataclasses.replace(
                    envelope.policy.adapter.dev,
                    name=candidate["profile"],
                    model=candidate.get("model", ""),
                    effort=candidate.get("effort", ""),
                )
                profiles = dict(envelope.profiles)
                profiles["dev"] = profiles["campaign:" + candidate["profile"]]
                envelope = dataclasses.replace(
                    envelope,
                    profiles=profiles,
                    policy=dataclasses.replace(
                        envelope.policy,
                        adapter=dataclasses.replace(
                            envelope.policy.adapter,
                            dev=selection,
                        ),
                    ),
                )
            if action.payload.get("review_candidate"):
                candidate = action.payload["review_candidate"]
                selection = dataclasses.replace(
                    envelope.policy.adapter.review,
                    name=candidate["profile"],
                    model=candidate.get("model", ""),
                    effort=candidate.get("effort", ""),
                )
                profiles = dict(envelope.profiles)
                profiles["review"] = profiles["campaign:" + candidate["profile"]]
                envelope = dataclasses.replace(
                    envelope,
                    profiles=profiles,
                    policy=dataclasses.replace(
                        envelope.policy,
                        adapter=dataclasses.replace(
                            envelope.policy.adapter,
                            review=selection,
                        ),
                    ),
                )
        workspace = Path(action.payload.get("verification_workspace", unit.bindings["workspace"]))
        request = {
            **action.payload,
            "workspace": str(workspace),
            "branch": unit.bindings["branch"],
            "common_dir": snapshot.common_dir,
            "launch_id": action.id,
            "run_id": claim.run_id,
            "source": unit.bindings.get(
                "effective_source", dataclasses.asdict(self.source(snapshot, unit))
            ),
            "unit": dataclasses.asdict(unit.spec),
            "remote": snapshot.spec.remote,
            "target": snapshot.spec.target,
            "sources": unit.bindings.get("effective_sources", []),
            "private_paths": unit.bindings.get("private_paths", []),
            "checkpoint_interval_s": snapshot.spec.checkpoints.target_interval_s,
        }
        from .campaign_jobs import digest as configuration_digest

        request["headless_capabilities"] = snapshot.config_snapshot.get(
            "reasoning_capabilities", {}
        )
        request["headless_coordinates"] = {
            "campaign": snapshot.id,
            "unit": unit.id,
            "head": unit.bindings["head"],
            "base": unit.bindings["base"],
            "graph_revision": snapshot.graph_revision,
            "config_revision": snapshot.config_revision,
        }
        request["headless_grants"] = {
            "schema_version": 1,
            "workspace": str(workspace.resolve()),
            "scopes": list(unit.spec.scopes or unit.spec.paths or (".",)),
            "write_paths": list(request["private_paths"]),
            "git_branch": request["branch"],
            "protected_branches": list(dict.fromkeys((snapshot.spec.target, "main", "master"))),
            "config_digest": configuration_digest(snapshot.config_snapshot),
            "generation": claim.generation,
            "plugin_grants": json.loads(
                json.dumps(dataclasses.asdict(envelope.grants), default=str)
            ),
        }
        from .campaign_handoff_flow import decorate_launch

        decorate_launch(snapshot, unit, request)
        from .campaign_hold import channel

        request["hold_channel"] = str(channel(self.store.directory(snapshot.id), unit.id))
        if (
            request["kind"] == "execution"
            and unit.bindings.get("initial_source")
            and not any(
                receipt.get("run_dir") for receipt in unit.evidence.get("worker_history", [])
            )
        ):
            request["initial_source"] = unit.bindings["initial_source"]
        amendments = unit.evidence.get("amendment_history", [])
        if (
            action.payload["kind"] == "execution"
            and amendments
            and not amendments[-1].get("first_execution")
        ):
            request["initial_source"] = amendments[-1]["receipt"]["initial_source"]
        from .campaign_reflection_flow import compose_context, sanitized_actor_env

        request["claim"] = dataclasses.asdict(claim)
        request["sibling_context"] = compose_context(snapshot, unit)
        request["reflection_environment"] = sanitized_actor_env(
            {}, communication=snapshot.spec.communication
        )
        if snapshot.spec.paradev.reflect:
            from .campaign_reflection_flow import sanitized_worker_env, workspace_binding

            request["reflection_environment"] = sanitized_worker_env(
                {},
                binding=workspace_binding(unit),
                state_dir=self.store.directory(snapshot.id) / "native-transport",
                communication=snapshot.spec.communication,
            )
        if request["kind"] == "queue_verify":
            request["candidate"] = request.pop("queue_candidate")
            request["timeout_s"] = template.policy.verify.command_timeout_min * 60
        elif request["kind"] == "tracking":
            request["tracking"] = unit.bindings["tracking"]
        elif request["kind"] == "recovery":
            original_dir = (
                self.store.directory(snapshot.id) / "workers" / request["original_launch"]
            )
            request.update(
                recovery_request=str(original_dir / "recovery-request.json"),
                canonical_project=str(self.project),
                publication_state=str(self.store.directory(snapshot.id) / "publication"),
                recovery_action="recover-" + request["original_launch"],
                allowed_paths=list(unit.spec.paths or unit.spec.scopes or (".",)),
            )
        elif request["kind"] in {"verify", "verify_existing"}:
            request["config_revision"] = str(snapshot.config_revision)
            request["timeout_s"] = template.policy.verify.command_timeout_min * 60
            request["commands"] = request.get("commands") or [
                list(command) for command in snapshot.spec.verify_commands
            ]
            if not request["commands"]:
                # Preserve project verification commands, including its shared
                # verification locks, through the established verifier seam.
                request["legacy_verify"] = True
            if request["kind"] == "verify_existing":
                request["adoption_mode"] = (
                    unit.bindings.get("adoption", {}).get("manifest", {}).get("mode", "")
                )
        elif request["kind"] == "stage":
            role = request["role"]
            stage = request.get("stage", role)
            if stage == "planning":
                done = unit.evidence.get("planning_stages", [])
                stage = next(
                    (name for name in snapshot.spec.planning.stages if name not in done),
                    "planning_review",
                )
            coordinates = self._coordinates(snapshot, unit, stage)
            context = {
                "unit": dataclasses.asdict(unit.spec),
                "source": snapshot.bindings.get("source_observations", {}).get(unit.id, {}),
                "inventory": snapshot.bindings.get("inventory"),
                "prior_artifacts": unit.evidence.get("planning_results", []),
                "brief": snapshot.spec.planning.brief,
                "verification": unit.evidence.get("verification"),
                "source_artifacts": unit.evidence.get("source_artifacts", []),
                "source_artifact_digest": unit.bindings.get("source_revision", ""),
                "feedback": unit.evidence.get("required_findings", []),
            }
            context["github_stage_inputs"] = request["stage_boundary"]["inputs"]
            if stage == "admission_review":
                from .campaign_discovery import evidence_ids

                inventory_fields = dict(snapshot.bindings["inventory"])
                inventory_fields.pop("digest", None)
                context["available_evidence_ids"] = sorted(
                    evidence_ids(CoverageReceipt(**inventory_fields))
                )
                context["owned_pr"] = unit.bindings.get("pr")
                context["inventory_digest"] = snapshot.bindings["inventory"]["digest"]
                context["external_ownership"] = snapshot.bindings.get("external_workers", [])
            from .campaign_runtime_intake import stage_context as intake_stage_context

            context.update(intake_stage_context(snapshot, unit.id))
            from .campaign_reasoning_results import merge_into_stage_context

            context.update(merge_into_stage_context(self, snapshot, unit, stage))
            if unit.spec.kind == "planning":
                from .campaign_planning_flow import context as planning_context

                context.update(planning_context(snapshot, unit.id))
                context["sources"] = unit.bindings.get("effective_sources") or [
                    dataclasses.asdict(row) for row in snapshot.spec.sources
                ]
                context["source_observations"] = snapshot.bindings.get("source_observations", {})
                context["existing_units"] = [
                    dataclasses.asdict(row.spec) for row in snapshot.units.values()
                ]
                context["planning_control"] = unit.id
            if stage == "closure":
                from .campaign_closure import context as closure_context
                from .campaign_closure import prepare as prepare_closure
                from .campaign_source_publication import SourceBinding

                binding = SourceBinding.from_dict(unit.bindings["canonical_source"])
                publisher = self._source_publisher(snapshot, project=Path(binding.project))
                canonical_head, _ = publisher._canonical(
                    action.id + ":closure", unit.bindings["base"]
                )
                publisher._closure_ready(binding, canonical_head)
                request["closure_manifest"] = prepare_closure(
                    workspace,
                    SourceSelector(**request["source"]),
                    unit.spec,
                    canonical=binding.to_dict(),
                    canonical_head=canonical_head,
                )
                import base64

                preimage = publisher._source_bytes(
                    binding, canonical_head, request["closure_manifest"]["canonical_retrospective"]
                )
                request["closure_manifest"]["retrospective_preimage"] = (
                    base64.b64encode(preimage).decode("ascii") if preimage is not None else None
                )
                context.update(closure_context(request["closure_manifest"]))
            if stage == "publication_review":
                from .campaign_acceptance import context as acceptance_context

                context["acceptance_ledger"] = acceptance_context(unit)
                context["diff"] = git(
                    workspace, "diff", "--stat", coordinates.base + "..." + coordinates.head
                )
                context["diff_instruction"] = (
                    f"Inspect the complete git diff {coordinates.base}...{coordinates.head}"
                )
                context["github"] = HierarchyCoordinator(self.gateway(snapshot)).refresh(
                    snapshot, unit.id
                )
            request["coordinates"] = dataclasses.asdict(coordinates)
            request["read_only"] = request.get(
                "read_only",
                stage
                in {
                    "publication_review",
                    "planning_review",
                    "escalation",
                    "decision_review",
                    "admission_review",
                },
            )
            request["prompt"] = request.get("custom_prompt") or stage_packet(
                coordinates,
                context=context,
                skill=dict(snapshot.spec.planning.skills).get(stage, ""),
            )
            if request.get("custom_prompt"):
                from .campaign_boundary_sync import custom_prompt

                request["prompt"] = custom_prompt(
                    request["custom_prompt"], context["github_stage_inputs"]
                )
            if unit.spec.kind == "planning" and stage in snapshot.spec.planning.stages:
                from .campaign_planning_skill import packet as planning_packet

                request["prompt"], request["native_planning_skill"] = planning_packet(
                    workspace, snapshot.spec.planning, stage, request["prompt"]
                )
            request["profile_key"] = "campaign:" + request["candidate"]["profile"]
            request["timeout_s"] = snapshot.spec.role(role).timeout_s
            request["max_tokens"] = snapshot.spec.role(role).max_tokens
        if request["kind"] == "execution" or (
            request["kind"] == "stage" and not request.get("read_only")
        ):
            if (
                not all(key in action.payload for key in ("checkpoint_base", "checkpoint_paths"))
                or verify.rev_parse_head(workspace) != action.payload["checkpoint_base"]
            ):
                return Observation(
                    "rejected",
                    reason="checkpoint scope/baseline must match the durable launch and exact accepted head",
                )
        path = prepare_launch(envelope, request)
        launch_env = sanitized_actor_env(os.environ, communication=snapshot.spec.communication)
        if snapshot.spec.paradev.reflect:
            from .campaign_reflection_flow import sanitized_worker_env, workspace_binding

            launch_env = sanitized_worker_env(
                os.environ,
                binding=workspace_binding(unit),
                state_dir=self.store.directory(snapshot.id) / "native-transport",
                communication=snapshot.spec.communication,
            )
        if campaign_hold.active(
            campaign_hold.channel(self.store.directory(snapshot.id), unit.id),
            campaign=snapshot.id,
            unit=unit.id,
        ):
            return Observation("confirmed", {"not_launched": True})
        process = spawn(path, env=launch_env)
        return Observation(
            "confirmed",
            {
                "launched": True,
                "pid": process.pid,
                "directory": str(directory),
                "request": str(path),
            },
        )

    def _perform_observe_worker(self, snapshot, action) -> Observation:
        unit = snapshot.units[action.unit]
        claim = unit.claim
        if claim is None or claim.launch_id != action.payload["launch_id"]:
            return Observation("rejected", reason="worker observation claim moved")
        directory = self.store.directory(snapshot.id) / "workers" / claim.launch_id
        expected = {
            "campaign_id": snapshot.id,
            "unit_id": unit.id,
            "attempt": claim.attempt,
            "generation": claim.generation,
            "launch_id": claim.launch_id,
        }
        result = observe(directory, expected=expected)
        if result["state"] in {"running", "unknown"}:
            return Observation(
                "waiting", result, result.get("reason", "worker running"), self.clock() + 1
            )
        # Child completion is not enough: prove all owned mux windows are gone.
        request = json.loads((directory / "request.json").read_text(encoding="utf-8"))
        retained_id = None
        if request["kind"] == "recovery":
            from .campaign_finalization import continuation_run_id

            retained_id = continuation_run_id(request)
        if request["kind"] in {"stage", "execution"} or retained_id:
            from .adapters.multiplexer import configure_multiplexer, get_multiplexer

            configure_multiplexer(
                self.template(snapshot).policy.mux.backend or None, origin=self.project
            )
            mux = self._native_mux(snapshot) if snapshot.spec.paradev.reflect else get_multiplexer()
            try:
                windows = mux.list_window_ids(runs.session_name(retained_id or claim.run_id))
            except Exception as exc:
                return Observation(
                    "unknown", result, f"coding-session quiescence unavailable: {exc}"
                )
            if windows:
                return Observation(
                    "unknown", result, "worker exited while owned coding windows remain"
                )
        if result["state"] == "interrupted":
            result["quiescence_confirmed"] = True
            result["head"] = verify.rev_parse_head(Path(unit.bindings["workspace"]))
            from .campaign_usage import worker_accounting

            result["accounting"] = worker_accounting(request, {"outcome": "interrupted"})
            return Observation("confirmed", result)
        receipt = result["receipt"]
        if receipt.get("outcome") == "locally_complete":
            from .campaign_ownership import validate_execution

            try:
                receipt["changed_paths"] = validate_execution(unit, receipt, request["source"])
            except CampaignError as exc:
                receipt["outcome"] = "ownership_uncertain"
                receipt["error"] = str(exc)
        if (
            request["kind"] == "stage"
            and receipt.get("outcome") == "stage_complete"
            and not request.get("stage_boundary")
        ):
            try:
                self._validate_stage_receipt(snapshot, unit, request, receipt)
            except (CampaignError, TypeError, ValueError, KeyError) as exc:
                # The actor and its coding windows are positively terminal.
                # Invalid model artifacts hold the unit, not live capacity.
                receipt["invalid_stage_result"] = receipt.get("artifact")
                receipt["outcome"] = "invalid_stage_result"
                receipt["error"] = str(exc)
        return Observation("confirmed", result)

    def _validate_stage_receipt(self, snapshot, unit, request, receipt) -> None:
        coordinates = StageCoordinates(**request["coordinates"])
        if request.get("result_contract") in {"decision-v1", "resolution-v1"}:
            from .campaign_decision_sources import github_provider
            from .campaign_resolution_flow import validate_stage_result
            from .campaign_runtime_decisions import MappedEvidence
            from .campaign_runtime_decisions import collect as collect_mapped

            mapped = (
                collect_mapped(self, snapshot, unit)
                if request.get("result_contract") == "decision-v1"
                else MappedEvidence()
            )

            receipt["validated_resolution"] = validate_stage_result(
                self.project,
                snapshot,
                unit.id,
                receipt,
                providers={
                    "github": github_provider(self.gateway(snapshot)),
                    **self.evidence_providers,
                },
                mapped_sources=mapped.sources,
                facts=mapped.facts,
                mapped_unavailable=mapped.unavailable,
            )
        elif request.get("result_contract") == "repair-v1":
            from .campaign_resolution_delivery import validate_repair

            receipt["validated_repair"] = validate_repair(snapshot, unit, receipt)
        else:
            receipt["validated"] = validate_result(
                receipt["artifact"],
                coordinates,
                Path(unit.bindings["workspace"]),
                snapshot=snapshot,
            )
            from .campaign_planning_publication import bind_mirrors

            bind_mirrors(Path(unit.bindings["workspace"]), receipt)
            if coordinates.stage == "closure":
                from .campaign_closure import validate as validate_closure

                receipt["validated_closure"] = validate_closure(
                    Path(unit.bindings["workspace"]),
                    unit.spec,
                    request["closure_manifest"],
                    receipt["artifact"],
                    mirrors=receipt.get("artifact_mirrors", {}),
                    committed_head=receipt["head"],
                )
                receipt["closure_manifest"] = request["closure_manifest"]
            if unit.spec.kind == "planning" and coordinates.stage in {
                "requirements",
                "architecture",
                "decomposition",
            }:
                from .campaign_planning_publication import validate_stage_products

                sources = tuple(
                    SourceSelector(**row) for row in unit.bindings.get("effective_sources", [])
                )
                validate_stage_products(Path(unit.bindings["workspace"]), receipt, sources)
            if request.get("result_contract") == "intake-triage-v1":
                from .campaign_runtime_intake import validate_receipt as validate_intake_receipt

                validate_intake_receipt(snapshot, unit.id, receipt, launch_id=unit.claim.launch_id)
            if coordinates.stage == "decomposition":
                from .campaign_runtime_intake import validate_decomposition

                validate_decomposition(snapshot, unit.id, receipt["artifact"])
            if coordinates.stage == "admission_review":
                raw = receipt["artifact"].get("overlap")
                if not isinstance(raw, dict) or raw.get("unit_id") != unit.id:
                    raise CampaignError("admission review lacks the exact unit overlap disposition")
                disposition = OverlapDisposition(**raw)
                if set(unit.spec.acceptance) - (
                    set(disposition.acceptance_coverage) | set(disposition.remaining)
                ):
                    raise CampaignError("overlap disposition omits accepted criteria")
                inventory_data = dict(snapshot.bindings["inventory"])
                inventory_data.pop("digest", None)
                inventory = CoverageReceipt(**inventory_data)
                validate_overlap(disposition, inventory, owned_pr=unit.bindings["pr"]["number"])
                receipt["validated_admission"] = raw
            if coordinates.stage == "publication_review":
                from .campaign_acceptance import evaluate as evaluate_acceptance

                receipt["validated_acceptance"] = evaluate_acceptance(
                    unit, self._candidate(snapshot, unit), review=receipt
                )
        receipt["stage"] = coordinates.stage
        receipt["result_contract"] = request.get("result_contract", "")

    def _candidate(self, snapshot, unit) -> Candidate:
        integrated = unit.evidence["integrated"]
        return Candidate(
            snapshot.repository,
            unit.id,
            unit.bindings["pr"]["number"],
            integrated["head"],
            integrated["base"],
            unit.bindings.get("source_revision", ""),
            snapshot.graph_revision,
            str(snapshot.config_revision),
            target=snapshot.spec.target,
        )

    def _publication_evidence(self, snapshot, unit) -> PublicationEvidence:
        from .campaign_acceptance import evaluate as acceptance_evaluate

        candidate = self._candidate(snapshot, unit)
        verification = unit.evidence.get("verification", {})
        review = unit.evidence.get("review", {})
        return PublicationEvidence(
            candidate,
            verification.get("outcome") == "verified"
            and acceptance_evaluate(unit, candidate)["complete"],
            review.get("validated", {}).get("verdict") == "approved",
            bool(unit.claim and unit.claim.quiescent),
            verify.worktree_clean(Path(unit.bindings["workspace"]))
            and verify.rev_parse_head(Path(unit.bindings["workspace"])) == candidate.head,
            unit.evidence.get("developer_family", ""),
            review.get("provider_family", ""),
            required_findings=tuple(unit.evidence.get("required_findings", [])),
            held=bool(unit.hold),
            observed_model=review.get("observed_model", ""),
            verification_receipts=(verification.get("launch_id", ""),),
            review_receipt=review.get("launch_id", ""),
            existing_canonical=bool(unit.evidence.get("existing_source_verified")),
            developer_families=tuple(unit.evidence.get("developer_families", ())),
            attribution_incomplete=bool(
                unit.evidence.get("developer_attribution_incomplete", False)
            ),
        )

    def _perform_prepare_ready(self, snapshot, action) -> Observation:
        unit = snapshot.units[action.unit]
        pr = self.gateway(snapshot).pull(unit.bindings["pr"]["number"])
        intent = self.gateway(snapshot).prepare_ready(
            pr["node_id"],
            action_id=action.id + "-ready",
            generation=snapshot.generation,
            expected_head=pr["head"]["sha"],
            expected_updated_at=pr["updated_at"],
            expected_draft=pr["draft"],
        )
        return Observation("confirmed", {"intent": intent.to_dict(), "purpose": "ready"})

    def _perform_prepare_draft(self, snapshot, action) -> Observation:
        unit = snapshot.units[action.unit]
        pr = self.gateway(snapshot).pull(unit.bindings["pr"]["number"])
        intent = self.gateway(snapshot).prepare_ready(
            pr["node_id"],
            action_id=action.id + "-draft",
            generation=snapshot.generation,
            draft=True,
            expected_head=pr["head"]["sha"],
            expected_updated_at=pr["updated_at"],
            expected_draft=pr["draft"],
        )
        return Observation("confirmed", {"intent": intent.to_dict(), "purpose": "repair-draft"})

    def _perform_prepare_merge(self, snapshot, action) -> Observation:
        unit = snapshot.units[action.unit]
        if unit.bindings.get("adoption"):
            from .campaign_adoption import guard_owned

            guard_owned(self, snapshot, unit.id)
        if unit.spec.kind == "planning":
            from .campaign_planning_publication import canonical_drift, drift

            changed_canonical = canonical_drift(unit)
            if changed_canonical:
                return Observation(
                    "confirmed",
                    {
                        "source_drift": "canonical planning source edited since capture: "
                        + ", ".join(changed_canonical)
                    },
                )

            changed = drift(unit)
            if changed:
                return Observation("confirmed", {"planning_drift": changed})
        else:
            from .campaign_ownership import canonical_unchanged

            try:
                canonical_unchanged(unit)
            except CampaignError as exc:
                return Observation("confirmed", {"source_drift": str(exc)})
            workspace = Path(unit.bindings["workspace"])
            for artifact in unit.evidence.get("source_artifacts", []):
                path = workspace / artifact["path"]
                if (
                    path.is_symlink()
                    or not path.is_file()
                    or path.stat().st_size > 8 * 1024 * 1024
                    or hashlib.sha256(path.read_bytes()).hexdigest() != artifact["sha256"]
                ):
                    return Observation("confirmed", {"source_drift": artifact["path"]})
        controller = PublicationController(
            self.gateway(snapshot),
            requirements=PublicationRequirements.from_spec(snapshot.spec.github),
        )
        candidate = self._candidate(snapshot, unit)
        observation = controller.inspect(candidate.pr_number)
        if observation.pr.get("merged") is True:
            from .campaign_external_merge import inspect as inspect_external_merge

            return inspect_external_merge(self, snapshot, unit, observation)
        from .campaign_remote_feedback import collect as collect_remote_feedback

        remote_defects = collect_remote_feedback(
            candidate,
            observation,
            now=controller.clock(),
            requirements=PublicationRequirements.from_spec(snapshot.spec.github),
        )
        if remote_defects is not None:
            return Observation("confirmed", {"remote_feedback": dataclasses.asdict(remote_defects)})
        if observation.base_head != candidate.base:
            return Observation(
                "confirmed", {"needs_integration": True, "base": observation.base_head}
            )
        evidence = self._publication_evidence(snapshot, unit)
        gate = controller.evaluate(
            candidate,
            evidence,
            observation,
            merge_mode=snapshot.spec.github.merge_mode,
            independent_families=snapshot.spec.github.require_independent_review,
            queue_evidence_context=snapshot.spec.github.candidate_check_context,
        )
        if not gate.ready:
            return Observation(
                "waiting",
                {"gate": dataclasses.asdict(gate)},
                "; ".join(gate.reasons),
                self.clock() + 15,
            )
        if snapshot.spec.github.merge_command:
            from .campaign_merge_adapter import check_project_gate

            project_gate = check_project_gate(
                snapshot.spec.github.merge_command,
                self.project,
                self.store.directory(snapshot.id) / "project-merge-gates" / action.id,
                candidate,
                evidence,
                controller,
                target=snapshot.spec.target,
                merge_mode=snapshot.spec.github.merge_mode,
                queue_context=snapshot.spec.github.candidate_check_context,
                independent_families=snapshot.spec.github.require_independent_review,
                clock=self.clock,
            )
            if project_gate.state != "ready":
                return Observation(
                    "waiting",
                    {"project_gate": dataclasses.asdict(project_gate)},
                    project_gate.reason or project_gate.state,
                    self.clock() + 15,
                )
        if snapshot.spec.github.merge_executor:
            from .campaign_merge_adapter import prepare_execution

            intent = prepare_execution(
                snapshot.spec.github.merge_executor,
                self.project,
                candidate,
                evidence,
                controller,
                action_id=action.id + "-merge",
                generation=snapshot.generation,
                independent_families=snapshot.spec.github.require_independent_review,
            )
            return Observation("confirmed", {"intent": intent.to_dict(), "purpose": "merge"})
        intent = controller.prepare_merge(
            candidate,
            evidence,
            action_id=action.id + "-merge",
            generation=snapshot.generation,
            merge_mode=snapshot.spec.github.merge_mode,
            merge_method=snapshot.spec.github.merge_method,
            independent_families=snapshot.spec.github.require_independent_review,
            queue_evidence_context=snapshot.spec.github.candidate_check_context,
        )
        return Observation("confirmed", {"intent": intent.to_dict(), "purpose": "merge"})

    def _perform_prepare_review_requests(self, snapshot, action) -> Observation:
        from .campaign_publication_flow import prepare_review_effect

        return prepare_review_effect(self, snapshot, action)

    def _perform_pr_head_checkout(self, snapshot, action) -> Observation:
        from .campaign_publication_flow import head_checkout_effect

        return head_checkout_effect(self, snapshot, action)

    def _perform_pr_head_publish(self, snapshot, action) -> Observation:
        from .campaign_publication_flow import head_publish_effect

        return head_publish_effect(self, snapshot, action)

    def _perform_queue_observe(self, snapshot, action) -> Observation:
        from .campaign_runtime_queue import observe_effect

        return observe_effect(self, snapshot, action)

    def _perform_queue_checkout(self, snapshot, action) -> Observation:
        from .campaign_runtime_queue import checkout_effect

        return checkout_effect(self, snapshot, action)

    def _perform_queue_publish(self, snapshot, action) -> Observation:
        from .campaign_runtime_queue import publish_effect

        return publish_effect(self, snapshot, action)

    def _perform_queue_dequeue(self, snapshot, action) -> Observation:
        from .campaign_runtime_queue import dequeue_effect

        return dequeue_effect(self, snapshot, action)

    def _perform_observe_merge(self, snapshot, action) -> Observation:
        unit = snapshot.units[action.unit]
        controller = PublicationController(
            self.gateway(snapshot),
            requirements=PublicationRequirements.from_spec(snapshot.spec.github),
        )
        if unit.bindings.get("project_merge_intent"):
            from .campaign_publication_flow import execute_special

            intent = MutationIntent.from_dict(unit.bindings["project_merge_intent"])
            result = execute_special(self, snapshot, action, intent, reconcile=True)
            data = (result.data.get("intent") or {}).get("receipt") or {}
            if result.state == "rejected":
                return Observation(
                    "confirmed",
                    {
                        "project_merge_rejected": True,
                        "intent": result.data["intent"],
                        "reason": result.reason,
                    },
                )
            if result.state != "confirmed" or data.get("status") != "merged":
                return Observation(
                    "waiting",
                    result.data,
                    result.reason or "project executor merge pending",
                    self.clock() + 5,
                )
        receipt = controller.merge_status(
            unit.bindings["pr"]["number"],
            request_id=(
                ""
                if unit.bindings.get("project_merge_intent")
                else unit.bindings.get("merge_request_id", "")
            ),
            expected_head=unit.evidence["integrated"]["head"],
        )
        if receipt.state != "merged_pending_sync":
            return Observation(
                "waiting",
                dataclasses.asdict(receipt),
                receipt.reason or receipt.state,
                self.clock() + 5,
            )
        canonical = controller.confirm_canonical(
            self._candidate(snapshot, unit),
            receipt,
            repo=self.project,
            remote=snapshot.spec.remote,
            target=snapshot.spec.target,
        )
        if not verify.is_ancestor(
            self.project, unit.evidence["integrated"]["head"], canonical.canonical_head
        ):
            return Observation("rejected", reason="merged history omits the accepted H head")
        for checkpoint in self._pusher(snapshot, unit).observations():
            if checkpoint.state in {
                "confirmed",
                "sent_unknown",
                "in_flight",
            } and not verify.is_ancestor(self.project, checkpoint.head, canonical.canonical_head):
                return Observation("rejected", reason="merged history omits a campaign checkpoint")
        return Observation("confirmed", dataclasses.asdict(canonical))

    def _perform_sync_tracking(self, snapshot, action) -> Observation:
        unit = snapshot.units[action.unit]
        from .campaign_runtime_tracking import (
            confirm_tracking,
            pending_tracking,
        )
        from .campaign_runtime_tracking import prepare as prepare_tracking

        pending = pending_tracking(snapshot, unit)
        if pending:
            return Observation(
                "waiting",
                {"tracking_units": list(pending)},
                "Canonical source tracking draft PR is not delivered",
                self.clock() + 5,
            )
        source = self.source(snapshot, unit)
        source_receipt = None
        if unit.spec.kind == "tracking":
            source_receipt = confirm_tracking(
                snapshot,
                unit,
                state_dir=self.store.directory(snapshot.id) / "tracking-confirmation",
            )
        elif unit.spec.kind == "planning":
            from .campaign_planning_publication import publish

            source_receipt = publish(self, snapshot, unit, action)
            if source_receipt.get("tracking_control"):
                return Observation(
                    "confirmed",
                    {
                        "source_publication": source_receipt,
                        "tracking_control": source_receipt["tracking_control"],
                    },
                )
            if source_receipt["state"] != "confirmed":
                return Observation(
                    "waiting",
                    {"source_publication": source_receipt},
                    source_receipt.get("detail") or source_receipt["state"],
                    self.clock() + 5,
                )
        elif source.kind != "explicit":
            from .campaign_source_publication import SourceBinding

            raw_binding = unit.bindings.get("canonical_source")
            if not raw_binding:
                raise CampaignError("canonical source identity was not captured before execution")
            binding = SourceBinding.from_dict(raw_binding)
            publisher = self._source_publisher(snapshot, project=Path(binding.project))
            artifact = next(iter(unit.evidence.get("source_artifacts", [])), None)
            artifact_bytes = None
            capability = None
            merge = unit.evidence["merge"]
            review = unit.evidence["review"]
            review_coordinates = review.get("validated", {}).get("coordinates", {}) or review.get(
                "artifact", {}
            ).get("coordinates", {})
            accepted_graph_revision = review_coordinates.get(
                "graph_revision", snapshot.graph_revision
            )
            accepted_config_revision = review_coordinates.get(
                "config_revision", snapshot.config_revision
            )
            if unit.spec.kind == "closure" and unit.evidence.get("closure_manifest"):
                from .campaign_closure import publish_retrospective, retrospective_change

                retrospective = publish_retrospective(
                    publisher,
                    source,
                    unit,
                    action.id,
                    merge={
                        "head": unit.evidence["integrated"]["head"],
                        "merged_commit": merge["merge_commit"],
                        "pr_number": unit.bindings["pr"]["number"],
                    },
                    acceptance={
                        "head": unit.evidence["integrated"]["head"],
                        "review_id": review.get("session_id") or review.get("launch_id"),
                        "verdict": "approved",
                        "graph_revision": accepted_graph_revision,
                        "config_revision": accepted_config_revision,
                    },
                    journal=self._operation_journal(snapshot, action),
                )
                if retrospective.state == "tracking_pr_required":
                    descriptor = prepare_tracking(
                        snapshot,
                        unit,
                        retrospective,
                        state_dir=self.store.directory(snapshot.id),
                        publisher_project=publisher.project,
                        source=source,
                        changes=(retrospective_change(unit),),
                        merge={
                            "head": unit.evidence["integrated"]["head"],
                            "merged_commit": merge["merge_commit"],
                            "pr_number": unit.bindings["pr"]["number"],
                        },
                        acceptance={
                            "head": unit.evidence["integrated"]["head"],
                            "review_id": review.get("session_id") or review.get("launch_id"),
                            "verdict": "approved",
                            "graph_revision": accepted_graph_revision,
                            "config_revision": accepted_config_revision,
                        },
                    )
                    return Observation(
                        "confirmed",
                        {
                            "source_publication": dataclasses.asdict(retrospective),
                            "tracking_control": descriptor,
                        },
                    )
                if retrospective.state != "confirmed":
                    return Observation(
                        "waiting",
                        {"retrospective_publication": dataclasses.asdict(retrospective)},
                        retrospective.detail or "canonical retrospective publication pending",
                        self.clock() + 5,
                    )
                accepted_retrospective = next(
                    row
                    for row in unit.evidence["source_artifacts"]
                    if row.get("kind") == "closure-retrospective"
                )
                binding = dataclasses.replace(
                    binding,
                    retrospective=(
                        accepted_retrospective["canonical_path"],
                        accepted_retrospective["sha256"],
                    ),
                )
            if artifact is not None and (binding.versioning != "code-repo" or not binding.path):
                workspace = Path(unit.bindings["workspace"])
                artifact_bytes = (workspace / artifact["path"]).read_bytes()
                if hashlib.sha256(artifact_bytes).hexdigest() != artifact["sha256"]:
                    raise CampaignError(
                        "accepted source artifact changed before canonical publication"
                    )
                review = unit.evidence["review"]
                canonical_path = None
                if not binding.path:
                    from .campaign_planning_publication import source_root

                    effective = SourceSelector(**unit.bindings["effective_source"])
                    local_root = source_root(workspace, effective)
                    local_artifact = (workspace / artifact["path"]).resolve()
                    if not local_artifact.is_relative_to(local_root):
                        raise CampaignError(
                            "new source artifact has no configured canonical path mapping"
                        )
                    canonical_path = Path(binding.source_root) / local_artifact.relative_to(
                        local_root
                    )
                capability = publisher.prepare_artifact(
                    action.id,
                    binding,
                    artifact_bytes,
                    merge={
                        "head": unit.evidence["integrated"]["head"],
                        "merged_commit": merge["merge_commit"],
                        "pr_number": unit.bindings["pr"]["number"],
                    },
                    acceptance={
                        "head": unit.evidence["integrated"]["head"],
                        "review_id": review.get("session_id") or review.get("launch_id"),
                        "verdict": "approved",
                        "artifact_digest": artifact["sha256"],
                        "review_path": artifact["review_path"],
                        "graph_revision": accepted_graph_revision,
                        "config_revision": accepted_config_revision,
                    },
                    canonical_path=canonical_path,
                )
            published = publisher.publish(
                action.id,
                source,
                unit.spec,
                binding,
                merged_commit=merge["merge_commit"],
                journal=self._operation_journal(snapshot, action),
                accepted_plan=capability,
                store_remote=getattr(source, "store_remote", ""),
                store_branch=getattr(source, "store_branch", ""),
            )
            source_receipt = dataclasses.asdict(published)
            if published.state == "tracking_pr_required":
                if artifact is not None and artifact_bytes is None:
                    artifact_bytes = (
                        Path(unit.bindings["workspace"]) / artifact["path"]
                    ).read_bytes()
                    if hashlib.sha256(artifact_bytes).hexdigest() != artifact["sha256"]:
                        raise CampaignError(
                            "Accepted tracking artifact changed before draft preparation"
                        )
                if not binding.path and capability is not None:
                    binding = dataclasses.replace(
                        binding,
                        path=str(capability.plan),
                        code_relative=capability.plan.relative_to(publisher.project).as_posix(),
                    )
                review = unit.evidence["review"]
                descriptor = prepare_tracking(
                    snapshot,
                    unit,
                    published,
                    state_dir=self.store.directory(snapshot.id),
                    publisher_project=publisher.project,
                    source=source,
                    binding=binding,
                    artifact=artifact_bytes,
                    merge={
                        "head": unit.evidence["integrated"]["head"],
                        "merged_commit": merge["merge_commit"],
                        "pr_number": unit.bindings["pr"]["number"],
                    },
                    acceptance={
                        "head": unit.evidence["integrated"]["head"],
                        "verdict": "approved",
                        "review_id": review.get("session_id") or review.get("launch_id"),
                        "graph_revision": accepted_graph_revision,
                        "config_revision": accepted_config_revision,
                        **(
                            {
                                "artifact_digest": artifact["sha256"],
                                "review_path": artifact["review_path"],
                            }
                            if artifact is not None
                            else {}
                        ),
                    },
                )
                return Observation(
                    "confirmed",
                    {"source_publication": source_receipt, "tracking_control": descriptor},
                )
            if published.state != "confirmed":
                return Observation(
                    "waiting",
                    {"source_publication": source_receipt},
                    published.detail or published.state,
                    self.clock() + 5,
                )
        issue = unit.bindings["issue"]
        intent = self.gateway(snapshot).close_issue_intent(
            issue["number"],
            action_id=action.id + "-close",
            generation=snapshot.generation,
            source_confirmed=True,
        )
        return Observation(
            "confirmed",
            {
                "intent": intent.to_dict(),
                "purpose": "tracking",
                "source_publication": source_receipt,
            },
        )

    def _queue_intent(
        self, snapshot, unit, data: dict[str, Any], *, tracking_key: str = ""
    ) -> None:
        intent = data["intent"]
        if any(row.id == intent["action_id"] for row in snapshot.outbox):
            return
        add_action(
            snapshot,
            "github",
            unit.id,
            {
                "intent": intent,
                "purpose": data.get("purpose", "hierarchy"),
                "tracking_key": tracking_key,
                "scope_revision": snapshot.graph_revision,
            },
            action_id=intent["action_id"],
        )

    def apply(self, snapshot, action, observation: Observation) -> None:
        previous = {key: unit.state for key, unit in snapshot.units.items()}
        self._apply_effect(snapshot, action, observation)
        from .campaign_hold import promote_pending

        for unit in snapshot.units.values():
            if promote_pending(unit):
                add_event(snapshot, "pending-hold-promoted", unit=unit.id, hold=unit.hold.id)
        from .campaign_boundary_sync import queue_transition

        for key, unit in snapshot.units.items():
            if key in previous and previous[key] != unit.state:
                queue_transition(
                    self,
                    snapshot,
                    unit,
                    event_id=action.id + "-" + hashlib.sha256(key.encode()).hexdigest()[:8],
                    message="State: " + str(previous[key]) + " → " + str(unit.state),
                )

    def _apply_effect(self, snapshot, action, observation: Observation) -> None:
        if action.kind.startswith("handoff-"):
            from .campaign_handoff_flow import apply

            apply(self, snapshot, action, observation)
            return
        if action.kind == "semantic-plan":
            from .campaign_reasoning_plan import apply

            apply(self, snapshot, action, observation)
            return
        if action.kind.startswith("reasoning-"):
            from .campaign_reasoning_flow import apply

            apply(self, snapshot, action, observation)
            return
        data = observation.data
        unit = snapshot.units.get(action.unit)
        kind = action.kind
        if data.get("operator_suppressed"):
            if kind == "prepare-unit" and unit.state == UnitState.PLANNING:
                transition(snapshot, unit.id, UnitState.PENDING)
            add_event(snapshot, "operator-held-effect-suppressed", unit=action.unit, effect=kind)
            return
        if kind in {"hierarchy", "hierarchy-close"}:
            from .campaign_tracking_delta import apply as apply_hierarchy

            try:
                current = apply_hierarchy(snapshot, data, unit.id if kind == "hierarchy" else "")
            except CampaignError as exc:
                self._set_hold(unit, Hold("tracking", str(exc), id=action.id))
                add_event(
                    snapshot,
                    "hierarchy-identity-conflict",
                    unit=unit.id,
                    action=action.id,
                    reason=str(exc),
                )
                return
            if not current:
                add_event(snapshot, "hierarchy-stale-observation", unit=unit.id, action=action.id)
                return
        if kind == "stage-boundary-before":
            from .campaign_boundary_sync import before_apply

            before_apply(self, snapshot, action, data)
        elif kind == "stage-boundary-after":
            from .campaign_boundary_sync import after_apply

            after_apply(self, snapshot, action, data)
        elif kind == "release-observation":
            if data.get("release_refused"):
                unit.evidence["operator_release_refused"] = data
                unit.bindings.pop("operator_release", None)
                add_event(
                    snapshot,
                    "operator-release-refused",
                    unit=unit.id,
                    reason=data["release_refused"],
                )
            else:
                from .campaign_takeover import apply as apply_release

                apply_release(self, snapshot, action, data)
        elif kind == "inventory":
            for key, fence_observation in data.pop("adoption_fences", {}).items():
                if key not in snapshot.units:
                    continue
                adopted = snapshot.units[key]
                if not fence_observation["valid"]:
                    self._set_hold(
                        adopted, Hold("adoption_fence", fence_observation["reason"], id=action.id)
                    )
                    add_event(
                        snapshot,
                        "adoption-fence-held",
                        unit=key,
                        reason=fence_observation["reason"],
                    )
                elif adopted.hold and adopted.hold.kind == "adoption_fence":
                    adopted.hold = None
                    add_event(snapshot, "adoption-fence-restored", unit=key)
            ownership = data.pop("external_ownership", None)
            if ownership is not None:
                snapshot.bindings["external_ownership"] = ownership
                snapshot.bindings["external_workers"] = [
                    item
                    for item in ownership["items"]
                    if item["kind"]
                    in {
                        "process",
                        "legacy-run",
                        "native-unit",
                        "automation",
                        "scheduler",
                        "publisher",
                    }
                    and not (
                        item["kind"] == "native-unit"
                        and (item.get("details") or {}).get("campaign_id") == snapshot.id
                    )
                ]
                for key, unit in snapshot.units.items():
                    if unit.state != UnitState.READY or unit.hold:
                        continue
                    for external in snapshot.bindings["external_workers"]:
                        detail = external.get("details") or {}
                        if (
                            external["kind"] == "legacy-run"
                            and unit.spec.selector in external.get("units", [])
                            and external.get("state") != "finished"
                            and detail.get("project") != unit.bindings.get("workspace")
                        ):
                            unit.hold = Hold(
                                "ownership_uncertain",
                                "legacy run still owns this selected source unit",
                                id=external["id"],
                            )
                            break
                        if (
                            external["kind"] == "process"
                            and detail.get("worktree_path") == unit.bindings.get("workspace")
                            and external["liveness"] != "dead"
                        ):
                            unit.hold = Hold(
                                "ownership_uncertain",
                                "an independently observed native actor occupies this unit checkout",
                                id=external["id"],
                            )
                            break
            snapshot.bindings["inventory"] = data
        elif kind == "source-observation":
            original = snapshot.bindings.setdefault("source_observations", {})
            for key, observed in data["observations"].items():
                held = snapshot.units.get(key)
                if not held or not held.hold or held.hold.kind != "source_blocked":
                    continue
                previous = original.get(key, {})
                if previous.get("source_metadata_revisions") != observed.get(
                    "source_metadata_revisions"
                ):
                    held.hold = Hold(
                        "source_changed",
                        "source dependencies or ownership changed; reviewed graph revision required",
                        id=action.id,
                    )
                elif not observed.get("blocked_reason") and not observed.get("hitl"):
                    held.hold = None
                    original[key] = observed
                    add_event(snapshot, "source-hold-satisfied", unit=key, action=action.id)
            snapshot.bindings["source_refresh_at"] = data["observed_at"]
        elif kind == "capacity":
            snapshot.bindings["capacity"] = data
        elif kind == "communication":
            snapshot.bindings["communication"] = data
            for peer in data.get("peer_bindings", ()):
                observed_unit = snapshot.units.get(peer["unit_id"])
                if (
                    observed_unit
                    and observed_unit.claim
                    and peer["attempt"] == observed_unit.claim.attempt
                ):
                    observed_unit.bindings["hcom"] = peer
            if not data.get("available", True):
                add_event(snapshot, "communication-unavailable", detail=data.get("detail", ""))
        elif kind == "reflection":
            if unit is not None:
                prior = unit.bindings.get("reflection")
                if prior and prior.get("stage") != data.get("stage"):
                    history = unit.bindings.setdefault("reflection_history", [])
                    history.append(prior)
                    unit.bindings["reflection_history"] = history[-20:]
                unit.bindings["reflection"] = data
                if data.get("history_path"):
                    add_event(
                        snapshot,
                        "reflection-history",
                        unit=unit.id,
                        history_path=data["history_path"],
                    )
                windows = data.get("lifecycle", {}).get("stage", {}).get("windows", [])
                peers = [
                    window["hcom_binding"]
                    for window in windows
                    if window.get("hcom_binding") and window.get("state") in {"active", "launching"}
                ]
                if len(peers) == 1:
                    unit.bindings["hcom"] = peers[0]
                    history = unit.bindings.setdefault("hcom_history", [])
                    if peers[0] not in history:
                        history.append(peers[0])
        elif kind == "lifecycle":
            if unit is not None:
                unit.bindings["lifecycle"] = data
                add_event(
                    snapshot,
                    "lifecycle-observed",
                    unit=unit.id,
                    state=data.get("state"),
                    detail=data.get("detail", ""),
                )
        elif kind == "intake":
            from .campaign_runtime_intake import apply_poll

            apply_poll(snapshot, data)
        elif kind == "intake-control":
            from .campaign_runtime_intake import create_control

            create_control(snapshot, data)
        elif kind == "usage-hold":
            self._set_hold(unit, Hold("lifetime_budget", data["reason"], id=action.id))
        elif kind == "adopt-draft":
            if data.get("intent"):
                self._queue_intent(snapshot, unit, data)
            else:
                unit.bindings["adoption"]["state"] = "draft_confirmed"
        elif kind == "adopt-retire":
            self._queue_intent(snapshot, unit, data)
        elif kind == "intake-refresh":
            if data.get("intent"):
                self._queue_intent(snapshot, unit, data)
            else:
                from .campaign_intake_amendment import apply

                apply(snapshot, unit, data)
        elif unit is None:
            raise CampaignError("effect names an unknown unit")
        elif kind == "reconcile-existing":
            unit.evidence["prior_delivery_checked"] = True
            if data.get("existing_hold"):
                self._set_hold(
                    unit, Hold("approval", data["existing_hold"], id=action.id, actor="github")
                )
            elif data.get("proof"):
                from .campaign_existing import PriorDeliveryProof
                from .campaign_store import reconcile_prior_delivery

                reconcile_prior_delivery(snapshot, unit.id, PriorDeliveryProof(**data["proof"]))
        elif kind == "prepare-unit":
            unit.bindings.update(data)
            if action.payload.get("adoption"):
                unit.bindings["adoption"]["state"] = "owned"
                if unit.hold and unit.hold.kind in {"migration", "overlap", "admission"}:
                    unit.hold = None
            transition(snapshot, unit.id, UnitState.DRAFT)
        elif kind == "hierarchy":
            unit.bindings["hierarchy_ready"] = data["complete"]
            if data.get("reason"):
                unit.hold = Hold("tracking", data["reason"], id=action.id)
            if data.get("intent"):
                self._queue_intent(
                    snapshot, unit, {"intent": data["intent"]}, tracking_key=data["action_key"]
                )
        elif kind == "hierarchy-close":
            if data.get("stale_scope") or data.get("graph_revision") != snapshot.graph_revision:
                return
            if data.get("intent"):
                self._queue_intent(
                    snapshot,
                    unit,
                    {"intent": data["intent"], "purpose": "parent-tracking"},
                    tracking_key=data["action_key"],
                )
            elif data.get("complete") or data.get("reason"):
                snapshot.bindings["hierarchy_finalized_graph"] = snapshot.graph_revision
                snapshot.bindings["hierarchy_remaining_scope"] = data.get("reason", "")
        elif kind in {
            "prepare-pr",
            "prepare-ready",
            "prepare-review-requests",
            "prepare-draft",
            "sync-tracking",
            "intake-cancel",
            "intake-cancel-issue",
            "intake-note",
            "intake-completion-note",
        }:
            if kind == "sync-tracking":
                unit.evidence["source_publication"] = data.get("source_publication")
                if data.get("tracking_control"):
                    from .campaign_runtime_tracking import admit as admit_tracking

                    try:
                        admit_tracking(snapshot, data["tracking_control"])
                    except CampaignError as exc:
                        unit.hold = Hold("tracking_authority_changed", str(exc), id=action.id)
                        add_event(snapshot, "tracking-control-held", unit=unit.id, reason=str(exc))
                    return
            self._queue_intent(snapshot, unit, data)
        elif kind == "ready":
            transition(snapshot, unit.id, UnitState.READY)
        elif kind == "planning-complete":
            artifacts = {}
            for receipt in unit.evidence["planning_flow"]["stages"].values():
                for artifact in receipt.get("validated", {}).get("artifacts", []):
                    artifacts[artifact["path"]] = artifact
            unit.evidence["source_artifacts"] = list(artifacts.values())
            unit.bindings["source_revision"] = hashlib.sha256(
                json.dumps(unit.evidence["source_artifacts"], sort_keys=True).encode()
            ).hexdigest()
            transition(snapshot, unit.id, UnitState.LOCALLY_COMPLETE)
        elif kind == "decision-project":
            for intent in data["intents"]:
                self._queue_intent(
                    snapshot, unit, {"intent": intent, "purpose": "decision-summary"}
                )
            unit.bindings.setdefault("decision_projections", {})[data["decision_id"]] = {
                "ledger": data["ledger"],
                "summary_actions": [row["action_id"] for row in data["intents"]],
            }
        elif kind == "resolution-context":
            if data.get("stale_context"):
                return
            from .campaign_resolution_flow import _context_current, apply_context

            if not _context_current(snapshot, unit.id, data["packet"]):
                add_event(
                    snapshot, "stale-recovery-context-discarded", unit=unit.id, action=action.id
                )
                return

            apply_context(snapshot, unit.id, data)
        elif kind == "remote-wait":
            if unit.hold and unit.hold.id == data["hold_id"]:
                unit.evidence["remote_wait_observation"] = data
                if data.get("ready_to_reaccept"):
                    unit.hold = None
                    unit.retry_at = None
                    add_event(snapshot, "remote-wait-cleared", unit=unit.id)
                else:
                    unit.retry_at = self.clock() + 15
        elif kind == "resolution-backoff":
            from .campaign_resolution_flow import apply_backoff

            apply_backoff(snapshot, unit.id, now=self.clock())
        elif kind == "resolution-finish":
            from .campaign_resolution_delivery import apply as apply_resolution

            apply_resolution(snapshot, unit, data)
        elif kind == "resolution-exhausted":
            unit.hold = Hold(
                "resolution_attempts_exhausted",
                "bounded same-PR repair did not converge",
                id=data["proposal_id"],
            )
        elif kind == "resolution-reprobe":
            if data["available"]:
                unit.hold = None
                unit.retry_at = None
                if unit.state != UnitState.READY:
                    transition(snapshot, unit.id, UnitState.READY)
        elif kind == "terminate-worker":
            unit.bindings["stop_requested_for"] = data["launch_id"]
        elif kind == "github":
            if data.get("canceled_before_write"):
                add_event(
                    snapshot,
                    "metadata-write-canceled",
                    action=action.id,
                    reason="required scope changed before parent closure",
                )
                if data.get("needs_integration") and unit.state == UnitState.READY_TO_PUBLISH:
                    from .campaign_publication_retry import invalidate

                    held = invalidate(snapshot, unit.id, action.id, data.get("base", ""))
                    if held:
                        self._set_hold(unit, held)
                if data.get("planning_drift"):
                    from .campaign_planning_publication import invalidate

                    invalidate(snapshot, unit, data["planning_drift"])
                elif data.get("source_drift"):
                    unit.hold = Hold(
                        "source_changed",
                        "accepted source changed before merge: " + data["source_drift"],
                        id=action.id,
                    )
                return
            intent = data["intent"]
            if intent["state"] != "confirmed":
                raise CampaignError("unconfirmed GitHub mutation cannot advance state")
            receipt = intent["receipt"]
            purpose = action.payload.get("purpose")
            if purpose == "pr":
                unit.bindings["pr"] = {
                    "number": receipt["number"],
                    "url": receipt.get("html_url", ""),
                    "node_id": receipt.get("node_id", ""),
                }
            elif purpose == "ready":
                unit.bindings["ready_for_review"] = True
            elif purpose == "review-requests":
                unit.bindings["recruited_head"] = intent["coordinates"]["candidate"]["head"]
                unit.evidence.setdefault("review_recruitment", []).append(intent)
            elif purpose == "pr-head-check":
                state = unit.evidence.setdefault("pr_head_check", {})
                if intent["coordinates"]["candidate"] == state.get("candidate"):
                    state["published"] = True
                    state["hosted_check"] = receipt
            elif purpose == "adoption-draft":
                unit.bindings["adoption"]["state"] = "draft_confirmed"
            elif purpose == "adoption-retire":
                from .campaign_adoption import apply_retirement

                apply_retirement(snapshot, unit, intent)
            elif purpose == "amendment-draft":
                unit.bindings["ready_for_review"] = False
            elif purpose == "repair-draft":
                unit.bindings["ready_for_review"] = False
            elif purpose == "merge":
                unit.bindings["merge_request_id"] = receipt.get("details", {}).get("uuid", "")
                if intent["kind"] == "project_merge":
                    unit.bindings["project_merge_intent"] = intent
                if snapshot.spec.github.merge_mode == "queue":
                    prior_queue = unit.evidence.pop("queue", None)
                    if prior_queue and prior_queue.get("workspace"):
                        unit.evidence.setdefault("queue_history", []).append(prior_queue)
                transition(snapshot, unit.id, UnitState.MERGE_REQUESTED)
            elif purpose == "queue-check":
                queue = unit.evidence.setdefault("queue", {})
                if (intent.get("coordinates") or {}).get("group_digest") == queue.get(
                    "group_digest"
                ):
                    if receipt.get("conclusion") == "success":
                        queue["check_published"] = True
                    else:
                        queue["failure_check_attempted"] = True
                    queue["observed_at"] = 0
            elif purpose == "queue-dequeue":
                unit.evidence.setdefault("queue", {})["observed_at"] = 0
            elif purpose == "intake-note":
                from .campaign_runtime_intake import apply_note

                apply_note(snapshot, unit.id, intent)
            elif purpose == "intake-completion-note":
                from .campaign_runtime_intake import apply_completion_note

                apply_completion_note(snapshot, unit.id, intent)
            elif purpose == "intake-cancel":
                from .campaign_runtime_intake import apply_cancellation

                apply_cancellation(snapshot, unit.id, intent)
            elif purpose == "intake-cancel-issue":
                from .campaign_runtime_intake import apply_issue_cancellation

                apply_issue_cancellation(snapshot, unit.id, intent)
            elif purpose == "tracking":
                if unit.spec.kind == "planning":
                    from .campaign_runtime_intake import validate_activation

                    try:
                        validate_activation(snapshot, unit.id)
                    except CampaignError as exc:
                        unit.hold = Hold("planning_activation_debt", str(exc), id=action.id)
                        add_event(
                            snapshot, "planning-activation-held", unit=unit.id, reason=str(exc)
                        )
                        return
                snapshot.bindings["last_delivery_at"] = self.clock()
                transition(snapshot, unit.id, UnitState.DELIVERED)
                if unit.spec.kind == "planning":
                    from .campaign_planning_flow import activate

                    publication = unit.evidence.get("source_publication") or {}
                    activate(
                        snapshot,
                        unit.id,
                        canonical_revision=publication["canonical_head"],
                        source_bindings=publication["source_bindings"],
                    )
                    from .campaign_runtime_intake import complete_delivery

                    complete_delivery(snapshot, unit.id)
        elif kind == "launch":
            if not unit.claim or unit.claim.launch_id != action.id:
                raise CampaignError("launch result no longer owns claim")
            if data.get("not_launched") and not data.get("remote_feedback_changed"):
                unit.claim.quiescent = True
                unit.claim.status = "not_launched"
                if data.get("adoption_refused"):
                    self._set_hold(
                        unit, Hold("adoption_fence", data["adoption_refused"], id=action.id)
                    )
                if unit.state == UnitState.CLAIMED:
                    transition(snapshot, unit.id, UnitState.READY)
                elif unit.state == UnitState.VERIFYING:
                    transition(snapshot, unit.id, UnitState.SYNCING)
                add_event(snapshot, "operator-held-launch-suppressed", unit=unit.id)
                return
            if data.get("not_launched") and data.get("remote_feedback_changed"):
                unit.claim.quiescent = True
                unit.claim.status = "not_launched"
                unit.evidence["remote_feedback_previous"] = unit.evidence.pop("remote_feedback")
                for key in (
                    "failure",
                    "accepted_resolution",
                    "resolution_context",
                    "resolution_next_step",
                    "required_findings",
                ):
                    unit.evidence.pop(key, None)
                unit.hold = None
                unit.resume_state = UnitState.SYNCING
                add_event(snapshot, "remote-feedback-refreshed", unit=unit.id)
                return
            unit.claim.status = "launched"
            unit.claim.pid = data.get("pid")
            unit.claim.started_at = self.clock()
            unit.claim.run_dir = data["directory"]
            amendments = unit.evidence.get("amendment_history", [])
            if action.payload.get("kind") == "execution" and amendments:
                amendments[-1].setdefault("first_execution", action.id)
            if unit.state == UnitState.CLAIMED:
                transition(snapshot, unit.id, UnitState.RUNNING)
        elif kind == "checkpoint-progress":
            unit.evidence["checkpoint_progress"] = data
            unit.bindings["checkpoint_observed_at"] = self.clock()
        elif kind in {"push", "checkpoint"}:
            unit.bindings["last_checkpoint_at"] = self.clock()
            if not data.get("skipped"):
                unit.bindings["last_pushed_head"] = data["head"]
                unit.bindings["last_pushed_at"] = time.time()
            if kind == "push":
                unit.evidence["pushed"] = data["head"]
        elif kind in {"queue-observe", "queue-checkout", "queue-publish", "queue-dequeue"}:
            from .campaign_runtime_queue import apply_effect

            apply_effect(self, snapshot, unit, action, data)
        elif kind in {"pr-head-checkout", "pr-head-publish"}:
            if data.get("needs_integration"):
                from .campaign_publication_retry import invalidate

                held = invalidate(snapshot, unit.id, action.id, data.get("base", ""))
                if held:
                    self._set_hold(unit, held)
            elif data.get("invalidate_head_check"):
                unit.evidence.pop("pr_head_check", None)
            elif kind == "pr-head-checkout":
                unit.evidence["pr_head_check"] = data
            else:
                self._queue_intent(snapshot, unit, data)
        elif kind == "prepare-merge":
            if data.get("remote_feedback"):
                from .campaign_remote_feedback import RemoteFeedback
                from .campaign_remote_feedback import apply as apply_remote_feedback

                raw = dict(data["remote_feedback"])
                raw["candidate"] = Candidate(**raw["candidate"])
                raw["defects"] = tuple(raw["defects"])
                apply_remote_feedback(snapshot, unit.id, RemoteFeedback(**raw))
            elif data.get("external_followup_hold"):
                unit.evidence["observed_external_merge"] = data["merge"]
                self._set_hold(
                    unit,
                    Hold(
                        "postmerge_acceptance",
                        data["external_followup_hold"],
                        id=action.id,
                        evidence={"merge": data["merge"]},
                    ),
                )
            elif data.get("external_merge"):
                from .campaign_external_merge import ExternalMergeEvidence
                from .campaign_store import reconcile_external_merge

                raw = dict(data["external_merge"])
                raw["parents"] = tuple(raw["parents"])
                raw["checkpoint_heads"] = tuple(raw["checkpoint_heads"])
                reconcile_external_merge(snapshot, unit.id, ExternalMergeEvidence(**raw))
                unit.evidence["merge"] = data["merge"]
            elif data.get("planning_drift"):
                from .campaign_planning_publication import invalidate

                invalidate(snapshot, unit, data["planning_drift"])
            elif data.get("source_drift"):
                unit.hold = Hold(
                    "source_changed",
                    "accepted source changed before merge: " + data["source_drift"],
                    id=action.id,
                )
            elif data.get("needs_integration"):
                from .campaign_publication_retry import invalidate

                held = invalidate(snapshot, unit.id, action.id, data.get("base", ""))
                if held:
                    self._set_hold(unit, held)
            else:
                self._queue_intent(snapshot, unit, data)
        elif kind == "observe-merge":
            if data.get("project_merge_rejected"):
                intent = data["intent"]
                unit.evidence.setdefault("project_merge_history", []).append(intent)
                receipt = intent.get("receipt") or {}
                if (
                    receipt.get("submission") == "not-submitted"
                    and receipt.get("definitive") is True
                ):
                    unit.bindings.pop("project_merge_intent", None)
                    unit.bindings.pop("merge_request_id", None)
                    transition(snapshot, unit.id, UnitState.READY_TO_PUBLISH)
                else:
                    self._set_hold(
                        unit,
                        Hold(
                            "external_wait",
                            data["reason"],
                            id=action.id,
                            actor="project-executor",
                            evidence={"intent": intent},
                        ),
                    )
                return
            unit.evidence["merge"] = data
            transition(snapshot, unit.id, UnitState.MERGED_PENDING_SYNC)
        elif kind == "observe-worker":
            self._apply_worker(snapshot, unit, action, data)
        else:
            raise CampaignError(f"unhandled campaign effect result: {kind}")

    def _apply_worker(self, snapshot, unit, action, data) -> None:
        claim = unit.claim
        if not claim or claim.launch_id != action.payload["launch_id"]:
            raise CampaignError("worker receipt no longer owns its claim")
        if data["state"] == "interrupted":
            if not data.get("quiescence_confirmed"):
                raise CampaignError(
                    "interrupted worker has no independent coding-session quiescence proof"
                )
            claim.quiescent = True
            claim.status = "interrupted"
            from .campaign_usage import apply as apply_usage

            apply_usage(snapshot, unit, data, role=claim.role)
            from .campaign_handoff_flow import intercept_terminal

            if intercept_terminal(self, snapshot, unit, data):
                unit.bindings["head"] = data["head"]
                return
            if claim.role == "queue_verify":
                launched = next((row for row in snapshot.outbox if row.id == claim.launch_id), None)
                if launched and launched.payload.get("candidate_kind") == "pr-head":
                    from .campaign_publication_flow import apply_head_worker as apply_worker
                else:
                    from .campaign_runtime_queue import apply_worker

                apply_worker(
                    self,
                    snapshot,
                    unit,
                    {
                        "outcome": "failed",
                        "error": "combined candidate worker interrupted after confirmed quiescence",
                    },
                )
                return
            unit.bindings["head"] = data["head"]
            unit.evidence["failure"] = {
                "code": "interrupted",
                "producer": "campaign.worker",
                "id": claim.launch_id,
                "revision": data["head"],
                "detail": "worker interrupted after coding actors were confirmed stopped",
            }
            self._set_hold(
                unit,
                Hold(
                    "technical_failure",
                    "worker interrupted; preserve checkpoint history before retry",
                    id=claim.launch_id,
                ),
            )
            return
        if data["state"] != "finished":
            raise CampaignError("nonterminal worker observation cannot release its claim")
        claim.quiescent = True
        claim.status = "finished"
        receipt = data["receipt"]
        if claim.role == "queue_verify":
            if receipt.get("candidate_kind") == "pr-head":
                from .campaign_publication_flow import apply_head_worker

                unit.evidence.setdefault("worker_history", []).append(receipt)
                apply_head_worker(self, snapshot, unit, receipt)
                return
            from .campaign_runtime_queue import apply_worker

            unit.evidence.setdefault("worker_history", []).append(receipt)
            apply_worker(self, snapshot, unit, receipt)
            return
        unit.bindings["head"] = receipt["head"]
        if receipt.get("usage") is not None or receipt.get("sessions"):
            # CLI token telemetry is not dollar-cost telemetry. Keep spend
            # unknown until a qualified account receipt supplies actual cost.
            snapshot.bindings["spend_usd"] = None
            snapshot.bindings.setdefault("role_spend", {})[claim.role] = None
        unit.evidence.setdefault("worker_history", []).append(receipt)
        from .campaign_usage import apply as apply_usage

        apply_usage(snapshot, unit, receipt, role=claim.role)
        from .campaign_handoff_flow import intercept_terminal

        if receipt.get("sessions"):
            from .campaign_attribution import record as record_contributors

            if any(row.get("role") == "dev" for row in receipt["sessions"]):
                record_contributors(snapshot, unit, receipt)
        if intercept_terminal(self, snapshot, unit, receipt):
            return
        outcome = receipt["outcome"]
        if outcome == "stage_complete":
            directory = self.store.directory(snapshot.id) / "workers" / claim.launch_id
            request_path = directory / "request.json"
            if request_path.exists():
                request = json.loads(request_path.read_text(encoding="utf-8"))
                if request.get("stage_boundary"):
                    unit.evidence["stage_sync_debt"] = {"request": request, "receipt": receipt}
                    unit.evidence.pop("stage_boundary_ready", None)
                    add_event(
                        snapshot,
                        "stage-sync-pending",
                        unit=unit.id,
                        launch=claim.launch_id,
                        stage=receipt["stage"],
                    )
                    return
        if outcome != "stage_complete":
            from .campaign_runtime_intake import control as intake_control
            from .campaign_runtime_intake import fail_stage as fail_intake_stage

            value = intake_control(unit)
            if value and value.get("state") == "triage_running" and claim.role == "triage":
                fail_intake_stage(snapshot, unit.id, receipt, launch_id=claim.launch_id)
                return
        if outcome == "operator_held":
            unit.evidence.setdefault("operator_paused_workers", []).append(receipt)
            if unit.state in {UnitState.RUNNING, UnitState.CLAIMED}:
                transition(snapshot, unit.id, UnitState.READY)
            return
        if outcome == "tracking_complete":
            if unit.spec.kind != "tracking" or receipt.get("tracking") != unit.bindings.get(
                "tracking"
            ):
                raise CampaignError("Tracking child receipt differs from its exact debt authority")
            unit.legacy_outcome = outcome
            unit.evidence["tracking_artifacts"] = receipt.get("artifacts", [])
            transition(snapshot, unit.id, UnitState.LOCALLY_COMPLETE)
        elif outcome in {"locally_complete", "existing_verified"}:
            if outcome == "existing_verified":
                if not receipt.get("source_terminal_receipt", {}).get(
                    "no_development_session"
                ) or not receipt.get("source_artifacts"):
                    raise CampaignError(
                        "existing source verification lacks its selected native source proof"
                    )
                unit.evidence["existing_source_verified"] = receipt["source_terminal_receipt"]
            else:
                unit.evidence.pop("existing_source_verified", None)
            if receipt.get("retained_finalization"):
                unit.evidence.pop("retained_finalization_pending", None)
                if unit.hold and unit.hold.kind == "recovery_required":
                    unit.hold = None
            unit.legacy_outcome = outcome
            unit.evidence["source_artifacts"] = receipt.get("source_artifacts", [])
            unit.bindings["source_revision"] = hashlib.sha256(
                json.dumps(unit.evidence["source_artifacts"], sort_keys=True).encode()
            ).hexdigest()
            from .campaign_attribution import record as record_development

            record_development(snapshot, unit, receipt)
            transition(snapshot, unit.id, UnitState.LOCALLY_COMPLETE)
        elif outcome == "recovered":
            unit.evidence.setdefault("recovery_history", []).append(receipt)
            if unit.hold and unit.hold.kind == "recovery_required":
                unit.hold = None
            transition(snapshot, unit.id, UnitState.READY)
            if (
                unit.evidence["dev_attempts_spent"]
                >= self.template(snapshot).policy.limits.max_dev_attempts
            ):
                unit.hold = Hold(
                    "attempts_exhausted",
                    "lifetime child attempt budget exhausted after preserved recovery",
                    id=claim.launch_id,
                )
        elif outcome == "integrated":
            unit.evidence["integrated"] = receipt
            for key in ("verification", "review"):
                unit.evidence.pop(key, None)
            if unit.evidence.get("pushed") != receipt["head"]:
                unit.evidence.pop("pushed", None)
        elif outcome == "verified":
            if receipt.get("stage") == "resolution_verify":
                receipt["resolution_id"] = unit.evidence["accepted_resolution"]["id"]
                unit.evidence["resolution_verification"] = receipt
            else:
                unit.evidence["verification"] = receipt
                transition(snapshot, unit.id, UnitState.REVIEWING)
        elif outcome == "stage_complete":
            self._apply_stage_result(snapshot, unit, receipt)
        else:
            if outcome == "ownership_uncertain":
                unit.evidence["failure"] = {
                    "code": "ownership_uncertain",
                    "producer": "campaign.ownership",
                    "id": claim.launch_id,
                    "revision": receipt["head"],
                    "detail": receipt.get("error", outcome),
                }
            if outcome == "retained_verification_failed":
                unit.evidence["retained_finalization_pending"] = {
                    **receipt["retained_finalization_pending"],
                    "quiescence_receipt": claim.launch_id,
                }
                unit.evidence["failure"] = {
                    "code": "verification_failed",
                    "producer": "campaign.retained_finalization",
                    "id": claim.launch_id,
                    "revision": receipt["head"],
                    "detail": receipt.get("reason", "retained candidate verification failed"),
                }
                outcome = "verification_failed"
            if unit.state in {
                UnitState.SYNCING,
                UnitState.VERIFYING,
                UnitState.REVIEWING,
                UnitState.READY_TO_PUBLISH,
            }:
                unit.resume_state = UnitState.SYNCING
            else:
                unit.resume_state = UnitState.READY
            if outcome == "integration_conflict":
                unit.evidence["preserved_refs"] = receipt.get("preserved_refs", [])
                unit.evidence["preserved_heads"] = receipt.get("preserved_heads", [])
                unit.evidence["integration_conflict"] = receipt
            self._set_hold(
                unit,
                Hold(
                    outcome,
                    receipt.get("error", receipt.get("reason", outcome)),
                    id=claim.launch_id,
                ),
            )

    def _apply_stage_result(self, snapshot, unit, receipt) -> None:
        claim = unit.claim
        stage = receipt["stage"]
        if receipt.get("result_contract") == "intake-triage-v1":
            from .campaign_runtime_intake import apply_triage

            apply_triage(snapshot, unit.id, receipt, launch_id=claim.launch_id)
        elif stage == "closure":
            if not receipt.get("validated_closure"):
                raise CampaignError("closure lacks exact native artifacts and child reconciliation")
            unit.evidence["source_artifacts"] = receipt["validated_closure"]
            unit.evidence["closure_manifest"] = receipt["closure_manifest"]
            from .campaign_publication_flow import record_stage_writer

            record_stage_writer(unit, receipt)
            unit.evidence.pop("existing_source_verified", None)
            unit.bindings["source_revision"] = hashlib.sha256(
                json.dumps(unit.evidence["source_artifacts"], sort_keys=True).encode()
            ).hexdigest()
            transition(snapshot, unit.id, UnitState.LOCALLY_COMPLETE)
        elif stage == "admission_review":
            unit.evidence["admission"] = receipt["validated_admission"]
            transition(snapshot, unit.id, UnitState.READY)
        elif receipt.get("result_contract") == "repair-v1":
            unit.evidence["repair_candidate"] = receipt["validated_repair"]
            unit.evidence.pop("existing_source_verified", None)
            from .campaign_publication_flow import record_stage_writer

            record_stage_writer(unit, receipt)
        elif receipt.get("result_contract") in {"decision-v1", "resolution-v1"}:
            from .campaign_resolution_flow import apply_diagnosis

            next_step = apply_diagnosis(
                snapshot,
                unit.id,
                receipt,
                candidate=self._role(
                    snapshot,
                    (
                        "conflict_repair"
                        if unit.hold and unit.hold.kind == "integration_conflict"
                        else "escalation"
                    ),
                ),
            )
            if next_step is not None:
                unit.evidence["resolution_next_step"] = dataclasses.asdict(next_step)
        elif unit.spec.kind == "planning" and stage in snapshot.spec.planning.stages:
            from .campaign_planning_flow import apply_stage

            apply_stage(
                snapshot,
                unit.id,
                stage,
                receipt,
                launch_id=claim.launch_id,
                committed_head=receipt["head"],
            )
            if stage != "planning_review":
                from .campaign_publication_flow import record_stage_writer

                record_stage_writer(unit, receipt)
        elif stage == "publication_review":
            from .campaign_acceptance import record as record_acceptance

            unit.evidence["review"] = receipt
            acceptance = record_acceptance(unit, self._candidate(snapshot, unit), review=receipt)
            if receipt["validated"]["verdict"] == "approved" and acceptance["complete"]:
                transition(snapshot, unit.id, UnitState.READY_TO_PUBLISH)
            else:
                unit.evidence["required_findings"] = receipt["artifact"].get("findings", [])
                unit.evidence["required_findings"].extend(
                    {
                        "id": row["id"],
                        "required": True,
                        "resolved": False,
                        "reason": f"Acceptance {row['status']}: {row['text']}: {row['reason']}",
                    }
                    for row in acceptance["criteria"]
                    if row["required"] and row["status"] != "passed"
                )
                unit.resume_state = UnitState.SYNCING
                self._set_hold(
                    unit,
                    Hold(
                        "review_findings",
                        "required review findings need same-PR repair",
                        id=claim.launch_id,
                    ),
                )
        else:
            unit.evidence.setdefault("planning_stages", []).append(stage)
            unit.evidence.setdefault("planning_results", []).append(receipt)
            unit.hold = Hold(
                "planning",
                "planning stage completed; graph revision validation pending",
                id=claim.launch_id,
            )

        # A resumed review may have been live when startup attribution repair
        # ran. Repair only after accepting its result, including deferred
        # stage-boundary results, so a same-family review is invalidated too.
        self.prepare_resume(snapshot)

    def attach(self, campaign_id: str, unit_id: str, role: str = "execution") -> int:
        from .campaign_operator import attach

        return attach(self, campaign_id, unit_id, role)
