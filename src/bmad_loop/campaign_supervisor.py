"""Single-writer campaign supervision with bounded concurrent effect execution.

The OS lock fences coordinator ownership; it is never acquired by workers.
Durable intents precede effects. The event loop only consumes bounded observations
and schedules independent work; engines and coding sessions live in child PIDs.
"""

from __future__ import annotations

import dataclasses
import sys
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import contextmanager
from typing import Any, Protocol

from .campaign_model import CampaignError, CampaignSnapshot, Hold, OutboxAction, UnitState
from .campaign_store import CampaignStore, add_action, add_event


@dataclasses.dataclass(frozen=True)
class Proposal:
    kind: str
    unit: str
    payload: dict[str, Any] = dataclasses.field(default_factory=dict)
    action_id: str = ""


@dataclasses.dataclass(frozen=True)
class Observation:
    state: str
    data: dict[str, Any] = dataclasses.field(default_factory=dict)
    reason: str = ""
    retry_at: float | None = None


class CampaignDriver(Protocol):
    def proposals(self, snapshot: CampaignSnapshot) -> tuple[Proposal, ...]: ...
    def perform(self, snapshot: CampaignSnapshot, action: OutboxAction) -> Observation: ...
    def reconcile_action(self, snapshot: CampaignSnapshot, action: OutboxAction) -> Observation: ...
    def apply(
        self, snapshot: CampaignSnapshot, action: OutboxAction, observation: Observation
    ) -> None: ...
    def observe_workers(self, snapshot: CampaignSnapshot) -> tuple[Proposal, ...]: ...
    def control(self, snapshot: CampaignSnapshot, command: dict[str, Any]) -> None: ...


