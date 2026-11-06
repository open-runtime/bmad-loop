"""Planning source bytes remain reviewable without granting actors store authority."""

import hashlib

import pytest
from conftest import git
from test_stories_engine import SPEC_FOLDER, setup_stories

from bmad_loop import verify
from bmad_loop.campaign_model import CampaignError, SourceSelector
from bmad_loop.campaign_planning_publication import bind_mirrors, capture, changes, mirror_artifacts


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
