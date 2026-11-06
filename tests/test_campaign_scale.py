"""Representative zero-token load uses the production coordinator and real Git.

These are scripted-service measurements, not REM or model-provider throughput.
The artifacts retain observed utilization, useful delivery and recovery latency.
"""

import json
import sys
import time
from collections import Counter
from dataclasses import replace
from pathlib import Path

from conftest import git
from test_campaign_headless import prepared as prepared
from test_campaign_runtime import production_case as production_case
from test_campaign_runtime import run_case

from bmad_loop import campaign_runtime
from bmad_loop.campaign_headless import file_digest
from bmad_loop.campaign_model import ReasoningSpec, ResourceSpec, RoleSpec, SourceSelector
from bmad_loop.campaign_scheduler import CapacityObservation


def scaled_sources(case, count=24):
    sources, ownership = [], []
    for epic in range(count):
        key = f"e{epic:02d}"
        relative = "specs/scale/" + key
        folder = case.repo / relative
        (folder / "stories").mkdir(parents=True)
        (folder / "SPEC.md").write_text("# Scale epic " + key + "\n")
        names = tuple(f"{key}s{story}" for story in range(2))
        (folder / "stories.yaml").write_text(
            "".join(
                f"- id: {name}\n  title: {name}\n  description: Accept {name}\n" for name in names
            )
        )
        for name in names:
            (folder / "stories" / (name + "-story.md")).write_text(
                "---\nstatus: ready-for-dev\n---\n# " + name + "\n"
            )
            ownership.append((f"{key}:project:{name}", ("src/" + name + ".txt",)))
        sources.append(
            SourceSelector("stories", id=key, path=relative, units=names, independent=True)
        )
    git(case.repo, "add", "specs/scale")
    git(case.repo, "commit", "-qm", "test: seed 24 independent epic sources")
    git(case.repo, "push", "origin", "main")
    return tuple(sources), tuple(ownership)


def reporting_route(tmp_path, prepared):
    runner, _, _, candidate, original = prepared
    script = tmp_path / "scripted-reporter.py"
    script.write_text("""import json,sys
payload=sys.stdin.buffer.read().decode()
source=next(json.loads(line) for line in payload.splitlines() if line.startswith('{"source_id":'))
context=json.loads(source['content']);unit=context['units'][0]
result={'schema_version':1,'context_digest':source['source_id'].removeprefix('context:'),'citations':[unit['source_id']],'summary':'Canonical scripted campaign observation','claims':[{'unit':unit['id'],**{key:unit[key] for key in ('state','head','base','source_id')}}]}
print(json.dumps({'type':'system','model':'gpt-test','session_id':'scale-report'}))
print(json.dumps({'type':'result','structured_output':result,'usage':{'input_tokens':10,'output_tokens':10}}))
""")
    cap = {
        **original,
        "argv": [str(Path(sys.executable).resolve()), str(script)],
        "dependencies": [{"path": str(script), "sha256": file_digest(script)}],
    }
    path = tmp_path / "scale-capabilities.json"
    path.write_text(
        json.dumps(
            {"schema_version": 1, "routes": {candidate.profile + ":" + candidate.model: cap}}
        )
    )
    return runner, candidate, path


