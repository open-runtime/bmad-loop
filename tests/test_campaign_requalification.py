"""A stopped startup can renew actual proof coordinates without changing authority."""

import copy
import json
from dataclasses import replace
from pathlib import Path

import pytest
from test_campaign_headless import fixture_capability
from test_campaign_headless import prepared as prepared

from bmad_loop import campaign_requalification as renewal
from bmad_loop import policy, runsetup
from bmad_loop.campaign_database import CampaignDatabase
from bmad_loop.campaign_execution import CampaignExecution
from bmad_loop.campaign_jobs import digest, prepare
from bmad_loop.campaign_model import (
    CampaignError,
    CampaignSnapshot,
    CampaignSpec,
    CampaignUnit,
    Hold,
    ReasoningSpec,
    UnitSpec,
    WorkerClaim,
)
from bmad_loop.campaign_store import CampaignStore, RevisionConflict, add_action


@pytest.fixture
def campaign(project, tmp_path, prepared, monkeypatch):
    _, _, request, candidate, original = prepared
    unit = UnitSpec("s:project:u", "s", "epic", "Story", acceptance=("Keep all obligations",))
    pol = policy.Policy()
    store = CampaignStore(tmp_path / "campaigns")
    snapshot = CampaignSnapshot(
        "startup",
        "owner/repo",
        CampaignSpec("Startup", units=(unit,), reasoning=ReasoningSpec(enabled=True)),
        {unit.id: CampaignUnit(unit)},
        phase="stopped",
        common_dir=str((project.project / ".git").resolve()),
        root=str(project.project),
        config_snapshot={
            "policy": pol.to_dict(),
            "configuration_revision": 1,
            "reasoning_capabilities": {candidate.profile + ":" + candidate.model: original},
        },
    )
    snapshot.units[unit.id].hold = Hold("operator", "Keep this hold", actor="owner")
    prepare(snapshot, replace(request, required=False, unit=unit.id))
    snapshot = store.create(snapshot)
    template = CampaignExecution(
        snapshot.id,
        "configuration",
        0,
        0,
        1,
        store.directory(snapshot.id) / "configuration",
        pol,
        runsetup.resolve_profiles(pol, project.project),
    )
    path, sha = template.save()
    snapshot.bindings["execution_template"] = {"path": str(path), "digest": sha}
    snapshot = store.save(snapshot, snapshot.revision)
    proposal = json.loads(
        Path(original["qualification"]["path"]).with_name("request.json").read_text()
    )["capability"]
    fresh = fixture_capability(tmp_path / "fresh", proposal, candidate)
    manifest = tmp_path / "renewed-capabilities.json"
    manifest.write_text(
        json.dumps(
            {"schema_version": 1, "routes": {candidate.profile + ":" + candidate.model: fresh}}
        )
    )
    monkeypatch.setattr(
        renewal,
        "installation",
        lambda: {"qualified": True, "commit": "a" * 40, "installation_kind": "git"},
    )
    return store, snapshot, template, manifest


def apply(case, **kwargs):
    store, snapshot, _, manifest = case
    return renewal.requalify(
        store,
        snapshot.id,
        manifest,
        expected_revision=kwargs.pop("expected_revision", snapshot.revision),
        reason="Install reviewed startup repair",
        **kwargs,
    )


def test_renewal_preserves_configuration_authority_and_history_without_resuming(campaign):
    store, before, template, manifest = campaign
    old_path = Path(before.bindings["execution_template"]["path"])
    original_bytes = old_path.read_bytes()
    old_request = copy.deepcopy(next(iter(before.jobs.values()))["request"])
    after = apply(campaign)
    assert after.phase == "stopped" and after.generation == before.generation
    assert after.config_revision == 2 and after.revision == before.revision + 1
    assert after.spec == before.spec and after.units == before.units
    assert after.config_snapshot["policy"] == before.config_snapshot["policy"]
    assert old_path.read_bytes() == original_bytes
    binding = after.bindings["execution_template"]
    new_template = CampaignExecution.load(Path(binding["path"]), binding["digest"])
    assert replace(new_template, directory=template.directory, config_revision=1) == template
    record = after.bindings["capability_requalifications"][0]
    assert record["previous_template"] == before.bindings["execution_template"]
    assert record["capabilities_digest"] == digest(json.loads(manifest.read_text())["routes"])
    assert record["installation"]["commit"] == "a" * 40
    assert (
        json.loads(Path(record["previous_capabilities_path"]).read_text())["routes"]
        == before.config_snapshot["reasoning_capabilities"]
    )
    assert (
        json.loads(Path(record["capabilities_path"]).read_text())["routes"]
        == after.config_snapshot["reasoning_capabilities"]
    )
    rows = store.jobs(after.id)
    assert len(rows) == 1 and rows[0]["state"] == "stale"
    assert rows[0]["request"] == old_request and rows[0]["attempt"] == 0
    assert rows[0]["claim"] is None
    assert any(row["kind"] == "campaign-capabilities-requalified" for row in after.events)


