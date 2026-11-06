"""Planning source bytes remain reviewable without granting actors store authority."""

import hashlib

import pytest
from conftest import (
    DEFAULT_TREE,
    git,
    install_bmad_config,
    write_plan,
    write_sprint,
    write_ticket_tree,
)
from test_stories_engine import SPEC_FOLDER, setup_stories

from bmad_loop import ticketlayout, verify
from bmad_loop.campaign_model import CampaignError, SourceSelector
from bmad_loop.campaign_planning_publication import (
    bind_mirrors,
    capture,
    changes,
    mirror_artifacts,
    validate_stage_products,
)


def test_ignored_artifact_has_committed_mirror_and_exact_preimage(project, tmp_path):
    setup_stories(project, [{"id": "one", "title": "One", "description": "One"}])
    repo = project.project
    source = SourceSelector("stories", id="stories", path=SPEC_FOLDER)
    folder = repo / SPEC_FOLDER
    before = folder / "original.md"
    before.write_text("old product", encoding="utf-8")
    manifest = capture(
        tmp_path / "manifest.json", project=repo, workspace=repo, sources=((source, source),)
    )
    (repo / ".gitignore").write_text("/" + SPEC_FOLDER + "/\n", encoding="utf-8")
    before.write_text("reviewed product", encoding="utf-8")
    relative = before.relative_to(repo).as_posix()
    paths, mapping = mirror_artifacts(repo, "planning", [relative])
    verify.commit_paths(repo, "review private plan", paths)
    artifact = {"path": relative, "sha256": hashlib.sha256(before.read_bytes()).hexdigest()}
    receipt = {
        "head": verify.rev_parse_head(repo),
        "artifact_mirrors": mapping,
        "validated": {"artifacts": [artifact]},
    }
    bind_mirrors(repo, receipt)
    authored = changes(manifest, repo, source.id, [artifact], {}, {})
    assert len(authored) == 1
    assert authored[0].preimage == b"old product"
    assert authored[0].data == b"reviewed product"
    assert authored[0].path == str(before)
    assert authored[0].review_path == mapping[relative]
    assert (
        verify.file_bytes_at_revision(repo, receipt["head"], mapping[relative])
        == b"reviewed product"
    )


def test_new_product_captures_absence_and_manifest_is_not_recaptured(project, tmp_path):
    setup_stories(project, [{"id": "one", "title": "One", "description": "One"}])
    repo = project.project
    source = SourceSelector("stories", id="stories", path=SPEC_FOLDER)
    path = tmp_path / "manifest.json"
    original = capture(path, project=repo, workspace=repo, sources=((source, source),))
    product = repo / SPEC_FOLDER / "new.md"
    product.write_text("new accepted product", encoding="utf-8")
    reloaded = capture(path, project=repo, workspace=repo, sources=((source, source),))
    assert reloaded == original
    authored = changes(
        reloaded,
        repo,
        source.id,
        [
            {
                "path": product.relative_to(repo).as_posix(),
                "sha256": hashlib.sha256(product.read_bytes()).hexdigest(),
            }
        ],
        {},
        {},
    )
    assert authored[0].preimage is None


@pytest.mark.parametrize("escape", ["../outside.md", "/outside.md"])
def test_artifact_escape_refused(project, escape):
    with pytest.raises(CampaignError, match="outside"):
        mirror_artifacts(project.project, "plan", [escape])


def test_committed_mirror_cannot_name_different_bytes(project):
    repo = project.project
    product = repo / "plan.md"
    product.write_text("reviewed", encoding="utf-8")
    git(repo, "add", "plan.md")
    git(repo, "commit", "-m", "reviewed plan")
    receipt = {
        "head": verify.rev_parse_head(repo),
        "artifact_mirrors": {"private.md": "plan.md"},
        "validated": {"artifacts": [{"path": "private.md", "sha256": "f" * 64}]},
    }
    with pytest.raises(CampaignError, match="exact committed"):
        bind_mirrors(repo, receipt)


@pytest.mark.parametrize("duplicate", [False, True])
def test_planning_native_story_manifest_is_validated_before_publication(project, duplicate):
    setup_stories(project, [{"id": "one", "title": "One", "description": "One"}])
    repo = project.project
    baseline = verify.rev_parse_head(repo)
    source = SourceSelector("stories", id="source", path=SPEC_FOLDER)
    manifest = repo / SPEC_FOLDER / "stories.yaml"
    manifest.write_text(
        manifest.read_text(encoding="utf-8")
        + "- id: "
        + ("one" if duplicate else "two")
        + "\n  title: New story\n  description: Accepted story\n",
        encoding="utf-8",
    )
    git(repo, "add", "-A")
    git(repo, "commit", "-m", "planned source output")
    receipt = {
        "base": baseline,
        "head": verify.rev_parse_head(repo),
        "validated": {"artifacts": [{"path": manifest.relative_to(repo).as_posix()}]},
    }
    if duplicate:
        with pytest.raises(CampaignError, match="invalid native stories manifest.*duplicate id"):
            validate_stage_products(repo, receipt, (source,))
    else:
        validate_stage_products(repo, receipt, (source,))


