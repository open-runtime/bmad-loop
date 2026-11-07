"""Explicit native history qualification, outside zero-token tests and campaign state.

Sources are already sealed manifests. The trial uses ordinary qualified headless
jobs and the production summary validators. Every finished job is durable, so
interruption resumes without repeating successful inference. This is evidence
collection, not an automatic semantic-fidelity grant or a worker lease transfer.
"""

from __future__ import annotations

import hashlib
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .adapters.multiplexer import MultiplexerError
from .campaign_headless import (
    HeadlessRunner,
    file_digest,
    read_json,
    render_input,
    validate_capability,
    write_json,
)
from .campaign_headless_protocol import parse_output
from .campaign_jobs import JobLimits, JobRequest
from .campaign_model import CampaignError, RoleCandidate
from .campaign_summary import (
    EXTRACT_PROMPT_VERSION,
    REDUCE_PROMPT_VERSION,
    ContextBudget,
    SummaryJobSpec,
    SummaryResultRejected,
    corrective_spec,
    measured_output_tokens,
    plan_reduction,
    plan_remaining_maps,
    project_reduction_input,
    split_map_input,
    split_reduction_input,
    summary_prompt,
    summary_schema,
    validate_summary,
)
from .campaign_transcripts import HistorySnapshot, blob, encoded, merge_lineage_events
from .platform_util import file_lock, is_link_like


def trial_request(
    job: SummaryJobSpec, capability: dict[str, Any], snapshots, timeout_s: int
) -> JobRequest:
    candidate = RoleCandidate(capability["profile"], capability["model"], capability["efforts"][0])
    return JobRequest(
        id=job.id,
        purpose=job.purpose.replace("-", "_"),
        role="summarization",
        coordinates={
            "graph_revision": 1,
            "config_revision": 1,
            "head": "",
            "base": "",
            "summary_spec": asdict(job),
            "history_snapshots": [asdict(s) for s in snapshots],
        },
        inputs=(
            {
                "source_id": job.id,
                "path": job.input_path,
                "sha256": job.input_sha256,
                "tokens": job.input_tokens,
            },
        ),
        prompt=summary_prompt(job)
        + f" Keep the complete JSON result below {job.output_tokens} UTF-8 bytes. Be concise while preserving required facts and all coverage IDs.",
        output_schema=summary_schema(job),
        limits=JobLimits(capability["input_tokens"], job.output_tokens, 1, timeout_s),
        candidates=(candidate,),
        dependencies=job.dependencies,
        required=False,
    )


