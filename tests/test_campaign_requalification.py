"""A stopped startup can renew actual proof coordinates without changing authority."""

import copy
import json
from dataclasses import asdict, replace
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


@pytest.fixture
def allocated_campaign(campaign, tmp_path, monkeypatch, request):
    """Earn allocation receipts with production preparation and a real Git checkout."""
    from conftest import git
    from test_campaign_paradev import Host
    from test_campaign_runtime_intake import observed

    from bmad_loop import campaign_paradev, campaign_runtime, campaign_runtime_intake
    from bmad_loop.campaign_intake import IntakeBatch
    from bmad_loop.campaign_model import ParadevSpec, UnitState
    from bmad_loop.campaign_supervisor import Observation

    native, intake = getattr(request, "param", (False, False))
    store, snapshot, template, manifest = campaign
    repo = Path(snapshot.root)
    remote = tmp_path / "allocation-remote.git"
    git(repo, "clone", "--bare", str(repo), str(remote))
    git(repo, "remote", "add", "origin", str(remote))
    root = tmp_path / "allocation-worktrees"
    root.mkdir()
    host = Host(repo, root)
    monkeypatch.setattr(campaign_paradev, "Host", lambda: host)
    unit = next(iter(snapshot.units.values()))
    unit.spec = replace(unit.spec, kind="planning")
    snapshot.graph_revision += 1
    snapshot.spec = replace(
        snapshot.spec,
        units=(unit.spec,),
        worktree_root=str(root),
        paradev=ParadevSpec(reflect=native, project_id="project", worktree_root=str(root)),
    )
    snapshot.config_revision += 1
    snapshot.config_snapshot["configuration_revision"] = snapshot.config_revision
    template = replace(
        template,
        config_revision=snapshot.config_revision,
        directory=template.directory.with_name("allocation-config"),
    )
    path, sha = template.save()
    snapshot.bindings["execution_template"] = {"path": str(path), "digest": sha}
    runtime = campaign_runtime.CampaignRuntime(repo, store)
    if intake:
        snapshot.bindings["inventory"] = {"complete": True, "issues": [], "pulls": []}
        observation = replace(
            observed(),
            repository=snapshot.repository,
            url=f"https://github.com/{snapshot.repository}/issues/7",
        )
        campaign_runtime_intake.apply_poll(
            snapshot,
            {
                "batch": asdict(IntakeBatch((observation,), observation.updated_at, True, 0)),
                "observed_at": 100,
            },
        )
        payload = {"packet_id": "a" * 64, "input_versions": [observation.version_id]}
        action = add_action(snapshot, "intake-control", "", payload)
        runtime.apply(snapshot, action, Observation("confirmed", payload))
        action.state, action.receipt = "confirmed", payload
        unit = snapshot.units["campaign:" + snapshot.id + ":intake:" + "a" * 16]
    unit.state = UnitState.PLANNING
    action = add_action(snapshot, "prepare-unit", unit.id, {})
    snapshot = store.save(snapshot, snapshot.revision)
    action = next(row for row in snapshot.outbox if row.id == action.id)
    result = runtime._perform_prepare_unit(snapshot, action)
    assert result.state == "confirmed"
    runtime.apply(snapshot, action, result)
    action.state, action.receipt = "confirmed", result.data
    snapshot = store.save(snapshot, snapshot.revision)
    return (store, snapshot, template, manifest), unit.id, action.id, host


@pytest.mark.parametrize(
    "allocated_campaign", [(False, False), (True, False), (True, True)], indirect=True
)
def test_confirmed_bootstrap_allocation_renews_without_recreating_or_accepting_work(
    allocated_campaign,
):
    from conftest import git

    from bmad_loop.campaign_model import UnitState

    case, key, action_id, host = allocated_campaign
    store, before, _, _ = case
    unit = before.units[key]
    original = copy.deepcopy(unit)
    calls = copy.deepcopy(host.calls)
    head = git(Path(unit.bindings["workspace"]), "rev-parse", "HEAD")
    after = apply(case)
    assert after.units[key] == original and after.units[key].state == UnitState.DRAFT
    assert after.phase == "stopped" and after.config_revision == before.config_revision + 1
    assert (
        host.calls == calls and git(Path(unit.bindings["workspace"]), "rev-parse", "HEAD") == head
    )
    assert after.units[key].attempt == 0 and after.units[key].claim is None
    assert not after.units[key].bindings.get("pr")
    from bmad_loop.campaign_action_history import action

    assert action(after, action_id).receipt == action(before, action_id).receipt
    assert store.load(after.id).units == after.units


