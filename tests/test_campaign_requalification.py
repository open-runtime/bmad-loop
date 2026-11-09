"""A stopped startup can renew actual proof coordinates without changing authority."""

import copy
import json
from dataclasses import replace
from pathlib import Path

import pytest
from test_campaign_fidelity import proof as proof
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


def semantic_proof(directory, capability, proof, purposes):
    """Bind the zero-token semantic fixture through the real fidelity validator."""
    from bmad_loop.campaign_fidelity import route_binding, semantic_contract
    from bmad_loop.campaign_handoff_flow import AUDIT_SCHEMA
    from bmad_loop.campaign_headless import file_digest, read_json, write_json

    _, original = proof
    evidence = read_json(original)
    receipt = read_json(Path(evidence["independent_review"]["native_receipt_path"]))
    directory.mkdir()
    for index, source in enumerate(evidence["corpus"]):
        copied = directory / f"corpus-{index}.txt"
        copied.write_bytes(Path(source["source_path"]).read_bytes())
        source["source_path"] = str(copied)
    receipt["result"]["corpus_digest"] = digest(evidence["corpus"])
    bindings = [route_binding(capability, purpose) for purpose in purposes]
    evidence["route_bindings"] = bindings
    receipt["result"]["route_bindings_digest"] = digest(bindings)
    receipt["result"]["semantic_contract_digests"] = {}
    receipt["result"]["contract_schema_digests"] = {}
    for purpose in purposes:
        contract = (
            "constraints-v1"
            if purpose.startswith("constraint_")
            else "recovery-v1" if purpose.startswith("recovery_") else ""
        )
        semantic = semantic_contract(purpose, AUDIT_SCHEMA, contract)
        if contract:
            field = (
                "constraint_schemas_digest"
                if contract == "constraints-v1"
                else "recovery_schemas_digest"
            )
            evidence[field] = semantic["schemas_digest"]
            receipt["result"]["contract_schema_digests"][contract] = semantic["schemas_digest"]
        else:
            receipt["result"]["semantic_contract_digests"][purpose] = digest(semantic)
            if purpose == "handoff_audit":
                evidence["audit_prompt_version"] = semantic["prompt_version"]
                receipt["result"]["audit_prompt_version"] = semantic["prompt_version"]
    native = directory / "independent.json"
    write_json(native, receipt)
    evidence["independent_review"] = {
        "native_receipt_path": str(native),
        "sha256": file_digest(native),
    }
    path = directory / "fidelity.json"
    write_json(path, evidence)
    return {"schema_version": 2, "proofs": [{"path": str(path), "sha256": file_digest(path)}]}


def semantic_campaign(case, old_binding, new_binding, *, retain_transport=False):
    store, snapshot, template, manifest = case
    old = next(iter(snapshot.config_snapshot["reasoning_capabilities"].values()))
    old["semantic_qualification"] = old_binding
    snapshot.config_revision += 1
    snapshot.config_snapshot["configuration_revision"] = snapshot.config_revision
    template = replace(
        template,
        config_revision=snapshot.config_revision,
        directory=template.directory.with_name("semantic-configuration"),
    )
    path, sha = template.save()
    snapshot.bindings["execution_template"] = {"path": str(path), "digest": sha}
    snapshot = store.save(snapshot, snapshot.revision)
    content = json.loads(manifest.read_text())
    key = next(iter(content["routes"]))
    if retain_transport:
        content["routes"][key] = copy.deepcopy(old)
    content["routes"][key]["semantic_qualification"] = new_binding
    manifest.write_text(json.dumps(content))
    return store, snapshot, template, manifest


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


