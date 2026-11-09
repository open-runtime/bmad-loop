"""Adoption and reviewed amendments retain canonical selected-input authority."""

import dataclasses
import json
import time
from pathlib import Path

import pytest
from conftest import OMIT, git, spec_path, write_spec, write_sprint
from test_campaign_adoption import case as case

from bmad_loop import campaign_adoption as adoption
from bmad_loop.campaign_model import CampaignError, SourceSelector
from bmad_loop.campaign_ownership import canonical_unchanged
from bmad_loop.campaign_runtime import CampaignRuntime
from bmad_loop.campaign_sources import source_paths
from bmad_loop.campaign_store import CampaignStore, add_action


def prepared_native_adoption(case, project, tmp_path, *, tracked=False):
    write_sprint(project, {"1-1-owned": "ready-for-dev"})
    product = spec_path(project, "1-1-owned")
    write_spec(product, "ready-for-dev", OMIT)
    with (case.repo / ".gitignore").open("a", encoding="utf-8") as stream:
        stream.write("\n/_bmad-output/\n")
    git(case.repo, "add", ".gitignore")
    git(case.repo, "add", "-f", str(project.sprint_status))
    if tracked:
        git(case.repo, "add", "-f", str(product))
    git(case.repo, "commit", "-m", "tracked board with selected owner intent")
    base = git(case.repo, "rev-parse", "HEAD")
    git(case.repo, "push", "origin", "main")
    git(case.workspace, "merge", "--no-edit", base)
    head = git(case.workspace, "rev-parse", "HEAD")
    git(case.workspace, "push", "origin", "legacy")
    case.unit = dataclasses.replace(case.unit, selector="1-1-owned")
    source = SourceSelector("sprint-status", id="source")
    case.snapshot.spec = dataclasses.replace(
        case.snapshot.spec, sources=(source,), units=(case.unit,)
    )
    case.snapshot.units[case.unit.id].spec = case.unit
    _, _, identity = adoption.source_binding(case.repo, case.snapshot, case.unit.id)
    case.manifest = dataclasses.replace(
        case.manifest, base=base, head=head, source_identity=identity
    )
    actor = case.service.items[0]
    actor = dataclasses.replace(
        actor, details={**actor.details, "base": base, "head": head, "source_identity": identity}
    )
    case.service = dataclasses.replace(case.service, items=(actor,), observed_at=time.time())
    case.pr["base"]["sha"] = base
    case.pr["head"]["sha"] = head
    store = CampaignStore(tmp_path / "state")
    snapshot = store.create(case.snapshot)
    runtime = CampaignRuntime(
        case.repo, store, gateway=case.gateway, adoption_resolver=case.resolver
    )
    path = tmp_path / "adopt.json"
    path.write_text(json.dumps(dataclasses.asdict(case.manifest)), encoding="utf-8")
    payload = adoption.prepare_request(
        case.repo,
        snapshot,
        case.unit.id,
        path,
        "handoff",
        gateway=case.gateway,
        resolver=case.resolver,
    )
    adoption.begin(snapshot, case.unit.id, payload, command_id="owner-adopt")
    case.pr["draft"] = True
    selected = snapshot.units[case.unit.id]
    selected.bindings["adoption"]["state"] = "draft_confirmed"
    action = add_action(snapshot, "prepare-unit", case.unit.id, {"adoption": True})
    result = adoption.prepare(runtime, snapshot, action)
    selected.bindings.update(result)
    assert selected.bindings["canonical_source"]["versioning"] == "code-repo"
    canonical_unchanged(selected)
    return (runtime, snapshot, selected, product)


def test_adoption_tracks_canonical_ignored_native_product(case, project, tmp_path):
    runtime, snapshot, selected, product = prepared_native_adoption(case, project, tmp_path)
    product.write_text(
        product.read_text(encoding="utf-8") + "\nUnreviewed canonical owner acceptance.\n",
        encoding="utf-8",
    )
    with pytest.raises(CampaignError, match="selected product input"):
        canonical_unchanged(selected)