@pytest.mark.parametrize("allocated_campaign", [(True, False)], indirect=True)
@pytest.mark.parametrize(
    "fault", [None, "unconfirmed", "receipt", "native", "stage", "extra-history"]
)
def test_prepared_reflection_requires_exact_confirmed_unstarted_projection(
    allocated_campaign, fault
):
    from bmad_loop.campaign_runtime import CampaignRuntime

    case, key, _, _ = allocated_campaign
    store, before, template, manifest = case
    runtime = CampaignRuntime(Path(before.root), store)
    action = add_action(before, "reflection", key, {})
    result = runtime._perform_reflection(before, action)
    assert result.state == "confirmed"
    assert result.data["stage"] == {"state": "pending", "windows": []}
    assert result.data["lifecycle"] == {
        "state": "pending",
        "detail": "No stage claim",
        "windows": [],
    }
    runtime.apply(before, action, result)
    action.state, action.receipt = "confirmed", copy.deepcopy(result.data)
    unit = before.units[key]
    if fault == "unconfirmed":
        action.state = "rejected"
    elif fault == "receipt":
        unit.bindings["reflection"]["fingerprint"] = "unconfirmed-change"
    elif fault in {"native", "stage"}:
        changed = unit.bindings["reflection"]
        if fault == "native":
            changed["workspace"]["task_id"] = "unconfirmed-task"
        else:
            changed["stage"] = {"state": "active", "windows": [{"window_id": "unexpected"}]}
        action.receipt = copy.deepcopy(changed)
    elif fault == "extra-history":
        unit.bindings["reflection_history"] = [copy.deepcopy(result.data)]
    original = copy.deepcopy(unit)
    before = store.save(before, before.revision)
    current = (store, before, template, manifest)
    if fault is not None:
        with pytest.raises(CampaignError, match="reflection|bindings differ"):
            apply(current)
        assert store.load(before.id).revision == before.revision
    else:
        after = apply(current)
        assert after.units[key] == original
        assert after.units[key].bindings["native"]["task_id"] is None
        assert after.config_revision == before.config_revision + 1


@pytest.fixture
def bootstrap_tracking(allocated_campaign):
    """Run real hierarchy production, queueing and fake remote create receipts."""
    from test_campaign_tracking import HierarchyTransport

    from bmad_loop.campaign_github import GithubGateway, MutationIntent
    from bmad_loop.campaign_runtime import CampaignRuntime
    from bmad_loop.campaign_supervisor import Observation

    case, key, _, _ = allocated_campaign
    store, state, template, manifest = case

    class Transport(HierarchyTransport):
        def request(self, method, path, payload=None):
            result = super().request(
                method, path.replace("repos/owner/repo/", "repos/a/b/"), payload
            )
            for row in (*self.issues.values(), *self.milestones.values()):
                row["html_url"] = row["html_url"].replace(
                    "github.com/a/b/", "github.com/owner/repo/"
                )
            return result

    transport = Transport()
    gateway = GithubGateway(transport, state.repository)
    runtime = CampaignRuntime(Path(state.root), store, gateway=gateway)
    # Milestone creation, its read-only projection, then epic creation. The
    # second confirmed receipt is intentionally not yet projected into bindings.
    for _ in range(3):
        action = add_action(state, "hierarchy", key, {})
        result = runtime._perform_hierarchy(state, action)
        runtime.apply(state, action, result)
        action.state, action.receipt = result.state, copy.deepcopy(result.data)
        if result.data["intent"]:
            effect = next(row for row in state.outbox if row.id == result.data["action_key"])
            intent = MutationIntent.from_dict(effect.payload["intent"])
            confirmed = gateway.execute(
                replace(intent, state="sent_unknown"), generation=state.generation
            )
            observed = Observation("confirmed", {"intent": confirmed.to_dict()})
            runtime.apply(state, effect, observed)
            effect.state, effect.receipt = observed.state, copy.deepcopy(observed.data)
    state = store.save(state, state.revision)
    assert len(transport.effects) == 2
    assert state.units[key].bindings["hierarchy_ready"] is False
    return (store, state, template, manifest), key, runtime, transport


