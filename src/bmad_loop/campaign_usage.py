"""Monotonic campaign accounting across fresh engines and retained continuations.

Actual legacy RunState counters are sampled by original run identity. A retained
finalization or compensating recovery observes the same stream and cannot charge
copied sessions again. Provider changes never create a new budget envelope.
Token telemetry is a lower bound when unavailable; it is never dollar telemetry.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from . import runs
from .campaign_model import CampaignError, CampaignSnapshot, CampaignUnit
from .campaign_store import add_event
from .model import RunState, TokenUsage
from .platform_util import atomic_write_text, is_link_like
from .runs import load_state

_TOKENS = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_creation_tokens")


def _counts(raw: Mapping[str, Any] | None) -> dict[str, int]:
    output = {}
    for key in _TOKENS:
        value = (raw or {}).get(key, 0)
        if type(value) is not int or value < 0:
            raise CampaignError("campaign token telemetry contains an invalid counter")
        output[key] = value
    return output


def legacy_receipt(state: RunState, *, origin_run_id: str | None = None) -> dict[str, Any]:
    origin = origin_run_id or state.replay_of or state.run_id
    tasks, sessions = {}, {}
    complete = True
    for key, task in state.tasks.items():
        counters = (task.charged_attempts, task.charged_review_cycles)
        if any(value < 0 for value in counters):
            raise CampaignError("legacy charged attempt counters are invalid")
        tasks[key] = {
            "dev_attempts": counters[0],
            "review_cycles": counters[1],
            "tokens": task.tokens.to_dict(),
        }
        for index, session in enumerate(task.sessions):
            # Logical task IDs can recur on a stopped-native continuation. Its
            # artifact identity and native session ID distinguish actual turns.
            identity = "\0".join(
                (key, session.artifact_task_id, session.session_id or "", str(index))
            )
            sessions[identity] = {
                "role": session.role,
                "native_id": session.session_id,
                "artifact_id": session.artifact_task_id,
                "models": list(session.models),
                "tokens": session.usage.to_dict() if session.usage else _counts(None),
                "known": session.usage is not None,
                "by_model": {
                    model: usage.to_dict() for model, usage in session.tokens_by_model.items()
                },
            }
            complete = complete and session.usage is not None
    return {
        "schema_version": 1,
        "kind": "legacy",
        "stream_id": "legacy:" + origin,
        "origin_run_id": origin,
        "run_id": state.run_id,
        "tasks": tasks,
        "sessions": sessions,
        "usage_complete": complete,
    }


def save_stage_usage(
    request: Mapping[str, Any], directory: Path, usage: TokenUsage | None, result: Any
) -> None:
    """Write native telemetry before validating the model's completion artifact."""
    row = {
        "schema_version": 1,
        "kind": "stage",
        "stream_id": "stage:" + str(request["launch_id"]),
        "role": request["role"],
        "session_id": result.session_id,
        "tokens": usage.to_dict() if usage else _counts(None),
        "models": list(usage.models or ()) if usage else [],
        "by_model": (
            {key: value.to_dict() for key, value in (usage.by_model or {}).items()} if usage else {}
        ),
        "usage_complete": usage is not None,
        "session_observed": bool(result.session_id) or usage is not None,
    }
    atomic_write_text(
        directory / "usage.json", json.dumps(row, sort_keys=True), follow_symlinks=False
    )