class CampaignSupervisor:
    _CONTROL_EFFECTS = frozenset(
        {"observe-worker", "terminate-worker", "reasoning-observe", "reasoning-terminate"}
    )

    def __init__(
        self,
        store: CampaignStore,
        driver: CampaignDriver,
        *,
        clock=time.time,
        progress=None,
        io_workers: int = 16,
    ):
        self.store, self.driver, self.clock = store, driver, clock
        self.progress = progress or (lambda message: print(message, file=sys.stderr, flush=True))
        self.io_workers = io_workers
        self._active: dict[str, Future[Observation]] = {}
        self._control_active: set[str] = set()
        self._wake = threading.Event()
        self._control_executor: ThreadPoolExecutor | None = None

    @contextmanager
    def _executors(self):
        # Git/source observations can be slow. They must not occupy the slots
        # needed to observe or stop already-owned native coding processes.
        with ThreadPoolExecutor(max_workers=2) as controls:
            with ThreadPoolExecutor(max_workers=self.io_workers) as effects:
                self._control_executor = controls
                try:
                    yield effects
                finally:
                    self._control_executor = None

    def _save(self, snapshot: CampaignSnapshot) -> CampaignSnapshot:
        return self.store.save(snapshot, snapshot.revision, expected_generation=snapshot.generation)

    def _commands(self, snapshot: CampaignSnapshot) -> CampaignSnapshot:
        commands = self.store.commands(snapshot.id, after=snapshot.processed_commands)
        if not commands:
            return snapshot
        for command in commands:
            try:
                self.driver.control(snapshot, command)
            except CampaignError as exc:
                add_event(
                    snapshot,
                    "operator-command-refused",
                    unit=command.get("unit", ""),
                    command=command["kind"],
                    reason=str(exc),
                )
                snapshot.processed_commands.append(command["id"])
                continue
            snapshot.processed_commands.append(command["id"])
            add_event(
                snapshot,
                "operator-command",
                unit=command.get("unit", ""),
                command=command["kind"],
                actor=command.get("actor", "operator"),
            )
        return self._save(snapshot)

    def _submit(
        self,
        snapshot: CampaignSnapshot,
        action: OutboxAction,
        executor: ThreadPoolExecutor,
        *,
        reconcile: bool,
    ) -> CampaignSnapshot:
        action = next(row for row in snapshot.outbox if row.id == action.id)
        if action.id in self._active:
            return snapshot
        adoption = getattr(self.driver, "prepare_reconciliation", None)
        if reconcile and adoption is not None:
            adoption(snapshot, action)
            snapshot = self._save(snapshot)
            action = next(row for row in snapshot.outbox if row.id == action.id)
        if action.state == "prepared":
            action.state = "sent_unknown"
            add_event(
                snapshot, "effect-submitted", unit=action.unit, action=action.id, effect=action.kind
            )
            snapshot = self._save(snapshot)
            action = next(row for row in snapshot.outbox if row.id == action.id)
        # Never pass a mutable authority object to an I/O executor.
        frozen = snapshot.clone()
        owned_action = next(row for row in frozen.outbox if row.id == action.id)
        operation = self.driver.reconcile_action if reconcile else self.driver.perform
        if self._control_executor is not None and action.kind in self._CONTROL_EFFECTS:
            executor = self._control_executor
        self._active[action.id] = executor.submit(operation, frozen, owned_action)
        if executor is self._control_executor:
            self._control_active.add(action.id)
        return snapshot

    def _harvest(self, snapshot: CampaignSnapshot) -> CampaignSnapshot:
        changed = False
        for action_id, future in tuple(self._active.items()):
            if not future.done():
                continue
            del self._active[action_id]
            self._control_active.discard(action_id)
            action = next(row for row in snapshot.outbox if row.id == action_id)
            try:
                observation = future.result()
            except Exception as exc:
                observation = Observation(
                    "unknown", reason=f"{type(exc).__name__}: {exc}", retry_at=self.clock() + 5
                )
            if observation.state not in {"confirmed", "unknown", "rejected", "waiting"}:
                raise CampaignError("driver returned an invalid effect state")
            if observation.state == "confirmed":
                # Driver validates receipt coordinates before any state mutation.
                self.driver.apply(snapshot, action, observation)
                action.state = "confirmed"
                from .campaign_boundary_receipts import project as project_receipt

                action.receipt = project_receipt(snapshot, action, observation.data)
                action.error = ""
                add_event(
                    snapshot,
                    "effect-confirmed",
                    unit=action.unit,
                    action=action.id,
                    effect=action.kind,
                )
            elif observation.state == "rejected":
                action.state = "rejected"
                action.error = observation.reason
                if action.unit in snapshot.units:
                    unit = snapshot.units[action.unit]
                    rejected_hold = Hold(
                        "effect_refused",
                        observation.reason,
                        id=action.id,
                        actor="campaign",
                        created_at=self.clock(),
                    )
                    setter = getattr(self.driver, "_set_hold", None)
                    if setter is None:
                        unit.hold = rejected_hold
                    else:
                        setter(unit, rejected_hold)
                add_event(
                    snapshot,
                    "effect-rejected",
                    unit=action.unit,
                    action=action.id,
                    reason=observation.reason,
                )
            else:
                action.error = observation.reason
                action.next_retry_at = observation.retry_at or self.clock() + 5
                action.receipt = {**action.receipt, **observation.data}
                add_event(
                    snapshot,
                    "effect-wait",
                    unit=action.unit,
                    action=action.id,
                    reason=observation.reason,
                    observation=observation.state,
                )
            from .campaign_metrics import observe_effect

            observe_effect(
                snapshot, action, state=observation.state, data=observation.data, now=self.clock()
            )
            changed = True
        return self._save(snapshot) if changed else snapshot

    def _dispatch_ready(
        self, snapshot: CampaignSnapshot, executor: ThreadPoolExecutor
    ) -> CampaignSnapshot:
        """Persist a bounded submission batch before starting any of its effects.

        A crash between commit and submission leaves every selected effect
        uncertain, exactly as an interrupted individual submission does. Each
        executor still receives its own independent mutable snapshot.
        """
        controls = len(self._control_active)
        regular = len(self._active) - controls
        selected: list[tuple[str, bool]] = []
        for action in snapshot.outbox:
            if action.state not in {"prepared", "sent_unknown"} or action.id in self._active:
                continue
            if action.next_retry_at and action.next_retry_at > self.clock():
                continue
            if action.kind in self._CONTROL_EFFECTS:
                if controls >= 2:
                    continue
                controls += 1
            else:
                if regular >= self.io_workers:
                    continue
                regular += 1
            selected.append((action.id, action.state == "sent_unknown"))
        fresh = {key for key, reconcile in selected if not reconcile}
        if fresh:
            for action in snapshot.outbox:
                if action.id in fresh:
                    action.state = "sent_unknown"
                    add_event(
                        snapshot,
                        "effect-submitted",
                        unit=action.unit,
                        action=action.id,
                        effect=action.kind,
                    )
            snapshot = self._save(snapshot)
        actions = {action.id: action for action in snapshot.outbox}
        for key, reconcile in selected:
            snapshot = self._submit(snapshot, actions[key], executor, reconcile=reconcile)
        return snapshot

    def _queue(
        self, snapshot: CampaignSnapshot, proposals: tuple[Proposal, ...]
    ) -> CampaignSnapshot:
        changed = False
        for proposal in proposals:
            if any(
                row.unit == proposal.unit
                and row.kind == proposal.kind
                and row.payload.get("job_id") == proposal.payload.get("job_id")
                and row.payload.get("parent_key") == proposal.payload.get("parent_key")
                and row.state in {"prepared", "sent_unknown"}
                for row in snapshot.outbox
            ):
                continue
            action = add_action(
                snapshot,
                proposal.kind,
                proposal.unit,
                proposal.payload,
                action_id=proposal.action_id or None,
            )
            self.driver.control(
                snapshot, {"kind": "prepare-effect", "action": dataclasses.asdict(action)}
            )
            changed = True
        return self._save(snapshot) if changed else snapshot

    def _reconciled(self, campaign_id: str, executor: ThreadPoolExecutor) -> CampaignSnapshot:
        # Queued holds/stops must reach the authority before an uncertain
        # effect can authorize any retry during startup reconciliation.
        snapshot = self._commands(self.store.load(campaign_id))
        prepare_resume = getattr(self.driver, "prepare_resume", None)
        if prepare_resume is not None and prepare_resume(snapshot):
            snapshot = self._save(snapshot)
        for action in tuple(snapshot.outbox):
            if action.state == "sent_unknown":
                snapshot = self._submit(snapshot, action, executor, reconcile=True)
        while self._active:
            snapshot = self._commands(snapshot)
            if snapshot.bindings.get("terminate_requested"):
                observations = getattr(self.driver, "observe_workers", lambda _: ())(snapshot)
                snapshot = self._queue(
                    snapshot,
                    tuple(
                        proposal
                        for proposal in observations
                        if proposal.kind in {"terminate-worker", "reasoning-terminate"}
                    ),
                )
                for action in tuple(snapshot.outbox):
                    if action.state == "prepared" and action.kind in {
                        "terminate-worker",
                        "reasoning-terminate",
                    }:
                        snapshot = self._submit(snapshot, action, executor, reconcile=False)
            snapshot = self._harvest(snapshot)
            if self._active:
                self._wake.wait(0.05)
        return snapshot

    def reconcile(self, campaign_id: str) -> CampaignSnapshot:
        snapshot = self.store.load(campaign_id)
        with self.store.coordinator(
            snapshot.common_dir, snapshot.spec.target, campaign_id=campaign_id
        ):
            with self._executors() as executor:
                snapshot = self._reconciled(campaign_id, executor)
                snapshot = self._commands(snapshot)
                observations = getattr(self.driver, "reconciliation_observations", None)
                if observations is not None:
                    snapshot = self._queue(snapshot, observations(snapshot))
                    for action in tuple(snapshot.outbox):
                        if action.state == "prepared" and action.kind in {
                            "inventory",
                            "capacity",
                            "intake",
                            "observe-worker",
                        }:
                            snapshot = self._submit(snapshot, action, executor, reconcile=False)
                    while self._active:
                        snapshot = self._harvest(snapshot)
                        if self._active:
                            self._wake.wait(0.05)
                return snapshot

    def run(self, campaign_id: str, *, max_ticks: int | None = None) -> CampaignSnapshot:
        snapshot = self.store.load(campaign_id)
        with self.store.coordinator(
            snapshot.common_dir, snapshot.spec.target, campaign_id=campaign_id
        ):
            # Reconcile old effects under their original coordinates before a
            # new coordinator generation can admit fresh work.
            with self._executors() as executor:
                snapshot = self._reconciled(campaign_id, executor)
                snapshot.generation += 1
                if snapshot.phase == "stopped":
                    snapshot.bindings.pop("terminate_requested", None)
                snapshot.phase = "active" if snapshot.phase == "stopped" else snapshot.phase
                for action in snapshot.outbox:
                    if action.state == "prepared":
                        # Prepared is durably never sent. Adopt the same intent
                        # explicitly, retaining its original action ID/history.
                        add_event(
                            snapshot,
                            "prepared-effect-adopted",
                            unit=action.unit,
                            action=action.id,
                            prior_generation=action.generation,
                            generation=snapshot.generation,
                        )
                        action.generation = snapshot.generation
                        if isinstance(action.payload.get("intent"), dict):
                            action.payload["intent"]["generation"] = snapshot.generation
                        unit = snapshot.units.get(action.unit)
                        if unit and unit.claim and unit.claim.status == "prepared":
                            unit.claim.generation = snapshot.generation
                        from .campaign_reasoning_flow import adopt_prepared_generation

                        adopt_prepared_generation(snapshot, action)
                add_event(snapshot, "coordinator-start", generation=snapshot.generation)
                # Generation ownership advances atomically; no prior actor may
                # be treated as dead merely because this generation is new.
                snapshot = self.store.save(snapshot, snapshot.revision)
                ticks = 0
                progress_at = 0.0
                try:
                    while snapshot.phase not in {"delivered", "stopped"}:
                        snapshot = self._commands(snapshot)
                        snapshot = self._harvest(snapshot)
                        if (
                            snapshot.phase == "draining"
                            and not self._active
                            and not any(
                                unit.claim and not unit.claim.quiescent
                                for unit in snapshot.units.values()
                            )
                            and not any(
                                job.get("claim") and not job["claim"]["quiescent"]
                                for job in snapshot.jobs.values()
                            )
                            and getattr(self.driver, "communication_settled", lambda _: True)(
                                snapshot
                            )
                        ):
                            # Leave durable API debt for resume. Retrying it
                            # before checking drain completion can keep an
                            # otherwise quiescent campaign running forever.
                            snapshot.phase = "stopped"
                            add_event(snapshot, "campaign-drained")
                            snapshot = self._save(snapshot)
                            break
                        if self.clock() - progress_at >= 10:
                            counts: dict[str, int] = {}
                            for unit in snapshot.units.values():
                                counts[str(unit.state)] = counts.get(str(unit.state), 0) + 1
                            self.progress(
                                f"{snapshot.id}: {snapshot.phase}; "
                                + ", ".join(
                                    f"{key}={value}" for key, value in sorted(counts.items())
                                )
                            )
                            progress_at = self.clock()
                        # Observation can admit reasoning jobs. Persist their
                        # claims before planning unit work against capacity.
                        snapshot = self._queue(snapshot, self.driver.observe_workers(snapshot))
                        proposals = ()
                        if snapshot.phase == "active":
                            proposals = self.driver.proposals(snapshot)
                        from .campaign_metrics import observe_admission

                        if observe_admission(snapshot, now=self.clock()):
                            snapshot = self._save(snapshot)
                        snapshot = self._queue(snapshot, proposals)
                        snapshot = self._dispatch_ready(snapshot, executor)
                        live = any(
                            unit.claim and not unit.claim.quiescent
                            for unit in snapshot.units.values()
                        )
                        live = live or any(
                            job.get("claim") and not job["claim"]["quiescent"]
                            for job in snapshot.jobs.values()
                        )
                        unsettled = any(
                            action.state in {"prepared", "sent_unknown"}
                            for action in snapshot.outbox
                        )
                        from .campaign_runtime_intake import optional_canceled, settled

                        if (
                            snapshot.units
                            and all(
                                unit.state == UnitState.DELIVERED or optional_canceled(unit)
                                for unit in snapshot.units.values()
                            )
                            and not unsettled
                            and not live
                            and settled(snapshot)
                            and getattr(self.driver, "completion_ready", lambda _: True)(snapshot)
                        ):
                            for job in snapshot.jobs.values():
                                if not job["request"]["required"] and job["state"] not in {
                                    "applied",
                                    "stale",
                                    "canceled",
                                }:
                                    job["state"] = "canceled"
                                    job["reason"] = (
                                        "optional background job canceled after delivery"
                                    )
                                    add_event(snapshot, "optional-job-canceled", job_id=job["id"])
                            snapshot.phase = "delivered"
                            add_event(snapshot, "campaign-delivered")
                            snapshot = self._save(snapshot)
                        elif (
                            snapshot.phase == "draining"
                            and not live
                            and not self._active
                            and getattr(self.driver, "communication_settled", lambda _: True)(
                                snapshot
                            )
                        ):
                            snapshot.phase = "stopped"
                            add_event(snapshot, "campaign-drained")
                            snapshot = self._save(snapshot)
                        ticks += 1
                        if max_ticks is not None and ticks >= max_ticks:
                            return snapshot
                        self._wake.wait(snapshot.spec.poll_interval_s)
                except KeyboardInterrupt:
                    # A save may have committed before interruption prevented its
                    # return (for example during disposable event projection).
                    # Only durable authority can determine what was accepted;
                    # replaying this caller's old revision loses that boundary.
                    snapshot = self.store.load(campaign_id)
                    if snapshot.phase not in {"delivered", "stopped"}:
                        snapshot.phase = "draining"
                    add_event(snapshot, "coordinator-interrupted")
                    self._save(snapshot)
                    raise
                return snapshot
