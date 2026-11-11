"""Optional extraction of sealed native-history prefixes while a worker runs.

Only the controller's exact handle and hook channel identify a source. Captures
and cache validation happen in an outbox effect, never the scheduling tick. The
cache grants no writer authority; final handoff revalidates every reused prefix.
"""

from __future__ import annotations

import dataclasses
import hashlib
from pathlib import Path
from typing import Any

from . import runs
from .campaign_headless import read_json
from .campaign_jobs import TERMINAL, JobLimits, JobRequest, prepare, request_from_dict
from .campaign_model import CampaignError
from .campaign_paradev import ReflectionError
from .campaign_store import add_event
from .campaign_supervisor import Observation, Proposal
from .campaign_transcripts import (
    HistorySnapshot,
    blob,
    digest,
    encoded,
    merge_lineage_events,
    snapshot_history,
)

KIND = "history-warmup"
INTERVAL_S = 60
MIN_BYTES = 65536
BATCH_SIZE = 4
MAX_INDEX_JOBS = 10000
MAX_HANDLES = 256
PLAN_BYTES = 2 * 1024 * 1024


def _identity(snapshot, unit):
    from .campaign_summary import EXTRACT_PROMPT_VERSION

    return digest(
        [
            snapshot.id,
            unit.id,
            unit.claim.launch_id,
            snapshot.config_revision,
            "warmup-v1",
            EXTRACT_PROMPT_VERSION,
        ]
    )


def proposals(runtime, snapshot):
    if (
        snapshot.phase != "active"
        or not snapshot.spec.reasoning.enabled
        or "handoff_map" not in snapshot.spec.reasoning.purposes
    ):
        return ()
    from .campaign_handoff_flow import ACTIVE

    result = []
    for unit in snapshot.units.values():
        claim = unit.claim
        if (
            not claim
            or claim.quiescent
            or claim.resource == "verify"
            or unit.evidence.get("handoff", {}).get("phase") in ACTIVE
        ):
            continue
        prior = unit.evidence.get("history_warmup", {})
        if runtime.clock() - max(claim.started_at, prior.get("checked_at", 0)) < INTERVAL_S:
            continue
        if any(
            action.kind == KIND
            and action.unit == unit.id
            and action.state in {"prepared", "sent_unknown"}
            for action in snapshot.outbox
        ):
            continue
        if any(
            key in snapshot.jobs
            and snapshot.jobs[key].get("state") not in TERMINAL
            and not (
                snapshot.jobs[key].get("state") == "failed"
                and snapshot.jobs[key].get("failure_attempts", 0)
                >= snapshot.spec.reasoning.max_attempts
            )
            for key in prior.get("pending_jobs", [])
        ):
            continue
        result.append(
            Proposal(
                KIND,
                unit.id,
                {
                    "launch_id": claim.launch_id,
                    "warmup_id": _identity(snapshot, unit),
                    "after": prior.get("checked_at", 0),
                },
            )
        )
    return tuple(result)


def current(snapshot, request):
    """Already admitted maps can finish after quiescence, but not another claim."""
    from .campaign_summary import summary_contract_current

    unit = snapshot.units.get(request.unit)
    coordinate = request.coordinates.get("history_warmup", {})
    state = unit.evidence.get("history_warmup", {}) if unit else {}
    return bool(
        unit
        and unit.claim
        and request.purpose == "handoff_map"
        and summary_contract_current(request)
        and not request.required
        and coordinate.get("id") == state.get("id") == _identity(snapshot, unit)
        and coordinate.get("launch_id") == unit.claim.launch_id
        and request.id in state.get("pending_jobs", [])
    )


def launch_allowed(snapshot, request):
    from .campaign_handoff_flow import ACTIVE

    unit = snapshot.units.get(request.unit)
    return bool(
        current(snapshot, request)
        and unit
        and not unit.claim.quiescent
        and unit.evidence.get("handoff", {}).get("phase") not in ACTIVE
    )


