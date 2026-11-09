"""Graph, checkpoint and terminal validation share bounded file authority."""

import dataclasses

import pytest
from test_campaign_ownership import committed, unit

from bmad_loop.campaign_checkpoint import owned_paths
from bmad_loop.campaign_model import (
    CampaignError,
    UnitSpec,
    product_scopes,
    unit_from_dict,
    validate_graph,
)
from bmad_loop.campaign_ownership import validate_execution
from bmad_loop.platform_util import safe_segment


@pytest.mark.parametrize("paths", [("*",), ("src/b",), ("src",), ("src/ab",)])
def test_paths_cannot_widen_declared_admission_scopes(project, paths):
    selected = unit(project, ("src/a",))
    selected.spec = dataclasses.replace(selected.spec, paths=paths)
    with pytest.raises(CampaignError, match="widens declared ownership"):
        unit_from_dict(dataclasses.asdict(selected.spec))
    with pytest.raises(CampaignError, match="widens declared ownership"):
        validate_graph([selected.spec])
    with pytest.raises(CampaignError, match="widens declared ownership"):
        owned_paths(
            project.project,
            {"unit": dataclasses.asdict(selected.spec), "source": {"kind": "explicit"}},
        )
    receipt = committed(project, "src/b/escaped.py")
    with pytest.raises(CampaignError, match="widens declared ownership"):
        validate_execution(selected, receipt, {})


@pytest.mark.parametrize(
    "scopes,paths,expected",
    [
        (("src", "resource:db"), ("./src/owned/",), ("src/owned",)),
        (("*",), ("src/owned",), ("src/owned",)),
        ((), ("src/owned",), ("src/owned",)),
        ((), (), ("*",)),
        (("resource:db",), (), ()),
    ],
)
def test_file_authority_distinguishes_unknown_from_resource_only(scopes, paths, expected):
    selected = UnitSpec("unit", "source", "epic", "Unit", scopes=scopes, paths=paths)
    assert product_scopes(selected) == expected
    assert unit_from_dict(dataclasses.asdict(selected)) == selected
    validate_graph([selected])


def test_narrower_paths_still_accept_owned_changes(project):
    selected = unit(project, ("src", "resource:db"))
    selected.spec = dataclasses.replace(selected.spec, paths=("src/owned",))
    receipt = committed(project, "src/owned/fix.py")
    assert validate_execution(selected, receipt, {}) == ["src/owned/fix.py"]
    receipt = committed(project, "src/other/fix.py")
    with pytest.raises(CampaignError, match="escaped declared ownership"):
        validate_execution(selected, receipt, {})


def test_resource_only_scope_never_grants_unowned_files(project):
    selected = unit(project, ("resource:db",))
    receipt = committed(project, "src/unowned.py")
    with pytest.raises(CampaignError, match="escaped declared ownership"):
        validate_execution(selected, receipt, {})
    allowed = owned_paths(
        project.project, {"unit": dataclasses.asdict(selected.spec), "source": {"kind": "explicit"}}
    )
    assert "*" not in allowed and "." not in allowed and "resource:db" not in allowed
    assert all(path.startswith("docs/bmad-campaign/") for path in allowed)


def test_resource_only_unit_preserves_exact_accepted_artifact_access(project):
    selected = unit(project, ("resource:db",))
    relative = "docs/bmad-campaign/" + safe_segment(selected.spec.id) + "/accepted-source.md"
    receipt = committed(project, relative)
    receipt["source_artifacts"] = [{"path": relative, "review_path": relative}]
    assert validate_execution(selected, receipt, {}) == [relative]


@pytest.mark.parametrize(
    "scopes,paths",
    [(("resource:db",), ("src",)), ((), ("resource:db",)), (("src",), ("../outside",))],
)
def test_invalid_product_paths_are_rejected_before_admission(scopes, paths):
    with pytest.raises(CampaignError):
        unit_from_dict(
            dataclasses.asdict(UnitSpec("unit", "s", "e", "Unit", scopes=scopes, paths=paths))
        )
