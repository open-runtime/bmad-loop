"""A slow but progressing landing repair is not a stalled production campaign."""

from dataclasses import replace

from test_campaign_supervisor import campaign_store as campaign_store

from bmad_loop.campaign_model import CampaignUnit, ResolutionSpec, UnitState


def test_bounded_landing_repair_gets_milestones_without_extending_initial_ceiling(campaign_store):
    from campaign_runtime_watchdog import ProgressWatchdog

    store, campaign_id = campaign_store
    snapshot = store.load(campaign_id)
    snapshot.spec = replace(
        snapshot.spec, resolution=ResolutionSpec(mode="bounded-auto", max_attempts_per_unit=1)
    )
    watch = ProgressWatchdog(snapshot, now=0, idle_s=10)
    total = watch.total_s
    parent = next(iter(snapshot.units.values()))
    parent.state = UnitState.MERGED_PENDING_SYNC
    assert watch.check(snapshot, now=9) == ""
    repair = CampaignUnit(
        replace(
            parent.spec, id="landing-repair:root:first", kind="landing_repair", parent=parent.id
        ),
        UnitState.READY,
    )
    snapshot.units[repair.id] = repair
    assert watch.check(snapshot, now=18) == ""
    repair.state = UnitState.RUNNING
    assert watch.check(snapshot, now=27) == ""
    assert repair.id in watch.units
    assert watch.total_s == total
    repair.state = UnitState.DELIVERED
    assert watch.check(snapshot, now=total) == f"terminal state within {total:g}s absolute ceiling"


def test_repeated_repairs_and_unbounded_graph_growth_cannot_buy_unbounded_wait(campaign_store):
    from campaign_runtime_watchdog import ProgressWatchdog

    store, campaign_id = campaign_store
    snapshot = store.load(campaign_id)
    snapshot.spec = replace(
        snapshot.spec, resolution=ResolutionSpec(mode="bounded-auto", max_attempts_per_unit=1)
    )
    watch = ProgressWatchdog(snapshot, now=0, idle_s=10)
    parent = next(iter(snapshot.units.values()))
    initial_budget = watch.unit_budget
    for index in range(initial_budget * 2):
        key = f"landing-repair:unexpected:{index:02d}"
        snapshot.units[key] = CampaignUnit(replace(parent.spec, id=key), UnitState.DELIVERED)
    assert watch.check(snapshot, now=1) == ""
    assert len(watch.units) == initial_budget
    for index in range(2, 11):
        key = f"landing-repair:later:{index}"
        snapshot.units[key] = CampaignUnit(replace(parent.spec, id=key), UnitState.DELIVERED)
        parent.failure_attempts += 1
        assert watch.check(snapshot, now=index) == ""
    assert watch.check(snapshot, now=11) == "new durable milestone within 10s"
    assert len(watch.units) == initial_budget
