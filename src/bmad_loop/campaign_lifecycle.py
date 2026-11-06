"""Retain campaign history and retire only proven-owned, ended native views.

Worker adapters keep their unconditional transport teardown. This bridge never
closes a PTY, sends input, deletes a worktree, archives a workspace, or removes
Git data. A residual shell remains execution until teardown proves its absence.
Native history and all commits survive view retention. Uncertain view closes are
observed on retry rather than repeated.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import shutil
import subprocess
import time
from pathlib import Path
from typing import Callable, Literal

from .adapters.paradev_runtime import private_json, read_private_json
from .campaign_model import CampaignSnapshot, CampaignUnit, UnitState
from .campaign_paradev import ReflectionError
from .campaign_reflection_flow import (
    discover_peer_binding,
    observe_communication,
    stage_binding,
    workspace_binding,
)
from .platform_util import file_lock, is_link_like


class LifecycleError(ReflectionError):
    """Native lifecycle proof is unavailable, changed, protected or uncertain."""


def _digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


@dataclasses.dataclass(frozen=True)
class LifecyclePolicy:
    scope: Literal["ephemeral", "protected"] = "ephemeral"
    retention: int = 20
    close_owned_views: bool = True
    require_controller: bool = True

    def __post_init__(self):
        if self.scope not in ("ephemeral", "protected") or self.retention < 0:
            raise ValueError("Explicit lifecycle scope and nonnegative retention are required")


class ControllerProtection:
    """Read only the installed controller's supported protection exit contract."""

    def __init__(self, executable=None, *, runner=None, timeout=10):
        self.executable = executable or shutil.which("superset-control")
        self.runner, self.timeout = runner or subprocess.run, timeout

    def available(self) -> bool:
        return bool(self.executable)

    def observe(self, workspace_id: str) -> dict:
        if not self.executable:
            return {"complete": False, "protected": True, "detail": "controller unavailable"}
        try:
            result = self.runner(
                [self.executable, "protection", "--workspace", workspace_id],
                capture_output=True,
                timeout=self.timeout,
                check=False,
            )
            if len(result.stdout) + len(result.stderr) > 512 * 1024:
                raise LifecycleError("Controller protection exceeded observation bound")
            return {
                "complete": result.returncode in (0, 10),
                "protected": result.returncode != 0,
                "revision": hashlib.sha256(result.stdout + result.stderr).hexdigest(),
                "detail": "clear" if result.returncode == 0 else "protected or unknown",
            }
        except (OSError, subprocess.SubprocessError, LifecycleError) as exc:
            return {"complete": False, "protected": True, "detail": type(exc).__name__}


def _views(layout: dict) -> list[dict]:
    if not isinstance(layout, dict) or not isinstance(layout.get("tabs"), list):
        raise LifecycleError("Native layout is unavailable")
    rows = []
    for area, tabs in (
        ("main", layout["tabs"]),
        ("sidebar", (layout.get("sidebar") or {}).get("tabs", [])),
    ):
        if not isinstance(tabs, list):
            raise LifecycleError("Native sidebar layout is unavailable")
        for tab_index, tab in enumerate(tabs):
            if not isinstance(tab, dict) or not isinstance(tab.get("panes", []), list):
                raise LifecycleError("Native tab layout is malformed")
            for pane_index, pane in enumerate(tab.get("panes", [])):
                if not isinstance(pane, dict):
                    raise LifecycleError("Native pane layout is malformed")
                rows.append(
                    {
                        "area": area,
                        "tab": tab.get("id"),
                        "tab_index": tab_index,
                        "pane_index": pane_index,
                        "pane": pane,
                    }
                )
    return rows


def _pane_id(view: dict) -> str | None:
    return view["pane"].get("paneId") or view["pane"].get("id")


