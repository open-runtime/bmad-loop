"""A real fast child still passes the coordinator's terminal ownership gate."""

import dataclasses

from conftest import git
from test_campaign_runtime import production_case as production_case
from test_campaign_runtime import run_case

from bmad_loop import campaign_runtime


def test_child_rename_is_held_even_when_periodic_checkpoints_are_disabled(
    production_case, monkeypatch
):
    case = production_case
    unrelated = case.repo / "unowned-original.txt"
    unrelated.write_text("accepted first\n", encoding="utf-8")
    git(case.repo, "add", unrelated.name)
    git(case.repo, "commit", "-m", "existing unowned product")
    git(case.repo, "push", "origin", "HEAD:main")
    git(case.repo, "config", "diff.renames", "true")
    spec = dataclasses.replace(
        case.snapshot.spec,
        units=(),
        sources=(dataclasses.replace(case.snapshot.spec.sources[0], units=("first",)),),
        ownership=tuple(
            row for row in case.snapshot.spec.ownership if row[0] == "source:project:first"
        ),
        checkpoints=dataclasses.replace(case.snapshot.spec.checkpoints, push=False),
    )
    _, case.snapshot = campaign_runtime.create(case.repo, spec)
    script = case.events.parent / "scripted-coding-cli.py"
    text = script.read_text().replace('if suffix in {"first","second"}:', "if False:")
    text = text.replace(
        '    product.write_text("accepted "+suffix+"\\n")',
        '    if suffix=="first": (workspace/"unowned-original.txt").rename(product)\n    product.write_text("accepted "+suffix+"\\n")',
    )
    text = text.replace(
        'verify.commit_paths(workspace,"feat: implement "+suffix,[product,spec])',
        'verify.commit_paths(workspace,"feat: implement "+suffix,[product,spec]+([workspace/"unowned-original.txt"] if suffix=="first" else []))',
    )
    script.write_text(text)
    original = case.runtime.apply

    def apply(snapshot, action, observation):
        original(snapshot, action, observation)
        if observation.data.get("receipt", {}).get("outcome") == "ownership_uncertain":
            case.store.enqueue(snapshot.id, "stop")

    monkeypatch.setattr(case.runtime, "apply", apply)
    result = run_case(case)
    first = result.units["source:project:first"]
    assert first.hold and first.hold.kind == "ownership_uncertain"
    assert "unowned-original.txt" in first.hold.reason
    assert first.claim.quiescent
    assert not any(action.kind == "checkpoint" for action in result.outbox)
    pr = next(
        row for row in case.service.pulls.values() if row["head"]["ref"] == first.bindings["branch"]
    )
    assert pr["draft"] and not pr["merged"]
