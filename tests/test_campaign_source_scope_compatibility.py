"""Default-scope source wires retain adoption and private-copy identities."""

import json
from dataclasses import asdict, replace

from conftest import git
from test_campaign_adoption import case as case
from test_campaign_adoption import qualify

from bmad_loop import campaign_adoption as adoption
from bmad_loop.campaign_model import SourceSelector, source_to_dict
from bmad_loop.campaign_sources import prepare_source_view


def test_old_adoption_identity_qualifies_without_rewriting_its_manifest(case):
    source, binding, current = adoption.source_binding(case.repo, case.snapshot, case.unit.id)
    legacy = asdict(source)
    legacy.pop("completion_scope")
    old_identity = adoption.digest(
        {"selector": legacy, "unit": asdict(case.unit), "binding": binding}
    )
    assert source_to_dict(source) == legacy
    assert current == old_identity
    case.manifest = replace(case.manifest, source_identity=old_identity)
    qualified = qualify(case)
    assert qualified.source["identity"] == old_identity
    assert qualified.source["selector"] == legacy


def test_old_source_payload_reuses_bound_private_copy_with_its_edits(
    project_v7_external_store, tmp_path
):
    layout = project_v7_external_store
    workspace = tmp_path / "worker"
    git(layout.project, "worktree", "add", "-b", "scope-compatible", str(workspace), "HEAD")
    old_wire = {
        "kind": "tickets",
        "initiative": layout.initiative,
        "epics": [],
        "units": [],
        "path": "",
        "independent": False,
        "id": "tickets",
        "store_remote": "",
        "store_branch": "",
        "scheduler_config": "",
    }
    source = SourceSelector(**old_wire)
    authority = tmp_path / "source-authority"
    first = prepare_source_view(layout.project, workspace, source, authority_dir=authority)
    assert first.paths.initiative_folder is not None
    marker = first.paths.initiative_folder / "preserve-worker-edit.md"
    marker.write_text("private edit survives source decoding\n")
    before = (authority / "ticket-store.json").read_bytes()
    persisted = json.loads(json.dumps(source_to_dict(source)))
    assert persisted == old_wire
    resumed = prepare_source_view(
        layout.project, workspace, SourceSelector(**persisted), authority_dir=authority
    )
    assert resumed.paths == first.paths
    assert marker.read_text() == "private edit survives source decoding\n"
    assert (authority / "ticket-store.json").read_bytes() == before