def _index(runtime, snapshot, unit) -> dict[str, Any]:
    state = unit.evidence.get("history_warmup", {})
    if not state.get("index_path"):
        return {"jobs": [], "sources": []}
    path = Path(state["index_path"])
    root = runtime.store.directory(snapshot.id) / "history-warmup"
    if not path.is_absolute() or not path.resolve().is_relative_to(root.resolve()):
        raise CampaignError("History warmup index escapes its campaign")
    value = read_json(path)
    if digest(value) != state.get("index_sha256") or len(value.get("jobs", [])) > MAX_INDEX_JOBS:
        raise CampaignError("History warmup index identity or bound changed")
    return value


def _sources(runtime, snapshot, unit):
    from .campaign_execution import CampaignExecution
    from .campaign_reflection_flow import _hook_identity, _read_document

    claim = unit.claim
    directory = runtime.store.directory(snapshot.id) / "workers" / claim.launch_id
    handles = sorted((directory / "native-sessions").glob("*.json"))
    if len(handles) > MAX_HANDLES:
        raise CampaignError("Live native handle coverage exceeds warmup bound")
    launch = next(
        (row for row in snapshot.outbox if row.id == claim.launch_id and row.state == "confirmed"),
        None,
    )
    if launch is None:
        raise CampaignError("Worker launch is not reconciled")
    result = []
    for path in handles:
        handle = _read_document(path)
        expected = {
            "campaign_id": snapshot.id,
            "unit_id": unit.id,
            "launch_id": claim.launch_id,
            "attempt": claim.attempt,
            "generation": claim.generation,
            "run_id": claim.run_id,
            "workspace": claim.workspace,
            "branch": claim.branch,
        }
        if (
            any(handle.get(key) != value for key, value in expected.items())
            or handle.get("task_id") != path.stem
        ):
            raise CampaignError("Native handle does not belong to the current worker claim")
        run_dir = Path(handle.get("run_dir", ""))
        allowed = {runs.run_dir_for(Path(claim.workspace), claim.run_id), directory / claim.run_id}
        if not run_dir.is_absolute() or run_dir not in allowed:
            raise CampaignError("Native handle run directory is not owned by this claim")
        managed = handle.get("managed_history")
        if managed:
            path = Path(managed.get("headless_request", ""))
            identifier = hashlib.sha256(handle["task_id"].encode()).hexdigest()[:24]
            expected_path = (
                directory / "native" / run_dir.name / "headless" / identifier / "request.json"
            )
            if path != expected_path:
                raise CampaignError("Managed history request is outside its exact worker stage")
            envelope = read_json(path)
            coordinates = envelope.get("request", {}).get("coordinates", {})
            if (
                envelope.get("launch_id") != handle["task_id"]
                or envelope.get("generation") != claim.generation
                or coordinates.get("campaign") != snapshot.id
                or coordinates.get("unit") != unit.id
                or coordinates.get("config_revision") != snapshot.config_revision
            ):
                raise CampaignError(
                    "Managed live history belongs to another worker or configuration"
                )
            result.append(
                {
                    "path": str(path.parent / "stdout.jsonl"),
                    "harness": "managed-headless",
                    "session_id": "headless:" + handle["task_id"],
                    "managed_history": managed,
                }
            )
            continue
        identity = _hook_identity(
            {
                "env": {
                    "BMAD_LOOP_EVENTS_DIR": str(
                        runs.events_dir_for(Path(claim.workspace), run_dir.name)
                    )
                }
            },
            handle,
            path.stem,
        )
        source, session = identity.get("transcript_path"), identity.get("cli_session_id")
        if not source or not session:
            continue
        profile = (launch.payload.get("candidate") or {}).get("profile")
        # Legacy mixed-role engines use the frozen adapter selected for this
        # exact native handle, not the outer development candidate for reviews.
        if (directory / "request.json").is_file():
            request = read_json(directory / "request.json")
            execution = CampaignExecution.load(Path(request["execution"]), request["digest"])
            profile = execution.policy.adapter.resolved(handle["role"]).name
        if not profile:
            continue
        source_path = Path(source)
        if not source_path.is_absolute():
            raise CampaignError("Hook transcript path is not absolute")
        item = {"path": source, "session_id": session, "harness": profile}
        if item not in result:
            result.append(item)
    return result