def test_48_stories_24_epics_verify8_and_reasoning_drain_resume(
    production_case, prepared, tmp_path
):
    case = production_case
    sources, ownership = scaled_sources(case)
    runner, candidate, capabilities = reporting_route(tmp_path, prepared)
    roles = tuple((name, replace(role, max_sessions=24)) for name, role in case.snapshot.spec.roles)
    roles += (("reporting", RoleSpec(pool="scale-reporting", max_sessions=4)),)
    spec = replace(
        case.snapshot.spec,
        name="scale48",
        units=(),
        sources=sources,
        ownership=ownership,
        max_workers=24,
        max_active_epics=24,
        max_in_flight_units=48,
        max_review_sessions=8,
        resources=ResourceSpec(verify_slots=8, max_sessions=56),
        roles=roles,
        role_pools=(*case.snapshot.spec.role_pools, ("scale-reporting", (candidate,))),
        reasoning=ReasoningSpec(
            enabled=True, capabilities=str(capabilities), max_sessions=4, purposes=("report",)
        ),
        github=replace(case.snapshot.spec.github, max_base_refreshes=128),
    )
    _, case.snapshot = campaign_runtime.create(case.repo, spec)
    case.runtime.clock = time.monotonic
    case.runtime.reasoning_runner = runner
    case.runtime.capacity_observer = lambda *a, **k: CapacityObservation(
        True, slots=64, model_access=True
    )
    original = case.runtime.proposals
    started = time.monotonic()
    metrics = {
        "stories": 48,
        "epics": 24,
        "verify_slots": 8,
        "max_live": 0,
        "max_execution": 0,
        "max_verify": 0,
        "max_review": 0,
        "max_reasoning": 0,
        "max_active_epics": 0,
        "stop_requested_at": None,
        "control_ack_seconds": None,
        "first_launch_epics": [],
    }
    observed_launches = set()
    sampled_at = started

    def proposals(snapshot):
        nonlocal sampled_at
        live = [unit for unit in snapshot.units.values() if unit.claim and not unit.claim.quiescent]
        metrics["max_live"] = max(metrics["max_live"], len(live))
        for resource in ("execution", "verify", "review"):
            metrics["max_" + resource] = max(
                metrics["max_" + resource], sum(unit.claim.resource == resource for unit in live)
            )
        metrics["max_reasoning"] = max(
            metrics["max_reasoning"],
            sum(
                bool(row.get("claim") and not row["claim"]["quiescent"])
                for row in snapshot.jobs.values()
            ),
        )
        metrics["max_active_epics"] = max(
            metrics["max_active_epics"], len({unit.spec.epic for unit in live})
        )
        for unit in live:
            if unit.claim.role == "execution" and unit.id not in observed_launches:
                observed_launches.add(unit.id)
                metrics["first_launch_epics"].append(unit.spec.epic)
        if metrics["stop_requested_at"] is None and len(live) >= 8:
            metrics["stop_requested_at"] = time.monotonic()
            case.store.enqueue(snapshot.id, "stop", reason="scale drain/restart qualification")
        if snapshot.phase == "draining" and metrics["control_ack_seconds"] is None:
            metrics["control_ack_seconds"] = time.monotonic() - metrics["stop_requested_at"]
        if time.monotonic() - sampled_at >= 5:
            sampled_at = time.monotonic()
            (tmp_path / "scale-metrics-live.json").write_text(
                json.dumps(
                    {
                        **metrics,
                        "elapsed_s": sampled_at - started,
                        "phase": snapshot.phase,
                        "units": dict(
                            Counter(unit.state.value for unit in snapshot.units.values())
                        ),
                        "holds": {
                            unit.id: unit.hold.kind for unit in snapshot.units.values() if unit.hold
                        },
                        "outbox_hot": len(snapshot.outbox),
                        "events": len(snapshot.events),
                    },
                    indent=2,
                )
            )
        return original(snapshot)

    case.runtime.proposals = proposals
    original_control = case.runtime.control

    def control(snapshot, request):
        result = original_control(snapshot, request)
        if request.get("kind") == "stop" and metrics["stop_requested_at"] is not None:
            metrics["control_ack_seconds"] = time.monotonic() - metrics["stop_requested_at"]
        return result

    case.runtime.control = control
    stopped = run_case(case, timeout_s=1200)
    assert stopped.phase == "stopped"
    assert all(not unit.claim or unit.claim.quiescent for unit in stopped.units.values())
    assert all(not row.get("claim") or row["claim"]["quiescent"] for row in stopped.jobs.values())
    result = run_case(case, timeout_s=1800)
    elapsed = time.monotonic() - started
    metrics.update(
        elapsed_s=elapsed,
        delivered=sum(unit.state.value == "delivered" for unit in result.units.values()),
        delivered_per_hour=48 / elapsed * 3600,
        events=len(result.events),
        outbox_hot=len(result.outbox),
        jobs_hot=len(result.jobs),
        jobs_total=len(case.store.jobs(result.id, limit=10000)),
    )
    (tmp_path / "scale-metrics.json").write_text(json.dumps(metrics, indent=2))
    assert result.phase == "delivered" and metrics["delivered"] == 48
    assert metrics["max_execution"] >= 2 and metrics["max_verify"] >= 2
    assert metrics["max_verify"] <= 8 and metrics["max_review"] <= 8
    assert metrics["max_reasoning"] >= 1
    assert metrics["control_ack_seconds"] is not None and metrics["control_ack_seconds"] < 30
    assert len(set(metrics["first_launch_epics"])) == 24
    assert len(case.service.closed_units) >= 48
