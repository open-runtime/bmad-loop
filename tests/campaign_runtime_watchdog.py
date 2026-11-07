"""Finite progress guard for scripted production campaigns, not a throughput SLA."""

from __future__ import annotations

import subprocess
import threading
import time
from pathlib import Path

from bmad_loop.campaign_model import CampaignSnapshot
from bmad_loop.campaign_planning_flow import STAGES


class TimedDriver:
    """Observe effect latency without changing the runtime or its wall clock."""

    def __init__(self, driver, *, clock=time.monotonic):
        self.driver = driver
        self.clock = clock
        self.lock = threading.Lock()
        self.active = {}
        self.slowest = []

    def __getattr__(self, name):
        return getattr(self.driver, name)

    def _call(self, operation, snapshot, action):
        started = self.clock()
        row = {"id": action.id, "kind": action.kind, "unit": action.unit, "started_at": started}
        with self.lock:
            self.active[action.id] = row
        try:
            return operation(snapshot, action)
        finally:
            row = {**row, "duration_s": self.clock() - started}
            with self.lock:
                self.active.pop(action.id, None)
                self.slowest = sorted(
                    [*self.slowest, row], key=lambda item: item["duration_s"], reverse=True
                )[:16]

    def perform(self, snapshot, action):
        return self._call(self.driver.perform, snapshot, action)

    def reconcile_action(self, snapshot, action):
        return self._call(self.driver.reconcile_action, snapshot, action)

    def details(self):
        with self.lock:
            return {
                "active_effect_timings": [
                    {**row, "elapsed_s": self.clock() - row["started_at"]}
                    for row in self.active.values()
                ],
                "slowest_effects": list(self.slowest),
            }


class ProgressWatchdog:
    """Wait for delivery while refusing a stagnant or endlessly changing run.

    Only a unit's first visit to a state or first validated planning stage counts.
    Polls, retry attempts, fresh heads and repeated review cycles do not count.
    The absolute ceiling is fixed from the initial graph and configured intake
    budget: one no-progress interval per possible unit/control, plus closure.
    Explicit caller deadlines retain their original absolute-time semantics.
    """

    def __init__(self, snapshot, *, now, timeout_s=None, idle_s=300.0):
        self.started_at = self.progress_at = now
        self.idle_s = idle_s
        self.timeout_s = timeout_s
        # Each allowed incoming unit can require its own planning control.
        incoming = snapshot.spec.intake.max_new_units if snapshot.spec.intake.allow_new_units else 0
        self.unit_budget = max(
            1, len(snapshot.units) + 2 * incoming + int(snapshot.spec.planning.enabled)
        )
        self.total_s = timeout_s if timeout_s is not None else idle_s * (self.unit_budget + 1)
        self.units = set(snapshot.units)
        self.seen = self._milestones(snapshot)

    def _milestones(self, snapshot):
        # Unexpected unbounded graph growth cannot buy an unbounded hang guard.
        self.units.update(
            sorted(set(snapshot.units) - self.units)[: max(0, self.unit_budget - len(self.units))]
        )
        result = set()
        for key in self.units:
            unit = snapshot.units.get(key)
            if unit is None:
                continue
            result.add((key, "state", unit.state.value))
            stages = unit.evidence.get("planning_flow", {}).get("stages", {})
            result.update(
                (key, "planning", stage)
                for stage in STAGES
                if stages.get(stage, {}).get("validated")
            )
        return result

    def check(self, snapshot: CampaignSnapshot, *, now: float) -> str:
        if now - self.started_at >= self.total_s:
            return f"terminal state within {self.total_s:g}s absolute ceiling"
        if self.timeout_s is not None:
            return ""
        milestones = self._milestones(snapshot)
        if milestones - self.seen:
            self.progress_at = now
            self.seen.update(milestones)
        if now - self.progress_at >= self.idle_s:
            return f"new durable milestone within {self.idle_s:g}s"
        return ""


def failure_details(case, snapshot, active):
    """Retain bounded evidence in pytest output before temp retention removes it."""
    workers = {}
    retained = [
        unit
        for unit in snapshot.units.values()
        if unit.claim and (not unit.claim.quiescent or unit.hold)
    ]
    for unit in retained[:16]:
        claim = unit.claim
        if claim is None or (claim.quiescent and not unit.hold):
            continue
        directory = Path(claim.run_dir)
        detail = {"bound_head": unit.bindings.get("head"), "files": {}}
        for name in ("engine.pid", "startup.json", "receipt.json", "script.log", "child.log"):
            path = directory / name
            try:
                with path.open("rb") as stream:
                    stream.seek(0, 2)
                    size = stream.tell()
                    stream.seek(max(0, size - 4096))
                    detail["files"][name] = {
                        "bytes": size,
                        "tail": stream.read(4096).decode("utf-8", errors="replace"),
                    }
            except OSError as exc:
                detail["files"][name] = {"error": str(exc)}
        process = getattr(case, "processes", {}).get(directory)
        if process is not None:
            detail["owned_pid"] = process.pid
            detail["owned_returncode"] = process.poll()
        workspace = unit.bindings.get("workspace")
        if workspace:
            try:
                result = subprocess.run(
                    ["git", "rev-parse", "HEAD"],
                    cwd=workspace,
                    capture_output=True,
                    text=True,
                    timeout=1,
                )
                detail["observed_head"] = result.stdout.strip()
                detail["head_returncode"] = result.returncode
            except (OSError, subprocess.TimeoutExpired) as exc:
                detail["head_error"] = str(exc)
        workers[unit.id] = detail
    return {
        "workers": workers,
        "omitted_worker_details": max(0, len(retained) - 16),
        "active_effects": {
            key: {"done": future.done(), "running": future.running()}
            for key, future in active.items()
        },
        "recent_events": list(snapshot.events[-16:]),
    }
