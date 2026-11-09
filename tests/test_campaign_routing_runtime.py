"""Approved campaign routes reach the frozen real worker launch envelope."""

from dataclasses import replace

import pytest
from test_campaign_checkpoint_runtime import checkpoint_case as checkpoint_case
from test_campaign_runtime import production_case as production_case

from bmad_loop import campaign_runtime


def test_execution_and_child_review_candidates_reach_frozen_launch(checkpoint_case, monkeypatch):
    case = checkpoint_case
    template = case.runtime.template(case.snapshot)
    profiles = dict(template.profiles)
    profiles["campaign:chosen-dev"] = replace(profiles["dev"], name="chosen-dev")
    profiles["campaign:chosen-review"] = replace(profiles["review"], name="chosen-review")
    monkeypatch.setattr(case.runtime, "template", lambda _: replace(template, profiles=profiles))
    action = next(row for row in case.snapshot.outbox if row.id == case.unit.claim.launch_id)
    action.payload.update(
        candidate={"profile": "chosen-dev", "model": "dev-model", "effort": "high"},
        review_candidate={"profile": "chosen-review", "model": "review-model", "effort": "medium"},
    )
    captured = []

    class Captured(Exception):
        pass

    def capture(envelope, request):
        captured.append((envelope, request))
        raise Captured

    monkeypatch.setattr(campaign_runtime, "prepare_launch", capture)
    with pytest.raises(Captured):
        case.runtime._perform_launch(case.snapshot, action)
    envelope, request = captured[0]
    assert envelope.policy.adapter.resolved("dev").model == "dev-model"
    assert envelope.policy.adapter.resolved("review").model == "review-model"
    assert envelope.profiles["dev"].name == "chosen-dev"
    assert envelope.profiles["review"].name == "chosen-review"
    assert request["headless_coordinates"]["head"] == case.unit.bindings["head"]
    assert request["headless_grants"]["workspace"] == str(case.workspace.resolve())
    assert request["headless_grants"]["generation"] == case.unit.claim.generation
    assert request["headless_grants"]["config_digest"]
    assert request["headless_grants"]["write_paths"] == [
        *request["private_paths"],
        str(case.workspace / "specs/epic/stories/first-story.md"),
    ]
    assert request["headless_grants"]["git_branch"] == case.unit.bindings["branch"]
    assert case.snapshot.spec.target in request["headless_grants"]["protected_branches"]
    assert template.policy.adapter.resolved("dev").model != "dev-model"


@pytest.mark.parametrize(
    "scopes,paths", [((), ()), (("resource:campaign-plan",), ()), (("src",), ("src/owned",))]
)
def test_writer_grants_keep_the_admitted_file_authority_and_exact_bookkeeping(
    checkpoint_case, monkeypatch, scopes, paths
):
    from bmad_loop.platform_util import safe_segment

    case = checkpoint_case
    case.unit.spec = replace(case.unit.spec, scopes=scopes, paths=paths)
    action = next(row for row in case.snapshot.outbox if row.id == case.unit.claim.launch_id)
    captured = []

    class Captured(Exception):
        pass

    def capture(envelope, request):
        captured.append(request)
        raise Captured

    monkeypatch.setattr(campaign_runtime, "prepare_launch", capture)
    with pytest.raises(Captured):
        case.runtime._perform_launch(case.snapshot, action)
    request = captured[0]
    grants = request["headless_grants"]
    if scopes == ("resource:campaign-plan",):
        namespace = "docs/bmad-campaign/" + safe_segment(case.unit.id)
        assert grants["scopes"] == [namespace, namespace + ".md"]
        assert "*" not in grants["scopes"] and "." not in grants["scopes"]
        from bmad_loop.campaign_execution_sandbox import write_paths

        control = case.monitor.directory / "grant-check"
        control.mkdir()
        workspace, writable, _ = write_paths(grants, control)
        assert workspace not in writable
        assert workspace / namespace in writable
        assert workspace / (namespace + ".md") in writable
        assert workspace / "src.txt" not in writable
    else:
        assert grants["scopes"] == (["src/owned"] if paths else ["."])
    assert grants["write_paths"] == [
        *request["private_paths"],
        str(case.workspace / "specs/epic/stories/first-story.md"),
    ]