@pytest.mark.parametrize(
    "tracked,remote_ahead,owner_overlay",
    [
        (False, False, "none"),
        (True, False, "none"),
        (True, True, "none"),
        (True, True, "worktree"),
        (True, True, "index"),
    ],
)
def test_amendment_refresh_tracks_canonical_native_product(
    case, project, tmp_path, tracked, remote_ahead, owner_overlay
):
    import hashlib

    from bmad_loop.campaign_intake_amendment import perform
    from bmad_loop.campaign_intake_flow import INBOX
    from bmad_loop.campaign_model import CampaignUnit, OutboxAction, UnitSpec, UnitState

    runtime, snapshot, selected, product = prepared_native_adoption(
        case, project, tmp_path, tracked=tracked
    )
    case.pr["head"]["sha"] = selected.bindings["head"]
    for row in snapshot.outbox:
        row.state = "confirmed"
    plan = CampaignUnit(
        UnitSpec("source:plan", "source", "epic", "Reviewed amendment", kind="planning"),
        state=UnitState.DELIVERED,
    )
    plan.bindings["intake_control"] = {
        "schema_version": 1,
        "state": "refreshing",
        "proposal_id": "proposal",
        "amendments": {selected.id: {}},
    }
    plan.evidence.update(
        planning_flow={"state": "activated"}, merge={"merge_commit": case.manifest.base}
    )
    snapshot.units[plan.id] = plan
    snapshot.bindings[INBOX] = {
        "proposals": {
            "proposal": {
                "state": "awaiting_refresh",
                "units": [dataclasses.asdict(selected.spec)],
                "input_versions": [],
            }
        }
    }
    snapshot.bindings["source_observations"] = {
        selected.id: {
            "path": str(project.sprint_status),
            "sha256": hashlib.sha256(project.sprint_status.read_bytes()).hexdigest(),
            "remote_publication_confirmed": True,
        }
    }
    baseline = git(case.repo, "rev-parse", "HEAD")
    if remote_ahead and tracked:
        product.write_text(
            product.read_text(encoding="utf-8") + "\nAccepted on newer main.\n", encoding="utf-8"
        )
        git(case.repo, "add", "-f", str(product))
        git(case.repo, "commit", "-m", "accepted amendment on main")
        plan.evidence["merge"]["merge_commit"] = git(case.repo, "rev-parse", "HEAD")
        git(case.repo, "push", "origin", "main")
        git(case.repo, "reset", "--hard", baseline)
    if owner_overlay != "none":
        original = product.read_text(encoding="utf-8")
        product.write_text(original + "\nUncommitted owner changes.\n", encoding="utf-8")
        if owner_overlay == "index":
            git(case.repo, "add", "-f", str(product))
            product.write_text(original, encoding="utf-8")
    action = OutboxAction(
        "review-native-refresh",
        "intake-refresh",
        selected.id,
        {"planning_unit": plan.id, "proposal_id": "proposal"},
        snapshot.generation,
    )
    if owner_overlay != "none":
        with pytest.raises(CampaignError, match="canonical selected product"):
            perform(runtime, snapshot, action)
        return
    result = perform(runtime, snapshot, action)
    selected.bindings.update(result)
    canonical_unchanged(selected)
    assert selected.bindings["canonical_source"]["product_project"] == str(case.repo)
    private = (
        source_paths(
            Path(selected.bindings["workspace"]), SourceSelector(**result["effective_source"])
        ).implementation_artifacts
        / product.name
    )
    if remote_ahead and tracked:
        assert "Accepted on newer main" in private.read_text(encoding="utf-8")
        assert git(case.repo, "rev-parse", "HEAD") == baseline
    private.write_text(
        private.read_text(encoding="utf-8").replace("ready-for-dev", "done"), encoding="utf-8"
    )
    canonical_unchanged(selected)
    product.write_text(
        product.read_text(encoding="utf-8") + "\nNew owner intent after reviewed amendment.\n",
        encoding="utf-8",
    )
    with pytest.raises(CampaignError, match="selected product input"):
        canonical_unchanged(selected)


def test_adopted_private_lifecycle_can_reach_publication_gate(case, project, tmp_path):
    from pathlib import Path

    from bmad_loop.campaign_sources import source_paths

    runtime, snapshot, selected, product = prepared_native_adoption(case, project, tmp_path)
    selected.bindings["adoption"]["state"] = "owned"
    case.pr["head"]["sha"] = selected.bindings["head"]
    effective = SourceSelector(**selected.bindings["effective_source"])
    private = (
        source_paths(Path(selected.bindings["workspace"]), effective).implementation_artifacts
        / product.name
    )
    private.write_text(
        private.read_text(encoding="utf-8").replace("ready-for-dev", "done"), encoding="utf-8"
    )
    adoption.guard_owned(runtime, snapshot, selected.id)
    canonical_unchanged(selected)