def test_bootstrap_tracking_renewal_preserves_confirmed_and_unapplied_creates(bootstrap_tracking):
    from bmad_loop.campaign_action_history import actions

    case, key, _, transport = bootstrap_tracking
    store, before, _, _ = case
    original = copy.deepcopy(before.units[key])
    hierarchy = copy.deepcopy(before.bindings["github_hierarchy"])
    history = [asdict(row) for row in actions(before, kinds=("hierarchy", "github"))]
    calls = copy.deepcopy(transport.effects)
    after = apply(case)
    assert after.phase == "stopped" and after.units[key] == original
    assert after.bindings["github_hierarchy"] == hierarchy
    assert [asdict(row) for row in actions(after, kinds=("hierarchy", "github"))] == history
    assert transport.effects == calls
    assert after.config_revision == before.config_revision + 1
    assert store.load(after.id).units == after.units


@pytest.mark.parametrize(
    "fault",
    [
        "unknown",
        "rejected",
        "unlinked",
        "payload",
        "method",
        "pr",
        "generation",
        "native-id",
        "foreign",
        "marker",
        "projection-version",
        "graph",
        "complete",
        "unit-changes",
        "read-fence",
        "unproved-binding",
        "ready",
        "missing-receipt",
    ],
)
def test_bootstrap_tracking_renewal_refuses_unproved_effects(bootstrap_tracking, fault):
    from bmad_loop.campaign_action_history import actions

    case, key, _, _ = bootstrap_tracking
    store, state, template, manifest = case
    history = actions(state, kinds=("hierarchy", "github"))
    # Update detached archived rows through the hot overlay, preserving the real
    # SQLite archival reader used by the positive case.
    for row in history:
        if not any(hot.id == row.id for hot in state.outbox):
            state.outbox.append(row)
    effects = [row for row in history if row.kind == "github"]
    projected = [row for row in history if row.kind == "hierarchy"]
    effect = effects[0]
    if fault in {"unknown", "rejected"}:
        effect.state = "sent_unknown" if fault == "unknown" else "rejected"
    elif fault == "unlinked":
        projected[0].receipt["intent"] = None
    elif fault == "payload":
        effect.payload["intent"]["payload"]["title"] = "different request"
    elif fault in {"method", "pr", "generation"}:
        name, value = {
            "method": ("method", "PATCH"),
            "pr": ("kind", "pr"),
            "generation": ("generation", 999),
        }[fault]
        for intent in (
            projected[0].receipt["intent"],
            effect.payload["intent"],
            effect.receipt["intent"],
        ):
            intent[name] = value
    elif fault in {"native-id", "foreign", "marker"}:
        native = effect.receipt["intent"]["receipt"]
        if fault == "native-id":
            native["id"] += 1
        elif fault == "foreign":
            native["html_url"] = "https://github.com/foreign/repo/milestone/1"
        else:
            native["description"] = "missing ownership marker"
    elif fault in {"projection-version", "graph", "complete", "unit-changes", "read-fence"}:
        name, value = {
            "projection-version": ("projection_version", 2),
            "graph": ("graph_revision", 999),
            "complete": ("complete", True),
            "unit-changes": ("unit_changes", {"issue": {"before": None, "after": 1}}),
            "read-fence": ("read_fence", {"unobserved": {"number": 99}}),
        }[fault]
        projected[-1].receipt[name] = value
    elif fault == "unproved-binding":
        state.bindings["github_hierarchy"]["initiative:unproved"] = {"number": 99}
    elif fault == "ready":
        state.units[key].bindings["hierarchy_ready"] = True
    else:
        effects[-1].receipt = {}
    with pytest.raises(CampaignError, match="bootstrap tracking|settled"):
        renewal._startup_only(state, store.directory(state.id))
    assert store.load(state.id).revision == state.revision


