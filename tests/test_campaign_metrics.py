"""Timing survives restarts without changing scheduler or worker authority."""

from bmad_loop.campaign_metrics import document, observe_admission, observe_effect
from bmad_loop.campaign_model import (
    CampaignSnapshot,
    CampaignSpec,
    CampaignUnit,
    OutboxAction,
    UnitSpec,
    UnitState,
    WorkerClaim,
)
from bmad_loop.campaign_store import CampaignStore, add_event


def snapshot():
    first = CampaignUnit(UnitSpec("s:e:a", "s", "e", "A", scopes=("src/a",)), UnitState.RUNNING)
    first.claim = WorkerClaim(first.id, 1, 1, "worker-a")
    second = CampaignUnit(UnitSpec("s:e:b", "s", "e", "B", scopes=("src/b",)), UnitState.READY)
    return CampaignSnapshot(
        "timings",
        "org/repo",
        CampaignSpec("timings", max_workers=1, units=(first.spec, second.spec)),
        {first.id: first, second.id: second},
        created_at=90,
    )


def test_admission_intervals_survive_restart_without_sampling_duplicates(tmp_path):
    store = CampaignStore(tmp_path / "state", clock=lambda: 100)
    state = store.create(snapshot())
    assert observe_admission(state, now=100)
    state = store.save(state, state.revision)
    event_count = len(state.events)
    assert not observe_admission(state, now=105)
    assert len(state.events) == event_count
    state = store.load(state.id)
    assert document(state, now=110)["admission_wait_s"]["execution capacity"] == 10
    state.units["s:e:a"].claim.quiescent = True
    assert observe_admission(state, now=115)
    timing = document(state, now=120)["units"]["s:e:b"]
    assert timing["admission_wait_s"] == {"execution capacity": 15}
    assert timing["admission_delay_s"] == 25
    assert state.units["s:e:b"].state == UnitState.READY
    assert state.units["s:e:b"].claim is None


def test_effect_retry_debt_does_not_occupy_a_worker_or_double_count():
    state = snapshot()
    action = OutboxAction("check", "prepare-merge", "s:e:b", {}, 1)
    observe_effect(state, action, state="waiting", data={}, now=100)
    observe_effect(state, action, state="unknown", data={}, now=105)
    assert document(state, now=112)["effect_wait_s"] == {"prepare-merge": 12}
    observe_effect(state, action, state="confirmed", data={}, now=115)
    observe_effect(state, action, state="confirmed", data={}, now=120)
    assert document(state, now=125)["effect_wait_s"] == {"prepare-merge": 15}
    assert state.units["s:e:b"].claim is None


def test_real_worker_receipt_timing_is_idempotent_and_historical_duration_unknown():
    state = snapshot()
    action = OutboxAction("observed", "observe-worker", "s:e:a", {"launch_id": "worker-a"}, 1)
    data = {"state": "finished", "receipt": {"stage": "review", "finished_at": 130}}
    observe_effect(state, action, state="confirmed", data=data, now=150)
    assert document(state, now=150)["units"][action.unit]["stages"] == []
    data["receipt"]["started_at"] = 110
    observe_effect(state, action, state="confirmed", data=data, now=150)
    observe_effect(state, action, state="confirmed", data=data, now=160)
    rows = document(state, now=160)["units"][action.unit]["stages"]
    assert len(rows) == 1 and rows[0]["duration_s"] == 20
    assert not state.units[action.unit].claim.quiescent


def test_verification_wait_is_visible_without_consuming_coding_capacity():
    state = snapshot()
    first, second = state.units.values()
    first.claim.resource = "verify"
    second.state = UnitState.SYNCING
    second.evidence.update({"integrated": {"head": "head"}, "pushed": "head"})
    assert observe_admission(state, now=100)
    result = document(state, now=108)
    assert result["admission_wait_s"] == {"verify capacity": 8}
    assert result["resources"]["occupied"] == {"verify": 1}
    assert result["resources"]["limits"]["execution"] == 1


def test_event_clock_is_persisted_with_authority_and_old_prefix_stays_exact(tmp_path):
    clock = [100]
    store = CampaignStore(tmp_path / "state", clock=lambda: clock[0])
    state = snapshot()
    add_event(state, "created")
    state = store.create(state)
    first = dict(state.events[0])
    assert first["observed_at"] == 90
    clock[0] = 120
    add_event(state, "later")
    state = store.save(state, state.revision)
    assert state.events[0] == first
    assert state.events[1]["observed_at"] == 120
    assert store.load(state.id).events == state.events
