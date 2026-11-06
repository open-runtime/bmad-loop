"""Installed commit claims are tied to the loaded package, not its version text."""

import json
from types import SimpleNamespace

import pytest
from conftest import git

from bmad_loop import verify
from bmad_loop.campaign_provenance import inspect


def distribution(data, module):
    return SimpleNamespace(read_text=lambda _: json.dumps(data), locate_file=lambda _: module)


def test_git_install_proves_exact_commit_and_removes_url_credentials(tmp_path):
    module = tmp_path / "site" / "campaign_provenance.py"
    commit = "a" * 40
    dist = distribution(
        {
            "url": "https://dummy-secret@github.com/open-runtime/bmad-loop.git?credential=dummy",
            "vcs_info": {"vcs": "git", "commit_id": commit},
        },
        module,
    )
    result = inspect(expected_commit=commit, distribution=dist, module_file=module)
    assert result["qualified"] and result["matches_expected"]
    assert result["source_url"] == "https://github.com/open-runtime/bmad-loop.git"
    assert "dummy-secret" not in json.dumps(result)


def test_git_metadata_does_not_authorize_a_different_loaded_package(tmp_path):
    dist = distribution(
        {"vcs_info": {"vcs": "git", "commit_id": "a" * 40}}, tmp_path / "installed.py"
    )
    result = inspect(expected_commit="a" * 40, distribution=dist, module_file=tmp_path / "other.py")
    assert not result["qualified"] and not result["matches_expected"]


def test_editable_install_requires_exact_clean_checkout(project):
    repo = project.project
    module = repo / "src" / "bmad_loop" / "campaign_provenance.py"
    module.parent.mkdir(parents=True)
    module.write_text("# installed source\n", encoding="utf-8")
    git(repo, "add", "src")
    git(repo, "commit", "-m", "source installation")
    commit = verify.rev_parse_head(repo)
    dist = distribution({"url": repo.as_uri(), "dir_info": {"editable": True}}, module)
    assert inspect(expected_commit=commit, distribution=dist, module_file=module)[
        "matches_expected"
    ]
    module.write_text("# uncommitted change\n", encoding="utf-8")
    dirty = inspect(expected_commit=commit, distribution=dist, module_file=module)
    assert dirty["commit"] == commit and dirty["dirty"] and not dirty["qualified"]


def test_directory_built_wheel_does_not_guess_build_commit_from_current_source(project):
    dist = distribution(
        {"url": project.project.as_uri(), "dir_info": {}}, project.project / "module.py"
    )
    result = inspect(distribution=dist, module_file=project.project / "module.py")
    assert result["installation_kind"] == "directory-build" and not result["qualified"]
    assert result["commit"] == ""


def test_expected_commit_must_be_complete():
    with pytest.raises(ValueError, match="complete Git"):
        inspect(expected_commit="main")