class CampaignLifecycle:
    """Persist each unit's real stages independently of its current claim."""

    def __init__(
        self, state_dir: Path, *, mux, provider, controller=None, clock: Callable = time.time
    ):
        self.root = state_dir.resolve()
        self.mux, self.provider = mux, provider
        self.controller, self.clock = controller or ControllerProtection(), clock

    def preflight(self, policy: LifecyclePolicy) -> dict:
        available = self.controller.available() if policy.require_controller else True
        return {
            "qualified": available,
            "scope": policy.scope,
            "controller_required": policy.require_controller,
            "controller_available": self.controller.available(),
            "native_state_root": str(self.mux.root.resolve()),
            "pty_retirement": "existing owned adapter teardown only",
            "workspace_retirement": "retain workspace, branch, worktree and history",
            "automatic_operator_retirement": False,
            "detail": "" if available else "Required supported controller route unavailable",
        }

    def _unit_path(self, snapshot, unit):
        return self.root / "units" / (_digest([snapshot.id, unit.id]) + ".json")

    def _layout(self, binding):
        return _views(
            self.provider.host.cli(["panes", "list", "--workspace", binding.workspace_id])
        )

    def _created_views(self) -> set[tuple[str, str]]:
        path = self.mux.root / "journal.jsonl"
        if is_link_like(path) or not path.is_file() or path.stat().st_size > 8 * 1024 * 1024:
            raise LifecycleError("Native pane creation journal is unavailable or exceeds bound")
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
        return {
            (row["id"], row["pane"])
            for row in rows
            if row.get("event") == "pane-attach-requested" and row.get("id") and row.get("pane")
        }

    def observe(
        self,
        snapshot: CampaignSnapshot,
        unit: CampaignUnit,
        *,
        run_directories=(),
        observer=None,
        projection=None,
    ) -> dict:
        binding = workspace_binding(unit)
        if unit.claim is None:
            return {"state": "pending", "detail": "No stage claim", "windows": []}
        stage = projection or stage_binding(
            snapshot, unit, unit.claim, mux=self.mux, run_directories=run_directories
        )
        if (stage.get("campaign_id"), stage.get("unit_id"), stage.get("launch_id")) != (
            snapshot.id,
            unit.id,
            unit.claim.launch_id,
        ):
            raise LifecycleError("Lifecycle observation names a different stage claim")
        if observer is not None and snapshot.spec.communication.mode != "off":
            for window in stage.get("windows", []):
                try:
                    peer = discover_peer_binding(
                        snapshot, unit, unit.claim, window, observer=observer
                    )
                    window["hcom_binding"] = dataclasses.asdict(peer) if peer is not None else None
                except Exception as exc:
                    window["hcom_binding"] = None
                    window["communication_detail"] = (
                        "Scoped hcom observation unavailable: " + type(exc).__name__
                    )
        communication = (
            observe_communication(snapshot, observer=observer)
            if observer is not None
            else {
                "mode": snapshot.spec.communication.mode,
                "available": snapshot.spec.communication.mode == "off",
                "detail": (
                    ""
                    if snapshot.spec.communication.mode == "off"
                    else "hcom observer not configured"
                ),
            }
        )
        result = {
            "schema_version": 1,
            "stage": stage,
            "communication": communication,
            "workspace_retained": True,
        }
        if stage["state"] != "observed":
            return result
        path = self._unit_path(snapshot, unit)
        with file_lock(self.root / "observation.lock"):
            old = (
                read_private_json(path)
                if path.exists()
                else {
                    "schema_version": 1,
                    "campaign_id": snapshot.id,
                    "unit_id": unit.id,
                    "binding": dataclasses.asdict(binding),
                    "native_state_root": str(self.mux.root.resolve()),
                    "windows": {},
                }
            )
            if old["binding"] != dataclasses.asdict(binding) or old["native_state_root"] != str(
                self.mux.root.resolve()
            ):
                raise LifecycleError("Lifecycle workspace/transport identity changed")
            layout, created = self._layout(binding), self._created_views()
            for window in stage["windows"]:
                terminal = window["terminal_id"]
                prior = old["windows"].get(terminal)
                if prior and (prior["run_dir"], prior["task_id"], prior["attempt"]) != (
                    window["run_dir"],
                    window["task_id"],
                    unit.claim.attempt,
                ):
                    raise LifecycleError("Lifecycle terminal was reused for another stage")
                matching = [view for view in layout if view["pane"].get("terminalId") == terminal]
                view = matching[0] if len(matching) == 1 else None
                owned = bool(
                    view
                    and (terminal, _pane_id(view)) in created
                    and _pane_id(view) == window.get("pane_id")
                )
                record = {
                    **window,
                    "attempt": unit.claim.attempt,
                    "launch_id": unit.claim.launch_id,
                    "generation": unit.claim.generation,
                    "observed_at": self.clock(),
                    "view_owned": prior.get("view_owned", owned) if prior else owned,
                    "view": prior.get("view") if prior else view,
                    "first_observed_at": prior.get("first_observed_at") if prior else self.clock(),
                }
                if (
                    prior
                    and prior.get("native")
                    and window.get("native")
                    and prior["native"].get("agentSessionId")
                    != window["native"].get("agentSessionId")
                ):
                    raise LifecycleError("Lifecycle native conversation identity changed")
                old["windows"][terminal] = record
            old["latest_completed_phase"] = (
                unit.claim.role if unit.claim.quiescent else old.get("latest_completed_phase", "")
            )
            if unit.claim.quiescent:
                old["latest_stage"] = {
                    "phase": unit.claim.role,
                    "attempt": unit.claim.attempt,
                    "launch_id": unit.claim.launch_id,
                    "outcome": unit.legacy_outcome,
                    "unit_state": unit.state.value,
                }
                if unit.legacy_outcome in (
                    "failed",
                    "interrupted",
                    "escalated",
                    "recovery_required",
                ):
                    old["latest_failed_phase"] = unit.claim.role
            old["last_observed_at"] = self.clock()
            private_json(path, old)
        result["history"] = old
        return result

    def _ready(self, snapshot, unit, history, policy):
        if policy.scope != "ephemeral":
            raise LifecycleError("Protected operator sessions are retained")
        if (
            unit.state != UnitState.DELIVERED
            or unit.hold is not None
            or snapshot.phase not in ("active", "complete", "completed")
        ):
            raise LifecycleError("Only delivered, unheld campaign work can retire views")
        if unit.claim is not None and not unit.claim.quiescent:
            raise LifecycleError("Worker execution is live or uncertain")
        if history["native_state_root"] != str(self.mux.root.resolve()):
            raise LifecycleError("Lifecycle transport state root changed")
        binding = workspace_binding(unit)
        if history["binding"] != dataclasses.asdict(binding):
            raise LifecycleError("Lifecycle workspace identity changed")
        rows = [
            row
            for row in self.provider._rows("workspace.list")
            if row.get("id") == binding.workspace_id
        ]
        if len(rows) != 1:
            raise LifecycleError("Lifecycle workspace is missing or ambiguous")
        observed = self.provider._verify(
            rows[0],
            {
                "unit_id": binding.unit_id,
                "action_id": binding.action_id,
                "branch": binding.branch,
                "group": binding.group,
            },
        )
        if (observed.path, observed.git_dir, observed.common_dir) != (
            binding.path,
            binding.git_dir,
            binding.common_dir,
        ):
            raise LifecycleError("Lifecycle workspace Git identity changed")
        if policy.require_controller:
            protection = self.controller.observe(binding.workspace_id)
            if not protection.get("complete") or protection.get("protected"):
                raise LifecycleError(
                    "Controller protects workspace or cannot prove protection clear"
                )
        return binding

    def retire(
        self,
        snapshot: CampaignSnapshot,
        unit: CampaignUnit,
        action_id: str,
        *,
        policy: LifecyclePolicy,
    ) -> dict:
        path = self.root / "actions" / (_digest([snapshot.id, unit.id, action_id]) + ".json")
        with file_lock(self.root / "retirement.lock"):
            history = read_private_json(self._unit_path(snapshot, unit))
            binding = self._ready(snapshot, unit, history, policy)
            records = sorted(
                history["windows"].values(), key=lambda row: row["first_observed_at"], reverse=True
            )
            desired = records[policy.retention :] if policy.close_owned_views else []
            receipt = (
                read_private_json(path)
                if path.exists()
                else {
                    "schema_version": 1,
                    "campaign_id": snapshot.id,
                    "unit_id": unit.id,
                    "action_id": action_id,
                    "policy": dataclasses.asdict(policy),
                    "targets": {},
                    "workspace_retained": True,
                    "worktree_retained": True,
                    "git_data_retained": True,
                    "planned": [window["terminal_id"] for window in desired],
                }
            )
            if receipt["policy"] != dataclasses.asdict(policy) or receipt["planned"] != [
                window["terminal_id"] for window in desired
            ]:
                raise LifecycleError("Lifecycle action policy or exact targets changed")
            for window in desired:
                terminal = window["terminal_id"]
                target = receipt["targets"].get(terminal)
                views = self._layout(binding)
                if target and target["state"] in ("sent_unknown", "confirmed"):
                    remains = [view for view in views if _pane_id(view) == target["pane_id"]]
                    target["state"] = "sent_unknown" if remains else "confirmed"
                    continue
                self._ready(snapshot, unit, history, policy)
                daemon = self.mux._daemon()
                registry = [
                    row for row in self.mux.status(native=False)["windows"] if row["id"] == terminal
                ]
                if len(registry) != 1 or registry[0].get("workspace") != binding.workspace_id:
                    raise LifecycleError("Historical terminal registry identity changed")
                launch = read_private_json(self.mux.root / "launches" / terminal / "launch.json")
                if (
                    launch.get("workspaceId") != binding.workspace_id
                    or Path(launch.get("cwd", "")).resolve() != Path(binding.path).resolve()
                    or (launch.get("env") or {}).get("BMAD_LOOP_RUN_DIR") != window["run_dir"]
                    or (launch.get("env") or {}).get("BMAD_LOOP_TASK_ID") != window["task_id"]
                ):
                    raise LifecycleError("Historical terminal launch identity changed")
                if registry[0]["state"] not in ("gone", "disposed", "void") or terminal in daemon:
                    raise LifecycleError(
                        "Native worker or residual PTY is live/uncertain; teardown must finish"
                    )
                if not window["view_owned"] or window.get("view") is None:
                    raise LifecycleError("Terminal view lacks exact BMAD creation ownership")
                current = [view for view in views if _pane_id(view) == _pane_id(window["view"])]
                if current and current != [window["view"]]:
                    raise LifecycleError("Operator changed terminal view or layout")
                log = Path(window["log_path"])
                if is_link_like(log) or not log.is_file():
                    raise LifecycleError("Retained terminal output is unavailable")
                with log.open("rb") as stream:
                    stream.read(1)
                native = window.get("native") or {}
                if not native.get("agentSessionId"):
                    raise LifecycleError("Exact native conversation attribution is unconfirmed")
                observed = self.mux._rpc(
                    "terminalAgents.get",
                    {"workspaceId": binding.workspace_id, "terminalId": terminal},
                )
                if (
                    not isinstance(observed, dict)
                    or observed.get("agentSessionId") != native["agentSessionId"]
                    or not observed.get("endedAt")
                ):
                    raise LifecycleError("Exact native conversation has not been confirmed ended")
                target = {
                    "pane_id": _pane_id(window["view"]),
                    "terminal_id": terminal,
                    "state": "sent_unknown" if current else "confirmed",
                    "log_path": str(log),
                    "native_session_id": native.get("agentSessionId"),
                }
                receipt["targets"][terminal] = target
                private_json(path, receipt)
                if current:
                    try:
                        self.provider.host.cli(
                            [
                                "panes",
                                "close",
                                "--workspace",
                                binding.workspace_id,
                                "--pane",
                                target["pane_id"],
                                "--keep-terminal",
                            ]
                        )
                    except Exception as exc:
                        target["detail"] = "View close outcome unknown: " + type(exc).__name__
                    if not any(
                        _pane_id(view) == target["pane_id"] for view in self._layout(binding)
                    ):
                        target["state"] = "confirmed"
                    private_json(path, receipt)
            receipt["state"] = (
                "confirmed"
                if all(row["state"] == "confirmed" for row in receipt["targets"].values())
                else "unknown"
            )
            receipt["retained_stages"] = len(records) - len(desired)
            receipt["latest_completed_phase"] = history.get("latest_completed_phase", "")
            private_json(path, receipt)
            return receipt
