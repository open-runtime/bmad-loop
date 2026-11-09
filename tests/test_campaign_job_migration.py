"""Explicit storage migration accounts for reasoning ownership and cold history."""

import pytest
from test_campaign_headless import prepared as prepared
from test_campaign_reasoning_jobs import case

from bmad_loop.campaign_model import CampaignError
from bmad_loop.campaign_store import CampaignStore


def test_json_migration_refuses_uncertain_reasoning_owner(tmp_path, prepared, project):
    snapshot, runtime, supervisor, _ = case(tmp_path, prepared, count=1)
    snapshot = supervisor._queue(snapshot, runtime.observe_workers(snapshot))
    # Prepared process ownership is independent of the outbound intent list.
    snapshot.outbox.clear()
    snapshot.events = list(snapshot.events)
    snapshot.common_dir = str(project.project / ".git")
    legacy = CampaignStore(tmp_path / "legacy", storage="json")
    snapshot = legacy.create(snapshot)
    with pytest.raises(CampaignError, match="quiescent reasoning jobs"):
        legacy.migrate_state(snapshot.id)
    assert not (legacy.directory(snapshot.id) / "campaign.sqlite").exists()


def test_json_migration_preserves_terminal_job_history(tmp_path, prepared, project):
    snapshot, _, _, _ = case(tmp_path, prepared, count=1)
    snapshot.jobs["job-0"].update(state="canceled", application="canceled")
    snapshot.events = list(snapshot.events)
    snapshot.common_dir = str(project.project / ".git")
    legacy = CampaignStore(tmp_path / "legacy", storage="json")
    snapshot = legacy.create(snapshot)
    migrated = legacy.migrate_state(snapshot.id)
    assert migrated.jobs == {}
    assert legacy.jobs(snapshot.id, job_id="job-0") == [snapshot.jobs["job-0"]]
