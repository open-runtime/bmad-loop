"""Actual changed paths, not model declarations, determine ownership escapes."""

import dataclasses
from types import SimpleNamespace

import pytest
from conftest import git

from bmad_loop import verify
from bmad_loop.campaign_model import CampaignError, UnitSpec
from bmad_loop.campaign_ownership import validate_execution


def committed(project, path):
    repo = project.project
    base = verify.rev_parse_head(repo)
    file = repo / path
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text("implementation", encoding="utf-8")
    git(repo, "add", path)
    git(repo, "commit", "-m", "checkpoint")
    return {"base": base, "head": verify.rev_parse_head(repo)}


def unit(project, scopes):
    return SimpleNamespace(
        spec=UnitSpec("source:p:s", "source", "epic", "Story", scopes=scopes),
        bindings={"workspace": str(project.project)},
    )


def test_owned_directory_is_verified_from_real_diff(project):
    receipt = committed(project, "src/owned/fix.py")
    assert validate_execution(unit(project, ("src/owned",)), receipt, {}) == ["src/owned/fix.py"]


def test_neighboring_prefix_is_not_owned(project):
    receipt = committed(project, "src/owned_elsewhere/fix.py")
    with pytest.raises(CampaignError, match="escaped declared"):
        validate_execution(unit(project, ("src/owned",)), receipt, {})


def test_only_exact_source_artifact_receipts_are_exempt(project):
    receipt = committed(project, "docs/bmad-campaign/story/accepted.md")
    relative = "docs/bmad-campaign/story/accepted.md"
    receipt["source_artifacts"] = [{"path": relative, "review_path": relative}]
    assert validate_execution(unit(project, ("src/owned",)), receipt, {}) == [relative]


def test_unknown_footprint_keeps_exclusive_scope_semantics(project):
    receipt = committed(project, "unknown.py")
    assert validate_execution(unit(project, ()), receipt, {}) == ["unknown.py"]


@pytest.mark.parametrize("scopes", [("src/owned",), ()])
def test_sprint_bookkeeping_cannot_advance_a_different_story(project, scopes):
    from conftest import install_bmad_config, write_sprint

    from bmad_loop import bmadconfig

    install_bmad_config(project)
    write_sprint(project, {"1-1-owned": "ready-for-dev", "1-2-other": "ready-for-dev"})
    repo = project.project
    git(repo, "add", "-A")
    git(repo, "commit", "-m", "source board")
    base = verify.rev_parse_head(repo)
    board = bmadconfig.load_paths(repo).sprint_status
    board.write_text(
        board.read_text().replace("1-2-other: ready-for-dev", "1-2-other: done"), encoding="utf-8"
    )
    git(repo, "add", "-A")
    git(repo, "commit", "-m", "invalid board escape")
    selected = unit(project, scopes)
    selected.spec = dataclasses.replace(selected.spec, selector="1-1-owned")
    with pytest.raises(CampaignError, match="unowned sprint board row"):
        validate_execution(
            selected, {"base": base, "head": verify.rev_parse_head(repo)}, {"kind": "sprint-status"}
        )


def test_ignored_canonical_acceptance_edit_is_detected_before_merge(project, tmp_path):
    from conftest import install_bmad_config
    from test_stories_engine import SPEC_FOLDER, setup_stories

    from bmad_loop.campaign_model import SourceSelector
    from bmad_loop.campaign_ownership import canonical_unchanged
    from bmad_loop.campaign_source_publication import SourcePublication

    install_bmad_config(project)
    setup_stories(project, [{"id": "one", "title": "One", "description": "One"}])
    repo = project.project
    folder = repo / SPEC_FOLDER / "stories"
    folder.mkdir(exist_ok=True)
    product = folder / "one-story.md"
    product.write_text("---\nstatus: ready-for-dev\n---\nApproved acceptance\n", encoding="utf-8")
    with (repo / ".gitignore").open("a", encoding="utf-8") as handle:
        handle.write("\n/" + SPEC_FOLDER + "/\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-m", "shared source configuration")
    selected = unit(project, ("src/owned",))
    selected.spec = dataclasses.replace(selected.spec, selector="one")
    source = SourceSelector("stories", id="source", path=SPEC_FOLDER)
    publisher = SourcePublication(repo, tmp_path / "authority")
    captured = publisher.capture(source, selected.spec)
    assert captured.versioning != "code-repo"
    selected.bindings.update(
        canonical_source=captured.to_dict(),
        canonical_selector=dataclasses.asdict(source),
        source_authority_root=str(tmp_path / "authority"),
    )
    canonical_unchanged(selected)
    product.write_text(product.read_text() + "New owner acceptance\n", encoding="utf-8")
    with pytest.raises(CampaignError, match="canonical source changed"):
        canonical_unchanged(selected)
