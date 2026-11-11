"""Initial issue baselines traverse the real zero-token reasoning lifecycle."""

import copy
import json
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
from pathlib import Path

import pytest
from conftest import git
from test_campaign_headless import fixture_capability
from test_campaign_headless import prepared as prepared
from test_campaign_reasoning_jobs import case
from test_campaign_runtime_intake import observed

from bmad_loop import campaign_intake_baseline as baseline
from bmad_loop import campaign_intake_flow as flow
from bmad_loop import campaign_runtime_intake as intake
from bmad_loop.campaign_headless import file_digest, read_json
from bmad_loop.campaign_intake import IntakeBatch
from bmad_loop.campaign_model import Hold, RoleSpec


def test_initial_issue_baseline_runs_child_validates_and_applies_without_control_workspace(
    tmp_path, prepared, project
):
    runner, path, request, candidate, capability = prepared
    script = tmp_path / "baseline_child.py"
    script.write_text(
        """import json,re,sys
payload=sys.stdin.read()
assert 'Preserve quantities across restart.' in payload
context=re.search(r'context_digest=([0-9a-f]{64})', payload).group(1)
print(json.dumps({'type':'system','model':'gpt-test','session_id':'baseline-child'}))
print(json.dumps({'type':'result','structured_output':{
    'context_digest':context,'disposition':'represented',
    'rationale':'The committed specification covers the exact issue requirement.',
    'unrepresented_requirements':[]},
    'usage':{'input_tokens':100,'output_tokens':50}}))
""",
        encoding="utf-8",
    )
    capability = fixture_capability(
        tmp_path,
        {
            **capability,
            "argv": [capability["binary"], str(script)],
            "dependencies": [{"path": str(script), "sha256": file_digest(script)}],
        },
        candidate,
    )
    specification = project.project / "docs/baseline.md"
    specification.parent.mkdir(exist_ok=True)
    specification.write_text(
        "# Accepted story\nPreserve quantities across restart.\n", encoding="utf-8"
    )
    git(project.project, "add", "docs/baseline.md")
    git(project.project, "commit", "-m", "fixture: selected product intent")
    head = git(project.project, "rev-parse", "HEAD")
    observation = observed(body="Preserve quantities across restart.")

    def configure(snapshot):
        snapshot.repository = "a/b"
        snapshot.spec = replace(
            snapshot.spec,
            reasoning=replace(snapshot.spec.reasoning, purposes=("triage",)),
        )
        snapshot.bindings["inventory"] = {
            "complete": True,
            "observed_at": 100,
            "issues": [],
            "pulls": [],
        }
        selected, *others = snapshot.units.values()
        selected.bindings.update(
            workspace=str(project.project),
            spec_path="docs/baseline.md",
            head=head,
            issue={"number": observation.number, "database_id": int(observation.object_id)},
        )
        for unit in others:
            unit.hold = Hold("operator", "Only the selected story participates in this fixture")
        intake.apply_poll(
            snapshot,
            {
                "batch": asdict(IntakeBatch((observation,), observation.updated_at, True, 0)),
                "observed_at": 100,
            },
        )

    snapshot, driver, supervisor, clock = case(
        tmp_path,
        (runner, path, request, candidate, capability),
        count=0,
        extra_roles=(("triage", RoleSpec(pool="summary-pool")),),
        configure=configure,
    )
    unit_id = next(iter(snapshot.units))
    original_bindings = {key: copy.deepcopy(unit.bindings) for key, unit in snapshot.units.items()}
    assert baseline.waiting(snapshot, unit_id)
    assert not flow.untriaged(snapshot)
    initial = driver.proposals(snapshot)
    assert [proposal.kind for proposal in initial] == ["intake-baseline"]
    snapshot = supervisor._queue(snapshot, initial)
    seen_states = set()
    deadline = time.monotonic() + 30
    with ThreadPoolExecutor(max_workers=2) as executor:
        while time.monotonic() < deadline:
            snapshot = supervisor._harvest(snapshot)
            seen_states.update(row["state"] for row in snapshot.jobs.values())
            record = snapshot.bindings[flow.INBOX]["records"][observation.version_id]
            if record["state"] == "baseline":
                break
            clock[0] += 1
            proposals = driver.observe_workers(snapshot)
            assert all(proposal.kind.startswith("reasoning-") for proposal in proposals)
            snapshot = supervisor._queue(snapshot, proposals)
            for action in tuple(snapshot.outbox):
                if action.state == "prepared":
                    snapshot = supervisor._submit(snapshot, action, executor, reconcile=False)
                elif (
                    action.state == "sent_unknown"
                    and action.id not in supervisor._active
                    and (action.next_retry_at or 0) <= clock[0]
                ):
                    snapshot = supervisor._submit(snapshot, action, executor, reconcile=True)
            time.sleep(0.03)
        else:
            pytest.fail(
                repr(
                    {
                        "jobs": [(row["state"], row["reason"]) for row in snapshot.jobs.values()],
                        "effects": [(row.kind, row.state, row.error) for row in snapshot.outbox],
                    }
                )
            )

    # Terminal jobs leave hot state; query the real persisted lifecycle, not a fabricated result.
    job_id = record["baseline_job"]
    saved = driver.store.jobs(snapshot.id, job_id=job_id)
    assert len(saved) == 1
    job = saved[0]
    assert job["state"] == "applied" and job["validation"] == "valid"
    assert "validated" in seen_states
    assert job["attempt"] == 1 and job["claim"]["quiescent"]
    assert job["receipt"]["input_complete"] and job["receipt"]["processes_quiescent"]
    assert job["receipt"]["session_id"] == "baseline-child"
    assert job["receipt"]["result"] == record["baseline_result"]
    envelope = read_json(Path(job["claim"]["run_dir"]) / "request.json")
    packet = json.loads(Path(job["request"]["inputs"][0]["path"]).read_text(encoding="utf-8"))
    assert packet["selected_specification"] == specification.read_text(encoding="utf-8")
    assert packet["observation"] == observation.to_dict()
    assert envelope["request"]["coordinates"]["intake_baseline"]["unit"] == unit_id
    assert len(runner.mux.processes) == 1
    assert all(process.poll() is not None for process in runner.mux.processes.values())
    assert not baseline.waiting(snapshot, unit_id)
    assert not flow.untriaged(snapshot) and not intake.proposals(snapshot, candidate)
    assert {key: unit.bindings for key, unit in snapshot.units.items()} == original_bindings
    assert all(unit.attempt == 0 and unit.claim is None for unit in snapshot.units.values())
    assert {row.kind for row in snapshot.outbox} <= {
        "intake-baseline",
        "reasoning-launch",
        "reasoning-observe",
        "reasoning-apply",
    }