def worker_accounting(
    request: Mapping[str, Any], result: Mapping[str, Any]
) -> dict[str, Any] | None:
    """Read actual native state after every child outcome, including failure."""
    kind = request["kind"]
    if kind not in {"execution", "recovery", "stage"}:
        return None
    try:
        if kind == "stage":
            path = Path(request["execution"]).parent / "usage.json"
            if not path.exists():
                return {
                    "schema_version": 1,
                    "kind": "stage",
                    "stream_id": "stage:" + str(request["launch_id"]),
                    "role": request["role"],
                    "tokens": _counts(None),
                    "models": [],
                    "usage_complete": False,
                    "session_observed": False,
                    "unavailable": "No terminal coding-session token receipt",
                }
            if is_link_like(path) or path.stat().st_size > 1024 * 1024:
                raise CampaignError("stage usage receipt is redirected or unbounded")
            row = json.loads(path.read_text(encoding="utf-8"))
            if (
                row.get("stream_id") != "stage:" + str(request["launch_id"])
                or row.get("role") != request["role"]
            ):
                raise CampaignError("stage usage receipt belongs to another launch")
            return row
        workspace = Path(request["workspace"])
        run_id = result.get("run_id") or request["run_id"]
        directory = (
            Path(result["run_dir"])
            if result.get("run_dir")
            else runs.run_dir_for(workspace, run_id)
        )
        if kind == "recovery" and not result.get("run_dir"):
            original = Path(request["recovery_request"]).parent / "request.json"
            launch = json.loads(original.read_text(encoding="utf-8"))
            run_id = launch["run_id"]
            directory = runs.run_dir_for(workspace, run_id)
        state = load_state(directory)
        if state.run_id != run_id:
            raise CampaignError("legacy usage run identity differs from its native state")
        origin, seen = state, set()
        while origin.replay_of:
            if origin.run_id in seen or len(seen) >= 8:
                raise CampaignError("legacy usage replay lineage is cyclic or unbounded")
            seen.add(origin.run_id)
            origin = load_state(runs.run_dir_for(workspace, origin.replay_of))
        return legacy_receipt(state, origin_run_id=origin.run_id)
    except (OSError, ValueError, KeyError, TypeError, CampaignError) as exc:
        if kind == "execution":
            directory = runs.run_dir_for(Path(request["workspace"]), request["run_id"])
            if not directory.exists() and not is_link_like(directory):
                empty = RunState(request["run_id"], request["workspace"], "")
                return {
                    **legacy_receipt(empty),
                    "unavailable": "Child exited before its native run was created",
                }
        return {
            "schema_version": 1,
            "kind": "unknown",
            "stream_id": "worker:" + str(request["launch_id"]),
            "origin_run_id": request.get("run_id"),
            "legacy_counters_unknown": kind in {"execution", "recovery"},
            "usage_complete": False,
            "unavailable": type(exc).__name__ + ": native usage/counter receipt unavailable",
        }


def _maximum(first: Mapping[str, Any], second: Mapping[str, Any]) -> dict[str, int]:
    return {key: max(_counts(first)[key], _counts(second)[key]) for key in _TOKENS}


def _sum(rows) -> dict[str, int]:
    result = _counts(None)
    for row in rows:
        for key, value in _counts(row).items():
            result[key] += value
    return result


