"""Selected delivery retains full merge/landing debt and legacy resume contracts."""

import functools
import json
import sys
from dataclasses import replace
from pathlib import Path

from conftest import EntrySpec, EpicSpec, git, install_bmad_v7, write_plan, write_ticket_tree
from test_campaign_runtime import diagnose
from test_campaign_runtime import production_case as production_case
from test_campaign_runtime import run_case

from bmad_loop import campaign_native_merge, campaign_runtime, ticketlayout
from bmad_loop.campaign_jobs import digest
from bmad_loop.campaign_model import ResolutionSpec, SourceSelector, UnitState
from bmad_loop.childrun import ChildRun


def test_old_wire_source_and_frozen_execution_resume_through_delivery(production_case):
    case = production_case
    original = case.runtime.proposals
    requested = []

    def proposals(snapshot):
        if not requested and any(
            unit.bindings.get("effective_source") for unit in snapshot.units.values()
        ):
            requested.append(True)
            case.store.enqueue(snapshot.id, "stop", request_id="old-wire-resume")
        return original(snapshot)

    case.runtime.proposals = proposals
    stopped = run_case(case)
    assert stopped.phase == "stopped"
    frozen = stopped.bindings["execution_template"].copy()
    template_bytes = Path(frozen["path"]).read_bytes()
    config_digest = digest(stopped.config_snapshot)
    from bmad_loop.campaign_model import snapshot_to_dict, spec_to_dict

    wire = snapshot_to_dict(stopped)
    assert all("completion_scope" not in source for source in wire["spec"]["sources"])
    bindings = {
        key: unit.bindings["effective_source"]
        for key, unit in stopped.units.items()
        if unit.bindings.get("effective_source")
    }
    assert bindings and all("completion_scope" not in source for source in bindings.values())
    prior_spec_digest = digest(wire["spec"])
    case.runtime = campaign_runtime.CampaignRuntime(
        case.repo,
        case.store,
        gateway=case.runtime.gateway(stopped),
        capacity_observer=case.runtime.capacity_observer,
        clock=case.runtime.clock,
    )
    resumed = case.store.load(stopped.id)
    assert digest(spec_to_dict(resumed.spec)) == prior_spec_digest
    result = run_case(case)
    assert result.phase == "delivered", diagnose(result)
    assert result.bindings["execution_template"] == frozen
    assert Path(frozen["path"]).read_bytes() == template_bytes
    assert digest(result.config_snapshot) == config_digest
    assert all(
        result.units[key].bindings["effective_source"] == source for key, source in bindings.items()
    )