def _prefix_matches(old, new):
    """Compare immutable raw bytes, including complete records and a partial tail."""
    if old["source_id"] != new["source_id"] or old["captured_bytes"] > new["captured_bytes"]:
        return False
    raw = Path(new["raw_path"])
    # read_json/snapshot_history own these immutable blobs; verify both hashes
    # rather than trusting event IDs to authorize obsolete summary facts.
    data = raw.read_bytes()
    return (
        hashlib.sha256(data).hexdigest() == new["raw_sha256"]
        and hashlib.sha256(data[: old["captured_bytes"]]).hexdigest() == old["raw_sha256"]
    )


def reusable(runtime, snapshot, unit, sources):
    """Return only applied maps whose complete input still belongs to final history."""
    from .campaign_handoff_flow import (
        _applied,
        _budget,
        _cap,
        _job,
        validate_fidelity_qualification,
    )
    from .campaign_summary import (
        SummaryJobSpec,
        load_input,
        summary_contract_current,
        summary_prompt,
        summary_schema,
    )

    try:
        index = _index(runtime, snapshot, unit)
    except (CampaignError, OSError, ValueError, KeyError) as exc:
        return (), ["cache unavailable: " + str(exc)]
    by_source = {row["source_id"]: row for row in sources}
    events = {
        event.id: event
        for event in merge_lineage_events(tuple(HistorySnapshot(**row) for row in sources))
    }
    completed, rejected = [], []
    prefix_cache, route_cache = {}, {}
    for key in index["jobs"]:
        try:
            row = _job(runtime, snapshot, key)
        except CampaignError:
            rejected.append(key)
            continue
        if not row or row.get("state") != "applied":
            continue
        request = request_from_dict(row["request"])
        if not summary_contract_current(request):
            rejected.append(key)
            continue
        if (
            request.purpose != "handoff_map"
            or request.coordinates["config_revision"] != snapshot.config_revision
            or not request.coordinates.get("history_warmup")
        ):
            continue
        from .campaign_summary_routes import candidates as summary_candidates
        from .campaign_summary_routes import enabled

        if len(request.candidates) != 1 or request.candidates[0] not in summary_candidates(
            snapshot.spec, "summarization", "leaf" if enabled(snapshot.spec) else ""
        ):
            rejected.append(key)
            continue
        candidate = request.candidates[0]
        route_key = digest(dataclasses.asdict(candidate))
        if route_key not in route_cache:
            try:
                cap = _cap(snapshot, candidate, "reasoning")
                validate_fidelity_qualification(cap, candidate=candidate, purpose="handoff_map")
                route_cache[route_key] = _budget(candidate, cap).route
            except CampaignError:
                route_cache[route_key] = None
        route = route_cache[route_key]
        if route is None:
            rejected.append(key)
            continue
        old_sources = request.coordinates.get("history_snapshots", [])
        valid = bool(old_sources)
        for old in old_sources:
            cache_key = (old["source_id"], old["raw_sha256"])
            if cache_key not in prefix_cache:
                now = by_source.get(old["source_id"])
                prefix_cache[cache_key] = bool(now and _prefix_matches(old, now))
            valid = valid and prefix_cache[cache_key]
        spec = SummaryJobSpec(**request.coordinates["summary_spec"])
        if (
            spec.route != route
            or request.prompt != summary_prompt(spec)
            or request.output_schema != summary_schema(spec)
        ):
            rejected.append(key)
            continue
        try:
            data = load_input(spec)
        except (CampaignError, OSError, ValueError):
            rejected.append(key)
            continue
        for fragment in [*data.get("primary", []), *data.get("halo", [])]:
            event = events.get(fragment["event_id"])
            valid = valid and bool(
                event
                and 0 <= fragment["start"] <= fragment["end"] <= len(event.record.text)
                and event.record.text[fragment["start"] : fragment["end"]] == fragment["text"]
            )
        if valid:
            try:
                completed.append((spec, _applied(row)))
            except (CampaignError, OSError, ValueError):
                rejected.append(key)
        else:
            rejected.append(key)
    return tuple(completed), rejected