def _observed_receipt(request_path, runner, observation=None):
    """Persist terminal-death proof separately from the child's earlier receipt."""
    directory = request_path.parent
    raw_path = directory / "receipt.json"
    path, binding_path = (
        directory / "observed-receipt.json",
        directory / "observed-receipt-binding.json",
    )
    if path.exists() and binding_path.exists():
        binding = read_json(binding_path)
        if binding.get("native_receipt_sha256") != file_digest(raw_path) or binding.get(
            "observed_sha256"
        ) != file_digest(path):
            raise CampaignError("Observed native lifecycle receipt changed")
        receipt = read_json(path)
    else:
        observation = observation or runner.observe(request_path)
        receipt = observation.get("receipt", {})
        if (
            observation.get("state") != "finished"
            or receipt.get("quiescent") is not True
            or receipt.get("processes_quiescent") is not True
        ):
            raise CampaignError("Native terminal lifecycle is not proven quiescent")
        write_json(path, receipt)
        write_json(
            binding_path,
            {
                "schema_version": 1,
                "native_receipt_sha256": file_digest(raw_path),
                "observed_sha256": file_digest(path),
            },
        )
    envelope = read_json(request_path)
    if (
        receipt.get("request_digest") != envelope["request_digest"]
        or receipt.get("launch_id") != envelope["launch_id"]
    ):
        raise CampaignError("Observed lifecycle belongs to another immutable request")
    # Older workers discarded native telemetry when Grok rejected an output
    # schema. Reclassify only this evidenced failure; never turn it into success
    # or mutate the historical receipt. The sidecar binds the complete native
    # output and delivery bytes to the already-quiescent attempt.
    if (
        receipt.get("reason")
        == ("CampaignError: Native session identity or structured result is missing")
        and envelope["capability"]["protocol"] == "grok-json"
    ):
        output_path = directory / "stdout.jsonl"
        cap = envelope["capability"]
        native = parse_output(
            output_path.read_text(encoding="utf-8"),
            protocol=cap["protocol"],
            selected_model=envelope["selection"]["model"],
            served_model_aliases=tuple(cap.get("served_model_aliases", ())),
        )
        if (
            native.get("failure_kind") != "schema"
            or native.get("native_terminal_completed") is not True
            or receipt.get("quiescent") is not True
            or receipt.get("processes_quiescent") is not True
            or file_digest(directory / "input.txt") != envelope["input"]["sha256"]
        ):
            raise CampaignError("Historical failure has no complete native schema-rejection proof")
        receipt = {
            **receipt,
            **native,
            "native_outcome": native["outcome"],
            "input_complete": True,
            "input_delivery": envelope["input"],
        }
        limits = envelope["request"]["limits"]
        if (
            native["turns"] > limits["max_turns"]
            or native["usage"].get("output_tokens", 0) > limits["output_tokens"]
        ):
            receipt.update(
                outcome="limit",
                reason="Native turn/output receipt exceeds the job bound",
                limit_kind=(
                    "native-turns"
                    if native["turns"] > limits["max_turns"]
                    else "native-output-tokens"
                ),
            )
        reparse = {
            "schema_version": 2,
            "native_receipt_sha256": file_digest(raw_path),
            "stdout_sha256": file_digest(output_path),
            "request_sha256": file_digest(request_path),
            "receipt": receipt,
        }
        reparse_path = directory / "schema-rejection-reparse-v2.json"
        if reparse_path.exists() and read_json(reparse_path) != reparse:
            raise CampaignError("Historical schema-rejection evidence changed")
        write_json(reparse_path, reparse)
    return receipt


def _schema_rejected(receipt, request):
    return (
        receipt.get("failure_kind") == "schema"
        and receipt.get("native_terminal_completed") is True
        and receipt.get("outcome") in {"failed", "limit"}
        and receipt.get("limit_kind", "") in {"", "native-output-tokens"}
        and receipt.get("native_outcome", "failed") == "failed"
        and type(receipt.get("turns")) is int
        and 1 <= receipt["turns"] <= request.limits.max_turns
        and receipt.get("input_complete") is True
        and receipt.get("quiescent") is True
        and receipt.get("processes_quiescent") is True
        and receipt.get("truncated") is False
    )


def _output_rejected(receipt, request):
    """A complete native output limit can use the remaining validation budget.

    Older receipts lack typed limit/native-outcome fields, but input_complete
    was emitted only after completed native parsing. Every positive lifecycle,
    measured-output and other-limit check remains mandatory.
    """
    tokens = (receipt.get("usage") or {}).get("output_tokens")
    turns = receipt.get("turns")
    return (
        receipt.get("outcome") == "limit"
        and receipt.get("limit_kind", "") in {"", "native-output-tokens"}
        and receipt.get("native_outcome", "completed") == "completed"
        and receipt.get("failure_kind", "") == ""
        and not receipt.get("telemetry_error")
        and receipt.get("input_complete") is True
        and receipt.get("quiescent") is True
        and receipt.get("processes_quiescent") is True
        and receipt.get("truncated") is False
        and isinstance(receipt.get("result"), dict)
        and receipt.get("usage_known") is True
        and type(tokens) is int
        and tokens > request.limits.output_tokens
        and type(turns) is int
        and 1 <= turns <= request.limits.max_turns
    )


