"""An external PR writer must converge through the normal delivery supervisor.

The coding sessions and GitHub API are scripted; candidate branches, remote bot
commits, integration, review invalidation, publication and tracking are real.
"""

import dataclasses
import json
import os
import subprocess

import pytest
from conftest import git
from test_campaign_runtime import diagnose
from test_campaign_runtime import production_case as production_case
from test_campaign_runtime import run_case

from bmad_loop import campaign_runtime, verify
from bmad_loop.campaign_model import UnitState


def bot_commit(case, branch, index):
    """Append an owned file on the server, without touching the worker checkout."""
    env = {**os.environ, "GIT_INDEX_FILE": str(index)}

    def run(*args, data=None):
        return (
            subprocess.run(
                ["git", "-C", str(case.remote), *args],
                input=data,
                capture_output=True,
                check=True,
                env=env,
            )
            .stdout.decode()
            .strip()
        )

    before = run("rev-parse", "refs/heads/" + branch)
    blob = run("hash-object", "-w", "--stdin", data=b"accepted bot review fix\n")
    run("read-tree", before)
    run("update-index", "--add", "--cacheinfo", "100644," + blob + ",src/bot-first.txt")
    tree = run("write-tree")
    head = run(
        "-c",
        "user.name=Scripted Review Bot",
        "-c",
        "user.email=bot@example.test",
        "commit-tree",
        tree,
        "-p",
        before,
        "-m",
        "Review fix from an external writer",
    )
    run("update-ref", "refs/heads/" + branch, head, before)
    return before, head


@pytest.mark.parametrize("injection", ["before-admission", "before-push", "after-review"])
def test_owned_bot_contribution_reaches_reviewed_merge_and_tracking(
    production_case, tmp_path, injection
):
    case = production_case
    key = "source:project:first"
    ownership = tuple(
        (unit_id, (*paths, "src/bot-first.txt") if unit_id == key else paths)
        for unit_id, paths in case.snapshot.spec.ownership
    )
    _, case.snapshot = campaign_runtime.create(
        case.repo, dataclasses.replace(case.snapshot.spec, units=(), ownership=ownership)
    )
    dependent = case.snapshot.units["source:project:dependent"]
    dependent.spec = dataclasses.replace(dependent.spec, after=(key,))
    case.snapshot.graph_revision += 1
    case.snapshot = case.store.save(case.snapshot, case.snapshot.revision)

    original_proposals = case.runtime.proposals
    changed = {}

    def proposals(snapshot):
        unit = snapshot.units[key]
        if injection == "before-admission":
            ready = unit.state == UnitState.READY and not unit.evidence.get("admission")
        elif injection == "before-push":
            ready = (
                unit.state == UnitState.SYNCING
                and unit.evidence.get("integrated")
                and unit.evidence.get("pushed") != unit.evidence["integrated"]["head"]
            )
        else:
            ready = unit.state == UnitState.READY_TO_PUBLISH and unit.evidence.get("review")
        if ready and not changed and (not unit.claim or unit.claim.quiescent):
            with case.service.lock:
                before, head = bot_commit(case, unit.bindings["branch"], tmp_path / "bot.index")
                changed.update(
                    remote_before=before,
                    bot_head=head,
                    local_head=unit.bindings["head"],
                    workspace=unit.bindings["workspace"],
                    pr=unit.bindings["pr"]["number"],
                )
                case.service._pull(changed["pr"])
                case.service._save()
        return original_proposals(snapshot)

    case.runtime.proposals = proposals
    result = run_case(case, timeout_s=300)
    assert changed, "the bot contribution must occur before asserting recovery"
    assert result.phase == "delivered", diagnose(result)
    unit = result.units[key]
    assert unit.state == UnitState.DELIVERED
    assert unit.bindings["pr"]["number"] == changed["pr"]
    assert unit.bindings["workspace"] == changed["workspace"]
    assert len(case.service.pulls) == 3
    canonical = git(case.remote, "rev-parse", "main")
    assert verify.is_ancestor(case.remote, changed["bot_head"], canonical)
    assert verify.is_ancestor(case.remote, changed["local_head"], canonical)
    assert git(case.remote, "show", canonical + ":src/bot-first.txt") == "accepted bot review fix"
    contributions = unit.evidence["external_contributions"]
    assert any(
        row["sha"] == changed["bot_head"] and row["model_status"] == "unobserved_external"
        for item in contributions
        for row in item["provenance"]["commits"]
    )
    assert unit.evidence["developer_families"] == ["openai"]
    assert not unit.evidence.get("remote_head_sync")
    rows = [json.loads(line) for line in case.events.read_text().splitlines()]
    reviews = [row for row in rows if row["event"] == "publication-review" and row["unit"] == key]
    assert reviews[-1]["head"] == unit.evidence["integrated"]["head"]
    assert verify.is_ancestor(case.remote, changed["bot_head"], reviews[-1]["head"])
    assert reviews[-1]["base"] == case.service.merge_bases[changed["pr"]]
    assert key in case.service.closed_units
    if injection == "after-review":
        assert any(row["head"] == changed["local_head"] for row in reviews[:-1])
    if injection == "before-admission":
        execution = next(
            receipt
            for receipt in unit.evidence["worker_history"]
            if receipt["outcome"] == "locally_complete"
        )
        assert verify.is_ancestor(case.remote, changed["bot_head"], execution["base"])
