"""Only positively unsent native publication may retire a moved PR candidate."""

import copy
import json
from dataclasses import replace

import pytest
from test_campaign_admin_publication import native_case
from test_campaign_publication import publishable as publishable

from bmad_loop.campaign_native_merge import execute


@pytest.mark.parametrize(
    "phase", ["prepared", "normal-refused", "normal-submitted", "admin-submitted", "acknowledged"]
)
def test_remote_move_retires_only_proven_unsent_native_merge(
    project, tmp_path, publishable, monkeypatch, phase
):
    controller, candidate, evidence, intent, _ = native_case(publishable, monkeypatch)
    current = controller.inspect(1)
    pr = copy.deepcopy(current.pr)
    pr["head"]["sha"] = "b" * 40
    monkeypatch.setattr(controller, "inspect", lambda number: replace(current, pr=pr))
    directory = tmp_path / "native"
    directory.mkdir()
    (directory / "submission.json").write_text(
        json.dumps({"payload": intent.payload, "attempts": [], "phase": phase}), encoding="utf-8"
    )

    def never_submit(*args, **kwargs):
        pytest.fail("changed candidate must not submit another merge")

    result = execute(
        intent,
        project.project,
        directory,
        controller,
        evidence=evidence,
        generation=1,
        runner=never_submit,
    )
    if phase in {"prepared", "normal-refused"}:
        assert result.state == "rejected"
        assert result.receipt["definitive"] is True
        assert result.receipt["submission"] == "not-submitted"
        assert result.receipt["remote_head_changed"] is True
    else:
        assert result.state == "sent_unknown"
        assert not result.receipt.get("remote_head_changed")