@pytest.mark.parametrize("mutation", ["valid", "duplicate", "syntax", "problem"])
def test_planning_native_ticket_manifest_is_validated_before_publication(project_v7, mutation):
    layout = project_v7
    write_ticket_tree(layout, DEFAULT_TREE)
    repo = layout.project
    git(repo, "add", "-A")
    git(repo, "commit", "--allow-empty", "-m", "native ticket baseline")
    baseline = verify.rev_parse_head(repo)
    source = SourceSelector(
        "tickets", id="tickets", initiative=layout.initiative, epics=("epic-cart",)
    )
    manifest = (
        ticketlayout.breakdown_dir(ticketlayout.epic_folder(layout.initiative_folder, "epic-cart"))
        / ticketlayout.BREAKDOWN
    )
    additions = {
        "valid": '\n[[entry]]\nid = 3\ntype = "story"\ntitle = "Next story"\n',
        "duplicate": '\n[[entry]]\nid = 1\ntype = "story"\ntitle = "Duplicate"\n',
        "syntax": "\n[[entry]\n",
        "problem": "\n# inspect the authored native plan\n",
    }
    manifest.write_text(
        manifest.read_text(encoding="utf-8") + additions[mutation], encoding="utf-8"
    )
    artifacts = [{"path": manifest.relative_to(repo).as_posix()}]
    if mutation == "problem":
        orphan = write_plan(layout, "epic-cart", "orphan-plan", 999, "ready-for-dev")
        artifacts.append({"path": orphan.relative_to(repo).as_posix()})
    git(repo, "add", "-A")
    git(repo, "commit", "-m", "planned native tickets")
    receipt = {
        "base": baseline,
        "head": verify.rev_parse_head(repo),
        "validated": {"artifacts": artifacts},
    }
    if mutation == "valid":
        validate_stage_products(repo, receipt, (source,))
    else:
        with pytest.raises(CampaignError, match="invalid native ticket tree"):
            validate_stage_products(repo, receipt, (source,))


@pytest.mark.parametrize("mutation", ["valid", "duplicate", "syntax"])
def test_planning_native_sprint_board_is_validated_before_publication(project, mutation):
    install_bmad_config(project)
    write_sprint(project, {"1-1-one": "ready-for-dev"})
    repo = project.project
    git(repo, "add", "-A")
    git(repo, "commit", "--allow-empty", "-m", "native sprint baseline")
    baseline = verify.rev_parse_head(repo)
    board = project.sprint_status
    additions = {
        "valid": "\n  1-2-next: backlog\n",
        "duplicate": "\n  1-1-one: done\n",
        "syntax": "\n[",
    }
    board.write_text(board.read_text(encoding="utf-8") + additions[mutation], encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-m", "planned native sprint board")
    receipt = {
        "base": baseline,
        "head": verify.rev_parse_head(repo),
        "validated": {"artifacts": [{"path": board.relative_to(repo).as_posix()}]},
    }
    if mutation == "valid":
        validate_stage_products(repo, receipt, (SourceSelector("sprint-status", id="sprint"),))
    else:
        with pytest.raises(CampaignError, match="invalid native sprint board"):
            validate_stage_products(repo, receipt, (SourceSelector("sprint-status", id="sprint"),))


@pytest.mark.parametrize("orphan", [False, True])
def test_planning_ticket_plan_only_edit_is_validated_without_manifest_edit(project_v7, orphan):
    layout = project_v7
    repo = layout.project
    baseline = verify.rev_parse_head(repo)
    source = SourceSelector("tickets", id="tickets", initiative=layout.initiative)
    plan = write_plan(
        layout,
        "epic-cart",
        "orphan-plan" if orphan else "story-cart-model-plan",
        999 if orphan else 1,
        "ready-for-dev",
    )
    git(repo, "add", "-A")
    git(repo, "commit", "-m", "planning authored only a native plan")
    receipt = {
        "base": baseline,
        "head": verify.rev_parse_head(repo),
        "validated": {"artifacts": [{"path": plan.relative_to(repo).as_posix()}]},
    }
    assert (
        git(repo, "diff", "--name-only", baseline, receipt["head"])
        == plan.relative_to(repo).as_posix()
    )
    if orphan:
        with pytest.raises(CampaignError, match="invalid native ticket tree"):
            validate_stage_products(repo, receipt, (source,))
    else:
        validate_stage_products(repo, receipt, (source,))
