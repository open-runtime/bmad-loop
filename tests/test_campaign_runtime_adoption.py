"""Real worker/Git delivery adopts one explicitly fenced existing native PR."""

import dataclasses
import json
import time

import pytest
from conftest import git
from test_campaign_runtime import diagnose
from test_campaign_runtime import production_case as production_case
from test_campaign_runtime import run_case

from bmad_loop import campaign_adoption as adoption
from bmad_loop import verify
from bmad_loop.campaign_github import managed_marker
from bmad_loop.campaign_migration import MigrationItem, ServiceInventory
from bmad_loop.campaign_model import Hold, UnitState


@pytest.mark.parametrize(
    "mode,retire_provisional", [("continue", False), ("verify-existing", False), ("continue", True)]
)
def test_existing_pr_is_converted_to_draft_then_owned_verified_merged_and_tracked(
    production_case, mode, retire_provisional
):
    case = production_case
    first = case.snapshot.units["source:project:first"]
    workspace = case.events.parent / "workers" / "external-first"
    workspace.parent.mkdir(exist_ok=True)
    git(case.repo, "worktree", "add", "-b", "external-first", str(workspace), "HEAD")
    (workspace / "src").mkdir(exist_ok=True)
    (workspace / "src" / "first.txt").write_text(
        "accepted first\n" if mode == "verify-existing" else "accepted partial first\n",
        encoding="utf-8",
    )
    paths = ["src/first.txt"]
    if mode == "verify-existing":
        spec = workspace / "specs" / "epic" / "stories" / "first-story.md"
        spec.write_text(
            "---\nstatus: done\n---\n# First\nAccepted first behavior.\n", encoding="utf-8"
        )
        paths.append("specs/epic/stories/first-story.md")
    git(workspace, "add", *paths)
    git(workspace, "commit", "-qm", "external implementation retained verbatim")
    original = git(workspace, "rev-parse", "HEAD")
    git(workspace, "push", "origin", "external-first")
    pr = case.service.request(
        "POST",
        "repos/a/b/pulls",
        {
            "head": "external-first",
            "base": "main",
            "title": "First",
            "body": "Human-owned PR discussion and design context.",
            "draft": False,
        },
    ).data
    # Real native REST responses contain the head repository as well as base.
    case.service.pulls[pr["number"]]["head"]["repo"] = {"full_name": "a/b"}
    case.service._save()
    predecessor = None
    if retire_provisional:
        old = workspace.parent / "unused-campaign-first"
        git(case.repo, "worktree", "add", "-b", "unused-campaign-first", str(old), "HEAD")
        spec = old / "docs" / "bmad-campaign" / "provisional-first.md"
        spec.parent.mkdir(parents=True)
        spec.write_text("# First\n\nAccepted source intent.\n", encoding="utf-8")
        git(old, "add", str(spec.relative_to(old)))
        git(old, "commit", "-qm", "specify unused campaign story")
        git(old, "push", "origin", "unused-campaign-first")
        predecessor = case.service.request(
            "POST",
            "repos/a/b/pulls",
            {
                "head": "unused-campaign-first",
                "base": "main",
                "title": "Provisional first",
                "body": managed_marker(case.snapshot.id, first.id, "pr"),
                "draft": True,
            },
        ).data
        first.state = UnitState.READY
        first.hold = Hold(
            "overlap", "Use the owner-selected existing implementation", id="admission"
        )
        first.bindings.update(
            workspace=str(old),
            branch="unused-campaign-first",
            base=git(case.repo, "rev-parse", "HEAD"),
            head=git(old, "rev-parse", "HEAD"),
            pr={
                "number": predecessor["number"],
                "node_id": predecessor["node_id"],
                "url": predecessor["html_url"],
            },
            spec_path=spec.relative_to(old).as_posix(),
        )
        case.snapshot = case.store.save(case.snapshot, case.snapshot.revision)
    _, _, source_id = adoption.source_binding(case.repo, case.snapshot, first.id)
    manifest = adoption.AdoptionManifest(
        1,
        case.snapshot.id,
        first.id,
        "a/b",
        case.snapshot.common_dir,
        pr["number"],
        pr["node_id"],
        "external-first",
        original,
        pr["base"]["sha"],
        "main",
        source_id,
        "external-first",
        "actor-v1",
        "binding-v1",
        "fence-v1",
        "explicit owner-selected existing PR handoff",
        mode=mode,
        workspace=str(workspace),
    )
    details = {
        "repository": "a/b",
        "pr_number": manifest.pr_number,
        "pr_node_id": manifest.pr_node_id,
        "head": original,
        "base": manifest.base,
        "branch": manifest.branch,
        "source_unit": first.id,
        "source_identity": source_id,
        "binding_revision": "binding-v1",
        "fence_revision": "fence-v1",
        "workspace": str(workspace),
        "preserved_clean": True,
        "resumable_binding_verified": True,
        "admissions_open": False,
        "active_workers": 0,
    }
    actor = MigrationItem(
        "external-first",
        "workspace",
        "actor-v1",
        "installed-owner-handoff",
        "stopped",
        "dead",
        "unowned",
        (first.id,),
        details,
    )
    provider = adoption.ProviderIdentity(
        "handoff", "installed:observer", "owner-handoff", "1", "a" * 64
    )

    def observer(*args):
        return ServiceInventory(
            "installed-owner-handoff", case.snapshot.common_dir, time.time(), "service-v1", (actor,)
        )

    def resolver(*args, **kwargs):
        return (provider, observer)

    case.runtime.adoption_resolver = resolver
    path = case.events.parent / "adoption.json"
    path.write_text(json.dumps(dataclasses.asdict(manifest)), encoding="utf-8")
    payload = adoption.prepare_request(
        case.repo,
        case.snapshot,
        first.id,
        path,
        "handoff",
        gateway=case.runtime.gateway(case.snapshot),
        resolver=resolver,
    )
    case.store.enqueue(case.snapshot.id, "adopt", unit=first.id, adoption=payload)
    script = case.events.parent / "scripted-coding-cli.py"
    text = script.read_text(encoding="utf-8")
    # Existing PR handoff has an additional native draft/fence transaction, so
    # this fixture does not require the other story to wait for its first coder.
    # The base production canary separately proves simultaneous story dispatch.
    text = text.replace('if suffix in {"first","second"}:', "if False:")
    # Continuation must cite the exact observed existing PR; the adopted branch
    # does not become permission to ignore other competing work.
    needle = '    return SessionResult(status="completed",session_id="scripted-"+request["launch_id"],result_json=artifact)'
    text = text.replace(
        needle,
        """    if name=="admission_review" and request["unit_id"]=="source:project:first":
        pull=next(row for row in context["inventory"]["pulls"] if row["head"]["ref"]=="external-first")
        artifact["overlap"].update(disposition="continue_existing_work",adopted_pr=pull["number"],evidence_ids=["pr:"+str(pull["number"])+"@"+pull["head"]["sha"]],reason="Explicit owner-qualified existing PR handoff; retain its accepted implementation and remaining source work")
""" + needle,
    )
    script.write_text(text, encoding="utf-8")
    result = run_case(case)
    if result.phase != "delivered":
        pytest.fail(json.dumps(diagnose(result), indent=2))
    delivered = result.units[first.id]
    assert delivered.state == UnitState.DELIVERED
    assert delivered.bindings["pr"]["number"] == pr["number"]
    assert delivered.bindings["workspace"] == str(workspace)
    assert len(case.service.pulls) == (4 if retire_provisional else 3)
    assert (
        case.service.pulls[pr["number"]]["body"] == "Human-owned PR discussion and design context."
    )
    assert verify.is_ancestor(case.remote, original, delivered.evidence["merge"]["merge_commit"])
    assert case.service.issues[delivered.bindings["issue"]["number"]]["state"] == "closed"
    conversions = [
        row
        for row in case.service.calls
        if row[0] == "POST"
        and row[1] == "graphql"
        and "convertPullRequestToDraft" in row[2].get("query", "")
    ]
    assert conversions and conversions[0][2]["variables"]["input"]["pullRequestId"] == pr["node_id"]
    if predecessor:
        assert case.service.pulls[predecessor["number"]]["state"] == "closed"
        assert case.service.pulls[predecessor["number"]]["merged"] is False
        preserved = delivered.evidence["adoption_predecessors"][0]
        assert preserved["pr_number"] == predecessor["number"]
        assert git(preserved["bindings"]["workspace"], "rev-parse", "HEAD") == preserved["head"]
    if mode == "verify-existing":
        assert not any(
            row.get("event") == "execution-start" and row.get("unit") == first.id
            for row in map(json.loads, case.events.read_text(encoding="utf-8").splitlines())
        )
        assert delivered.evidence["existing_source_verified"]["no_development_session"]
