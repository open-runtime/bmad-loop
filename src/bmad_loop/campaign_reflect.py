"""Stopped display refresh: exact existing resources, no launch or configuration mutation.

Native display writes have durable outbox intent before submission. Preview only
reads identities/settings and leaves campaign and local reflection records alone.
The native folder API has no CAS; manual overrides remain visible in results.
"""

from __future__ import annotations

import re
import uuid
from pathlib import Path

from .adapters.multiplexer import MultiplexerError
from .adapters.paradev_backend import ParadevMultiplexer
from .campaign_model import CampaignError, OutboxAction
from .campaign_paradev import ParadevWorkspaceProvider
from .campaign_reflection_flow import reflect
from .campaign_store import RevisionConflict, add_event


def _historical_views(snapshot, unit, directory, provider, mux, *, preview):
    """Relabel only exact, confirmed ended attempts; retain earned receipts untouched."""
    from .campaign_action_history import action as find_action
    from .campaign_action_history import actions
    from .campaign_headless import read_json
    from .campaign_jobs import digest
    from .campaign_reflection_flow import workspace_binding
    from .campaign_titles import session_title, work_title

    binding = workspace_binding(unit)
    result, seen = [], set()
    for observed in actions(snapshot, kinds=("reasoning-launch", "reasoning-observe")):
        data = observed.receipt
        if observed.state != "confirmed" or data.get("state") != "finished":
            continue
        coordinates = data.get("effect_binding", {})
        launch_id = coordinates.get("launch_id")
        if launch_id in seen:
            continue
        seen.add(launch_id)
        job_id = observed.payload.get("job_id", "")
        record = {"job_id": job_id, "launch_id": launch_id, "state": "unavailable"}
        try:
            attempt = coordinates.get("attempt")
            if (
                not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", job_id)
                or type(attempt) is not int
                or attempt < 1
            ):
                raise CampaignError("Historical reasoning path identity is invalid")
            launch = find_action(snapshot, launch_id)
            if (
                not launch
                or launch.kind != "reasoning-launch"
                or launch.payload.get("job_id") != job_id
                or launch.payload.get("job_binding") != coordinates
                or observed.payload.get("job_binding") != coordinates
            ):
                raise CampaignError("Historical reasoning launch receipt binding differs")
            path = directory / "jobs" / job_id / str(attempt)
            if data.get("run_dir") != str(path):
                raise CampaignError("Historical reasoning directory differs from owned attempt")
            envelope = read_json(path / "request.json")
            if any(
                envelope.get(k) != coordinates.get(k)
                for k in ("launch_id", "generation", "request_digest")
            ):
                raise CampaignError("Historical reasoning envelope differs from confirmed attempt")
            request = envelope["request"]
            if request.get("id") != job_id or digest(request) != coordinates["request_digest"]:
                raise CampaignError("Historical reasoning request digest differs")
            if envelope.get("selection") != launch.payload.get("candidate"):
                raise CampaignError("Historical reasoning selection differs from owned launch")
            receipt = data.get("receipt", {})
            if (
                receipt.get("quiescent") is not True
                or receipt.get("processes_quiescent") is not True
            ):
                raise CampaignError("Historical reasoning process settlement is unconfirmed")
            if any(
                receipt.get(k) != coordinates.get(k)
                for k in ("launch_id", "generation", "request_digest")
            ):
                raise CampaignError(
                    "Historical reasoning completion differs from confirmed attempt"
                )
            view = envelope.get("presentation")
            if not view or view.get("workspace_id") != binding.workspace_id:
                continue
            if view.get("workspace") != binding.path:
                raise CampaignError("Historical reasoning presentation path changed")
            window = read_json(path / "window.json")
            if window.get("launch_id") != launch_id:
                raise CampaignError("Historical reasoning terminal launch differs")
            terminal = mux._address(window["native_id"])
            if (
                terminal["workspace"] != binding.workspace_id
                or Path(terminal["cwd"]).resolve() != Path(binding.path).resolve()
            ):
                raise CampaignError("Historical reasoning terminal workspace differs")
            owned = snapshot.units.get(request.get("unit", ""))
            title = session_title(
                role=request["role"],
                model=envelope["selection"]["model"],
                attempt=attempt,
                bridge=envelope.get("managed_binding"),
                historical=True,
                description=work_title(owned, snapshot=snapshot) if owned else "Portfolio",
                limit=150,
            )
            record.update(
                state="preview" if preview else "confirmed",
                title=title,
                terminal_id=window["native_id"],
                generation=coordinates["generation"],
                source_receipt=observed.id,
            )
            if not preview:
                record["view"] = provider.bind_terminal(
                    binding,
                    mux,
                    window["native_id"],
                    phase=request["role"],
                    attempt=attempt,
                    agent=envelope["selection"]["profile"],
                    title=title,
                    navigation_prefix=title,
                    ensure_pane=False,
                )
        except (CampaignError, MultiplexerError, OSError, ValueError, KeyError, TypeError) as exc:
            record["reason"] = f"{type(exc).__name__}: {str(exc)[:300]}"
        result.append(record)
    return result