def _single_attempt(job, snapshots, events, capability, directory, timeout_s, runner):
    destination = directory / "jobs" / job.id
    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    with file_lock(destination / "trial.lock"):
        validated_path = destination / "validated.json"
        request = trial_request(job, capability, snapshots, timeout_s)
        request_path = destination / "request.json"
        if validated_path.exists():
            # Revalidate the original result and native receipt, not a mutable cache verdict.
            saved = read_json(validated_path)
            receipt = _observed_receipt(request_path, runner)
            if saved["receipt_sha256"] != file_digest(destination / "receipt.json"):
                raise CampaignError("Trial receipt changed after validation")
            validated = validate_summary(
                job,
                receipt["result"],
                events,
                observed_output_tokens=measured_output_tokens(receipt),
            )
            if saved["artifact_sha256"] != file_digest(Path(saved["artifact_path"])):
                raise CampaignError("Trial validated summary changed")
            return job, validated
        candidate = request.candidates[0]
        prepared = runner.prepare(
            destination, request, candidate, capability, launch_id="trial-" + job.id, generation=1
        )
        if prepared != request_path:
            raise CampaignError("Trial runner returned an unexpected request path")
        runner.launch(request_path)
        deadline = time.monotonic() + timeout_s + 15
        while True:
            observation = runner.observe(request_path)
            if observation.get("state") == "finished":
                break
            if time.monotonic() >= deadline:
                runner.terminate(request_path)
                raise CampaignError(
                    "Native trial timed out; cancellation requested, resume for quiescence readback"
                )
            time.sleep(0.2)
        receipt = _observed_receipt(request_path, runner, observation)
        if _schema_rejected(receipt, request):
            raise SummaryResultRejected(
                str(receipt["reason"])
                + ". Reducer citations must copy the child fact's citations array, never its id. "
                "Use concise facts and preserve the configured output bound."
            )
        if _output_rejected(receipt, request):
            raise SummaryResultRejected(
                f"Native output {receipt['usage']['output_tokens']} tokens exceeds {request.limits.output_tokens}. "
                f"Keep the complete result JSON below {max(1024, job.output_tokens // 3)} UTF-8 bytes, leaving room for native reasoning. "
                "Consolidate repeated contextual facts without losing owner constraints or child coverage."
            )
        if (
            receipt.get("outcome") != "completed"
            or receipt.get("input_complete") is not True
            or receipt.get("processes_quiescent") is not True
            or receipt.get("quiescent") is not True
        ):
            raise CampaignError(
                "Native trial job did not complete with full input and owned-process quiescence: "
                + str(receipt.get("reason", ""))
            )
        validated = validate_summary(
            job, receipt["result"], events, observed_output_tokens=measured_output_tokens(receipt)
        )
        content = encoded(validated)
        artifact = blob(directory / "summaries", content)
        write_json(
            validated_path,
            {
                "schema_version": 1,
                "artifact_path": str(artifact),
                "artifact_sha256": hashlib.sha256(content).hexdigest(),
                "receipt_sha256": file_digest(destination / "receipt.json"),
            },
        )
        return job, validated