def test_legacy_inventory_budget_default_preserves_template_and_renews_normally(campaign):
    store, before, template, _ = campaign
    database = CampaignDatabase(store.directory(before.id))
    with database.connect(write=True) as connection:
        with connection:
            connection.execute(
                "UPDATE authority SET payload=json_remove(payload,"
                "'$.spec.github.inventory_metadata_bytes') WHERE id=1"
            )
    raw, _ = database.read()
    assert "inventory_metadata_bytes" not in raw["spec"]["github"]
    loaded = store.load(before.id)
    assert loaded.spec.github.inventory_metadata_bytes == 64 * 1024 * 1024
    assert loaded.spec == before.spec
    assert digest(loaded.config_snapshot) == digest(raw["config_snapshot"])
    binding = loaded.bindings["execution_template"]
    assert CampaignExecution.load(Path(binding["path"]), binding["digest"]) == template
    old_template = Path(binding["path"]).read_bytes()
    after = apply(campaign)
    assert after.phase == "stopped" and after.config_revision == before.config_revision + 1
    assert after.spec == loaded.spec and after.units == loaded.units
    assert after.config_snapshot["policy"] == loaded.config_snapshot["policy"]
    renewed = after.bindings["execution_template"]
    restored = CampaignExecution.load(Path(renewed["path"]), renewed["digest"])
    assert replace(restored, directory=template.directory, config_revision=1) == template
    assert Path(binding["path"]).read_bytes() == old_template
    saved, _ = database.read()
    assert saved["spec"]["github"]["inventory_metadata_bytes"] == 64 * 1024 * 1024


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


@pytest.mark.parametrize(
    "purposes",
    [
        ("handoff_map", "handoff_reduce", "handoff_audit"),
        ("constraint_reduce", "constraint_audit"),
        ("recovery_extract", "recovery_audit"),
    ],
)
def test_renewed_transport_accepts_fresh_proofs_for_same_semantic_contracts(
    campaign, proof, tmp_path, purposes
):
    old = next(iter(campaign[1].config_snapshot["reasoning_capabilities"].values()))
    new = next(iter(json.loads(campaign[3].read_text())["routes"].values()))
    assert old["qualification"]["sha256"] != new["qualification"]["sha256"]
    old_binding = semantic_proof(tmp_path / "old-semantics", old, proof, purposes)
    new_binding = semantic_proof(tmp_path / "new-semantics", new, proof, purposes)
    case = semantic_campaign(campaign, old_binding, new_binding)
    after = apply(case)
    assert after.phase == "stopped" and after.config_revision == case[1].config_revision + 1
    record = after.bindings["capability_requalifications"][0]
    previous = json.loads(Path(record["previous_capabilities_path"]).read_text())["routes"]
    renewed = json.loads(Path(record["capabilities_path"]).read_text())["routes"]
    assert next(iter(previous.values()))["semantic_qualification"] == old_binding
    assert next(iter(renewed.values()))["semantic_qualification"] == new_binding


@pytest.mark.parametrize("superseded", ["route", "contract"])
def test_unchanged_transport_retains_valid_semantics_among_superseded_proofs(
    campaign, proof, tmp_path, superseded
):
    from bmad_loop.campaign_headless import file_digest

    old = next(iter(campaign[1].config_snapshot["reasoning_capabilities"].values()))
    valid = semantic_proof(tmp_path / "valid", old, proof, ("handoff_map",))
    historical = semantic_proof(
        tmp_path / "historical",
        {**old, "cli_version": "superseded"} if superseded == "route" else old,
        proof,
        ("handoff_reduce",),
    )
    if superseded == "contract":
        path = Path(historical["proofs"][0]["path"])
        evidence = json.loads(path.read_text())
        evidence.pop("summary_job_schema_digest")
        path.write_text(json.dumps(evidence))
        historical["proofs"][0]["sha256"] = file_digest(path)
    binding = {"schema_version": 2, "proofs": historical["proofs"] + valid["proofs"]}
    case = semantic_campaign(campaign, binding, binding, retain_transport=True)
    assert apply(case).phase == "stopped"


@pytest.mark.parametrize("changed", ["transport", "corpus", "independent-review", "schema"])
def test_invalid_semantics_refuse_renewal_before_any_new_configuration(
    campaign, proof, tmp_path, changed
):
    old = next(iter(campaign[1].config_snapshot["reasoning_capabilities"].values()))
    binding = semantic_proof(tmp_path / "semantic", old, proof, ("handoff_map",))
    case = semantic_campaign(campaign, binding, binding, retain_transport=changed != "transport")
    if changed in {"corpus", "independent-review"}:
        evidence = json.loads(Path(binding["proofs"][0]["path"]).read_text())
        target = (
            evidence["corpus"][0]["source_path"]
            if changed == "corpus"
            else evidence["independent_review"]["native_receipt_path"]
        )
        Path(target).write_text("changed evidence", encoding="utf-8")
    elif changed == "schema":
        content = json.loads(case[3].read_text())
        next(iter(content["routes"].values()))["semantic_qualification"]["schema_version"] = 1
        case[3].write_text(json.dumps(content))
    with pytest.raises(CampaignError, match="[Ss]emantic|fidelity"):
        apply(case)
    assert case[0].load(case[1].id).revision == case[1].revision
    assert not (case[0].directory(case[1].id) / "configuration-revisions").exists()


