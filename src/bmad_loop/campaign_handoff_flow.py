"""Fenced cross-harness continuation of an existing campaign unit.

This collaborator never changes acceptance or transfers a native session ID.
Old writers drain before immutable capture; bounded no-tools jobs reconstruct
context before the existing scheduler may acquire a replacement writer claim.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
from pathlib import Path

from . import verify
from .campaign_headless import capability_for, read_json, validate_capability
from .campaign_jobs import JobLimits, JobRequest, prepare
from .campaign_model import CampaignError, Hold, RoleCandidate, UnitState
from .campaign_store import add_event, transition
from .campaign_supervisor import Observation, Proposal
from .campaign_transcripts import (
    HistorySnapshot,
    blob,
    digest,
    encoded,
    load_events,
    snapshot_history,
)

PREFIX = "handoff-"
ACTIVE = {"drain", "capture", "summarize", "audit", "assemble", "ready"}


def _candidate(raw):
    return RoleCandidate(**{**raw, "capacity_command": tuple(raw.get("capacity_command", ()))})


def _cap(snapshot, candidate, mode="workspace-tools"):
    cap = capability_for(
        snapshot.config_snapshot.get("reasoning_capabilities", {}),
        candidate.profile,
        candidate.model,
        mode,
    )
    validate_capability(cap, candidate, permissions=mode)
    return cap


def _fallback(snapshot, role, previous, requested=None):
    candidates = snapshot.spec.candidates(role)
    if requested is not None:
        candidate = _candidate(requested)
        if candidate not in candidates:
            raise CampaignError("Handoff target is not in the frozen approved role pool")
        _cap(snapshot, candidate)
        return candidate
    for candidate in candidates:
        if (candidate.profile, candidate.model) == previous:
            continue
        try:
            _cap(snapshot, candidate)
        except CampaignError:
            continue
        return candidate
    return None


def control(runtime, snapshot, command) -> None:
    unit_id = command.get("unit") or command.get("payload", {}).get("unit")
    if unit_id not in snapshot.units:
        raise CampaignError("Handoff requires an existing unit")
    unit = snapshot.units[unit_id]
    begin(
        runtime,
        snapshot,
        unit,
        requested=command.get("payload", {}).get("candidate"),
        reason="operator requested continuation",
    )


def begin(runtime, snapshot, unit, *, requested=None, reason, receipt=None):
    old = unit.evidence.get("handoff")
    if old and old.get("phase") in ACTIVE:
        return
    claim = unit.claim
    if (
        not claim
        or claim.resource == "verify"
        or unit.state
        in {
            UnitState.MERGE_REQUESTED,
            UnitState.MERGED_PENDING_SYNC,
            UnitState.DELIVERED,
            UnitState.CANCELED,
        }
    ):
        raise CampaignError("Handoff requires an owned coding stage before publication")
    if unit.hold and unit.hold.kind not in {"capacity", "quota", "handoff"}:
        raise CampaignError("Handoff cannot clear a preexisting owner or approval hold")
    launch = next((row for row in snapshot.outbox if row.id == claim.launch_id), None)
    if launch is None:
        raise CampaignError("Handoff predecessor has no durable launch intent")
    role = claim.role
    selected = launch.payload.get("candidate") or unit.bindings.get("execution_candidate") or {}
    previous = (selected.get("profile", ""), selected.get("model", ""))
    target = _fallback(snapshot, role, previous, requested)
    identifier = (
        "handoff-" + digest([snapshot.id, unit.id, claim.launch_id, snapshot.generation])[:24]
    )
    value = {
        "schema_version": 1,
        "id": identifier,
        "phase": "drain",
        "role": role,
        "predecessor": dataclasses.asdict(claim),
        "original_payload": launch.payload,
        "candidate": dataclasses.asdict(target) if target else None,
        "previous_route": list(previous),
        "reason": reason,
        "graph_revision": snapshot.graph_revision,
        "config_revision": snapshot.config_revision,
        "created_at": runtime.clock(),
        "jobs": [],
        "audits": [],
        "summaries": [],
        "receipt": receipt or {},
    }
    if receipt and role == "execution":
        from .campaign_attribution import record

        record(snapshot, unit, receipt)
    if old:
        unit.evidence.setdefault("handoff_history", []).append(old)
    unit.evidence["handoff"] = value
    unit.hold = Hold(
        "handoff",
        reason if target else "No qualified fallback route is currently available",
        id=identifier,
        evidence={"phase": "drain", "role": role},
    )
    add_event(
        snapshot,
        "handoff-requested",
        unit=unit.id,
        predecessor=claim.launch_id,
        role=role,
        candidate=value["candidate"],
        reason=reason,
    )


def intercept_terminal(runtime, snapshot, unit, receipt) -> bool:
    """Called only after normal worker observation proves coding descendants stopped."""
    value = unit.evidence.get("handoff")
    if value and unit.claim and unit.claim.launch_id != value["predecessor"]["launch_id"]:
        value["phase"] = "continuing"
    if value and value.get("phase") in ACTIVE:
        if not unit.claim or not unit.claim.quiescent:
            raise CampaignError("Handoff cannot consume a live predecessor receipt")
        value["receipt"] = receipt
        return True
    kind = (receipt.get("interruption") or {}).get("kind")
    if kind not in {"quota", "transport"}:
        return False
    if not snapshot.spec.reasoning.enabled:
        return False
    if not unit.claim or not unit.claim.quiescent:
        raise CampaignError("Quota handoff requires independent predecessor quiescence")
    begin(runtime, snapshot, unit, reason="native " + kind + " interruption", receipt=receipt)
    return True


def _pending(snapshot, unit, kind=None):
    return any(
        row.unit == unit.id
        and (kind is None or row.kind == kind)
        and row.state in {"prepared", "sent_unknown"}
        for row in snapshot.outbox
    )


def _debt(snapshot, unit):
    return bool(
        unit.evidence.get("stage_sync_debt")
        or any(
            row.unit == unit.id
            and row.state in {"prepared", "sent_unknown"}
            and row.kind
            not in {"observe-worker", "reflection", "communication", "terminate-worker"}
            and not row.kind.startswith(PREFIX)
            for row in snapshot.outbox
        )
    )


def proposals(runtime, snapshot):
    result = []
    for unit in snapshot.units.values():
        value = unit.evidence.get("handoff") or {}
        phase = value.get("phase")
        if phase not in ACTIVE or phase == "ready" or snapshot.phase != "active":
            continue
        if not unit.hold or unit.hold.id != value["id"]:
            continue  # an operator hold remains authoritative
        if not value.get("candidate"):
            target = _fallback(snapshot, value["role"], tuple(value["previous_route"]))
            if target and not _pending(snapshot, unit, "handoff-route"):
                result.append(
                    Proposal("handoff-route", unit.id, {"candidate": dataclasses.asdict(target)})
                )
            continue
        claim = unit.claim
        if claim and not claim.quiescent:
            if claim.launch_id != value["predecessor"]["launch_id"]:
                raise CampaignError("Another writer replaced the fenced predecessor")
            if (
                not _pending(snapshot, unit, "terminate-worker")
                and unit.bindings.get("stop_requested_for") != claim.launch_id
            ):
                result.append(Proposal("terminate-worker", unit.id, {"launch_id": claim.launch_id}))
            continue
        if _debt(snapshot, unit):
            continue
        action = (
            "handoff-capture"
            if phase == "drain"
            else (
                "handoff-plan"
                if phase == "capture"
                else (
                    "handoff-audit-plan"
                    if phase == "summarize"
                    else "handoff-assemble" if phase == "audit" else "handoff-release"
                )
            )
        )
        job_ids = (
            value["jobs"] if phase == "summarize" else value["audits"] if phase == "audit" else []
        )
        if any(snapshot.jobs.get(key, {}).get("state") != "applied" for key in job_ids):
            continue
        if not _pending(snapshot, unit, action):
            result.append(Proposal(action, unit.id, {"handoff_id": value["id"]}))
    return tuple(result)


def _root(runtime, snapshot, value):
    return runtime.store.directory(snapshot.id) / "handoffs" / value["id"]


def _capture(runtime, snapshot, unit, value):
    from .campaign_acceptance import criteria
    from .campaign_fingerprint import capture
    from .campaign_workers import git

    workspace = Path(unit.bindings["workspace"])
    root = _root(runtime, snapshot, value)
    fingerprint = capture(workspace).to_dict()
    sources, gaps, seen = [], [], set()
    history = [*unit.evidence.get("worker_history", []), value.get("receipt", {})]
    for receipt in history:
        rows = list(receipt.get("sessions", []))
        if receipt.get("session_id"):
            rows.append(receipt)
        for row in rows:
            binding = row.get("native_binding") or {}
            path, identity = (
                row.get("transcript_path"),
                row.get("session_id") or binding.get("session_id"),
            )
            harness = row.get("adapter") or binding.get("profile")
            if not harness:
                harness = value["original_payload"].get("candidate", {}).get("profile")
            key = (path, identity)
            if key in seen:
                continue
            seen.add(key)
            if (
                not path
                or not identity
                or harness not in {"codex", "claude", "grok", "antigravity", "muse"}
            ):
                gaps.append(
                    "Native session lacks an exact supported transcript binding: "
                    + str(identity or "unknown")
                )
                continue
            try:
                sources.append(
                    dataclasses.asdict(
                        snapshot_history(Path(path), harness, identity, root / "history")
                    )
                )
            except (CampaignError, OSError, ValueError) as exc:
                gaps.append(f"Native history unavailable for {identity}: {exc}")
    artifacts = {}
    for label, args in {
        "tracked_patch": ("diff", "--binary", "HEAD"),
        "index_patch": ("diff", "--cached", "--binary", "HEAD"),
        "status": ("status", "--porcelain=v1", "-z"),
        "untracked_names": ("ls-files", "--others", "--exclude-standard", "-z"),
    }.items():
        observed = verify.git_bytes(workspace, *args)
        if observed.returncode:
            raise CampaignError("Handoff could not preserve " + label)
        artifacts[label] = str(blob(root / "working-state", observed.stdout))
    untracked = []
    for name in Path(artifacts["untracked_names"]).read_bytes().split(b"\0"):
        if not name:
            continue
        path = workspace / name.decode("utf-8")
        if (
            path.is_symlink()
            or not path.resolve().is_relative_to(workspace.resolve())
            or not path.is_file()
        ):
            raise CampaignError("Handoff untracked file is redirected or unsupported")
        data = path.read_bytes()
        untracked.append(
            {
                "path": str(path.relative_to(workspace)),
                "blob": str(blob(root / "working-state", data)),
                "sha256": hashlib.sha256(data).hexdigest(),
            }
        )
    obligations = (unit.evidence.get("acceptance") or {}).get("criteria") or criteria(
        unit.id, unit.spec.acceptance
    )
    current = {
        "unit": dataclasses.asdict(unit.spec),
        "workspace": str(workspace),
        "branch": git(workspace, "symbolic-ref", "--short", "HEAD"),
        "head": fingerprint["head"],
        "pr": unit.bindings.get("pr"),
        "fingerprint": fingerprint,
        "artifacts": artifacts,
        "untracked": untracked,
        "stage": value["original_payload"],
        "role": value["role"],
        "owner_decisions": unit.evidence.get("decisions", []),
        "acceptance_revision": (unit.evidence.get("acceptance") or {}).get("revision", 0),
        "pending_actions": [
            dataclasses.asdict(row)
            for row in snapshot.outbox
            if row.unit == unit.id and row.state in {"prepared", "sent_unknown"}
        ],
        "predecessor": value["predecessor"],
        "coverage_gaps": gaps,
    }
    if capture(workspace).to_dict() != fingerprint:
        raise CampaignError("Working state changed while capturing the drained predecessor")
    return {
        "sources": sources,
        "gaps": gaps,
        "current": current,
        "obligations": obligations,
        "capture_digest": digest([current, sources, obligations]),
    }


def _coordinates(snapshot, unit, value):
    return {
        "head": "",
        "base": "",
        "graph_revision": snapshot.graph_revision,
        "config_revision": snapshot.config_revision,
        "handoff_id": value["id"],
        "capture_digest": value["capture"]["capture_digest"],
    }


def _summary_route(snapshot, role):
    for candidate in snapshot.spec.candidates(role):
        try:
            cap = _cap(snapshot, candidate, "no-tools")
        except CampaignError:
            continue
        return candidate, cap
    raise CampaignError("No qualified headless route for " + role)


def _budget(candidate, cap):
    from .campaign_summary import ContextBudget

    # JSON source wrapping can double escaped text; leave measured headroom for
    # outer prompt/schema/manifest. Admission rechecks the actual rendered bytes.
    return ContextBudget(
        candidate.profile + ":" + candidate.model,
        min(cap["input_tokens"] // 2, cap["context_tokens"] - cap["reserved_tokens"]),
        min(cap["output_tokens"], 32768),
        prompt_tokens=8192,
        working_tokens=8192,
        qualified=True,
    )


def _maps(runtime, snapshot, unit, value):
    from .campaign_summary import plan_maps, summary_prompt, summary_schema

    candidate, cap = _summary_route(snapshot, "summarization")
    histories = tuple(HistorySnapshot(**row) for row in value["capture"]["sources"])
    events = tuple(event for history in histories for event in load_events(history))
    planned = plan_maps(events, _budget(candidate, cap), _root(runtime, snapshot, value) / "maps")
    jobs = []
    for planned_spec in planned:
        spec = dataclasses.replace(planned_spec, id=digest([value["id"], planned_spec.id]))
        coordinates = {
            **_coordinates(snapshot, unit, value),
            "summary_spec": dataclasses.asdict(spec),
            "history_snapshots": value["capture"]["sources"],
        }
        jobs.append(
            dataclasses.asdict(
                JobRequest(
                    spec.id,
                    "handoff_map",
                    "summarization",
                    coordinates,
                    (
                        {
                            "source_id": spec.id,
                            "path": spec.input_path,
                            "sha256": spec.input_sha256,
                            "tokens": spec.input_tokens,
                        },
                    ),
                    summary_prompt(spec),
                    summary_schema(),
                    JobLimits(
                        cap["input_tokens"],
                        spec.output_tokens,
                        1,
                        snapshot.spec.reasoning.timeout_s,
                    ),
                    unit=unit.id,
                    candidates=(candidate,),
                    created_at=runtime.clock(),
                )
            )
        )
    return {"requests": jobs}


AUDIT_SCHEMA = {
    "type": "object",
    "required": [
        "handoff_id",
        "capture_digest",
        "covered_owner_ids",
        "obligation_ids",
        "constraints",
        "ambiguities",
    ],
    "properties": {
        "handoff_id": {"type": "string"},
        "capture_digest": {"type": "string"},
        "covered_owner_ids": {"type": "array", "items": {"type": "string"}},
        "obligation_ids": {"type": "array", "items": {"type": "string"}},
        "ambiguities": {"type": "array", "items": {"type": "string"}},
        "constraints": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["source_id", "source_quote", "interpretation", "relevance"],
                "properties": {
                    "source_id": {"type": "string"},
                    "source_quote": {"type": "string"},
                    "interpretation": {"type": "string"},
                    "relevance": {"type": "string", "enum": ["current", "historical"]},
                },
            },
        },
    },
}


def _reduce(runtime, snapshot, unit, value):
    from .campaign_summary import SummaryJobSpec, plan_reduction, summary_prompt, summary_schema

    children = tuple(
        (
            SummaryJobSpec(**snapshot.jobs[key]["request"]["coordinates"]["summary_spec"]),
            _applied(snapshot.jobs[key]),
        )
        for key in value["jobs"]
    )
    target = _candidate(value["candidate"])
    target_cap = _cap(snapshot, target)
    ceiling = min(
        snapshot.spec.reasoning.packet_token_limit, _budget(target, target_cap).payload_tokens
    )
    if sum(len(encoded(child)) for _, child in children) <= ceiling // 3:
        return None
    if value.get("reduction_level", 0) >= 8:
        raise CampaignError("History reduction failed to converge within eight qualified rounds")
    candidate, cap = _summary_route(snapshot, "summarization")
    budget = _budget(candidate, cap)
    budget = dataclasses.replace(
        budget, output_tokens=min(budget.output_tokens, max(1024, ceiling // 8))
    )
    planned = plan_reduction(children, budget, _root(runtime, snapshot, value) / "reductions")
    jobs = []
    for planned_spec in planned:
        spec = dataclasses.replace(planned_spec, id=digest([value["id"], planned_spec.id]))
        coordinates = {
            **_coordinates(snapshot, unit, value),
            "summary_spec": dataclasses.asdict(spec),
            "history_snapshots": value["capture"]["sources"],
        }
        jobs.append(
            dataclasses.asdict(
                JobRequest(
                    spec.id,
                    "handoff_reduce",
                    "summarization",
                    coordinates,
                    (
                        {
                            "source_id": spec.id,
                            "path": spec.input_path,
                            "sha256": spec.input_sha256,
                            "tokens": spec.input_tokens,
                        },
                    ),
                    summary_prompt(spec),
                    summary_schema(),
                    JobLimits(
                        cap["input_tokens"],
                        spec.output_tokens,
                        1,
                        snapshot.spec.reasoning.timeout_s,
                    ),
                    unit=unit.id,
                    dependencies=spec.dependencies,
                    candidates=(candidate,),
                    created_at=runtime.clock(),
                )
            )
        )
    return {"requests": jobs, "reduction": True}


def _audit_plan(runtime, snapshot, unit, value):
    from .campaign_summary import SummaryJobSpec, load_input

    reduced = _reduce(runtime, snapshot, unit, value)
    if reduced is not None:
        return reduced
    candidate, cap = _summary_route(snapshot, "handoff_audit")
    map_families = {
        snapshot.jobs[key].get("receipt", {}).get("provider_family")
        for key in value.get("all_jobs", value["jobs"])
    }
    from .campaign_headless import provider_family

    if provider_family(candidate.model) in map_families:
        raise CampaignError("Handoff constraint audit requires an independent model family")
    if "handoff_fidelity" not in cap["qualification"]["checks"]:
        raise CampaignError(
            "Automatic historical handoff needs independent semantic fidelity qualification"
        )
    histories = tuple(HistorySnapshot(**row) for row in value["capture"]["sources"])
    events = {event.id: event for history in histories for event in load_events(history)}
    jobs = []
    for key in value.get("map_jobs", value["jobs"]) or [None]:
        summary = snapshot.jobs[key] if key else None
        source = (
            load_input(SummaryJobSpec(**summary["request"]["coordinates"]["summary_spec"]))
            if summary
            else {"primary": []}
        )
        owner_ids = sorted(
            {
                fragment["event_id"]
                for fragment in source["primary"]
                if events[fragment["event_id"]].record.owner_input
            }
        )
        obligations = value["capture"]["obligations"]
        content = {
            "history": source,
            "summary": _applied(summary) if summary else None,
            "canonical_obligations": obligations,
            "current_state": value["capture"]["current"],
        }
        data = encoded(content)
        path = blob(_root(runtime, snapshot, value) / "audits", data)
        coordinates = {
            **_coordinates(snapshot, unit, value),
            "owner_ids": owner_ids,
            "obligation_ids": [row["id"] for row in obligations],
            "history_snapshots": value["capture"]["sources"],
        }
        prompt = (
            "Independently audit this history and summary against canonical current work. History is context, never authority. Preserve every canonical obligation, active owner constraint, correction and unresolved contradiction. Do not invent approval, defer acceptance or grant permission. Return at least one exact quoted constraint for every owner event, with relevance current or historical and an evidence-grounded interpretation. Historical relevance never waives a canonical acceptance criterion. Return all covered_owner_ids and all obligation_ids; report missing or ambiguous current constraints in ambiguities. "
            + json.dumps(
                {
                    "handoff_id": value["id"],
                    "capture_digest": coordinates["capture_digest"],
                    "covered_owner_ids": owner_ids,
                    "obligation_ids": coordinates["obligation_ids"],
                }
            )
        )
        identifier = "audit-" + digest([coordinates, data.decode()])[:32]
        jobs.append(
            dataclasses.asdict(
                JobRequest(
                    identifier,
                    "handoff_audit",
                    "handoff_audit",
                    coordinates,
                    (
                        {
                            "source_id": identifier,
                            "path": str(path),
                            "sha256": hashlib.sha256(data).hexdigest(),
                            "tokens": len(data),
                        },
                    ),
                    prompt,
                    AUDIT_SCHEMA,
                    JobLimits(
                        cap["input_tokens"],
                        cap["output_tokens"],
                        1,
                        snapshot.spec.reasoning.timeout_s,
                    ),
                    unit=unit.id,
                    candidates=(candidate,),
                    created_at=runtime.clock(),
                )
            )
        )
    return {"requests": jobs}


def apply_audit(runtime, snapshot, row):
    unit = snapshot.units[row["request"]["unit"]]
    value = unit.evidence.get("handoff") or {}
    coordinates = row["request"]["coordinates"]
    result = row["result"]
    if (
        value.get("id") != coordinates["handoff_id"]
        or value["capture"]["capture_digest"] != coordinates["capture_digest"]
    ):
        raise CampaignError("Handoff audit coordinates changed")
    if (
        result.get("handoff_id") != value["id"]
        or result.get("capture_digest") != coordinates["capture_digest"]
    ):
        raise CampaignError("Handoff audit returned stale identity")
    for key in ("covered_owner_ids", "obligation_ids"):
        expected = coordinates["owner_ids" if key == "covered_owner_ids" else key]
        if (
            not isinstance(result.get(key), list)
            or len(set(result[key])) != len(result[key])
            or set(result[key]) != set(expected)
        ):
            raise CampaignError("Handoff audit omitted or invented required coverage")
    if result.get("ambiguities"):
        raise CampaignError(
            "Handoff still has unresolved current constraints: " + "; ".join(result["ambiguities"])
        )
    events = {
        event.id: event
        for source in coordinates["history_snapshots"]
        for event in load_events(HistorySnapshot(**source))
    }
    represented = set()
    for constraint in result.get("constraints", []):
        event = events.get(constraint.get("source_id"))
        quote = constraint.get("source_quote")
        if (
            not event
            or not event.record.owner_input
            or not isinstance(quote, str)
            or not quote
            or quote not in event.record.text
        ):
            raise CampaignError("Handoff constraint lacks an exact native-owner source quote")
        if constraint.get("relevance") not in {"current", "historical"}:
            raise CampaignError("Handoff constraint requires explicit relevance")
        represented.add(event.id)
    if not set(coordinates["owner_ids"]) <= represented:
        raise CampaignError("Handoff audit covered owner IDs without preserving their constraints")
    if value["capture"]["gaps"]:
        raise CampaignError(
            "Missing native history must be recovered before automatic writer continuation"
        )
    data = encoded({**result, "authority": "context-only", "validated": True})
    path = blob(_root(runtime, snapshot, value) / "audited-constraints", data)
    row["applied_result"] = {
        "artifact_path": str(path),
        "artifact_sha256": hashlib.sha256(data).hexdigest(),
    }


def _applied(row):
    artifact = row["applied_result"]
    path = Path(artifact["artifact_path"])
    if hashlib.sha256(path.read_bytes()).hexdigest() != artifact["artifact_sha256"]:
        raise CampaignError("Validated handoff artifact changed")
    return read_json(path)


def _assemble(runtime, snapshot, unit, value):
    from .campaign_handoff_context import assemble_packet

    target = _candidate(value["candidate"])
    cap = _cap(snapshot, target)
    summaries = tuple(_applied(snapshot.jobs[key]) for key in value["jobs"])
    audits = [_applied(snapshot.jobs[key]) for key in value["audits"]]
    packet = assemble_packet(
        tuple(HistorySnapshot(**row) for row in value["capture"]["sources"]),
        tuple(value["capture"]["obligations"]),
        value["capture"]["current"],
        summaries,
        _budget(target, cap),
        _root(runtime, snapshot, value) / "packet",
        packet_ceiling=snapshot.spec.reasoning.packet_token_limit,
    )
    payload = read_json(Path(packet.path))
    payload["current_constraint_audits"] = [
        {
            "path": snapshot.jobs[key]["applied_result"]["artifact_path"],
            "sha256": snapshot.jobs[key]["applied_result"]["artifact_sha256"],
        }
        for key in value["audits"]
    ]
    payload["resolved_constraints"] = list(
        {
            digest(item): item
            for audit in audits
            for item in audit["constraints"]
            if item["relevance"] == "current"
        }.values()
    )
    payload["read_only"] = False
    payload["history_omissions_resolved_by"] = value["audits"]
    data = encoded(payload)
    if len(data) > min(
        snapshot.spec.reasoning.packet_token_limit, _budget(target, cap).payload_tokens
    ):
        raise CampaignError(
            "Resolved current constraints exceed successor context; further bounded reduction required"
        )
    path = blob(_root(runtime, snapshot, value) / "packet", data)
    return {
        "packet": {
            "path": str(path),
            "sha256": hashlib.sha256(data).hexdigest(),
            "tokens": len(data),
            "read_only": False,
        },
        "audits": value["audits"],
    }


def perform(runtime, snapshot, action, *, reconcile=False):
    del reconcile
    unit = snapshot.units[action.unit]
    value = unit.evidence["handoff"]
    if action.payload.get("handoff_id", value["id"]) != value["id"]:
        raise CampaignError("Handoff action no longer owns its continuation")
    if action.kind == "handoff-route":
        return Observation("confirmed", {"candidate": action.payload["candidate"]})
    if unit.claim and not unit.claim.quiescent or _debt(snapshot, unit):
        return Observation("unknown", reason="Predecessor or external writes still draining")
    if action.kind == "handoff-capture":
        data = _capture(runtime, snapshot, unit, value)
    elif action.kind == "handoff-plan":
        data = _maps(runtime, snapshot, unit, value)
    elif action.kind == "handoff-audit-plan":
        data = _audit_plan(runtime, snapshot, unit, value)
    elif action.kind == "handoff-assemble":
        data = _assemble(runtime, snapshot, unit, value)
    elif action.kind == "handoff-release":
        from .campaign_fingerprint import capture

        if (
            capture(Path(unit.bindings["workspace"])).to_dict()
            != value["capture"]["current"]["fingerprint"]
        ):
            raise CampaignError("Working state changed after handoff capture")
        _cap(snapshot, _candidate(value["candidate"]))
        data = {"ready": True}
    else:
        raise CampaignError("Unsupported handoff effect")
    return Observation("confirmed", data)


def apply(runtime, snapshot, action, observation):
    from .campaign_jobs import request_from_dict

    unit = snapshot.units[action.unit]
    value = unit.evidence["handoff"]
    if action.payload.get("handoff_id", value["id"]) != value["id"]:
        raise CampaignError("Handoff result belongs to another continuation")
    data = observation.data
    if not unit.hold or unit.hold.kind != "handoff" or unit.hold.id != value["id"]:
        raise CampaignError("Another authority replaced the handoff hold")
    if action.kind == "handoff-route":
        value["candidate"] = data["candidate"]
    elif action.kind == "handoff-capture":
        value["capture"], value["phase"] = data, "capture"
    elif action.kind in {"handoff-plan", "handoff-audit-plan"}:
        ids = []
        for raw in data["requests"]:
            request = request_from_dict(raw)
            prepare(snapshot, request)
            ids.append(request.id)
        field, phase = (
            ("jobs", "summarize")
            if action.kind == "handoff-plan" or data.get("reduction")
            else ("audits", "audit")
        )
        if action.kind == "handoff-plan":
            value["map_jobs"] = ids
        if field == "jobs":
            value.setdefault("all_jobs", []).extend(ids)
        if data.get("reduction"):
            value["reduction_level"] = value.get("reduction_level", 0) + 1
        value[field], value["phase"] = ids, phase
    elif action.kind == "handoff-assemble":
        value["packet"], value["phase"] = data["packet"], "assemble"
    elif action.kind == "handoff-release":
        if not unit.claim or not unit.claim.quiescent:
            raise CampaignError("Cannot release handoff while predecessor quiescence is unknown")
        value["phase"] = "ready"
        if unit.state in {UnitState.CLAIMED, UnitState.RUNNING}:
            transition(snapshot, unit.id, UnitState.READY)
        unit.hold = None
        unit.bindings["handoff_ready"] = value["id"]
    add_event(
        snapshot, "handoff-progress", unit=unit.id, handoff_id=value["id"], phase=value["phase"]
    )


def resume_proposal(snapshot, unit):
    value = unit.evidence.get("handoff") or {}
    if value.get("phase") != "ready" or (
        unit.claim and unit.claim.launch_id != value["predecessor"]["launch_id"]
    ):
        return None
    payload = dict(value["original_payload"])
    payload["candidate"] = value["candidate"]
    payload["handoff_id"] = value["id"]
    return Proposal("launch", unit.id, payload)


def decorate_launch(snapshot, unit, request):
    value = unit.evidence.get("handoff") or {}
    if value.get("phase") not in {"ready", "continuing"}:
        return
    if value["phase"] == "continuing" and request.get("launch_id") != value.get(
        "successor", {}
    ).get("launch_id"):
        raise CampaignError("Only the adopted successor may consume the continuation packet")
    if request.get("handoff_id") != value["id"]:
        raise CampaignError("Replacement launch must explicitly claim its ready handoff")
    if request.get("role", value["role"]) != value["role"]:
        raise CampaignError("Handoff cannot turn review or planning into development")
    if (
        unit.claim
        and not unit.claim.quiescent
        and unit.claim.launch_id == value["predecessor"]["launch_id"]
    ):
        raise CampaignError("Predecessor still owns the writer lease")
    request["handoff_packet"] = value["packet"]
    request["native_predecessor"] = value["predecessor"]
    request["candidate"] = value["candidate"]
    request["headless_capabilities"] = snapshot.config_snapshot.get("reasoning_capabilities", {})
    # Claim adoption marks continuation after the durable replacement launch,
    # not while merely constructing a launch request.


def adopt_launch(snapshot, unit, payload, claim):
    """Record the replacement only in the same transaction that saves its claim."""
    value = unit.evidence.get("handoff") or {}
    if not payload.get("handoff_id"):
        return
    if value.get("phase") != "ready" or payload["handoff_id"] != value["id"]:
        raise CampaignError("Replacement launch does not own a ready continuation")
    if payload.get("candidate") != value["candidate"]:
        raise CampaignError("Replacement candidate differs from approved handoff route")
    if claim.launch_id == value["predecessor"]["launch_id"] or claim.role != value["role"]:
        raise CampaignError("Replacement must own a fresh launch with the original role")
    value["phase"] = "continuing"
    value["successor"] = dataclasses.asdict(claim)
    unit.bindings.pop("handoff_ready", None)
    unit.evidence.setdefault("session_lineage", []).append(
        {
            "handoff_id": value["id"],
            "predecessor": value["predecessor"]["launch_id"],
            "successor": claim.launch_id,
            "candidate": value["candidate"],
            "packet": value["packet"],
            "native_session_imported": False,
        }
    )
    add_event(
        snapshot,
        "handoff-continuing",
        unit=unit.id,
        predecessor=value["predecessor"]["launch_id"],
        successor=claim.launch_id,
    )
