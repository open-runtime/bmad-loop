"""A full imported done baseline completes through real verification drafts."""

from dataclasses import asdict, replace

from conftest import git, write_spec
from test_campaign_runtime import diagnose
from test_campaign_runtime import production_case as production_case
from test_campaign_runtime import run_case

from bmad_loop import campaign_runtime, verify
from bmad_loop.campaign_model import UnitState


def test_preexisting_done_baseline_uses_no_engine_and_reaches_delivery_through_drafts(
    production_case,
):
    case = production_case
    baseline = verify.rev_parse_head(case.repo)
    for name in ("first", "second", "dependent"):
        path = case.repo / "specs/epic/stories" / (name + "-story.md")
        write_spec(path, "done", baseline)
        code = case.repo / "src" / (name + ".txt")
        code.parent.mkdir(exist_ok=True)
        code.write_text("accepted " + name + " existing\n")
    git(case.repo, "add", "-A")
    git(case.repo, "commit", "-qm", "existing completed baseline")
    git(case.repo, "push", "origin", "main")
    spec = replace(case.snapshot.spec, name="existing-canonical-baseline", units=())
    _, current = campaign_runtime.create(case.repo, spec)
    case.snapshot = current
    result = run_case(case, ticks=1600)
    assert result.phase == "delivered", diagnose(result)
    assert all(unit.state == UnitState.DELIVERED for unit in result.units.values())
    assert len(case.service.pulls) == 3
    for unit in result.units.values():
        history = unit.evidence["worker_history"]
        assert unit.evidence["existing_source_verified"]["no_development_session"] is True
        assert any(receipt["outcome"] == "existing_verified" for receipt in history)
        assert not [receipt for receipt in history if receipt.get("run_dir")]
        assert unit.evidence["review"]["validated"]["verdict"] == "approved"
        assert verify.is_ancestor(
            case.remote, unit.evidence["integrated"]["head"], git(case.remote, "rev-parse", "main")
        )


def test_same_delivered_source_reuses_actual_prior_delivery_after_fresh_remote_readback(
    production_case,
):
    case = production_case
    # The first campaign is ordinary production development and captures actual
    # source/verification/review/merge/tracker evidence for the second import.
    first = run_case(case)
    assert first.phase == "delivered", diagnose(first)
    git(case.repo, "fetch", "origin", "main")
    git(case.repo, "merge", "--ff-only", "origin/main")
    source = replace(first.spec.sources[0], units=("first",))
    spec = replace(
        first.spec,
        name="reconcile-actual-prior-delivery",
        sources=(source,),
        units=(),
        ownership=tuple(
            (key, scopes) for key, scopes in first.spec.ownership if key == "source:project:first"
        ),
    )
    # Selector narrowing changes the explicit source selector, so the fast path
    # conservatively chooses a new verification draft unless an identical prior
    # selector exists. Use the original full selector to exercise exact reuse.
    spec = replace(spec, sources=first.spec.sources, ownership=first.spec.ownership)
    _, current = campaign_runtime.create(case.repo, spec)
    # Reproduce the reviewed graph edge from the original campaign.
    current.units["source:project:dependent"].spec = first.units["source:project:dependent"].spec
    current.graph_revision += 1
    current = case.store.save(current, current.revision)
    case.snapshot = current
    before = len(case.service.pulls)
    result = run_case(case, ticks=1000)
    assert result.phase == "delivered", {
        "current": diagnose(result),
        "prior_hierarchy": {
            key: value
            for key, value in first.bindings.items()
            if "hierarchy" in key or "remaining" in key
        },
        "prior_closure_effects": [
            asdict(row) for row in first.outbox if row.kind == "hierarchy-close"
        ],
        "issue2": case.service.issues.get(2),
        "issue2_children": [
            case.service.issues.get(number)
            for number, parent in case.service.parents.items()
            if parent.get("number") == 2
        ],
    }
    assert all(unit.evidence.get("prior_delivery") for unit in result.units.values())
    assert len(case.service.pulls) == before
    assert not [row for row in result.outbox if row.kind == "launch"]
