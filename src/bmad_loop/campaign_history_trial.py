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

from .campaign_headless import (
    HeadlessRunner,
    file_digest,
    read_json,
    validate_capability,
    write_json,
)
from .campaign_jobs import JobLimits, JobRequest
from .campaign_model import CampaignError, RoleCandidate
from .campaign_summary import (
    ContextBudget,
    SummaryJobSpec,
    plan_maps,
    plan_reduction,
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
        output_schema=summary_schema(),
        limits=JobLimits(capability["input_tokens"], job.output_tokens, 1, timeout_s),
        candidates=(candidate,),
        dependencies=job.dependencies,
        required=False,
    )


def _one(job, snapshots, events, capability, directory, timeout_s, runner):
    destination = directory / "jobs" / job.id
    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    with file_lock(destination / "trial.lock"):
        validated_path = destination / "validated.json"
        request = trial_request(job, capability, snapshots, timeout_s)
        request_path = destination / "request.json"
        if validated_path.exists():
            # Revalidate the original result and native receipt, not a mutable cache verdict.
            saved = read_json(validated_path)
            receipt = read_json(destination / "receipt.json")
            if saved["receipt_sha256"] != file_digest(destination / "receipt.json"):
                raise CampaignError("Trial receipt changed after validation")
            validated = validate_summary(job, receipt["result"], events)
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
        receipt = observation.get("receipt", {})
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
        validated = validate_summary(job, receipt["result"], events)
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


def run_trial(
    snapshot_path: Path,
    capability_path: Path,
    directory: Path,
    *,
    parallel=4,
    timeout_s=600,
    execute=True,
    runner=None,
) -> dict[str, Any]:
    """Process all sealed sources; no aggregate token ceiling or silent partial success."""
    if not 1 <= parallel <= 16 or not 1 <= timeout_s <= 3600 or is_link_like(directory):
        raise CampaignError("Invalid native history trial bounds")
    capability = read_json(capability_path)
    selection = RoleCandidate(capability["profile"], capability["model"], capability["efforts"][0])
    validate_capability(capability, selection)
    document = read_json(snapshot_path)
    if document.get("schema_version") != 1 or not document.get("snapshots"):
        raise CampaignError("Trial requires explicit sealed source snapshots")
    snapshots = tuple(HistorySnapshot(**row) for row in document["snapshots"])
    events = merge_lineage_events(snapshots)
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
    binding = {
        "schema_version": 1,
        "snapshots_sha256": file_digest(snapshot_path),
        "capability_sha256": file_digest(capability_path),
        "budget": asdict(budget),
        "timeout_s": timeout_s,
    }
    path = directory / "binding.json"
    with file_lock(directory / "trial.lock"):
        if path.exists() and read_json(path) != binding:
            raise CampaignError(
                "Trial directory belongs to different frozen sources, route or limits"
            )
        write_json(path, binding)
        jobs = plan_maps(events, budget, directory / "inputs", overlap_tokens=payload // 8)
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
            "map_jobs": len(jobs),
            "completed_jobs": 0,
            "semantic_fidelity": "unqualified",
            "problems": [],
        }
        write_json(directory / "report.json", report)
        if not execute:
            return report
        runner = runner or HeadlessRunner()
        completed = []
        layer = 0
        while jobs:
            current, problems = [], []
            # Every submitted job has a bounded native lifecycle. A failed job
            # stops new admissions while existing futures drain and keep receipts.
            with ThreadPoolExecutor(max_workers=parallel) as pool:
                pending = {}
                iterator = iter(jobs)
                for _ in range(min(parallel, len(jobs))):
                    job = next(iterator)
                    pending[
                        pool.submit(
                            _one, job, snapshots, events, capability, directory, timeout_s, runner
                        )
                    ] = job
                while pending:
                    future = next(as_completed(pending))
                    job = pending.pop(future)
                    try:
                        current.append(future.result())
                        report["completed_jobs"] += 1
                    except (CampaignError, OSError, ValueError, KeyError, TypeError) as exc:
                        problems.append({"job_id": job.id, "problem": str(exc)})
                    write_json(
                        directory / "report.json",
                        {**report, "state": "running", "layer": layer, "problems": problems},
                    )
                    if not problems:
                        next_job = next(iterator, None)
                        if next_job is not None:
                            pending[
                                pool.submit(
                                    _one,
                                    next_job,
                                    snapshots,
                                    events,
                                    capability,
                                    directory,
                                    timeout_s,
                                    runner,
                                )
                            ] = next_job
            if problems:
                report.update(state="incomplete", problems=problems, layer=layer)
                break
            order = {job.id: index for index, job in enumerate(jobs)}
            current.sort(key=lambda pair: order[pair[0].id])
            completed.extend(current)
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
            next_jobs = plan_reduction(tuple(current), budget, directory / "inputs")
            if len(next_jobs) >= len(jobs):
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
        for receipt_path in (directory / "jobs").glob("*/receipt.json"):
            for key, value in read_json(receipt_path).get("usage", {}).items():
                if type(value) in (int, float):
                    report["usage"][key] = report["usage"].get(key, 0) + value
        write_json(directory / "report.json", report)
        return report