def perform(runtime, snapshot, action):
    from .campaign_handoff_flow import _budget, _job, _stage_coordinates, _summary_route
    from .campaign_summary import (
        SummaryJobSpec,
        load_input,
        plan_remaining_maps,
        summary_prompt,
        summary_schema,
    )

    unit = snapshot.units[action.unit]
    if (
        not unit.claim
        or unit.claim.launch_id != action.payload["launch_id"]
        or _identity(snapshot, unit) != action.payload["warmup_id"]
    ):
        return Observation("rejected", reason="History warmup claim changed")
    state = {
        "id": action.payload["warmup_id"],
        "launch_id": unit.claim.launch_id,
        "checked_at": runtime.clock(),
        "state": "unavailable",
        "detail": "",
        "pending_jobs": [],
    }
    try:
        index = _index(runtime, snapshot, unit)
        routing = {"all_jobs": index["jobs"], "audit_family": index.get("audit_family", "")}
        candidate, cap = _summary_route(runtime, snapshot, "summarization", routing)
        bindings = _sources(runtime, snapshot, unit)
        if not bindings:
            state["detail"] = "No exact native transcript or managed live request binding"
            return Observation("confirmed", {"state": state, "requests": []})
        if len(index["jobs"]) >= MAX_INDEX_JOBS:
            raise CampaignError(
                "History warmup cache reached its bounded job index; final capture remains available"
            )
        root = runtime.store.directory(snapshot.id) / "history-warmup" / state["id"]
        sizes = {digest(binding): Path(binding["path"]).stat().st_size for binding in bindings}
        prior_sizes = index.get("sizes", {})
        changed = unit.evidence.get("history_warmup", {}).get("id") != state["id"] or any(
            key not in prior_sizes
            or size < prior_sizes[key]
            or size - prior_sizes[key] >= MIN_BYTES
            for key, size in sizes.items()
        )
        if not changed and not index.get("backlog"):
            prior = unit.evidence.get("history_warmup", {})
            state.update(
                {
                    key: prior[key]
                    for key in (
                        "index_path",
                        "index_sha256",
                        "source_count",
                        "event_count",
                        "reusable_jobs",
                        "invalidated_jobs",
                        "deferred_large_events",
                        "backlog",
                    )
                    if key in prior
                }
            )
            state.update(
                state="available", detail="Cached sealed prefixes; waiting for transcript growth"
            )
            return Observation("confirmed", {"state": state, "requests": []})
        sources = [] if changed else index["sources"]
        for binding in bindings if changed else []:
            path = Path(binding["path"])
            if path.stat().st_size < MIN_BYTES:
                continue
            if binding.get("managed_history"):
                from .campaign_managed_history import snapshot_managed_prefix

                source = snapshot_managed_prefix(
                    binding["managed_history"]["headless_request"],
                    binding["managed_history"],
                    root / "sources",
                    max_bytes=snapshot.spec.reasoning.history_max_bytes,
                )
            else:
                source = snapshot_history(
                    path,
                    binding["harness"],
                    binding["session_id"],
                    root / "sources",
                    max_bytes=snapshot.spec.reasoning.history_max_bytes,
                )
            sources.append(dataclasses.asdict(source))
        completed, rejected = reusable(
            runtime,
            snapshot,
            unit,
            sources,
        )
        # Optional paging also advances past fully attempted events. Failure is
        # never reusable semantic coverage: final handoff still extracts those
        # sources. Bind attempts to this claim/config, preserve lifetime limits,
        # and retain partial events until all their fragments have been attempted.
        attempted = []
        from .campaign_summary import summary_contract_current

        for key in index["jobs"]:
            row = _job(runtime, snapshot, key)
            coordinates = row["request"]["coordinates"]
            if (
                coordinates.get("history_warmup", {}).get("id") == state["id"]
                and coordinates["config_revision"] == snapshot.config_revision
                and summary_contract_current(row["request"])
            ):
                attempted.append(SummaryJobSpec(**coordinates["summary_spec"]))
        # Bound map planning as well as launches. Oversized events remain for
        # mandatory final capture, with the limitation exposed in diagnostics.
        ranges = {}
        for job in (*[spec for spec, _ in completed], *attempted):
            for part in load_input(job).get("primary", []):
                ranges.setdefault(part["event_id"], []).append((part["start"], part["end"]))
        selected, size, deferred, oversized = [], 0, False, 0
        events = merge_lineage_events(tuple(HistorySnapshot(**row) for row in sources))
        for event in events:
            end = 0
            for start, stop in sorted(ranges.get(event.id, [])):
                if start > end:
                    break
                end = max(end, stop)
            if event.id in ranges and end >= len(event.record.text):
                continue
            length = len(encoded(dataclasses.asdict(event)))
            if length > PLAN_BYTES:
                oversized += 1
                continue
            if size + length > PLAN_BYTES or len(selected) >= 256:
                deferred = True
                break
            selected.append(event)
            size += length
        planned = plan_remaining_maps(
            tuple(selected), completed, _budget(candidate, cap), root / "maps"
        )
        indexed = set(index["jobs"])
        planned = tuple(
            dataclasses.replace(spec, id=identifier)
            for spec in planned
            if (identifier := digest([state["id"], spec.id, snapshot.config_revision]))
            not in indexed
        )
        limit = min(BATCH_SIZE, MAX_INDEX_JOBS - len(indexed))
        backlog = deferred or len(planned) > limit
        planned = planned[:limit]
        requests = []
        for spec in planned:
            request = JobRequest(
                spec.id,
                "handoff_map",
                "summarization",
                {
                    **_stage_coordinates(runtime, snapshot, routing, candidate, "leaf"),
                    "head": "",
                    "base": "",
                    "graph_revision": snapshot.graph_revision,
                    "config_revision": snapshot.config_revision,
                    "history_warmup": {"id": state["id"], "launch_id": unit.claim.launch_id},
                    "summary_spec": dataclasses.asdict(spec),
                    "history_snapshots": [
                        source
                        for source in sources
                        if source["source_id"]
                        in {
                            fragment["lineage"]["source_id"]
                            for fragment in (
                                *load_input(spec).get("primary", []),
                                *load_input(spec).get("halo", []),
                            )
                        }
                    ],
                },
                (
                    {
                        "source_id": spec.id,
                        "path": spec.input_path,
                        "sha256": spec.input_sha256,
                        "tokens": spec.input_tokens,
                    },
                ),
                summary_prompt(spec),
                summary_schema(spec),
                JobLimits(
                    cap["input_tokens"], spec.output_tokens, 1, snapshot.spec.reasoning.timeout_s
                ),
                unit=unit.id,
                candidates=(candidate,),
                required=False,
                created_at=runtime.clock(),
            )
            requests.append(dataclasses.asdict(request))
        index = {
            "jobs": [*index["jobs"], *(row["id"] for row in requests)],
            "sources": sources,
            "sizes": sizes if changed else prior_sizes,
            "backlog": backlog,
            "audit_family": (
                requests[0]["coordinates"].get("audit_family", "")
                if requests
                else index.get("audit_family", "")
            ),
        }
        index_path = blob(root, encoded(index), suffix=".json")
        state.update(
            state="prepared" if requests else "available",
            detail="Sealed owned history prefixes; final capture and audit remain required",
            index_path=str(index_path),
            index_sha256=digest(index),
            pending_jobs=[row["id"] for row in requests],
            source_count=len(sources),
            event_count=len(events),
            reusable_jobs=len(completed),
            invalidated_jobs=len(rejected),
            deferred_large_events=oversized,
            backlog=backlog,
        )
        return Observation("confirmed", {"state": state, "requests": requests})
    except (CampaignError, ReflectionError, OSError, ValueError, KeyError) as exc:
        state.update(state="unavailable", detail=str(exc))
        return Observation("confirmed", {"state": state, "requests": []})


def apply(runtime, snapshot, action, observation):
    unit = snapshot.units[action.unit]
    if (
        observation.state != "confirmed"
        or not unit.claim
        or unit.claim.launch_id != action.payload["launch_id"]
        or _identity(snapshot, unit) != action.payload["warmup_id"]
    ):
        return
    state = observation.data["state"]
    prior = unit.evidence.get("history_warmup", {})
    # An unavailable binding or route must not erase previously retained work.
    for key in ("index_path", "index_sha256"):
        if key not in state and key in prior:
            state[key] = prior[key]
    unit.evidence["history_warmup"] = state
    for raw in observation.data["requests"]:
        prepare(snapshot, request_from_dict(raw))
    add_event(
        snapshot,
        "history-warmup-observed",
        unit=unit.id,
        state=state["state"],
        detail=state["detail"],
        prepared_jobs=len(observation.data["requests"]),
        invalidated_jobs=state.get("invalidated_jobs", 0),
    )
