"""Batch submission amortizes authority writes without sharing worker state."""

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from test_campaign_supervisor import PipelineDriver
from test_campaign_supervisor import campaign_store as campaign_store

from bmad_loop.campaign_supervisor import CampaignSupervisor


def test_batch_commits_all_selected_intents_once_before_isolated_workers(campaign_store):
    store, key = campaign_store
    barrier = threading.Barrier(2)
    received = []

    class Isolated(PipelineDriver):
        def perform(self, snapshot, action):
            durable = store.load(key)
            assert all(row.state == "sent_unknown" for row in durable.outbox)
            snapshot.bindings["worker"] = action.id
            received.append(snapshot)
            barrier.wait(timeout=5)
            assert snapshot.bindings["worker"] == action.id
            return super().perform(snapshot, action)

    driver = Isolated(store)
    supervisor = CampaignSupervisor(store, driver, progress=lambda _: None)
    state = store.load(key)
    state = supervisor._queue(state, driver.proposals(state))
    revision = state.revision
    with ThreadPoolExecutor(max_workers=2) as pool:
        state = supervisor._dispatch_ready(state, pool)
        for future in supervisor._active.values():
            future.result(timeout=5)
    assert state.revision == revision + 1
    assert len(received) == 2 and received[0] is not received[1]
    assert "worker" not in state.bindings
    assert "worker" not in store.load(key).bindings


def test_lost_batch_save_ack_submits_nothing_and_preserves_uncertain_intents(
    campaign_store, monkeypatch
):
    store, key = campaign_store
    driver = PipelineDriver(store)
    supervisor = CampaignSupervisor(store, driver, progress=lambda _: None)
    state = store.load(key)
    state = supervisor._queue(state, driver.proposals(state))
    save = store.save

    def interrupted(*args, **kwargs):
        save(*args, **kwargs)
        raise OSError("ack lost after commit")

    monkeypatch.setattr(store, "save", interrupted)
    with ThreadPoolExecutor(max_workers=2) as pool:
        with pytest.raises(OSError, match="ack lost"):
            supervisor._dispatch_ready(state, pool)
    assert driver.performed == []
    assert all(row.state == "sent_unknown" for row in store.load(key).outbox)