def _one(job, snapshots, events, capability, directory, timeout_s, runner):
    if (
        job.purpose == "handoff-reduce"
        and not (directory / "jobs" / job.id / "request.json").exists()
    ):
        job = project_reduction_input(job, directory / "inputs")
    original = job
    for attempt in range(3):
        try:
            return _single_attempt(job, snapshots, events, capability, directory, timeout_s, runner)
        except SummaryResultRejected as exc:
            destination = directory / "jobs" / job.id
            receipt_path = destination / "receipt.json"
            receipt = _observed_receipt(destination / "request.json", runner)
            request = trial_request(job, capability, snapshots, timeout_s)
            if (
                receipt.get("outcome") != "completed"
                and not _schema_rejected(receipt, request)
                and not _output_rejected(receipt, request)
            ) or receipt.get("processes_quiescent") is not True:
                raise CampaignError(
                    "Correction requires a completed, quiescent native attempt"
                ) from exc
            rejected = {
                "schema_version": 1,
                "root_job_id": original.id,
                "job_id": job.id,
                "attempt": attempt,
                "reason": str(exc),
                "receipt_sha256": file_digest(receipt_path),
            }
            write_json(destination / "rejected.json", rejected)
            if attempt == 2:
                if original.purpose == "handoff-reduce":
                    revised = project_reduction_input(original, directory / "inputs")
                    if revised.id != original.id:
                        # One explicit presentation revision for a frozen legacy
                        # input; no reset of its three exhausted native attempts.
                        write_json(
                            directory / "jobs" / original.id / "presentation-revision.json",
                            {
                                "schema_version": 1,
                                "original_job_id": original.id,
                                "revised_job_id": revised.id,
                                "prior_attempts": 3,
                                "additional_attempt_limit": 1,
                                "last_rejection": rejected,
                            },
                        )
                        try:
                            return _single_attempt(
                                revised, snapshots, events, capability, directory, timeout_s, runner
                            )
                        except SummaryResultRejected as revision_error:
                            raise SummaryResultRejected(
                                "Reducer presentation revision exhausted its single native attempt: "
                                + str(revision_error)
                            ) from revision_error
                raise SummaryResultRejected(
                    "Summary validation budget exhausted after two corrective jobs: " + str(exc)
                ) from exc
            job = corrective_spec(
                original,
                attempt=attempt + 1,
                receipt_sha256=rejected["receipt_sha256"],
                reason=str(exc),
            )
    raise AssertionError("bounded summary attempts did not terminate")


def _reuse_maps(roots, events, capability, runner):
    completed = {}
    for root in roots:
        for path in sorted((root / "jobs").glob("*/validated.json")):
            saved = read_json(path)
            envelope = read_json(path.parent / "request.json")
            prior_cap = envelope["capability"]
            validate_capability(
                prior_cap,
                RoleCandidate(
                    prior_cap["profile"], prior_cap["model"], envelope["selection"]["effort"]
                ),
            )
            receipt_path = path.parent / "receipt.json"
            receipt = _observed_receipt(path.parent / "request.json", runner)
            spec = SummaryJobSpec(**envelope["request"]["coordinates"]["summary_spec"])
            if spec.purpose != "handoff-map":
                continue
            if (
                envelope["capability"]["profile"] != capability["profile"]
                or envelope["capability"]["model"] != capability["model"]
            ):
                raise CampaignError(
                    "Reused summary route differs from this explicitly selected route"
                )
            if (
                saved["receipt_sha256"] != file_digest(receipt_path)
                or saved["artifact_sha256"] != file_digest(Path(saved["artifact_path"]))
                or receipt.get("request_digest") != envelope["request_digest"]
                or receipt.get("launch_id") != envelope["launch_id"]
            ):
                raise CampaignError("Reused summary native receipt/artifact binding changed")
            if (
                receipt.get("outcome") != "completed"
                or receipt.get("quiescent") is not True
                or receipt.get("processes_quiescent") is not True
                or receipt.get("input_complete") is not True
            ):
                raise CampaignError("Reused summary lacks completed native lifecycle proof")
            completed[spec.id] = (
                spec,
                validate_summary(
                    spec,
                    receipt["result"],
                    events,
                    observed_output_tokens=measured_output_tokens(receipt),
                ),
            )
    return tuple(completed.values())


def _reuse_roots(directory, reuse_directory):
    """Follow only explicitly retained trial bindings, never discover histories."""
    roots, pending = [], [directory, *([reuse_directory] if reuse_directory else [])]
    seen = set()
    while pending:
        root = pending.pop(0).absolute()
        if root in seen:
            continue
        if is_link_like(root) or len(seen) >= 32:
            raise CampaignError("Trial reuse lineage is redirected or exceeds its bound")
        seen.add(root)
        roots.append(root)
        binding = root / "binding.json"
        if binding.exists() and (previous := read_json(binding).get("reuse_directory")):
            pending.append(Path(previous))
    return tuple(roots)