def _eligible(snapshot):
    if snapshot.phase != "stopped":
        raise CampaignError("Display refresh requires a stopped campaign")
    if not snapshot.spec.paradev.reflect:
        raise CampaignError("Native reflection is disabled for this campaign")
    if any(unit.claim and not unit.claim.quiescent for unit in snapshot.units.values()) or any(
        row.get("claim") and not row["claim"].get("quiescent") for row in snapshot.jobs.values()
    ):
        raise CampaignError("Display refresh requires settled owned claims")


def refresh(
    store,
    campaign_id,
    *,
    unit_id="",
    preview=False,
    expected_revision=None,
    provider_factory=ParadevWorkspaceProvider,
    mux_factory=ParadevMultiplexer,
):
    before = store.load(campaign_id)
    _eligible(before)
    if expected_revision is not None and before.revision != expected_revision:
        raise RevisionConflict(f"campaign revision {before.revision}, expected {expected_revision}")
    if unit_id and unit_id not in before.units:
        raise CampaignError("Unknown reflection unit")

    def run(snapshot):
        _eligible(snapshot)
        if snapshot.revision != before.revision:
            raise RevisionConflict("Campaign changed before display refresh")
        provider = provider_factory(
            project_id=snapshot.spec.paradev.project_id,
            allowed_root=Path(snapshot.spec.paradev.worktree_root),
            repository=Path(snapshot.root),
            state_dir=store.directory(campaign_id) / "reflection",
        )
        mux = mux_factory(state_dir=store.directory(campaign_id) / "native-transport")
        results = []
        keys = [unit_id] if unit_id else sorted(snapshot.units)
        for key in keys:
            unit = snapshot.units[key]
            if not unit.bindings.get("native"):
                results.append({"unit": key, "state": "unbound"})
                continue
            pending = [
                a
                for a in snapshot.outbox
                if a.unit == key
                and a.kind == "reflection"
                and a.state in {"prepared", "sent_unknown"}
            ]
            if len(pending) > 1 or (pending and not pending[0].payload.get("display_only")):
                raise CampaignError(
                    "Existing runtime reflection must be reconciled before stopped refresh"
                )
            action = pending[0] if pending else None
            if action and action.payload.get("config_revision") != snapshot.config_revision:
                raise CampaignError("Pending display reflection belongs to another configuration")
            if not preview:
                if action is None:
                    action = OutboxAction(
                        "reflection-" + uuid.uuid4().hex,
                        "reflection",
                        key,
                        {"display_only": True, "config_revision": snapshot.config_revision},
                        snapshot.generation,
                    )
                    snapshot.outbox.append(action)
                action.state = "sent_unknown"
                snapshot = store.save(
                    snapshot, snapshot.revision, expected_generation=snapshot.generation
                )
                unit = snapshot.units[key]
                action = next(a for a in snapshot.outbox if a.id == action.id)
            try:
                result = reflect(
                    snapshot,
                    unit,
                    unit.claim,
                    provider=provider,
                    mux=mux,
                    preview=preview,
                    display_only=True,
                    effect_id=action.id if action else "",
                )
                result["historical_sessions"] = _historical_views(
                    snapshot, unit, store.directory(campaign_id), provider, mux, preview=preview
                )
            except Exception as exc:
                if preview:
                    raise
                assert action is not None
                action.error = f"{type(exc).__name__}: {str(exc)[:500]}"
                snapshot = store.save(
                    snapshot, snapshot.revision, expected_generation=snapshot.generation
                )
                results.append({"unit": key, "state": "unknown", "error": action.error})
                break
            results.append({"unit": key, "state": "preview" if preview else "confirmed", **result})
            if not preview:
                assert action is not None
                action.state, action.receipt, action.error = "confirmed", result, ""
                previous = unit.bindings.get("reflection")
                if previous:
                    unit.bindings.setdefault("reflection_history", []).append(previous)
                    unit.bindings["reflection_history"] = unit.bindings["reflection_history"][-20:]
                unit.bindings["reflection"] = result
                add_event(snapshot, "campaign-display-refreshed", unit=key, action_id=action.id)
                snapshot = store.save(
                    snapshot, snapshot.revision, expected_generation=snapshot.generation
                )
        if preview and store.load(campaign_id).revision != snapshot.revision:
            raise RevisionConflict("Campaign changed during reflection preview")
        return {
            "ok": not any(r["state"] == "unknown" for r in results),
            "campaign_id": campaign_id,
            "revision": snapshot.revision,
            "config_revision": snapshot.config_revision,
            "preview": preview,
            "workspaces": results,
        }

    if preview:
        return run(before)
    with store.coordinator(before.common_dir, before.spec.target, campaign_id=campaign_id):
        return run(store.load(campaign_id))