def test_selected_native_stories_complete_dependency_and_landing_repair_without_parent_closure(
    production_case,
    monkeypatch,
):
    case = production_case
    layout = install_bmad_v7(case.repo)
    write_ticket_tree(
        layout,
        (
            EpicSpec(
                1,
                "epic-cart",
                "Cart",
                (
                    EntrySpec(1, "Cart model"),
                    EntrySpec(2, "Cart rules", after=(1,)),
                    EntrySpec(3, "Unselected sibling"),
                ),
            ),
        ),
    )
    for number, stem, status in (
        (1, "story-cart-model-plan", "done"),
        (2, "story-cart-rules-plan", "done"),
        (3, "story-unselected-sibling-plan", "ready-for-dev"),
    ):
        write_plan(layout, "epic-cart", stem, number, status)
    for name in ("first", "dependent"):
        product = case.repo / "src" / (name + ".txt")
        product.parent.mkdir(exist_ok=True)
        product.write_text("accepted existing " + name + "\n")
    ignored = case.repo / ".gitignore"
    ignored.write_text(
        (ignored.read_text() if ignored.exists() else "") + "\n_bmad/custom/*.user.toml\n"
    )
    git(case.repo, "add", "-A")
    git(case.repo, "commit", "-qm", "accepted selected ticket baseline")
    git(case.repo, "push", "origin", "main")
    source = SourceSelector(
        "tickets",
        id="native",
        initiative=layout.initiative,
        units=("1.1", "1.2"),
        completion_scope="selected-units",
    )
    prefix = f"native:{layout.initiative}:"
    spec = replace(
        case.snapshot.spec,
        name="selected-native",
        sources=(source,),
        units=(),
        ownership=((prefix + "1.1", ("src/first.txt",)), (prefix + "1.2", ("src/dependent.txt",))),
        github=replace(
            case.snapshot.spec.github, merge_mode="direct-authorized", allow_admin_merge=True
        ),
        resolution=ResolutionSpec(mode="bounded-auto"),
    )
    _, case.snapshot = campaign_runtime.create(case.repo, spec)
    assert case.snapshot.units[prefix + "1.2"].spec.after == (prefix + "1.1",)
    before = {
        path.relative_to(case.repo).as_posix(): path.read_bytes()
        for path in layout.initiative_folder.rglob("*")
        if path.is_file()
    }
    script = case.events.parent / "scripted-coding-cli.py"
    text = script.read_text().replace(
        '"publication_review","admission_review","requirements"',
        '"landing_repair","publication_review","admission_review","requirements"',
    )
    text = text.replace(
        '    if name=="publication_review":',
        """    if name=="landing_repair":
        workspace=Path(request["workspace"])
        changed=[]
        for relative in request["unit"]["scopes"]:
            path=workspace/relative
            path.write_text("accepted repaired landing\\n")
            changed.append(path)
        verify.commit_paths(workspace,"fix: selected landing acceptance",changed)
    if name=="publication_review":""",
    )
    script.write_text(text)
    execute = campaign_native_merge.execute
    monkeypatch.setattr(
        campaign_native_merge,
        "prepare",
        functools.partial(campaign_native_merge.prepare, executable=sys.executable),
    )
    broken_once = []
    merged = []

    def native_gh(argv, **kwargs):
        number = int(argv[3])
        with case.service.lock:
            pr = case.service._pull(number)
            head = argv[argv.index("--match-head-commit") + 1]
            assert head == pr["head"]["sha"]
            base = case.service._base()
            tree = git(case.remote, "rev-parse", head + "^{tree}")
            if not broken_once and "Cart rules" in pr["title"]:
                assert (
                    len(merged) == 1
                ), "native declared dependency must deliver before dependent merge"
                broken = case.events.parent / "broken-selected-landing.txt"
                broken.write_text("broken actual landing\n")
                blob = git(case.remote, "hash-object", "-w", str(broken))
                git(case.remote, "read-tree", tree)
                git(
                    case.remote,
                    "update-index",
                    "--cacheinfo",
                    "100644," + blob + ",src/dependent.txt",
                )
                tree = git(case.remote, "write-tree")
                broken_once.append(number)
            landed = git(
                case.remote,
                "-c",
                "user.name=Scripted Bot",
                "-c",
                "user.email=bot@example.test",
                "commit-tree",
                tree,
                "-p",
                base,
                "-p",
                head,
                "-m",
                "native selected merge",
            )
            git(case.remote, "update-ref", "refs/heads/main", landed, base)
            pr.update(merged=True, state="closed", merge_commit_sha=landed)
            merged.append(number)
            case.service._save()
        return ChildRun(0, "", "")

    monkeypatch.setattr(
        campaign_native_merge, "execute", functools.partial(execute, runner=native_gh)
    )
    observed_debt = []
    original = case.runtime.proposals

    def proposals(snapshot):
        dependent = snapshot.units[prefix + "1.2"]
        debt = dependent.evidence.get("landing_acceptance", {})
        if (
            debt.get("repair_unit")
            and snapshot.units[debt["repair_unit"]].state != UnitState.DELIVERED
        ):
            assert dependent.state == UnitState.MERGED_PENDING_SYNC
            assert snapshot.phase != "delivered"
            observed_debt.append(debt["repair_unit"])
        return original(snapshot)

    case.runtime.proposals = proposals
    result = run_case(case)
    assert result.phase == "delivered", diagnose(result)
    assert observed_debt and len(set(observed_debt)) == 1
    assert len(merged) == 3 and len(set(merged)) == 3
    assert all(unit.state == UnitState.DELIVERED for unit in result.units.values())
    assert {unit.spec.kind for unit in result.units.values()} == {"story", "landing_repair"}
    assert "parent scope remains open" in result.bindings["hierarchy_remaining_scope"]
    assert len(case.service.milestones) == 1
    assert all(row["state"] == "open" for row in case.service.milestones.values())
    parents = [
        row for key, row in result.bindings["github_hierarchy"].items() if key.startswith("epic:")
    ]
    assert parents and all(case.service.issues[row["number"]]["state"] == "open" for row in parents)
    for relative, content in before.items():
        assert git(case.remote, "show", "main:" + relative) == content.decode().strip()
    retrospective = ticketlayout.retrospective_doc(layout.initiative_folder, "epic-cart")
    assert not retrospective.exists()
    assert "accepted repaired landing" in git(case.remote, "show", "main:src/dependent.txt")
    rows = [json.loads(line) for line in case.events.read_text().splitlines()]
    assert not any(row["event"] == "planning-closure" for row in rows)
