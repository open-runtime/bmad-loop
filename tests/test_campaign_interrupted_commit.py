"""An uncertain coordinator save is reconciled from authority on interruption."""

import pytest
from test_campaign_supervisor import PipelineDriver
from test_campaign_supervisor import campaign_store as campaign_store

from bmad_loop.campaign_supervisor import CampaignSupervisor


@pytest.mark.parametrize("committed", [False, True])
def test_interrupt_at_save_boundary_preserves_only_durable_intents(
    campaign_store, monkeypatch, committed
):
    store, campaign_id = campaign_store
    save = store.save
    interrupted = False

    def uncertain(snapshot, revision, **kwargs):
        nonlocal interrupted
        if snapshot.outbox and not interrupted:
            interrupted = True
            if committed:
                save(snapshot, revision, **kwargs)
            raise KeyboardInterrupt
        return save(snapshot, revision, **kwargs)

    monkeypatch.setattr(store, "save", uncertain)
    driver = PipelineDriver(store)
    with pytest.raises(KeyboardInterrupt):
        CampaignSupervisor(store, driver, progress=lambda _: None).run(campaign_id, max_ticks=5)
    result = store.load(campaign_id)
    assert result.phase == "draining"
    assert len(result.outbox) == (2 if committed else 0)
    assert all(action.state == "prepared" for action in result.outbox)
    assert not driver.performed
    assert result.events[-1]["kind"] == "coordinator-interrupted"
