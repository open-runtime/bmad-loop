"""Actual landing verification and automatic repair retain delivery obligations."""

import dataclasses
import functools
import sys

import pytest
from conftest import git
from test_campaign_runtime import diagnose
from test_campaign_runtime import production_case as production_case
from test_campaign_runtime import run_case

from bmad_loop import campaign_landing_acceptance as landing
from bmad_loop import campaign_native_merge
from bmad_loop.campaign_model import ResolutionSpec, UnitState
from bmad_loop.campaign_scheduler import delegates_tracking_slot
from bmad_loop.childrun import ChildRun


def debt_for(unit, head):
    return {
        "state": "repair_pending",
        "merge_commit": head,
        "failure": {"outcome": "verification_failed"},
    }


def test_landing_repair_is_deduplicated_and_does_not_depend_on_original_delivery(production_case):
    case = production_case
    snapshot = case.snapshot
    snapshot.spec = dataclasses.replace(
        snapshot.spec, resolution=ResolutionSpec(mode="bounded-auto")
    )
    unit = snapshot.units["source:project:first"]
    unit.state = UnitState.MERGED_PENDING_SYNC
    unit.evidence["landing_acceptance"] = debt_for(unit, git(case.repo, "rev-parse", "HEAD"))
    landing.admit_repair(
        case.runtime,
        snapshot,
        unit,
        {"merge_commit": unit.evidence["landing_acceptance"]["merge_commit"]},
    )
    repair = snapshot.units[unit.evidence["landing_acceptance"]["repair_unit"]]
    assert repair.spec.parent == unit.id and unit.id not in repair.spec.after
    assert repair.spec.acceptance == unit.spec.acceptance
    assert repair.spec.scopes == unit.spec.scopes
    assert delegates_tracking_slot(snapshot, unit)
    count = len(snapshot.units)
    landing.admit_repair(
        case.runtime,
        snapshot,
        unit,
        {"merge_commit": unit.evidence["landing_acceptance"]["merge_commit"]},
    )
    assert len(snapshot.units) == count
    assert not landing.satisfied(unit)


def test_landing_repair_budget_survives_nested_failures(production_case):
    case = production_case
    snapshot = case.snapshot
    snapshot.spec = dataclasses.replace(
        snapshot.spec, resolution=ResolutionSpec(mode="bounded-auto", max_attempts_per_unit=1)
    )
    original = snapshot.units["source:project:first"]
    original.state = UnitState.MERGED_PENDING_SYNC
    head = git(case.repo, "rev-parse", "HEAD")
    original.evidence["landing_acceptance"] = debt_for(original, head)
    landing.admit_repair(case.runtime, snapshot, original, {"merge_commit": head})
    repair = snapshot.units[original.evidence["landing_acceptance"]["repair_unit"]]
    repair.state = UnitState.MERGED_PENDING_SYNC
    repair.evidence["landing_acceptance"] = debt_for(repair, head)
    landing.admit_repair(case.runtime, snapshot, repair, {"merge_commit": head})
    assert repair.hold.kind == "landing_repair_required"
    assert len(original.bindings["landing_repair_units"]) == 1


