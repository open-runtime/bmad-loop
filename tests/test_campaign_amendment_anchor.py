"""Reviewed source pushes become owned checkpoints, without trusting later edits."""

import dataclasses

import pytest
import test_campaign_branch_integration
from conftest import git

from bmad_loop import campaign_intake_amendment as amendment
from bmad_loop import campaign_remote_head as remote
from bmad_loop import campaign_workers as workers
from bmad_loop.campaign_intake_flow import INBOX
from bmad_loop.campaign_model import (
    CampaignSnapshot,
    CampaignSpec,
    CampaignUnit,
    UnitSpec,
    UnitState,
    unit_from_dict,
)
from bmad_loop.campaign_runtime import CampaignRuntime
from bmad_loop.campaign_store import CampaignStore

branch_case = test_campaign_branch_integration.branch_case
commit = test_campaign_branch_integration.commit


@pytest.mark.parametrize("later_external_edit", [False, True])
def test_confirmed_amendment_anchors_only_its_own_push(branch_case, tmp_path, later_external_edit):
    repo, remote_repo, bot, _, baseline, context, request = branch_case
    spec = unit_from_dict(request()["unit"])
    unit = CampaignUnit(spec, UnitState.READY)
    unit.bindings.update(
        workspace=str(repo),
        branch="owned-unit",
        head=baseline,
        last_pushed_head=baseline,
        remote_head_anchor=baseline,
        pr={"number": 12, "node_id": "PR_owned"},
    )
    plan = CampaignUnit(
        UnitSpec("plan", "s", "project", "Plan", kind="planning"), UnitState.DELIVERED
    )
    plan.evidence["planning_flow"] = {"state": "activated"}
    plan.bindings["intake_control"] = {
        "schema_version": 1,
        "state": "refreshing",
        "proposal_id": "proposal",
        "amendments": {unit.id: {}},
        "attempt_id": "attempt",
    }
    snapshot = CampaignSnapshot(
        context.campaign_id,
        "owner/repo",
        CampaignSpec("amendment"),
        {unit.id: unit, plan.id: plan},
        config_revision=context.config_revision,
        remote_url=str(remote_repo),
    )
    snapshot.bindings[INBOX] = {
        "proposals": {
            "proposal": {
                "state": "awaiting_refresh",
                "units": [dataclasses.asdict(spec)],
                "input_versions": [],
            }
        },
        "records": {},
        "latest": {},
    }
    runtime = CampaignRuntime(repo, CampaignStore(tmp_path / "state"))
    path = "docs/bmad-campaign/reviewed-source.md"
    approved = commit(repo, path, "Reviewed amended acceptance\n", "approved amendment")
    pushed = runtime._pusher(snapshot, unit).push("refresh-refresh", approved)
    assert pushed.state == "confirmed" and pushed.head == approved
    remote_head = approved
    if later_external_edit:
        git(bot, "pull", "--ff-only", "origin", "owned-unit")
        remote_head = commit(bot, path, "External acceptance change\n", "external source edit")
        git(bot, "push", "origin", "owned-unit")
    receipt = {
        "action_id": "refresh",
        "unit": unit.id,
        "planning_unit": plan.id,
        "proposal_id": "proposal",
        "pr_number": 12,
        "branch": "owned-unit",
        "old_head": baseline,
        "head": approved,
        "effective_source": {},
        "canonical_source": {},
        "source_revision": "reviewed-source",
        "generation": str(tmp_path / "source-generation"),
    }
    amendment.apply(snapshot, unit, receipt)
    assert unit.bindings["head"] == approved
    assert unit.bindings["last_pushed_head"] == approved
    assert unit.bindings["remote_head_anchor"] == approved
    assert unit.evidence["amendment_history"][-1]["receipt"] == receipt
    pr = {
        "number": 12,
        "node_id": "PR_owned",
        "state": "open",
        "merged": False,
        "head": {"ref": "owned-unit", "sha": remote_head, "repo": {"full_name": "owner/repo"}},
        "base": {"ref": "main", "repo": {"full_name": "owner/repo"}},
    }
    observation = remote.observe(runtime, snapshot, unit, pr=pr)
    if not later_external_edit:
        assert observation["remote_head_current"] == approved
        assert "remote_head_sync" not in observation
        return
    debt = observation["remote_head_sync"]
    assert debt["anchor"] == approved and debt["head"] == remote_head
    incoming = request()
    incoming["remote_head_sync"] = debt
    incoming["source_artifacts"] = [{"path": path}]
    result = workers._integrate(incoming, context)
    assert result["outcome"] == "ownership_uncertain"
    assert "protected source or policy" in result["error"]
    assert git(repo, "rev-parse", "HEAD") == approved
    assert (repo / path).read_text() == "Reviewed amended acceptance\n"