def _fit_maps(jobs, capability, snapshots, timeout_s, directory):
    fitted, pending = [], [(job, 0) for job in reversed(jobs)]
    while pending:
        job, depth = pending.pop()
        try:
            minimum_output = {
                "schema_version": 1,
                "job_id": job.id,
                "input_sha256": job.input_sha256,
                "covered_ids": list(job.primary_ids),
                "facts": [],
                "gaps": [],
            }
            if len(encoded(minimum_output)) > job.output_tokens // 2:
                raise CampaignError("Mandatory output coverage leaves insufficient summary space")
            render_input(trial_request(job, capability, snapshots, timeout_s), capability)
        except CampaignError as exc:
            if (
                "Mandatory output coverage" not in str(exc)
                and "exceeds its input token bound" not in str(exc)
                and "insufficient output/thinking context" not in str(exc)
            ):
                raise
            if depth >= 64:
                raise CampaignError("Adaptive map partition did not converge") from exc
            split = split_map_input if job.purpose == "handoff-map" else split_reduction_input
            pending.extend((part, depth + 1) for part in reversed(split(job, directory)))
        else:
            fitted.append(job)
    return tuple(fitted)


def _draining(directory):
    path = directory / "drain.json"
    if not path.exists():
        return False
    if read_json(path).get("binding_sha256") != file_digest(directory / "binding.json"):
        raise CampaignError("Trial drain request belongs to different frozen coordinates")
    return True


