"""Bounded operator projections and attachment to already-owned sessions.

This observer never starts workers or interprets terminal activity as delivery.
Private prompt/evidence payloads are deliberately absent from public status.
"""

from __future__ import annotations

import dataclasses
import subprocess
from pathlib import Path

from . import runs
from .campaign_model import CampaignError
from .platform_util import is_link_like
from .sanitize import scrub_text


def sessions(unit, *, role: str = "") -> list[dict]:
    """Return retained, role-addressable native observations without private hooks."""
    projections = [
        *unit.bindings.get("reflection_history", []),
        unit.bindings.get("reflection", {}),
    ]
    rows = {}
    for projection in projections:
        stages = [projection.get("stage", {}), projection.get("lifecycle", {}).get("stage", {})]
        for stage in stages:
            for window in stage.get("windows", []):
                phase = window.get("phase", "unknown")
                if (
                    role
                    and phase != role
                    and not (role == "execution" and phase in {"dev", "review"})
                ):
                    continue
                row = {
                    key: window.get(key)
                    for key in (
                        "phase",
                        "terminal_id",
                        "session_name",
                        "task_id",
                        "run_dir",
                        "state",
                        "pane_id",
                        "log_path",
                        "attribution",
                        "detail",
                    )
                }
                native = window.get("native") or {}
                row["native"] = {
                    key: native.get(key)
                    for key in (
                        "agentSessionId",
                        "definitionId",
                        "sessionId",
                        "state",
                        "detail",
                    )
                    if key in native
                }
                hook = window.get("hook") or {}
                row["cli_session_id"] = hook.get("cli_session_id")
                row["launch_id"] = stage.get("launch_id")
                row["attempt"] = stage.get("attempt")
                rows[(row["terminal_id"], row["task_id"])] = row
    return list(rows.values())


def diagnostics(unit) -> dict:
    """Expose nested failure reasons and identities, never arbitrary private fields."""
    output: dict[str, object] = {"sessions": sessions(unit)}
    for name in ("reflection", "lifecycle"):
        value = unit.bindings.get(name) or {}
        projected = {}
        for key in ("state", "detail", "reason", "workspace_retained", "task_id"):
            if key in value:
                projected[key] = (
                    scrub_text(value[key]) if isinstance(value[key], str) else value[key]
                )
        for key in ("stage", "lifecycle", "workspace", "task"):
            nested = value.get(key)
            if isinstance(nested, dict):
                projected[key] = {
                    field: (
                        scrub_text(nested[field])
                        if isinstance(nested[field], str)
                        else nested[field]
                    )
                    for field in (
                        "state",
                        "detail",
                        "reason",
                        "attribution",
                        "task_id",
                        "folder",
                        "folder_selection",
                    )
                    if field in nested
                }
        output[name] = projected
    return output


def historical_sessions(store, snapshot, events, *, role="") -> tuple[list[dict], list[dict]]:
    """Follow a bounded event page into retained projections after hot-state eviction."""
    from .campaign_reflection_flow import ReflectionError, _read_document

    root = (store.directory(snapshot.id) / "reflection-history").resolve()
    output, gaps = [], []
    for event in events:
        raw = event.get("history_path")
        unit = snapshot.units.get(event.get("unit", ""))
        if not raw or unit is None:
            continue
        path = Path(raw)
        try:
            if not path.is_absolute() or not path.resolve().is_relative_to(root):
                raise ReflectionError("Retained projection escapes the campaign history root")
            projection = _read_document(path)
            projected_unit = dataclasses.replace(unit, bindings={"reflection": projection})
            output.extend(
                {"unit": unit.id, "history_path": str(path), **row}
                for row in sessions(projected_unit, role=role)
            )
        except (OSError, ValueError, ReflectionError) as exc:
            gaps.append({"unit": unit.id, "history_path": str(path), "detail": str(exc)})
    return output, gaps


def attach(runtime, campaign_id: str, unit_id: str, role: str = "execution") -> int:
    """Attach the requested existing role or expose its completed file history."""
    snapshot = runtime.store.load(campaign_id)
    unit = snapshot.units[unit_id]
    rows = sessions(unit, role=role)
    claim = unit.claim
    live = [
        row
        for row in rows
        if row.get("state") in {"active", "launching"}
        and claim
        and row.get("launch_id") == claim.launch_id
    ]
    if snapshot.spec.paradev.reflect and claim and not claim.quiescent:
        if len(live) != 1:
            raise CampaignError("requested role has no unique live bound terminal")
        mux = runtime._native_mux(snapshot)
        return subprocess.call(mux.attach_target_argv(live[0]["terminal_id"]))
    if claim and not claim.quiescent and claim.run_id:
        if role != claim.role:
            raise CampaignError("requested role differs from the live worker role")
        return subprocess.call(runs.attach_argv(claim.run_id))
    if rows:
        latest = rows[-1]
        raw_log, raw_run = latest.get("log_path"), latest.get("run_dir")
        if not raw_log or not raw_run:
            raise CampaignError("retained session has no bound history file")
        log, run = Path(raw_log), Path(raw_run)
        if (
            not log.is_absolute()
            or not run.is_absolute()
            or is_link_like(log)
            or not log.resolve().is_relative_to((run / "logs").resolve())
            or not log.is_file()
        ):
            raise CampaignError("retained session history is missing or redirected")
        if snapshot.spec.paradev.reflect:
            from .campaign_reflection_flow import workspace_binding

            runtime._lifecycle_bridge(snapshot).provider.history_view(workspace_binding(unit), log)
        else:
            with log.open("rb") as stream:
                stream.seek(max(0, log.stat().st_size - 16384))
                print(stream.read(16384).decode("utf-8", errors="replace"))
        return 0
    raise CampaignError("requested role has no live session or retained history")
