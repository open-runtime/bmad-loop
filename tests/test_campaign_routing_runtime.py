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