def run_trial(
    snapshot_path: Path,
    capability_path: Path,
    directory: Path,
    *,
    parallel=4,
    timeout_s=600,
    execute=True,
    runner=None,
    reuse_directory: Path | None = None,
    reducer_capability_path: Path | None = None,
    drain: bool = False,
) -> dict[str, Any]:
    """Process all sealed sources; no aggregate token ceiling or silent partial success."""
    if not 1 <= parallel <= 64 or not 1 <= timeout_s <= 3600 or is_link_like(directory):
        raise CampaignError("Invalid native history trial bounds")
    if drain:
        binding_path = directory / "binding.json"
        binding = read_json(binding_path)
        if binding.get("snapshots_sha256") != file_digest(snapshot_path) or binding.get(
            "capability_sha256"
        ) != file_digest(capability_path):
            raise CampaignError("Trial drain does not match frozen sources and route")
        write_json(
            directory / "drain.json",
            {
                "schema_version": 1,
                "binding_sha256": file_digest(binding_path),
                "requested_at": time.time(),
            },
        )
        return {
            "schema_version": 1,
            "ok": True,
            "state": "drain-requested",
            "semantic_fidelity": "unqualified",
        }
    capability = read_json(capability_path)
    selection = RoleCandidate(capability["profile"], capability["model"], capability["efforts"][0])
    validate_capability(capability, selection)
    reducer_capability = (
        read_json(reducer_capability_path) if reducer_capability_path else capability
    )
    validate_capability(
        reducer_capability,
        RoleCandidate(
            reducer_capability["profile"],
            reducer_capability["model"],
            reducer_capability["efforts"][0],
        ),
    )
    document = read_json(snapshot_path)
    if document.get("schema_version") != 1 or not document.get("snapshots"):
        raise CampaignError("Trial requires explicit sealed source snapshots")
    snapshots = tuple(HistorySnapshot(**row) for row in document["snapshots"])
    events = merge_lineage_events(snapshots)
    runner = runner or HeadlessRunner()
    roots = _reuse_roots(directory, reuse_directory)
    reusable = _reuse_maps(
        roots,
        events,
        capability,
        runner,
    )
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Source JSON becomes a string inside the native request. Leave space for
    # escaping, source IDs, schema, and the exact primary-coverage list.
    payload = max(1024, capability["input_tokens"] * 3 // 4 - 8192)
    output = capability["output_tokens"]
    budget = ContextBudget(
        selection.profile + ":" + selection.model,
        payload + output,
        output,
        prompt_tokens=0,
        working_tokens=0,
        qualified=True,
    )
    path = directory / "binding.json"
    with file_lock(directory / "trial.lock"):
        if (
            (directory / "report.json").exists()
            and read_json(directory / "report.json").get("state") == "drained"
            and _draining(directory)
        ):
            control = directory / "drain.json"
            blob(directory / "control-history", control.read_bytes())
            control.unlink()
        planning = "adaptive-render-v1"
        if path.exists():
            prior = read_json(path)
            budget = ContextBudget(**prior["budget"])
            payload = budget.payload_tokens
            planning = prior.get("planning", "legacy-global")
        while True:
            jobs = plan_remaining_maps(
                events, reusable, budget, directory / "inputs", overlap_tokens=payload // 8
            )
            try:
                if planning == "adaptive-render-v1":
                    jobs = _fit_maps(jobs, capability, snapshots, timeout_s, directory / "inputs")
                    break
                for job in jobs:
                    render_input(trial_request(job, capability, snapshots, timeout_s), capability)
                break
            except CampaignError as exc:
                if "exceeds its input token bound" not in str(
                    exc
                ) and "insufficient output/thinking context" not in str(exc):
                    raise
                payload = payload * 3 // 4
                if payload < 2048:
                    raise CampaignError(
                        "Native route cannot fit rendered summary metadata"
                    ) from exc
                budget = ContextBudget(
                    selection.profile + ":" + selection.model,
                    payload + output,
                    output,
                    prompt_tokens=0,
                    working_tokens=0,
                    qualified=True,
                )
        binding = {
            "schema_version": 1,
            "snapshots_sha256": file_digest(snapshot_path),
            "capability_sha256": file_digest(capability_path),
            "budget": asdict(budget),
            "timeout_s": timeout_s,
            "reuse_directory": str(reuse_directory.absolute()) if reuse_directory else "",
            "prompt_versions": [EXTRACT_PROMPT_VERSION, REDUCE_PROMPT_VERSION],
            "correction_budget": 2,
        }
        if planning == "adaptive-render-v1":
            binding["planning"] = planning
        if reducer_capability_path:
            binding["reducer_capability_sha256"] = file_digest(reducer_capability_path)
        completed_ids = {job.id for job, _ in reusable}
        jobs = tuple(job for job in jobs if job.id not in completed_ids)
        if path.exists() and read_json(path) != binding:
            raise CampaignError(
                "Trial directory belongs to different frozen sources, route or limits"
            )
        write_json(path, binding)
        write_json(
            directory / "map-plan.json",
            {"schema_version": 1, "jobs": [asdict(job) for job in jobs]},
        )
        report = {
            "schema_version": 1,
            "ok": False,
            "state": "prepared",
            "source_count": len(snapshots),
            "source_bytes": sum(s.captured_bytes for s in snapshots),
            "unique_events": sum(not e.duplicate_of for e in events),
            "map_jobs": len(jobs) + len(reusable),
            "new_map_jobs": len(jobs),
            "reused_jobs": len(reusable),
            "completed_jobs": len(reusable),
            "semantic_fidelity": "unqualified",
            "problems": [],
        }
        write_json(directory / "report.json", report)
        if not execute:
            return report
        runner = runner or HeadlessRunner()
        layer = 0
        while True:
            current, problems = (list(reusable) if layer == 0 else []), []
            active_capability = capability if layer == 0 else reducer_capability
            # Every submitted job has a bounded native lifecycle. A failed job
            # stops new admissions while existing futures drain and keep receipts.
            with ThreadPoolExecutor(max_workers=parallel) as pool:
                pending = {}
                iterator = iter(jobs)
                for _ in range(min(parallel, len(jobs))):
                    if _draining(directory):
                        break
                    job = next(iterator)
                    pending[
                        pool.submit(
                            _one,
                            job,
                            snapshots,
                            events,
                            active_capability,
                            directory,
                            timeout_s,
                            runner,
                        )
                    ] = job
                while pending:
                    future = next(as_completed(pending))
                    job = pending.pop(future)
                    try:
                        current.append(future.result())
                        report["completed_jobs"] += 1
                    except (
                        CampaignError,
                        OSError,
                        ValueError,
                        KeyError,
                        TypeError,
                        MultiplexerError,
                    ) as exc:
                        problems.append({"job_id": job.id, "problem": str(exc)})
                    write_json(
                        directory / "report.json",
                        {**report, "state": "running", "layer": layer, "problems": problems},
                    )
                    if not problems and not _draining(directory):
                        next_job = next(iterator, None)
                        if next_job is not None:
                            pending[
                                pool.submit(
                                    _one,
                                    next_job,
                                    snapshots,
                                    events,
                                    active_capability,
                                    directory,
                                    timeout_s,
                                    runner,
                                )
                            ] = next_job
            if problems:
                report.update(state="incomplete", problems=problems, layer=layer)
                break
            if _draining(directory):
                report.update(state="drained", layer=layer)
                break
            event_order = {event.id: index for index, event in enumerate(events)}
            current.sort(
                key=lambda pair: (
                    min(
                        (event_order.get(ref, len(events)) for ref in pair[0].event_ids),
                        default=len(events),
                    ),
                    pair[0].id,
                )
            )
            # A final collection may contain several summaries. No promise of a
            # single giant prompt; exact source indexes remain separately retrievable.
            if sum(len(encoded(result)) for _, result in current) <= 160000:
                report.update(
                    ok=True,
                    state="structurally-complete",
                    final_summaries=[job.id for job, _ in current],
                    layer=layer,
                )
                break
            reducer_output = reducer_capability["output_tokens"]
            reducer_payload = max(1024, reducer_capability["input_tokens"] * 3 // 4 - 8192)
            reducer_budget = ContextBudget(
                reducer_capability["profile"] + ":" + reducer_capability["model"],
                reducer_payload + reducer_output,
                reducer_output,
                prompt_tokens=0,
                working_tokens=0,
                qualified=True,
            )
            next_jobs = plan_reduction(tuple(current), reducer_budget, directory / "inputs")
            next_jobs = _fit_maps(
                next_jobs, reducer_capability, snapshots, timeout_s, directory / "inputs"
            )
            if len(next_jobs) >= len(current) or layer >= 8:
                report.update(
                    state="incomplete",
                    problems=[
                        {
                            "problem": "Reduction cannot shrink within qualified route; use a qualified larger reducer or smaller map output"
                        }
                    ],
                    layer=layer,
                )
                break
            jobs = next_jobs
            layer += 1
        report["usage"] = {}
        receipt_paths = {
            p.resolve() for root in roots for p in (root / "jobs").glob("*/receipt.json")
        }
        report["usage_unknown_jobs"] = 0
        for receipt_path in receipt_paths:
            receipt = read_json(receipt_path)
            if any(
                (receipt_path.parent / name).exists()
                for name in ("schema-rejection-reparse.json", "schema-rejection-reparse-v2.json")
            ):
                receipt = _observed_receipt(receipt_path.parent / "request.json", runner)
            if not receipt.get("usage_known"):
                report["usage_unknown_jobs"] += 1
            for key, value in receipt.get("usage", {}).items():
                if type(value) in (int, float):
                    report["usage"][key] = report["usage"].get(key, 0) + value
        write_json(directory / "report.json", report)
        return report
