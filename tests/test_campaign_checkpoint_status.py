"""Checkpoint status exposes backup debt without private worker evidence."""

import json
from types import SimpleNamespace

from test_campaign_checkpoint import case as case

from bmad_loop import campaign_checkpoint as checkpoint
from bmad_loop import campaign_cli
from bmad_loop.campaign_cli import status_document
from bmad_loop.campaign_model import (
    CampaignSnapshot,
    CampaignSpec,
    CampaignUnit,
    OutboxAction,
    UnitSpec,
    WorkerClaim,
)


def snapshot():
    unit = CampaignUnit(UnitSpec("unit", "source", "epic", "Unit"))
    state = CampaignSnapshot("campaign", "org/repo", CampaignSpec("checkpoint"), {unit.id: unit})
    return state, unit


def test_real_checkpoint_shape_exposes_current_progress_and_exact_backup_debt(
    case, tmp_path, monkeypatch
):
    repo, native, original_request, _, original_monitor = case
    request = {**original_request, "checkpoint_base": original_monitor.base}
    state, unit = snapshot()
    authority = tmp_path / "checkpoint-authority"
    directory = authority / "workers" / request["launch_id"]
    directory.mkdir(parents=True)
    (directory / "request.json").write_text(json.dumps(request))
    monitor = checkpoint.ProgressMonitor(request, directory, clock=lambda: 100.0)
    native.write_text("private native source changed after launch\n")
    observed = monitor.observe()
    state.outbox.append(
        OutboxAction(
            request["launch_id"],
            "launch",
            unit.id,
            {key: request[key] for key in ("checkpoint_base", "checkpoint_paths")},
            request["generation"],
        )
    )
    action = OutboxAction(
        "original-backup",
        "checkpoint",
        unit.id,
        {"progress": observed},
        0,
        state="sent_unknown",
        receipt={"backup_failures": 5},
        next_retry_at=140.0,
        error="private scope: /private/source-secret; prompt-private; ghp_secret-token",
    )
    state.outbox.append(action)
    unit.claim = WorkerClaim(unit.id, 2, 3, request["launch_id"])
    unit.bindings.update(
        workspace=str(repo),
        branch=request["branch"],
        last_pushed_head="prior-head",
        last_pushed_at=80.0,
    )
    runtime = SimpleNamespace(
        store=SimpleNamespace(directory=lambda _: authority),
        _pusher=lambda *args: SimpleNamespace(observations=lambda: ()),
    )
    monkeypatch.setattr(checkpoint.time, "time", lambda: 100.0)
    unit.evidence["checkpoint_progress"] = checkpoint.status(runtime, state, unit)
    unit.evidence["checkpoint_progress"].update(
        prompt="prompt-private", policy={"token": "private-policy"}
    )
    monkeypatch.setattr(campaign_cli.time, "time", lambda: 125.0)
    public = status_document(state)["units"][0]["checkpoint"]
    assert public["state"] == "observed"
    assert public["head"] == observed["head"]
    assert public["dirty"] and public["private_source_changed"]
    assert public["private_source_unbacked"]
    assert public["progress_age_s"] == 25.0 and public["dirty_age_s"] == 25.0
    assert public["last_backed_head"] == "prior-head" and public["backup_age_s"] == 45.0
    assert public["backup_retry_exhausted"]
    assert not public["backup_observation_unavailable"]
    debt = public["backup_debt"][0]
    assert debt["action_id"] == action.id and debt["head"] == observed["head"]
    assert debt["state"] == "sent_unknown" and debt["failures"] == 5 and debt["retry_at"] == 140.0
    assert debt["pending_age_s"] == 25.0 and debt["reason"]
    raw = json.dumps(public)
    for private in (
        "committed_paths",
        "commit_metadata",
        "private-plan.md",
        "source-secret",
        "prompt-private",
        "private-policy",
        "ghp_secret-token",
    ):
        assert private not in raw


def test_unavailable_checkpoint_stays_visible_without_leaking_failure_context(monkeypatch):
    state, unit = snapshot()
    unit.evidence["checkpoint_progress"] = {
        "state": "unavailable",
        "reason": "private prompt at /private/owner-notes; token=credential",
        "backup_debt": [],
        "backup_retry_exhausted": False,
    }
    monkeypatch.setattr(campaign_cli.time, "time", lambda: 125.0)
    public = status_document(state)["units"][0]["checkpoint"]
    assert public["state"] == "unavailable" and public["reason"]
    assert "dirty" not in public and "unbacked_committed" not in public
    assert public["progress_age_s"] is None and public["backup_age_s"] is None
    assert "owner-notes" not in json.dumps(public) and "credential" not in json.dumps(public)


def test_checkpoint_without_a_worker_receipt_is_not_reported_clean():
    state, _ = snapshot()
    public = status_document(state)["units"][0]["checkpoint"]
    assert public["state"] == "not_observed"
    assert "dirty" not in public and public["backup_age_s"] is None


def test_backup_observation_failure_cannot_appear_as_healthy_empty_debt():
    state, unit = snapshot()
    unit.evidence["checkpoint_progress"] = {
        "state": "observed",
        "backup_debt": [],
        "backup_observation_unavailable": "Failure at /private/owner-source with prompt-secret",
    }
    public = status_document(state)["units"][0]["checkpoint"]
    assert public["backup_debt"] == []
    assert public["backup_observation_unavailable"]
    assert public["backup_observation_reason"]
    assert "owner-source" not in json.dumps(public) and "prompt-secret" not in json.dumps(public)
