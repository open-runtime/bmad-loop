"""Only the durable launch can grant checkpoint scope and baseline authority."""

import copy
import json

import pytest
from test_campaign_checkpoint_runtime import action
from test_campaign_checkpoint_runtime import checkpoint_case as checkpoint_case
from test_campaign_checkpoint_runtime import progress
from test_campaign_runtime import production_case as production_case

from bmad_loop import campaign_checkpoint as checkpoint
from bmad_loop import campaign_runtime, verify
from bmad_loop.campaign_model import CampaignError


@pytest.mark.parametrize("tamper", ["scope", "baseline"])
def test_same_claim_request_cannot_widen_scope_or_move_baseline(checkpoint_case, tamper):
    case = checkpoint_case
    row = progress(case, "actor outside its owned scope\n", path="outside-actor.txt")
    directory = case.monitor.directory
    request = json.loads((directory / "request.json").read_text())
    original = copy.deepcopy(
        next(value for value in case.snapshot.outbox if value.id == request["launch_id"]).payload
    )
    if tamper == "scope":
        request["checkpoint_paths"] = ["*"]
    else:
        request["checkpoint_base"] = row["head"]
        row["baseline"] = row["head"]
        row["committed_paths"] = []
        (directory / checkpoint.NAME).write_text(json.dumps(row))
    (directory / "request.json").write_text(json.dumps(request))
    pending = action(case, row)
    with pytest.raises(CampaignError, match="scope/baseline/repair authority differs"):
        case.runtime._perform_checkpoint(case.snapshot, pending)
    assert (
        next(value for value in case.snapshot.outbox if value.id == request["launch_id"]).payload
        == original
    )
    assert case.pusher.remote_head() == original["checkpoint_base"]


def test_launch_requires_exact_durably_accepted_head_before_preparing_child(
    checkpoint_case, monkeypatch
):
    case = checkpoint_case
    progress(case, "head changed after launch authority was sealed\n")
    launch = next(value for value in case.snapshot.outbox if value.id == case.unit.claim.launch_id)
    monkeypatch.setattr(
        campaign_runtime,
        "prepare_launch",
        lambda *args: pytest.fail("prepared a child for a different accepted head"),
    )
    observed = case.runtime._perform_launch(case.snapshot, launch)
    assert observed.state == "rejected"
    assert "exact accepted head" in observed.reason
    assert verify.rev_parse_head(case.workspace) != launch.payload["checkpoint_base"]