@pytest.mark.parametrize("changed", ["expanded", "narrowed"])
def test_valid_new_semantics_cannot_change_frozen_purpose_authority(
    campaign, proof, tmp_path, changed
):
    old = next(iter(campaign[1].config_snapshot["reasoning_capabilities"].values()))
    new = next(iter(json.loads(campaign[3].read_text())["routes"].values()))
    original, replacement = ("handoff_map",), ("handoff_map", "handoff_reduce")
    if changed == "narrowed":
        original, replacement = replacement, original
    case = semantic_campaign(
        campaign,
        semantic_proof(tmp_path / "old", old, proof, original),
        semantic_proof(tmp_path / "new", new, proof, replacement),
    )
    with pytest.raises(CampaignError, match="frozen route authority or limits"):
        apply(case)
    assert case[0].load(case[1].id).revision == case[1].revision


@pytest.mark.parametrize("changed", ["corpus", "independent-review"])
def test_shared_corruption_cannot_silently_drop_one_frozen_semantic_purpose(
    campaign, proof, tmp_path, changed
):
    old = next(iter(campaign[1].config_snapshot["reasoning_capabilities"].values()))
    mapping = semantic_proof(tmp_path / "mapping", old, proof, ("handoff_map",))
    reduction = semantic_proof(tmp_path / "reduction", old, proof, ("handoff_reduce",))
    binding = {"schema_version": 2, "proofs": mapping["proofs"] + reduction["proofs"]}
    case = semantic_campaign(campaign, binding, binding, retain_transport=True)
    evidence = json.loads(Path(reduction["proofs"][0]["path"]).read_text())
    target = (
        evidence["corpus"][0]["source_path"]
        if changed == "corpus"
        else evidence["independent_review"]["native_receipt_path"]
    )
    Path(target).write_text("changed only the reduction proof", encoding="utf-8")
    with pytest.raises(CampaignError, match="[Ss]emantic|fidelity"):
        apply(case)
    assert case[0].load(case[1].id).revision == case[1].revision
    assert not (case[0].directory(case[1].id) / "configuration-revisions").exists()


def test_fresh_proof_can_replace_unavailable_old_closure_without_losing_authority(
    campaign, proof, tmp_path
):
    old = next(iter(campaign[1].config_snapshot["reasoning_capabilities"].values()))
    new = next(iter(json.loads(campaign[3].read_text())["routes"].values()))
    purposes = ("handoff_map", "handoff_reduce")
    original = semantic_proof(tmp_path / "old", old, proof, purposes)
    fresh = semantic_proof(tmp_path / "new", new, proof, purposes)
    case = semantic_campaign(campaign, original, fresh)
    evidence = json.loads(Path(original["proofs"][0]["path"]).read_text())
    Path(evidence["corpus"][0]["source_path"]).unlink()
    Path(evidence["independent_review"]["native_receipt_path"]).unlink()
    assert apply(case).config_revision == case[1].config_revision + 1


def test_changed_old_declaration_cannot_authorize_narrowed_replacement(campaign, proof, tmp_path):
    old = next(iter(campaign[1].config_snapshot["reasoning_capabilities"].values()))
    new = next(iter(json.loads(campaign[3].read_text())["routes"].values()))
    original = semantic_proof(tmp_path / "old", old, proof, ("handoff_map", "handoff_reduce"))
    fresh = semantic_proof(tmp_path / "new", new, proof, ("handoff_map",))
    case = semantic_campaign(campaign, original, fresh)
    path = Path(original["proofs"][0]["path"])
    evidence = json.loads(path.read_text())
    evidence["route_bindings"] = evidence["route_bindings"][:1]
    path.write_text(json.dumps(evidence))
    with pytest.raises(CampaignError, match="declaration changed"):
        apply(case)
    assert case[0].load(case[1].id).revision == case[1].revision
