"""Reasoning jobs use production claims, storage and native child lifecycle."""

import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest
from test_campaign_core import make_snapshot
from test_campaign_headless import prepared as prepared

from bmad_loop.campaign_jobs import accept_receipt, prepare, validate_result
from bmad_loop.campaign_model import CampaignError, ReasoningSpec, RoleSpec
from bmad_loop.campaign_runtime import CampaignRuntime
from bmad_loop.campaign_scheduler import admit
from bmad_loop.campaign_store import CampaignStore, add_event
from bmad_loop.campaign_supervisor import CampaignSupervisor


def case(tmp_path, prepared, count=2):
    runner, _, request, candidate, cap = prepared
    snapshot = make_snapshot()
    snapshot.generation = 1
    snapshot.spec = replace(
        snapshot.spec,
        reasoning=ReasoningSpec(enabled=True, purposes=(request.purpose,)),
        roles=(
            (
                "summarization",
                RoleSpec(
                    pool="summary-pool",
                    max_sessions=4,
                ),
            ),
        ),
        role_pools=(("summary-pool", (candidate,)),),
    )
    snapshot.config_snapshot["reasoning_capabilities"] = {
        candidate.profile + ":" + candidate.model: cap
    }
    snapshot.bindings["capacity"] = {
        "observed_at": 100,
        "observations": [
            {
                "profile": candidate.profile,
                "model": candidate.model,
                "capacity": {
                    "available": True,
                    "model_access": True,
                    "slots": 4,
                    "active": 0,
                    "observed_at": 100,
                    "expires_at": 10000,
                },
            }
        ],
    }
    unit = next(iter(snapshot.units))
    for index in range(count):
        prepare(snapshot, replace(request, id=f"job-{index}", role="summarization", unit=unit))
    store = CampaignStore(tmp_path / "campaigns")
    snapshot = store.create(snapshot)
    clock = [100]
    runtime = CampaignRuntime(tmp_path, store, clock=lambda: clock[0])
    runtime.reasoning_runner = runner
    supervisor = CampaignSupervisor(store, runtime, clock=lambda: clock[0], progress=lambda _: None)
    return snapshot, runtime, supervisor, clock


def test_two_jobs_share_a_unit_without_colliding_or_borrowing_its_writer_claim(tmp_path, prepared):
    snapshot, driver, supervisor, clock = case(tmp_path, prepared)
    proposals = driver.observe_workers(snapshot)
    assert len(proposals) == 2 and proposals[0].unit == proposals[1].unit
    snapshot = supervisor._queue(snapshot, proposals)
    assert len(snapshot.outbox) == 2
    assert len({row["claim"]["launch_id"] for row in snapshot.jobs.values()}) == 2
    assert all(unit.claim is None for unit in snapshot.units.values())
    assert len(admit(snapshot).units) == 2  # Two global sessions are reserved by jobs.
    with ThreadPoolExecutor(max_workers=4) as executor:
        for action in snapshot.outbox:
            snapshot = supervisor._submit(snapshot, action, executor, reconcile=False)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            snapshot = supervisor._harvest(snapshot)
            if all(row["state"] == "validated" for row in snapshot.jobs.values()):
                break
            clock[0] += 1
            proposals = tuple(
                p for p in driver.observe_workers(snapshot) if p.kind != "reasoning-apply"
            )
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
            pytest.fail(str([(r["state"], r["reason"]) for r in snapshot.jobs.values()]))
    assert all(row["receipt"]["input_complete"] for row in snapshot.jobs.values())
    assert all(row["claim"]["quiescent"] for row in snapshot.jobs.values())
    assert not driver.completion_ready(snapshot)  # Results still require semantic application.
    assert len(prepared[0].mux.processes) == 2


def test_terminal_jobs_leave_hot_authority_but_remain_queryable(tmp_path, prepared):
    snapshot, driver, supervisor, _ = case(tmp_path, prepared, count=1)
    snapshot.jobs["job-0"].update(state="canceled", application="canceled")
    add_event(snapshot, "job-canceled", job_id="job-0")
    driver.store.save(snapshot, snapshot.revision)
    assert driver.store.load(snapshot.id).jobs == {}
    rows = driver.store.jobs(snapshot.id, job_id="job-0")
    assert len(rows) == 1 and rows[0]["state"] == "canceled"
    restored = driver.store.load(snapshot.id)
    restored.jobs["job-0"] = rows[0]
    restored.jobs["job-0"]["state"] = "proposed"
    with pytest.raises(CampaignError, match="reactivate"):
        driver.store.save(restored, restored.revision)


def test_job_admission_selects_qualified_subset_of_role_pool(tmp_path, prepared):
    snapshot, driver, _, _ = case(tmp_path, prepared, count=1)
    candidate = prepared[3]
    unqualified = replace(candidate, model="unqualified-model")
    snapshot.spec = replace(snapshot.spec, role_pools=(("summary-pool", (unqualified, candidate)),))
    capacity = snapshot.bindings["capacity"]["observations"][0]
    snapshot.bindings["capacity"]["observations"].append({**capacity, "model": unqualified.model})
    proposals = driver.observe_workers(snapshot)
    assert len(proposals) == 1
    assert proposals[0].payload["candidate"]["model"] == candidate.model