@pytest.mark.parametrize(
    "fault",
    [
        "active",
        "unit-claim",
        "attempt",
        "workspace",
        "pr",
        "proof",
        "pending-effect",
        "uncertain-effect",
        "job-claim",
        "job-attempt",
        "required-job",
    ],
)
def test_started_or_unsettled_work_is_refused_without_replacing_snapshot(campaign, fault):
    store, snapshot, template, manifest = campaign
    unit = next(iter(snapshot.units.values()))
    job = next(iter(snapshot.jobs.values()))
    if fault == "active":
        snapshot.phase = "active"
    elif fault == "unit-claim":
        unit.claim = WorkerClaim(unit.id, 1, 1, "launch")
    elif fault == "attempt":
        unit.attempt = 1
    elif fault in {"workspace", "pr"}:
        unit.bindings[fault] = "existing"
    elif fault == "proof":
        unit.evidence["verification"] = {"ok": True}
    elif fault in {"pending-effect", "uncertain-effect"}:
        action = add_action(snapshot, "inventory", "", {})
        if fault == "uncertain-effect":
            action.state = "sent_unknown"
    elif fault == "job-claim":
        job["claim"] = {"launch_id": "launch", "generation": 1, "quiescent": False}
    elif fault == "job-attempt":
        job["attempt"] = 1
    else:
        job["request"]["required"] = True
        job["request_digest"] = digest(job["request"])
        # Requests are immutable after first persistence; create another original campaign.
        snapshot.id = "required-job"
        snapshot.bindings.clear()
        store.create(snapshot)
        snapshot = store.load(snapshot.id)
    if fault != "required-job":
        snapshot = store.save(snapshot, snapshot.revision)
    revision = snapshot.revision
    with pytest.raises(CampaignError, match="requires|cannot invalidate"):
        renewal.requalify(
            store, snapshot.id, manifest, expected_revision=revision, reason="Explicit"
        )
    after = store.load(snapshot.id)
    assert after.revision == revision and after.config_revision == 1
    assert not (store.directory(snapshot.id) / "configuration-revisions").exists()


def test_stale_revision_and_unknown_installation_are_refused(campaign, monkeypatch):
    store, before, _, _ = campaign
    with pytest.raises(RevisionConflict):
        apply(campaign, expected_revision=before.revision - 1)
    monkeypatch.setattr(renewal, "installation", lambda: {"qualified": False, "commit": ""})
    with pytest.raises(CampaignError, match="immutable loaded installation"):
        apply(campaign)
    assert store.load(before.id).revision == before.revision


def test_clean_editable_installation_is_not_an_immutable_git_release(campaign, monkeypatch):
    monkeypatch.setattr(
        renewal,
        "installation",
        lambda: {"qualified": True, "commit": "a" * 40, "installation_kind": "editable"},
    )
    with pytest.raises(CampaignError, match="immutable loaded installation"):
        apply(campaign)


@pytest.mark.parametrize("quiescent", [False, None])
def test_cold_terminal_sqlite_claim_cannot_be_hidden_by_hot_snapshot(campaign, quiescent):
    store, snapshot, _, _ = campaign
    database = CampaignDatabase(store.directory(snapshot.id))
    with database.connect(write=True) as connection:
        with connection:
            connection.execute(
                "UPDATE jobs SET state='stale',payload=json_set(payload,'$.state','stale',"
                "'$.claim',json(?))",
                (
                    json.dumps(
                        {"launch_id": "unconfirmed", "generation": 1, "quiescent": quiescent}
                    ),
                ),
            )
    assert store.load(snapshot.id).jobs == {}
    with pytest.raises(CampaignError, match="reasoning job|reasoning claim"):
        apply(campaign)
    assert store.load(snapshot.id).revision == snapshot.revision


def test_real_qualification_validation_refuses_changed_evidence(campaign):
    store, before, _, manifest = campaign
    content = json.loads(manifest.read_text())
    capability = next(iter(content["routes"].values()))
    capability["qualification"]["sha256"] = "0" * 64
    manifest.write_text(json.dumps(content))
    with pytest.raises(CampaignError, match="qualification evidence changed"):
        apply(campaign)
    assert store.load(before.id).revision == before.revision


def test_valid_but_different_qualified_limits_cannot_change_authority(campaign):
    store, before, _, manifest = campaign
    content = json.loads(manifest.read_text())
    capability = next(iter(content["routes"].values()))
    capability["output_tokens"] -= 1  # Legitimate narrowed proof, but a different frozen budget.
    manifest.write_text(json.dumps(content))
    with pytest.raises(CampaignError, match="frozen route authority or limits"):
        apply(campaign)
    assert store.load(before.id).revision == before.revision


def test_crash_before_cas_keeps_original_template_authoritative_and_retryable(
    campaign, monkeypatch
):
    store, before, template, _ = campaign
    save = store.save

    def interrupted(*args, **kwargs):
        raise OSError("injected before CAS")

    monkeypatch.setattr(store, "save", interrupted)
    with pytest.raises(OSError, match="before CAS"):
        apply(campaign)
    current = store.load(before.id)
    assert current.revision == before.revision and current.config_revision == 1
    assert current.bindings["execution_template"] == before.bindings["execution_template"]
    monkeypatch.setattr(store, "save", save)
    assert apply(campaign).config_revision == 2


def test_existing_previous_route_archive_is_never_replaced(campaign):
    store, snapshot, _, _ = campaign
    archive = store.directory(snapshot.id) / "configuration-revisions/2/previous-capabilities.json"
    archive.parent.mkdir(parents=True)
    archive.write_text("preserved unexpected content", encoding="utf-8")
    with pytest.raises(CampaignError, match="cannot replace preserved"):
        apply(campaign)
    assert archive.read_text(encoding="utf-8") == "preserved unexpected content"
    assert store.load(snapshot.id).revision == snapshot.revision


def test_coordinator_lock_refuses_concurrent_requalification(campaign):
    store, before, _, _ = campaign
    with store.coordinator(before.common_dir, before.spec.target, campaign_id=before.id):
        with pytest.raises(OSError):
            apply(campaign)
    assert store.load(before.id).revision == before.revision
