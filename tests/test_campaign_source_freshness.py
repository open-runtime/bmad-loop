"""Remote native planning changes hold admission without editing local branches."""

import subprocess
from dataclasses import replace

import pytest
from conftest import git, install_bmad_config
from test_stories_engine import setup_stories

from bmad_loop import verify
from bmad_loop.campaign_model import CampaignError, CampaignSpec, SourceSelector
from bmad_loop.campaign_source_freshness import inspect, require


@pytest.fixture
def native_source(project, tmp_path):
    install_bmad_config(project)
    folder = setup_stories(
        project,
        [{"id": "existing", "title": "Existing", "description": "Accepted"}],
        spec_folder="specs/native",
    )
    remote = tmp_path / "remote.git"
    subprocess.run(
        ["git", "clone", "--bare", str(project.project), str(remote)],
        capture_output=True,
        check=True,
    )
    git(project.project, "remote", "add", "origin", str(remote))
    branch = git(project.project, "symbolic-ref", "--short", "HEAD").strip()
    spec = CampaignSpec(
        "Current sources",
        sources=(SourceSelector("stories", id="s", path="specs/native"),),
        target=branch,
    )
    return project, folder, remote, branch, spec


def remote_change(case, path, text):
    project, folder, remote, branch, spec = case
    git(project.project, "checkout", "-b", "remote-advance")
    if text is None:
        path.unlink()
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    git(project.project, "add", "-A")
    git(project.project, "commit", "-qm", "remote main update")
    git(project.project, "push", "origin", "HEAD:" + branch)
    git(project.project, "checkout", branch)


@pytest.mark.parametrize("mutation", ["changed", "deleted"])
def test_changed_or_deleted_remote_native_source_is_held_and_caller_tree_kept(
    native_source, mutation
):
    project, folder, remote, branch, spec = native_source
    before = verify.rev_parse_head(project.project)
    old = (folder / "stories.yaml").read_bytes()
    remote_change(
        native_source,
        folder / "stories.yaml",
        (
            None
            if mutation == "deleted"
            else "- id: new-main-work\n  title: New\n  description: New\n"
        ),
    )
    observed = inspect(project.project, spec, action_id="source-check")
    assert not observed.ready and "specs/native/stories.yaml" in observed.changed
    assert "update this project base" in observed.reason
    assert observed.head == before and observed.main_head != before
    assert verify.rev_parse_head(project.project) == before
    assert (folder / "stories.yaml").read_bytes() == old
    assert git(project.project, "status", "--porcelain").strip() == ""


def test_unrelated_remote_code_change_does_not_require_whole_checkout_parity(native_source):
    project, folder, remote, branch, spec = native_source
    before = verify.rev_parse_head(project.project)
    remote_change(native_source, project.project / "unrelated.py", "new unrelated code\n")
    observed = require(project.project, spec)
    assert observed.ready and observed.head == before and observed.main_head != before
    assert observed.changed == ()
    assert not (project.project / "unrelated.py").exists()


def test_native_config_changes_are_in_source_freshness_contract(native_source):
    project, folder, remote, branch, spec = native_source
    path = project.project / "_bmad/bmm/config.yaml"
    text = path.read_text() + "\nproject_name: remotely-updated\n"
    remote_change((project, folder, remote, branch, spec), path, text)
    observed = inspect(project.project, spec)
    assert not observed.ready
    assert path.relative_to(project.project).as_posix() in observed.changed


def test_selected_native_ticket_scheduler_is_compared_to_current_main(project_v7, tmp_path):
    project = project_v7
    path = project.project / "scheduler.toml"
    path.write_text("[scheduler]\nholds=[]\n")
    git(project.project, "add", "scheduler.toml")
    git(project.project, "commit", "-qm", "selected scheduler metadata")
    remote = tmp_path / "remote.git"
    subprocess.run(
        ["git", "clone", "--bare", str(project.project), str(remote)],
        capture_output=True,
        check=True,
    )
    git(project.project, "remote", "add", "origin", str(remote))
    branch = git(project.project, "symbolic-ref", "--short", "HEAD").strip()
    spec = CampaignSpec(
        "Ticket metadata",
        target=branch,
        sources=(
            SourceSelector(
                "tickets", id="t", initiative=project.initiative, scheduler_config="scheduler.toml"
            ),
        ),
    )
    remote_change(
        (project, None, remote, branch, spec),
        path,
        "[scheduler]\nholds=[{initiative='i',epic='e',until='x/1.1'}]\n",
    )
    assert "scheduler.toml" in inspect(project.project, spec).changed


def test_creation_refuses_stale_native_graph_before_allocating_campaign(native_source, monkeypatch):
    from bmad_loop import campaign_runtime

    project, folder, remote, branch, spec = native_source
    remote_change(native_source, folder / "stories.yaml", None)
    monkeypatch.setattr(
        campaign_runtime, "import_sources", lambda *args: pytest.fail("stale source graph imported")
    )
    with pytest.raises(CampaignError, match="update this project base"):
        campaign_runtime.create(
            project.project, replace(spec, github=replace(spec.github, repository="example/repo"))
        )


def test_source_copy_refuses_to_resurrect_native_file_deleted_on_fresh_main(native_source):
    from bmad_loop.campaign_sources import _copy_missing

    project, folder, remote, branch, spec = native_source
    target = project.project.parent / "fresh-worker-source"
    target.mkdir()
    with pytest.raises(CampaignError, match="instead of restoring deleted"):
        _copy_missing(folder, target, tracked_project=project.project)
    assert not (target / "stories.yaml").exists()


def test_local_unmerged_source_or_new_planning_file_cannot_be_imported_as_main(native_source):
    project, folder, remote, branch, spec = native_source
    path = folder / "stories/new-plan.md"
    path.write_text("unmerged native plan\n")
    with pytest.raises(CampaignError, match="deliver the planning PR"):
        require(project.project, spec)
    assert path.read_text() == "unmerged native plan\n"


def test_brief_planning_without_native_sources_never_fetches_or_requires_existing_graph(project):
    observed = inspect(project.project, CampaignSpec("Brief only"))
    assert observed.ready and not observed.observation_ref


def test_ignored_native_store_is_observed_as_private_without_scanning_dependency_trees(
    native_source,
):
    project, folder, remote, branch, spec = native_source
    private = project.project / "private-stories"
    (project.project / ".gitignore").write_text("private-stories/\n")
    git(project.project, "add", ".gitignore")
    git(project.project, "commit", "-qm", "explicit private source")
    git(project.project, "push", "origin", branch)
    private.mkdir()
    (private / "stories.yaml").write_text("private canonical source\n")
    spec = replace(spec, sources=(SourceSelector("stories", id="s", path="private-stories"),))
    observed = inspect(project.project, spec)
    assert observed.ready and str(private) in observed.private_roots