@pytest.mark.parametrize(
    "fault",
    [
        "unknown",
        "receipt",
        "adoption",
        "branch",
        "head",
        "dirty",
        "pr",
        "launch-history",
        "publication-history",
        "worker-artifact",
        "job-history",
    ],
)
def test_prepared_renewal_refuses_unsettled_changed_or_started_work(allocated_campaign, fault):
    from conftest import git

    from bmad_loop.campaign_action_history import action as historical_action

    case, key, action_id, _ = allocated_campaign
    store, before, template, manifest = case
    unit = before.units[key]
    workspace = Path(unit.bindings["workspace"])
    if fault in {"unknown", "receipt", "adoption"}:
        original = historical_action(before, action_id)
        if not any(row.id == original.id for row in before.outbox):
            before.outbox.append(original)
        if fault == "unknown":
            original.state = "sent_unknown"
        elif fault == "receipt":
            original.receipt = {**original.receipt, "head": "0" * 40}
        else:
            original.payload["adoption"] = True
    elif fault == "branch":
        git(workspace, "checkout", "-b", "unexpected-renewal-branch")
    elif fault == "head":
        git(workspace, "commit", "--allow-empty", "-m", "Unreviewed later work")
    elif fault == "dirty":
        (workspace / "new-work.txt").write_text("unaccepted", encoding="utf-8")
    elif fault == "pr":
        unit.bindings["pr"] = {"number": 123}
    elif fault in {"launch-history", "publication-history"}:
        prior = add_action(
            before,
            "reasoning-launch" if fault == "launch-history" else "prepare-pr",
            "" if fault == "launch-history" else key,
            {},
        )
        prior.state = "confirmed"
        prior.receipt = (
            {"launched": True} if fault == "launch-history" else {"intent": {"state": "prepared"}}
        )
    elif fault == "worker-artifact":
        worker = store.directory(before.id) / "workers" / "uncertain"
        worker.mkdir(parents=True)
        (worker / "request.json").write_text("{}", encoding="utf-8")
    else:
        database = CampaignDatabase(store.directory(before.id))
        with database.connect(write=True) as connection:
            with connection:
                connection.execute(
                    "UPDATE jobs SET state='stale',payload=json_set(payload,'$.state','stale','$.attempt',1)"
                )
        before = store.load(before.id)
    before = store.save(before, before.revision)
    with pytest.raises(CampaignError, match="prepar|settled|bootstrap"):
        apply((store, before, template, manifest))
    assert store.load(before.id).revision == before.revision


@pytest.mark.parametrize("allocated_campaign", [(True, False)], indirect=True)
@pytest.mark.parametrize("fault", ["git-dir", "allocation-pin"])
def test_native_preparation_requires_current_per_worktree_and_saved_identity(
    allocated_campaign, monkeypatch, fault
):
    import hashlib

    from bmad_loop import campaign_paradev
    from bmad_loop.adapters.paradev_runtime import private_json, read_private_json

    case, key, action_id, _ = allocated_campaign
    store, before, _, _ = case
    if fault == "git-dir":
        original = campaign_paradev._git_path
        monkeypatch.setattr(
            campaign_paradev,
            "_git_path",
            lambda repo, flag: (
                Path(before.common_dir) / "worktrees/replaced"
                if flag == "--absolute-git-dir"
                else original(repo, flag)
            ),
        )
    else:
        path = (
            store.directory(before.id)
            / "reflection/allocations"
            / (hashlib.sha256(action_id.encode()).hexdigest() + ".json")
        )
        record = read_private_json(path)
        record["binding"]["workspace_id"] = "different-workspace"
        private_json(path, record)
    with pytest.raises(CampaignError, match="pinned identity"):
        apply(case)
    assert store.load(before.id).revision == before.revision


@pytest.mark.parametrize("allocated_campaign", [(True, True)], indirect=True)
@pytest.mark.parametrize("fault", ["control", "attempted", "reservation"])
def test_prepared_intake_requires_authentic_unattempted_reservation(allocated_campaign, fault):
    from bmad_loop.campaign_intake_flow import INBOX

    case, key, _, _ = allocated_campaign
    store, before, template, manifest = case
    control = before.units[key].bindings["intake_control"]
    if fault == "control":
        control["state"] = "planning"
    else:
        record = before.bindings[INBOX]["records"][control["input_versions"][0]]
        record["attempts" if fault == "attempted" else "control_unit"] = (
            1 if fault == "attempted" else "other-unit"
        )
    before = store.save(before, before.revision)
    with pytest.raises(CampaignError, match="intake control"):
        apply((store, before, template, manifest))
    assert store.load(before.id).revision == before.revision


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


@pytest.mark.parametrize(
    "field,default",
    [("inventory_metadata_bytes", 64 * 1024 * 1024), ("max_remote_head_refreshes", 8)],
)
def test_legacy_inventory_budget_default_preserves_template_and_renews_normally(
    campaign, field, default
):
    store, before, template, _ = campaign
    database = CampaignDatabase(store.directory(before.id))
    with database.connect(write=True) as connection:
        with connection:
            connection.execute(
                "UPDATE authority SET payload=json_remove(payload, ?) WHERE id=1",
                ("$.spec.github." + field,),
            )
    raw, _ = database.read()
    assert field not in raw["spec"]["github"]
    loaded = store.load(before.id)
    assert getattr(loaded.spec.github, field) == default
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
    assert saved["spec"]["github"][field] == default


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
