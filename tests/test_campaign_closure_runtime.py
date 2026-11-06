"""A native ticket closure uses real stages and the complete delivery pipeline."""

import json
from dataclasses import replace

import pytest
from conftest import DEFAULT_TREE, git, install_bmad_v7, write_plan, write_ticket_tree
from test_campaign_runtime import diagnose
from test_campaign_runtime import production_case as production_case
from test_campaign_runtime import run_case

from bmad_loop import campaign_runtime, ticketlayout, verify
from bmad_loop.campaign_model import SourceSelector, UnitState


def ticket_campaign(case, *, second_done=True, private_store=False):
    layout = install_bmad_v7(case.repo)
    write_ticket_tree(layout, (DEFAULT_TREE[0],))
    write_plan(layout, "epic-cart", "story-cart-model-plan", 1, "done")
    write_plan(
        layout, "epic-cart", "story-cart-rules-plan", 2, "done" if second_done else "ready-for-dev"
    )
    ignored = case.repo / ".gitignore"
    ignored.write_text(
        (ignored.read_text() if ignored.exists() else "") + "\n_bmad/custom/*.user.toml\n",
        encoding="utf-8",
    )
    if private_store:
        if git(case.repo, "ls-files", "_bmad-output"):
            git(case.repo, "rm", "-r", "--cached", "_bmad-output")
        ignored.write_text(
            (ignored.read_text() if ignored.exists() else "") + "\n/_bmad-output/\n",
            encoding="utf-8",
        )
    git(case.repo, "add", "-A")
    git(case.repo, "commit", "-qm", "accepted native ticket baseline")
    git(case.repo, "push", "origin", "main")
    source = SourceSelector(
        "tickets",
        id="native",
        initiative=layout.initiative,
        epics=("epic-cart",),
        units=("1.1", "1.2") if second_done else ("1.1",),
    )
    spec = replace(
        case.snapshot.spec, name="native-closure", sources=(source,), units=(), ownership=()
    )
    _, case.snapshot = campaign_runtime.create(case.repo, spec)
    script = case.service.state_file.parent / "scripted-coding-cli.py"
    text = script.read_text(encoding="utf-8")
    text = text.replace(
        '"decomposition","planning_review"}', '"decomposition","planning_review","closure"}'
    )
    text = text.replace(
        '    if name=="planning_review":',
        """    if name=="closure":
        workspace=Path(request["workspace"])
        products=context["closure"]
        remote=json.loads(GH_FILE.read_text())
        matches=[row for row in remote["pulls"] if row["head"]["ref"]==request["branch"]]
        assert len(matches)==1 and matches[0]["draft"] and not matches[0]["merged"], "closure writer requires an unmerged early draft PR"
        epic=workspace/products["epic"]
        epic.write_text(epic.read_text().replace("status: in-progress","status: done"))
        retrospective=workspace/products["retrospective"]
        retrospective.write_text("# Cart retrospective\\n\\nAll native children are delivered and verified. Preserve their implementation; use these lessons in later epics.\\n")
        artifact["artifacts"]=[products["epic"],products["retrospective"]]
    if name=="planning_review":""",
    )
    script.write_text(text, encoding="utf-8")
    return layout


@pytest.mark.parametrize("private_store", [False, True])
def test_native_closure_authors_real_epic_and_retrospective_then_reviews_merges_and_delivers(
    production_case, private_store
):
    case = production_case
    layout = ticket_campaign(case, private_store=private_store)
    before = ticketlayout.epic_file(layout.initiative_folder, "epic-cart").read_bytes()
    assert b"status: in-progress" in before
    result = run_case(case, ticks=2200)
    assert result.phase == "delivered", diagnose(result)
    closure = next(unit for unit in result.units.values() if unit.spec.kind == "closure")
    assert closure.state == UnitState.DELIVERED
    assert closure.evidence["closure_manifest"]["canonical_head"]
    assert closure.evidence["developer_family"] == "anthropic"
    assert closure.evidence["review"]["provider_family"] == "openai"
    assert [row["kind"] for row in closure.evidence["source_artifacts"]] == [
        "closure",
        "closure-retrospective",
    ]
    assert closure.evidence["merge"]["merge_commit"]
    canonical = git(case.remote, "rev-parse", "main")
    for artifact in closure.evidence["source_artifacts"]:
        assert verify.file_bytes_at_revision(case.repo, canonical, artifact["review_path"])
    rows = [json.loads(line) for line in case.events.read_text().splitlines()]
    assert any(row["event"] == "planning-closure" for row in rows)
    assert not any(row["event"] == "execution-start" for row in rows)
    if private_store:
        assert (
            b"status: done"
            in ticketlayout.epic_file(layout.initiative_folder, "epic-cart").read_bytes()
        )
        assert ticketlayout.retrospective_doc(layout.initiative_folder, "epic-cart").read_bytes()


def test_unselected_unfinished_native_child_keeps_epic_open_without_closure_writer(production_case):
    case = production_case
    layout = ticket_campaign(case, second_done=False)
    result = run_case(case, ticks=650)
    closure = next(unit for unit in result.units.values() if unit.spec.kind == "closure")
    assert closure.state != UnitState.DELIVERED
    assert b"status: in-progress" in verify.file_bytes_at_revision(
        case.repo,
        git(case.remote, "rev-parse", "main"),
        ticketlayout.epic_file(layout.initiative_folder, "epic-cart")
        .relative_to(case.repo)
        .as_posix(),
    )
    rows = [json.loads(line) for line in case.events.read_text().splitlines()]
    assert not any(row["event"] == "planning-closure" for row in rows)