@pytest.mark.parametrize("repair_needed", [False, True])
def test_native_campaign_rechecks_actual_landing_then_completes_tracking(
    production_case, monkeypatch, repair_needed
):
    case = production_case
    snapshot = case.store.load(case.snapshot.id)
    template = case.runtime.template(snapshot)
    snapshot.spec = dataclasses.replace(
        snapshot.spec,
        github=dataclasses.replace(
            snapshot.spec.github, merge_mode="direct-authorized", allow_admin_merge=True
        ),
        resolution=ResolutionSpec(mode="bounded-auto"),
    )
    snapshot.config_revision += 1
    path, digest = dataclasses.replace(
        template,
        config_revision=snapshot.config_revision,
        directory=template.directory.parent / "configuration-2",
    ).save()
    snapshot.bindings["execution_template"] = {"path": str(path), "digest": digest}
    case.store.save(snapshot, snapshot.revision)
    script = case.events.parent / "scripted-coding-cli.py"
    content = script.read_text().replace(
        '"publication_review","admission_review","requirements"',
        '"landing_repair","publication_review","admission_review","requirements"',
    )
    content = content.replace(
        '    if name=="publication_review":',
        '    if name=="landing_repair":\n        workspace=Path(request["workspace"])\n        changed=[]\n        for relative in request["unit"]["scopes"]:\n            path=workspace/relative\n            path.write_text("accepted repaired landing\\n")\n            changed.append(path)\n        verify.commit_paths(workspace,"fix: repair merged acceptance",changed)\n    if name=="publication_review":',
    )
    script.write_text(content)
    original_execute = campaign_native_merge.execute
    monkeypatch.setattr(
        campaign_native_merge,
        "prepare",
        functools.partial(campaign_native_merge.prepare, executable=sys.executable),
    )
    moved = []
    calls = []

    def native_gh(argv, **kwargs):
        number = int(argv[3])
        calls.append(number)
        with case.service.lock:
            pr = case.service._pull(number)
            head = argv[argv.index("--match-head-commit") + 1]
            assert head == pr["head"]["sha"]
            base = case.service._base()
            changing = not moved and (not repair_needed or "dependent" in pr["title"].lower())
            if changing:
                # New accepted main commit arrives after the last pre-merge read.
                tree = git(case.remote, "rev-parse", base + "^{tree}")
                newer = git(
                    case.remote, "commit-tree", tree, "-p", base, "-m", "concurrent accepted main"
                )
                git(case.remote, "update-ref", "refs/heads/main", newer, base)
                base = newer
                moved.append(number)
            tree = git(case.remote, "rev-parse", head + "^{tree}")
            if changing and repair_needed:
                # A merged product can fail even though its pre-merge H passed.
                broken = case.events.parent / "broken-landing.txt"
                broken.write_text("broken landing\n")
                blob = git(case.remote, "hash-object", "-w", str(broken))
                git(case.remote, "read-tree", tree)
                git(
                    case.remote,
                    "update-index",
                    "--cacheinfo",
                    "100644," + blob + ",src/dependent.txt",
                )
                tree = git(case.remote, "write-tree")
            merged = git(
                case.remote, "commit-tree", tree, "-p", base, "-p", head, "-m", "native merge"
            )
            git(case.remote, "update-ref", "refs/heads/main", merged, base)
            pr.update(merged=True, state="closed", merge_commit_sha=merged)
            case.service._save()
        return ChildRun(0, "", "")

    monkeypatch.setattr(
        campaign_native_merge, "execute", functools.partial(original_execute, runner=native_gh)
    )
    result = run_case(case, timeout_s=90)
    assert result.phase == "delivered", diagnose(result)
    expected = 4 if repair_needed else 3
    assert len(calls) == expected and len(set(calls)) == expected
    unit = next(row for row in result.units.values() if row.bindings["pr"]["number"] == moved[0])
    debt = unit.evidence["landing_acceptance"]
    assert debt["state"] == "accepted"
    if repair_needed:
        repair = result.units[debt["repair_unit"]]
        assert debt["failure"]["head"] == debt["merge_commit"]
        assert repair.spec.kind == "landing_repair"
        assert repair.state == UnitState.DELIVERED
        assert debt["repaired"]["acceptance"]["complete"]
        assert "accepted repaired landing" in git(case.remote, "show", "main:src/dependent.txt")
    else:
        assert debt["verification"]["head"] == debt["merge_commit"]
        assert debt["review"]["head"] == debt["merge_commit"]
        assert debt["acceptance"]["complete"]
    assert unit.evidence["integrated"]["head"] != debt["merge_commit"]
    assert all(row.state == UnitState.DELIVERED for row in result.units.values())