def apply(
    snapshot: CampaignSnapshot, unit: CampaignUnit, receipt: Mapping[str, Any], *, role: str
) -> None:
    sample = receipt.get("accounting")
    if sample is None:
        return
    if (
        sample.get("schema_version") != 1
        or sample.get("kind") not in {"legacy", "stage", "unknown"}
        or not sample.get("stream_id")
    ):
        raise CampaignError("unsupported native campaign accounting receipt")
    ledger = snapshot.bindings.setdefault(
        "usage_ledger", {"schema_version": 1, "streams": {}, "publication_faults": {}}
    )
    key = sample["stream_id"]
    previous = ledger["streams"].get(key)
    if previous and previous["unit"] != unit.id:
        raise CampaignError("native usage stream cannot move between campaign units")
    row: dict[str, Any] = dict(
        previous
        or {
            "unit": unit.id,
            "kind": sample["kind"],
            "tasks": {},
            "sessions": {},
            "tokens": _counts(None),
            "models": {},
            "usage_complete": False,
        }
    )
    row["usage_complete"] = sample.get("usage_complete") is True
    row["observed_runs"] = sorted(
        set(row.get("observed_runs", [])) | ({sample["run_id"]} if sample.get("run_id") else set())
    )
    if sample["kind"] == "legacy":
        for task_id, incoming in sample["tasks"].items():
            saved = row["tasks"].get(task_id, {})
            values = {}
            for counter in ("dev_attempts", "review_cycles"):
                value = incoming[counter]
                if type(value) is not int or value < 0:
                    raise CampaignError("native campaign attempt counter is invalid")
                values[counter] = max(saved.get(counter, 0), value)
            values["tokens"] = _maximum(saved.get("tokens", {}), incoming["tokens"])
            row["tasks"][task_id] = values
        for session_id, session in sample["sessions"].items():
            saved = row["sessions"].get(session_id, {})
            row["sessions"][session_id] = {
                **session,
                "tokens": _maximum(saved.get("tokens", {}), session["tokens"]),
            }
        row["tokens"] = _sum(task["tokens"] for task in row["tasks"].values())
    elif sample["kind"] == "stage":
        row["tokens"] = _maximum(row["tokens"], sample["tokens"])
        row["models"] = {
            model: _counts(value) for model, value in sample.get("by_model", {}).items()
        }
    else:
        row["legacy_counters_unknown"] = sample.get("legacy_counters_unknown", False)
        row["origin_run_id"] = sample.get("origin_run_id")
    row["unavailable"] = sample.get(
        "unavailable", "" if row["usage_complete"] else "Token telemetry is a lower bound"
    )
    ledger["streams"][key] = row
    if sample["kind"] == "legacy":
        for unresolved in ledger["streams"].values():
            if (
                unresolved["unit"] == unit.id
                and unresolved.get("kind") == "unknown"
                and unresolved.get("origin_run_id") == sample.get("origin_run_id")
            ):
                unresolved["legacy_counters_unknown"] = False
                unresolved["resolved_by"] = key
    streams = [stream for stream in ledger["streams"].values() if stream["unit"] == unit.id]
    unit.evidence["dev_attempts_spent"] = sum(
        task["dev_attempts"] for stream in streams for task in stream["tasks"].values()
    )
    unit.evidence["review_cycles_spent"] = sum(
        task["review_cycles"] for stream in streams for task in stream["tasks"].values()
    )
    unit.evidence["usage"] = {
        "tokens": _sum(stream["tokens"] for stream in streams),
        "legacy_tokens": _sum(stream["tokens"] for stream in streams if stream["kind"] == "legacy"),
        "complete": all(stream["usage_complete"] for stream in streams),
        "legacy_counters_unknown": any(stream.get("legacy_counters_unknown") for stream in streams),
    }
    if role == "publication_review" and sample.get("session_observed", True):
        approved = (
            receipt.get("outcome") == "stage_complete"
            and receipt.get("validated", {}).get("verdict") == "approved"
        )
        if not approved:
            faults = ledger["publication_faults"].setdefault(unit.id, [])
            if key not in faults:
                faults.append(key)
        unit.review_attempts = len(ledger["publication_faults"].get(unit.id, []))
    ledger["tokens"] = _sum(stream["tokens"] for stream in ledger["streams"].values())
    ledger["usage_complete"] = all(
        stream["usage_complete"] for stream in ledger["streams"].values()
    )
    limits = snapshot.config_snapshot.get("policy", {}).get("limits", {})
    threshold = limits.get("max_tokens_per_story")
    if threshold and not unit.evidence.get("campaign_token_budget_warned"):
        tokens = TokenUsage.from_dict(unit.evidence["usage"]["legacy_tokens"])
        if tokens.weighted_total(limits.get("cache_read_weight", 0.1)) > threshold:
            unit.evidence["campaign_token_budget_warned"] = True
            add_event(
                snapshot,
                "story-token-budget-warning",
                unit=unit.id,
                mode="advisory",
                limit=threshold,
                weighted_tokens=tokens.weighted_total(limits.get("cache_read_weight", 0.1)),
            )
    add_event(
        snapshot,
        "native-usage-observed",
        unit=unit.id,
        stream=key,
        stream_kind=sample["kind"],
        complete=row["usage_complete"],
        dev_attempts=unit.evidence["dev_attempts_spent"],
        review_cycles=unit.evidence["review_cycles_spent"],
    )


def remaining(snapshot: CampaignSnapshot, unit: CampaignUnit) -> tuple[int, int]:
    limits = snapshot.config_snapshot.get("policy", {}).get("limits", {})
    return (
        int(limits.get("max_dev_attempts", 3)) - unit.evidence.get("dev_attempts_spent", 0),
        int(limits.get("max_review_cycles", 3)) - unit.evidence.get("review_cycles_spent", 0),
    )


def admission_reason(
    snapshot: CampaignSnapshot, unit: CampaignUnit, payload: Mapping[str, Any]
) -> str:
    if payload.get("kind") == "execution":
        if unit.evidence.get("usage", {}).get("legacy_counters_unknown"):
            return "native lifetime attempt accounting is unavailable; reconcile original run first"
        if remaining(snapshot, unit)[0] <= 0:
            return "campaign lifetime development attempt budget exhausted"
    if payload.get("role") == "publication_review":
        limit = int(
            snapshot.config_snapshot.get("policy", {}).get("limits", {}).get("max_review_cycles", 3)
        )
        if unit.review_attempts >= limit:
            return "campaign lifetime publication review fault budget exhausted"
    return ""