def test_invalid_usage_does_not_partially_release_job_claim(tmp_path, prepared):
    snapshot, driver, supervisor, _ = case(tmp_path, prepared, count=1)
    snapshot = supervisor._queue(snapshot, driver.observe_workers(snapshot))
    row = snapshot.jobs["job-0"]
    before = snapshot.clone().jobs["job-0"]
    with pytest.raises(CampaignError, match="usage counters"):
        accept_receipt(
            snapshot,
            row["id"],
            {
                "job_id": row["id"],
                "request_digest": row["request_digest"],
                "launch_id": row["claim"]["launch_id"],
                "generation": row["claim"]["generation"],
                "quiescent": True,
                "outcome": "quota",
                "usage": {"input_tokens": -1},
            },
        )
    assert row == before


def test_job_and_effect_intent_commit_atomically(tmp_path, prepared, monkeypatch):
    from bmad_loop.campaign_database import CampaignDatabase

    snapshot, driver, supervisor, _ = case(tmp_path, prepared)
    write = CampaignDatabase._write_jobs

    def fail(connection, rows):
        write(connection, rows)
        raise OSError("commit interrupted")

    monkeypatch.setattr(CampaignDatabase, "_write_jobs", staticmethod(fail))
    with pytest.raises(OSError, match="interrupted"):
        supervisor._queue(snapshot, driver.observe_workers(snapshot))
    restored = driver.store.load(snapshot.id)
    assert restored.outbox == []
    assert all(
        row["state"] == "proposed" and row["claim"] is None for row in restored.jobs.values()
    )


def test_quota_without_json_preserves_usage_and_does_not_become_invalid_output(tmp_path, prepared):
    snapshot, driver, supervisor, clock = case(tmp_path, prepared, count=1)
    snapshot = supervisor._queue(snapshot, driver.observe_workers(snapshot))
    row = snapshot.jobs["job-0"]
    claim = row["claim"]
    receipt = {
        "job_id": row["id"],
        "request_digest": row["request_digest"],
        "launch_id": claim["launch_id"],
        "generation": claim["generation"],
        "quiescent": True,
        "outcome": "quota",
        "usage": {"input_tokens": 17},
    }
    accept_receipt(snapshot, row["id"], receipt, now=101)
    assert row["state"] == "capacity_wait" and row["validation"] == "pending"
    assert row["usage"] == {"input_tokens": 17}
    accept_receipt(snapshot, row["id"], receipt, now=102)
    assert row["usage"] == {"input_tokens": 17}


def test_late_predecessor_receipt_cannot_complete_new_attempt(tmp_path, prepared):
    snapshot, driver, supervisor, _ = case(tmp_path, prepared, count=1)
    snapshot = supervisor._queue(snapshot, driver.observe_workers(snapshot))
    row = snapshot.jobs["job-0"]
    with pytest.raises(CampaignError, match="owned session"):
        accept_receipt(
            snapshot,
            row["id"],
            {
                "job_id": row["id"],
                "request_digest": row["request_digest"],
                "launch_id": "older-launch",
                "generation": 0,
                "quiescent": True,
                "outcome": "completed",
                "input_complete": True,
                "result": {"digest": "x", "bytes": 1},
            },
        )
    assert row["receipt"] is None and row["state"] == "prepared"


@pytest.mark.parametrize("value", [{"answer": True}, {"answer": "x", "approval": True}, {}])
def test_schema_validation_rejects_type_confusion_and_invented_fields(value):
    with pytest.raises(CampaignError):
        validate_result(
            value,
            {
                "type": "object",
                "properties": {"answer": {"type": "string"}},
                "required": ["answer"],
            },
        )


@pytest.mark.parametrize("known_first", [False, True])
def test_runtime_reasoning_cost_is_idempotent_and_missing_cost_stays_unknown(
    tmp_path, prepared, known_first
):
    from bmad_loop import campaign_reasoning_flow
    from bmad_loop.campaign_supervisor import Observation

    snapshot, driver, supervisor, _ = case(tmp_path, prepared, count=2)
    snapshot = supervisor._queue(snapshot, driver.observe_workers(snapshot))
    for index, action in enumerate(snapshot.outbox):
        row = snapshot.jobs[action.payload["job_id"]]
        usage = {"input_tokens": 17}
        if known_first or index == 1:
            usage["cost_usd"] = 0.25
        receipt = {
            "job_id": row["id"],
            "request_digest": row["request_digest"],
            "launch_id": row["claim"]["launch_id"],
            "generation": row["claim"]["generation"],
            "quiescent": True,
            "outcome": "quota",
            "usage": usage,
        }
        observation = Observation("confirmed", {"state": "finished", "receipt": receipt})
        campaign_reasoning_flow.apply(driver, snapshot, action, observation)
        campaign_reasoning_flow.apply(driver, snapshot, action, observation)
    assert snapshot.bindings["reasoning_usage"]["totals"]["input_tokens"] == 34
    assert snapshot.bindings["reasoning_usage"]["complete_cost"] is known_first
    assert snapshot.bindings["spend_usd"] == (0.5 if known_first else None)
    assert snapshot.bindings["role_spend"]["summarization"] == (0.5 if known_first else None)
