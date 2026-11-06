"""Canonical source delivery must follow actual code ancestry and source publication."""

import subprocess
from pathlib import Path

import pytest
from conftest import DEFAULT_TREE, git, write_plan, write_ticket_tree

from bmad_loop import frontmatter, runs, sprintstatus, verify
from bmad_loop.campaign_model import SourceSelector, UnitSpec
from bmad_loop.campaign_source_publication import SourcePublication, SourcePublicationHold, digest
from bmad_loop.journal import Journal
from bmad_loop.tickettree import TicketsError, TicketTree


def head(repo):
    return verify.rev_parse_head(repo)


def remote(repo, path, name):
    subprocess.run(
        ["git", "clone", "--bare", str(repo), str(path)], check=True, capture_output=True
    )
    git(repo, "remote", "add", name, str(path))
    return path


def configure(layout, tmp_path, *, shared=False):
    write_ticket_tree(layout, DEFAULT_TREE)
    plan = write_plan(layout, "epic-cart", "story-cart-model-plan", 1, "ready-for-dev")
    if shared:
        rel = layout.output_folder.relative_to(layout.project).as_posix()
        git(layout.project, "rm", "-r", "--cached", rel)
        with (layout.project / ".gitignore").open("a", encoding="utf-8") as out:
            out.write("/" + rel + "/\n")
        git(layout.project, "add", ".gitignore")
    else:
        git(layout.project, "add", "-A")
    git(layout.project, "commit", "-m", "source baseline")
    remote(layout.project, tmp_path / "code.git", "source-code")
    publication = SourcePublication(layout.project, tmp_path / "publication", remote="source-code")
    source = SourceSelector("tickets", id="tickets", initiative=layout.initiative)
    unit = UnitSpec("tickets:initiative:1.1", "tickets", "epic-cart", "Cart", selector="1.1")
    return publication, source, unit, plan


@pytest.fixture
def shared(project_v7, tmp_path):
    return configure(project_v7, tmp_path, shared=True)


def capability(publication, source, unit, binding, artifact, *, action="source-action"):
    mirror = publication.project / "docs" / "bmad-campaign" / "unit" / "accepted-source.md"
    mirror.parent.mkdir(parents=True, exist_ok=True)
    if not mirror.exists() or mirror.read_bytes() != artifact:
        mirror.write_bytes(artifact)
        git(publication.project, "add", mirror.relative_to(publication.project).as_posix())
        git(publication.project, "commit", "-m", "reviewed private source mirror")
        git(publication.project, "push", "source-code", "HEAD:refs/heads/main")
    current = head(publication.project)
    return publication.prepare_plan(
        action,
        binding,
        artifact,
        merge={"head": current, "merged_commit": current, "pr_number": 18},
        acceptance={
            "head": current,
            "review_id": "independent-review-session",
            "verdict": "approved",
            "artifact_digest": digest(artifact),
            "review_path": mirror.relative_to(publication.project).as_posix(),
            "graph_revision": 1,
            "config_revision": 1,
        },
    )


def built(binding):
    import base64

    before = (
        base64.b64decode(binding.preimage)
        if binding.preimage
        else b"---\nticket: 1\nstatus: ready-for-dev\n---\n"
    )
    return (
        before.replace(b"status: ready-for-dev", b"status: built")
        + b"\nAccepted implementation details.\n"
    )


def test_tracked_source_reads_fresh_immutable_main_without_editing_checkout(project_v7, tmp_path):
    publication, source, unit, plan = configure(project_v7, tmp_path)
    binding = publication.capture(source, unit)
    # Main does not yet contain done; status debt becomes a draft tracking PR.
    before = plan.read_bytes()
    result = publication.publish(
        "delivery",
        source,
        unit,
        binding,
        merged_commit=head(publication.project),
        journal=Journal(tmp_path / "journal"),
    )
    assert result.state == "tracking_pr_required"
    assert result.proposal["draft"]
    assert plan.read_bytes() == before
    # A dirty local done edit cannot masquerade as merged canonical source.
    plan.write_bytes(before.replace(b"ready-for-dev", b"done"))
    again = publication.publish(
        "delivery-2",
        source,
        unit,
        binding,
        merged_commit=head(publication.project),
        journal=Journal(tmp_path / "journal"),
    )
    assert again.state == "tracking_pr_required"


def test_tracked_done_is_confirmed_only_after_actual_remote_publication(project_v7, tmp_path):
    publication, source, unit, plan = configure(project_v7, tmp_path)
    binding = publication.capture(source, unit)
    plan.write_bytes(plan.read_bytes().replace(b"ready-for-dev", b"done"))
    git(publication.project, "add", str(plan))
    git(publication.project, "commit", "-m", "canonical source completed")
    commit = head(publication.project)
    with pytest.raises(SourcePublicationHold, match="reachable"):
        publication.publish(
            "delivery",
            source,
            unit,
            binding,
            merged_commit=commit,
            journal=Journal(tmp_path / "journal"),
        )
    git(publication.project, "push", "source-code", "HEAD:refs/heads/main")
    assert (
        publication.publish(
            "delivery",
            source,
            unit,
            binding,
            merged_commit=commit,
            journal=Journal(tmp_path / "journal"),
        ).state
        == "confirmed"
    )


def test_shared_plan_import_uses_mark_with_external_accepted_artifact(shared, tmp_path):
    publication, source, unit, plan = shared
    binding = publication.capture(source, unit)
    artifact = built(binding)
    accepted = capability(publication, source, unit, binding, artifact)
    result = publication.publish(
        "source-action",
        source,
        unit,
        binding,
        merged_commit=head(publication.project),
        journal=Journal(tmp_path / "journal"),
        accepted_plan=accepted,
    )
    assert result.state == "confirmed"
    assert frontmatter.read_frontmatter(plan)["status"] == "done"
    assert "Accepted implementation details" in plan.read_text(encoding="utf-8")
    assert (
        publication.publish(
            "source-action",
            source,
            unit,
            binding,
            merged_commit=head(publication.project),
            journal=Journal(tmp_path / "journal"),
            accepted_plan=accepted,
        ).state
        == "confirmed"
    )


def test_owner_plan_edit_after_capture_is_never_overwritten(shared, tmp_path):
    publication, source, unit, plan = shared
    binding = publication.capture(source, unit)
    accepted = capability(publication, source, unit, binding, built(binding))
    plan.write_bytes(plan.read_bytes() + b"\nOwner changed acceptance.\n")
    before = plan.read_bytes()
    with pytest.raises(SourcePublicationHold, match="changed since planning"):
        publication.publish(
            "source-action",
            source,
            unit,
            binding,
            merged_commit=head(publication.project),
            journal=Journal(tmp_path / "journal"),
            accepted_plan=accepted,
        )
    assert plan.read_bytes() == before


def test_mark_capability_checks_preimage_under_sole_writer_lock(shared, tmp_path):
    publication, source, unit, plan = shared
    binding = publication.capture(source, unit)
    accepted = capability(publication, source, unit, binding, built(binding))
    plan.write_bytes(plan.read_bytes() + b"\nOwner note\n")
    with pytest.raises(TicketsError, match="captured preimage"):
        TicketTree(publication.project).mark(
            unit.selector,
            "done",
            reason="campaign-publish",
            expected_plan=plan,
            journal=Journal(tmp_path / "journal"),
            publication=accepted,
        )
    assert plan.read_bytes().endswith(b"Owner note\n")


def test_missing_capability_cannot_use_new_reason_and_legacy_accept_stays_strict(shared, tmp_path):
    publication, _, unit, plan = shared
    for reason in ("accept", "campaign-publish"):
        with pytest.raises(TicketsError):
            TicketTree(publication.project).mark(
                unit.selector,
                "done",
                reason=reason,
                expected_plan=plan,
                journal=Journal(tmp_path / "journal"),
            )
    assert frontmatter.read_frontmatter(plan)["status"] == "ready-for-dev"


def test_boolean_merge_and_unreviewed_artifact_cannot_authorize_source(shared):
    publication, source, unit, _ = shared
    binding = publication.capture(source, unit)
    current = head(publication.project)
    with pytest.raises(SourcePublicationHold, match="Review receipt"):
        publication.prepare_plan(
            "bad",
            binding,
            built(binding),
            merge={"merged": True, "head": current, "merged_commit": current, "pr_number": 18},
            acceptance={"approved": True, "head": current},
        )


def test_capability_rejects_another_native_ticket(shared, tmp_path):
    publication, source, unit, plan = shared
    binding = publication.capture(source, unit)
    artifact = built(binding).replace(b"ticket: 1", b"ticket: 2")
    accepted = capability(publication, source, unit, binding, artifact)
    with pytest.raises(TicketsError, match="another native ticket"):
        TicketTree(publication.project).mark(
            unit.selector,
            "done",
            reason="campaign-publish",
            expected_plan=plan,
            journal=Journal(tmp_path / "journal"),
            publication=accepted,
        )
    assert frontmatter.read_frontmatter(plan)["status"] == "ready-for-dev"


def test_engine_child_cannot_use_coordinator_publication(shared, tmp_path):
    publication, source, unit, plan = shared
    binding = publication.capture(source, unit)
    accepted = capability(publication, source, unit, binding, built(binding))
    child = tmp_path / "child"
    child.mkdir()
    (child / "campaign-worker.json").write_text("{}", encoding="utf-8")
    token = runs.set_owner_run_dir(child)
    try:
        with pytest.raises(SourcePublicationHold, match="engine child"):
            accepted.validate(publication.project, unit.selector, plan)
    finally:
        runs.reset_owner_run_dir(token)


def test_initially_absent_plan_can_import_only_accepted_private_plan(shared, tmp_path):
    publication, source, unit, plan = shared
    plan.unlink()
    binding = publication.capture(source, unit)
    assert binding.preimage is None
    accepted = capability(publication, source, unit, binding, built(binding))
    result = publication.publish(
        "source-action",
        source,
        unit,
        binding,
        merged_commit=head(publication.project),
        journal=Journal(tmp_path / "journal"),
        accepted_plan=accepted,
    )
    assert result.state == "confirmed"
    assert frontmatter.read_frontmatter(Path(binding.path))["status"] == "done"


def test_versioned_external_store_commits_only_unit_and_confirms_remote(
    project_v7_external_store, tmp_path
):
    layout = project_v7_external_store
    write_ticket_tree(layout, DEFAULT_TREE)
    plan = write_plan(layout, "epic-cart", "story-cart-model-plan", 1, "ready-for-dev")
    git(layout.output_folder, "add", "-A")
    git(layout.output_folder, "commit", "-m", "source baseline")
    remote(layout.project, tmp_path / "code.git", "source-code")
    store_remote = remote(layout.output_folder, tmp_path / "source.git", "source-store")
    publication = SourcePublication(layout.project, tmp_path / "publication", remote="source-code")
    source = SourceSelector("tickets", id="tickets", initiative=layout.initiative)
    unit = UnitSpec("tickets:initiative:1.1", "tickets", "epic-cart", "Cart", selector="1.1")
    binding = publication.capture(source, unit)
    accepted = capability(publication, source, unit, binding, built(binding))
    unrelated = layout.output_folder / "owner-notes.md"
    unrelated.write_text("unrelated owner draft", encoding="utf-8")
    before = head(layout.output_folder)
    result = publication.publish(
        "source-action",
        source,
        unit,
        binding,
        merged_commit=head(layout.project),
        journal=Journal(tmp_path / "journal"),
        accepted_plan=accepted,
        store_remote="source-store",
        store_branch="main",
    )
    assert result.state == "confirmed" and result.source_commit
    assert verify.is_ancestor(layout.output_folder, before, result.source_commit)
    assert verify.ref_revision(store_remote, "refs/heads/main") == result.source_commit
    changed = (
        verify.git_bytes(
            layout.output_folder,
            "diff-tree",
            "--no-commit-id",
            "--name-only",
            "-r",
            result.source_commit,
        )
        .stdout.decode()
        .strip()
    )
    assert changed == plan.relative_to(layout.output_folder).as_posix()
    assert unrelated.read_text(encoding="utf-8") == "unrelated owner draft"
    assert verify.git_bytes(layout.output_folder, "ls-files", "--", unrelated.name).stdout == b""


def test_expected_sprint_row_guard_preserves_unrelated_changes(project, tmp_path):
    from conftest import write_sprint

    write_sprint(project, {"1-1-one": "ready-for-dev", "1-2-other": "ready-for-dev"})
    path = project.sprint_status
    sprintstatus.advance(path, "1-2-other", "in-progress")
    assert (
        sprintstatus.advance(path, "1-1-one", "done", expected_current=("ready-for-dev", "done"))
        == "done"
    )
    assert sprintstatus.story_status(path, "1-2-other") == "in-progress"
    with pytest.raises(sprintstatus.SprintStatusError, match="changed before semantic"):
        sprintstatus.advance(path, "1-2-other", "done", expected_current=("ready-for-dev",))
    assert sprintstatus.story_status(path, "1-2-other") == "in-progress"


def reviewed(publication, artifact, *, relative="docs/bmad-campaign/accepted-artifact.md"):
    mirror = publication.project / relative
    mirror.parent.mkdir(parents=True, exist_ok=True)
    mirror.write_bytes(artifact)
    git(publication.project, "add", relative)
    git(publication.project, "commit", "-m", "reviewed source artifact")
    git(publication.project, "push", "source-code", "HEAD:refs/heads/main")
    current = head(publication.project)
    return (
        {"head": current, "merged_commit": current, "pr_number": 18},
        {
            "head": current,
            "review_id": "independent-review",
            "verdict": "approved",
            "artifact_digest": digest(artifact),
            "review_path": relative,
            "graph_revision": 1,
            "config_revision": 1,
        },
    )


def test_planning_plan_uses_guarded_mark_and_refuses_redefining_built(shared, tmp_path):
    publication, source, unit, plan = shared
    binding = publication.capture(source, unit)
    artifact = (
        plan.read_bytes().replace(b"ready-for-dev", b"draft") + b"\nReviewed refined scope.\n"
    )
    merge, acceptance = reviewed(publication, artifact)
    accepted = publication.prepare_plan(
        "planning", binding, artifact, merge=merge, acceptance=acceptance, purpose="planning"
    )
    tree = TicketTree(publication.project)
    tree.mark(
        unit.selector,
        "draft",
        reason="campaign-plan",
        expected_plan=plan,
        journal=Journal(tmp_path / "journal"),
        publication=accepted,
    )
    assert plan.read_bytes() == artifact
    # A planning capability never licenses an execution completion.
    with pytest.raises(TicketsError):
        tree.mark(
            unit.selector,
            "done",
            reason="campaign-publish",
            expected_plan=plan,
            journal=Journal(tmp_path / "journal"),
            publication=accepted,
        )


def test_merged_mirror_mismatch_cannot_publish_local_private_artifact(shared):
    publication, source, unit, _ = shared
    binding = publication.capture(source, unit)
    artifact = built(binding)
    merge, acceptance = reviewed(publication, b"another artifact")
    acceptance["artifact_digest"] = digest(artifact)
    with pytest.raises(SourcePublicationHold, match="merged source mirror differs"):
        publication.prepare_plan("mismatch", binding, artifact, merge=merge, acceptance=acceptance)


def test_new_ignored_story_spec_publishes_only_accepted_unit(project, tmp_path):
    from conftest import install_bmad_config
    from test_stories_engine import SPEC_FOLDER, setup_stories

    install_bmad_config(project)
    folder = setup_stories(project, [{"id": "one", "title": "One", "description": "Implement one"}])
    git(project.project, "rm", "-r", "--cached", SPEC_FOLDER)
    with (project.project / ".gitignore").open("a", encoding="utf-8") as out:
        out.write("/" + SPEC_FOLDER + "/\n")
    git(project.project, "add", ".gitignore")
    git(project.project, "commit", "-m", "canonical ignored story scope")
    remote(project.project, tmp_path / "code.git", "source-code")
    publication = SourcePublication(project.project, tmp_path / "publication", remote="source-code")
    source = SourceSelector("stories", id="stories", path=SPEC_FOLDER)
    unit = UnitSpec("stories:one", "stories", "epic", "One", selector="one")
    binding = publication.capture(source, unit)
    assert binding.path == "" and binding.versioning == "none"
    artifact = b"---\nid: one\nstatus: done\n---\n# Implemented and reviewed\n"
    merge, acceptance = reviewed(publication, artifact)
    target = folder / "stories" / "one-implemented.md"
    other = folder / "stories" / "other-owner.md"
    other.write_text("owner text", encoding="utf-8")
    accepted = publication.prepare_artifact(
        "story", binding, artifact, canonical_path=target, merge=merge, acceptance=acceptance
    )
    result = publication.publish(
        "story",
        source,
        unit,
        binding,
        merged_commit=merge["merged_commit"],
        journal=Journal(tmp_path / "journal"),
        accepted_plan=accepted,
    )
    assert result.state == "confirmed" and target.read_bytes() == artifact
    assert other.read_text(encoding="utf-8") == "owner text"
    assert (
        publication.publish(
            "story",
            source,
            unit,
            binding,
            merged_commit=merge["merged_commit"],
            journal=Journal(tmp_path / "journal"),
            accepted_plan=accepted,
        ).state
        == "confirmed"
    )


def test_tracked_story_manifest_does_not_force_ignored_new_spec_into_code_pr(project, tmp_path):
    from conftest import install_bmad_config
    from test_stories_engine import SPEC_FOLDER, setup_stories

    install_bmad_config(project)
    folder = setup_stories(project, [{"id": "one", "title": "One", "description": "Implement one"}])
    with (project.project / ".gitignore").open("a", encoding="utf-8") as out:
        out.write("/" + SPEC_FOLDER + "/stories/*.md\n")
    git(project.project, "add", ".gitignore")
    git(project.project, "commit", "-m", "ignored new specs with tracked manifest")
    remote(project.project, tmp_path / "code.git", "source-code")
    publication = SourcePublication(project.project, tmp_path / "publication", remote="source-code")
    source = SourceSelector("stories", id="stories", path=SPEC_FOLDER)
    unit = UnitSpec("stories:one", "stories", "epic", "One", selector="one")
    binding = publication.capture(source, unit)
    assert binding.versioning == "code-repo" and binding.path == ""
    artifact = b"---\nid: one\nstatus: done\n---\n# Accepted new story\n"
    merge, acceptance = reviewed(publication, artifact)
    target = folder / "stories" / "one-accepted.md"
    accepted = publication.prepare_artifact(
        "story", binding, artifact, canonical_path=target, merge=merge, acceptance=acceptance
    )
    result = publication.publish(
        "story",
        source,
        unit,
        binding,
        merged_commit=merge["merged_commit"],
        journal=Journal(tmp_path / "journal"),
        accepted_plan=accepted,
    )
    assert result.state == "confirmed" and target.read_bytes() == artifact
    assert (
        verify.git_bytes(
            project.project, "ls-files", "--", target.relative_to(project.project).as_posix()
        ).stdout
        == b""
    )
    assert (
        publication.publish(
            "story",
            source,
            unit,
            binding,
            merged_commit=merge["merged_commit"],
            journal=Journal(tmp_path / "journal"),
            accepted_plan=accepted,
        ).state
        == "confirmed"
    )


def closure_case(shared):
    publication, source, _, plan = shared
    tree = TicketTree(publication.project)
    first = tree.find("1.1")
    second = tree.find("1.2")
    for found in (first, second):
        path = Path(found.plan)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "---\nticket: " + str(found.row.id) + "\nstatus: done\n---\nClosed child\n",
            encoding="utf-8",
        )
    unit = UnitSpec(
        "tickets:closure/cart",
        "tickets",
        "epic-cart",
        "Close Cart",
        selector="epic-cart",
        kind="closure",
    )
    binding = publication.capture(source, unit)
    return publication, source, unit, binding


def test_ignored_epic_closure_syncs_authored_artifact_after_all_source_children_done(
    shared, tmp_path
):
    publication, source, unit, binding = closure_case(shared)
    artifact = (
        Path(binding.path).read_bytes().replace(b"in-progress", b"done")
        + b"\nReviewed retrospective.\n"
    )
    merge, acceptance = reviewed(publication, artifact)
    accepted = publication.prepare_artifact(
        "closure", binding, artifact, merge=merge, acceptance=acceptance
    )
    result = publication.publish(
        "closure",
        source,
        unit,
        binding,
        merged_commit=merge["merged_commit"],
        journal=Journal(tmp_path / "journal"),
        accepted_plan=accepted,
    )
    assert result.state == "confirmed"
    assert Path(binding.path).read_bytes() == artifact


def test_unselected_unfinished_child_withholds_parent_closure(shared, tmp_path):
    publication, source, unit, binding = closure_case(shared)
    artifact = Path(binding.path).read_bytes().replace(b"in-progress", b"done")
    merge, acceptance = reviewed(publication, artifact)
    accepted = publication.prepare_artifact(
        "closure", binding, artifact, merge=merge, acceptance=acceptance
    )
    child = Path(binding.children[-1][1])
    child.write_bytes(child.read_bytes().replace(b"done", b"ready-for-dev"))
    original = Path(binding.path).read_bytes()
    with pytest.raises(SourcePublicationHold, match="unfinished selected or unselected"):
        publication.publish(
            "closure",
            source,
            unit,
            binding,
            merged_commit=merge["merged_commit"],
            journal=Journal(tmp_path / "journal"),
            accepted_plan=accepted,
        )
    assert Path(binding.path).read_bytes() == original


def test_closure_manifest_or_owner_epic_edit_is_never_overwritten(shared, tmp_path):
    publication, source, unit, binding = closure_case(shared)
    artifact = Path(binding.path).read_bytes().replace(b"in-progress", b"done")
    merge, acceptance = reviewed(publication, artifact)
    accepted = publication.prepare_artifact(
        "closure", binding, artifact, merge=merge, acceptance=acceptance
    )
    manifest = Path(binding.manifests[0][0])
    manifest.write_bytes(manifest.read_bytes() + b"\n# owner appended scope\n")
    with pytest.raises(SourcePublicationHold, match="children changed"):
        publication.publish(
            "closure",
            source,
            unit,
            binding,
            merged_commit=merge["merged_commit"],
            journal=Journal(tmp_path / "journal"),
            accepted_plan=accepted,
        )
    # Restore only this harness mutation to exercise an independent epic edit.
    manifest.write_bytes(manifest.read_bytes().replace(b"\n# owner appended scope\n", b""))
    path = Path(binding.path)
    path.write_bytes(path.read_bytes() + b"\nOwner instruction\n")
    with pytest.raises(SourcePublicationHold, match="captured preimage"):
        publication.publish(
            "closure",
            source,
            unit,
            binding,
            merged_commit=merge["merged_commit"],
            journal=Journal(tmp_path / "journal"),
            accepted_plan=accepted,
        )
    assert path.read_bytes().endswith(b"Owner instruction\n")


def test_planning_product_artifact_cas_preserves_neighboring_work(shared, tmp_path):
    from bmad_loop.campaign_source_publication import ArtifactChange

    publication, source, _, plan = shared
    unit = UnitSpec("planning", "explicit", "planning", "Plan", kind="planning")
    root = Path(
        publication.capture(
            source, UnitSpec("one", "tickets", "epic-cart", "One", selector="1.1")
        ).source_root
    )
    target = root / "architecture.md"
    target.write_text("prior architecture\n", encoding="utf-8")
    neighbor = root / "owner.md"
    neighbor.write_text("owner data", encoding="utf-8")
    artifact = b"Reviewed architecture\n"
    merge, acceptance = reviewed(publication, artifact)
    change = ArtifactChange(
        str(target), artifact, b"prior architecture\n", acceptance["review_path"]
    )
    result = publication.publish_artifacts(
        "planning",
        source,
        unit,
        (change,),
        merge=merge,
        acceptance=acceptance,
        journal=Journal(tmp_path / "journal"),
    )
    assert result.state == "confirmed" and target.read_bytes() == artifact
    assert neighbor.read_text(encoding="utf-8") == "owner data"
    target.write_bytes(b"owner changed architecture\n")
    with pytest.raises(SourcePublicationHold, match="edited after capture"):
        publication.publish_artifacts(
            "planning",
            source,
            unit,
            (change,),
            merge=merge,
            acceptance=acceptance,
            journal=Journal(tmp_path / "journal"),
        )


def test_tracking_checkout_prepares_ticket_done_without_touching_main(project_v7, tmp_path):
    publication, source, unit, plan = configure(project_v7, tmp_path)
    binding = publication.capture(source, unit)
    artifact = built(binding)
    merge, acceptance = reviewed(publication, artifact)
    checkout = tmp_path / "tracking"
    git(publication.project, "worktree", "add", "-b", "tracking-source", str(checkout), "HEAD")
    original = plan.read_bytes()
    changed = publication.prepare_tracking_checkout(
        checkout,
        source,
        unit,
        binding,
        journal=Journal(tmp_path / "journal"),
        mirror_bytes=artifact,
        merge=merge,
        acceptance=acceptance,
    )
    assert changed == (checkout / plan.relative_to(publication.project),)
    assert frontmatter.read_frontmatter(changed[0])["status"] == "done"
    assert plan.read_bytes() == original
    assert verify.rev_parse_head(checkout) == merge["head"]  # helper creates no commit or push.


def test_tracking_checkout_cannot_edit_canonical_branch(project_v7, tmp_path):
    publication, source, unit, plan = configure(project_v7, tmp_path)
    binding = publication.capture(source, unit)
    before = plan.read_bytes()
    with pytest.raises(SourcePublicationHold, match="isolated clean"):
        publication.prepare_tracking_checkout(
            publication.project, source, unit, binding, journal=Journal(tmp_path / "journal")
        )
    assert plan.read_bytes() == before


def test_tracking_refuses_newer_owner_body_in_fresh_main(project_v7, tmp_path):
    publication, source, unit, plan = configure(project_v7, tmp_path)
    binding = publication.capture(source, unit)
    artifact = built(binding)
    merge, acceptance = reviewed(publication, artifact)
    plan.write_bytes(plan.read_bytes() + b"\nOwner changed accepted scope.\n")
    git(publication.project, "add", str(plan))
    git(publication.project, "commit", "-m", "new owner source scope")
    git(publication.project, "push", "source-code", "HEAD:refs/heads/main")
    checkout = tmp_path / "tracking"
    git(publication.project, "worktree", "add", "-b", "tracking-source", str(checkout), "HEAD")
    with pytest.raises(SourcePublicationHold, match="newer canonical ticket body"):
        publication.prepare_tracking_checkout(
            checkout,
            source,
            unit,
            binding,
            journal=Journal(tmp_path / "journal"),
            mirror_bytes=artifact,
            merge=merge,
            acceptance=acceptance,
        )
    assert (checkout / plan.relative_to(publication.project)).read_bytes() == plan.read_bytes()


def test_planning_ticket_artifact_manifest_uses_mark(shared, tmp_path):
    from bmad_loop.campaign_source_publication import ArtifactChange

    publication, source, _, plan = shared
    artifact = plan.read_bytes().replace(b"ready-for-dev", b"draft") + b"\nRefined acceptance.\n"
    merge, acceptance = reviewed(publication, artifact)
    unit = UnitSpec("planning", "explicit", "planning", "Plan", kind="planning")
    change = ArtifactChange(
        str(plan), artifact, plan.read_bytes(), acceptance["review_path"], selector="1.1"
    )
    result = publication.publish_artifacts(
        "planning",
        source,
        unit,
        (change,),
        merge=merge,
        acceptance=acceptance,
        journal=Journal(tmp_path / "journal"),
    )
    assert result.state == "confirmed" and plan.read_bytes() == artifact


def test_tracking_planning_artifact_prepares_reviewed_plan_for_draft(project_v7, tmp_path):
    from bmad_loop.campaign_source_publication import ArtifactChange

    publication, source, _, plan = configure(project_v7, tmp_path)
    before = plan.read_bytes()
    artifact = before.replace(b"ready-for-dev", b"draft") + b"\nRefined acceptance.\n"
    merge, acceptance = reviewed(publication, artifact)
    change = ArtifactChange(str(plan), artifact, before, acceptance["review_path"], selector="1.1")
    unit = UnitSpec("planning", "explicit", "planning", "Plan", kind="planning")
    checkout = tmp_path / "tracking"
    git(publication.project, "worktree", "add", "-b", "tracking-plan", str(checkout), "HEAD")
    changed = publication.prepare_tracking_artifacts(
        checkout,
        source,
        unit,
        (change,),
        merge=merge,
        acceptance=acceptance,
        journal=Journal(tmp_path / "journal"),
    )
    assert changed == (checkout / plan.relative_to(publication.project),)
    assert changed[0].read_bytes() == artifact
    assert plan.read_bytes() == before


def test_store_publication_lost_reply_reconciles_without_second_commit(
    project_v7_external_store, tmp_path, monkeypatch
):
    from bmad_loop.campaign_recovery import CheckpointPusher, PushObservation

    layout = project_v7_external_store
    write_ticket_tree(layout, DEFAULT_TREE)
    plan = write_plan(layout, "epic-cart", "story-cart-model-plan", 1, "ready-for-dev")
    git(layout.output_folder, "add", "-A")
    git(layout.output_folder, "commit", "-m", "source baseline")
    remote(layout.project, tmp_path / "code.git", "source-code")
    store_remote = remote(layout.output_folder, tmp_path / "source.git", "source-store")
    publication = SourcePublication(layout.project, tmp_path / "publication", remote="source-code")
    source = SourceSelector("tickets", id="tickets", initiative=layout.initiative)
    unit = UnitSpec("tickets:initiative:1.1", "tickets", "epic-cart", "Cart", selector="1.1")
    binding = publication.capture(source, unit)
    accepted = capability(publication, source, unit, binding, built(binding))
    original = CheckpointPusher.push
    calls = []

    def lost_reply(self, action_id, commit, **kwargs):
        actual = original(self, action_id, commit, **kwargs)
        assert actual.state == "confirmed"
        import json

        path = self._path(action_id)
        row = json.loads(path.read_text(encoding="utf-8"))
        row["state"] = "sent_unknown"
        row.pop("remote_head", None)
        path.write_text(json.dumps(row), encoding="utf-8")
        calls.append(commit)
        return PushObservation(action_id, commit, "sent_unknown")

    monkeypatch.setattr(CheckpointPusher, "push", lost_reply)
    args = dict(
        merged_commit=head(layout.project),
        journal=Journal(tmp_path / "journal"),
        accepted_plan=accepted,
        store_remote="source-store",
        store_branch="main",
    )
    first = publication.publish("source-action", source, unit, binding, **args)
    assert first.state == "store_push_pending"
    source_head = head(layout.output_folder)
    second = publication.publish("source-action", source, unit, binding, **args)
    assert second.state == "confirmed" and second.source_commit == source_head
    assert head(layout.output_folder) == source_head and calls == [source_head]
    assert verify.ref_revision(store_remote, "refs/heads/main") == source_head
    assert frontmatter.status_of(frontmatter.parse_frontmatter(plan.read_text())) == "done"
    pusher = CheckpointPusher(
        layout.output_folder, publication.root / "store-pushes", "source-store", "main"
    )
    assert pusher.observations()[0].state == "confirmed"


def sprint_planning(project, tmp_path, *, absent=False, shared=True):
    from conftest import install_bmad_config, write_sprint

    from bmad_loop import bmadconfig

    install_bmad_config(project)
    write_sprint(project, {"1-1-old": "done"})
    path = bmadconfig.load_paths(project.project).sprint_status
    git(project.project, "add", "-A")
    git(project.project, "commit", "-m", "sprint source baseline")
    if shared:
        git(project.project, "rm", "--cached", path.relative_to(project.project).as_posix())
        with (project.project / ".gitignore").open("a", encoding="utf-8") as out:
            out.write("/" + path.relative_to(project.project).as_posix() + "\n")
        git(project.project, "add", ".gitignore")
        git(project.project, "commit", "-m", "shared sprint source")
    if absent:
        path.unlink()
    remote(project.project, tmp_path / "code.git", "source-code")
    pub = SourcePublication(project.project, tmp_path / "publication", remote="source-code")
    source = SourceSelector("sprint-status", id="sprint")
    unit = UnitSpec("planning", "explicit", "planning", "Plan", kind="planning")
    return pub, source, unit, path


@pytest.mark.parametrize("absent", [False, True])
def test_ignored_planning_board_imports_new_rows_through_advance(project, tmp_path, absent):
    from bmad_loop.campaign_source_publication import ArtifactChange

    pub, source, unit, path = sprint_planning(project, tmp_path, absent=absent)
    before = path.read_bytes() if path.exists() else None
    artifact = (before or b"development_status:\n") + b"  1-2-new: ready-for-dev\n"
    merge, acceptance = reviewed(pub, artifact)
    calls = []
    original = sprintstatus.advance

    def writer(*args, **kwargs):
        calls.append(kwargs.get("publication"))
        return original(*args, **kwargs)

    from unittest.mock import patch

    with patch.object(sprintstatus, "advance", writer):
        result = pub.publish_artifacts(
            "planning-board",
            source,
            unit,
            (ArtifactChange(str(path), artifact, before, acceptance["review_path"]),),
            merge=merge,
            acceptance=acceptance,
            journal=Journal(tmp_path / "journal"),
        )
    assert result.state == "confirmed" and path.read_bytes() == artifact
    assert len(calls) == 1 and calls[0].source_kind == "sprint-status"
    assert calls[0].owned_rows == ("1-2-new",)
    if not absent:
        assert sprintstatus.story_status(path, "1-1-old") == "done"


def test_board_planning_cannot_change_unowned_finished_row(project, tmp_path):
    from bmad_loop.campaign_source_publication import ArtifactChange
    from bmad_loop.statemachine import IllegalTransition

    pub, source, unit, path = sprint_planning(project, tmp_path)
    before = path.read_bytes()
    artifact = before.replace(b"done", b"ready-for-dev") + b"  1-2-new: ready-for-dev\n"
    merge, acceptance = reviewed(pub, artifact)
    with pytest.raises(IllegalTransition, match="nonowned"):
        pub.publish_artifacts(
            "planning-board",
            source,
            unit,
            (ArtifactChange(str(path), artifact, before, acceptance["review_path"]),),
            merge=merge,
            acceptance=acceptance,
            journal=Journal(tmp_path / "journal"),
        )
    assert path.read_bytes() == before


def test_planning_board_owner_edit_is_rechecked_inside_sole_writer(project, tmp_path):
    import base64

    from bmad_loop.campaign_source_publication import SourceBinding

    pub, _, unit, path = sprint_planning(project, tmp_path)
    before = path.read_bytes()
    artifact = before + b"  1-2-new: ready-for-dev\n"
    merge, acceptance = reviewed(pub, artifact)
    binding = SourceBinding(
        "sprint-status",
        unit.id,
        "",
        str(pub.project),
        str(path.parent.parent),
        str(path),
        "none",
        digest(before),
        "",
        base64.b64encode(before).decode(),
    )
    cap = pub.prepare_artifact(
        "board",
        binding,
        artifact,
        merge=merge,
        acceptance=acceptance,
        purpose="planning",
        owned_rows=("1-2-new",),
    )
    path.write_bytes(before + b"# Owner note\n")
    with pytest.raises(SourcePublicationHold, match="captured preimage"):
        sprintstatus.advance(path, "", "", publication=cap)
    assert path.read_bytes().endswith(b"# Owner note\n")


@pytest.mark.parametrize(
    ("before", "after", "owned"),
    [
        ({"1-1-old": "done"}, {"1-1-old": "ready-for-dev"}, ("1-1-old",)),
        ({"1-1-old": "done"}, {}, ("1-1-old",)),
        ({}, {"1-1-new": "done"}, ("1-1-new",)),
        ({}, {"epic-1": "ready-for-dev"}, ("epic-1",)),
        ({"1-1-old": "ready-for-dev"}, {"1-1-old": "backlog"}, ("1-1-old",)),
    ],
)
def test_board_planning_transition_table_has_no_general_regression(before, after, owned):
    from bmad_loop.statemachine import IllegalTransition, check_board_planning

    with pytest.raises(IllegalTransition):
        check_board_planning(before, after, owned)


def test_board_planning_parser_refuses_duplicate_native_rows():
    with pytest.raises(sprintstatus.SprintStatusError, match="ambiguous development rows"):
        sprintstatus.planning_rows_in_bytes(
            b"development_status:\n  1-1-same: done\n  1-1-same: backlog\n"
        )


def test_tracking_planning_board_uses_advance_and_keeps_canonical_clean(project, tmp_path):
    from bmad_loop.campaign_source_publication import ArtifactChange

    pub, source, unit, path = sprint_planning(project, tmp_path, shared=False)
    before = path.read_bytes()
    artifact = before + b"  1-2-new: ready-for-dev\n"
    merge, acceptance = reviewed(pub, artifact)
    checkout = tmp_path / "tracking"
    git(pub.project, "worktree", "add", "-b", "tracking-plan", str(checkout), "HEAD")
    changed = pub.prepare_tracking_artifacts(
        checkout,
        source,
        unit,
        (ArtifactChange(str(path), artifact, before, acceptance["review_path"]),),
        merge=merge,
        acceptance=acceptance,
        journal=Journal(tmp_path / "journal"),
    )
    assert changed == (checkout / path.relative_to(pub.project),)
    assert changed[0].read_bytes() == artifact and path.read_bytes() == before


def test_planning_board_cannot_create_native_unwritable_row(project, tmp_path):
    from bmad_loop.campaign_source_publication import ArtifactChange

    pub, source, unit, path = sprint_planning(project, tmp_path)
    before = path.read_bytes()
    artifact = before + b"  '1-2-new': ready-for-dev\n"
    merge, acceptance = reviewed(pub, artifact)
    with pytest.raises(sprintstatus.SprintStatusError, match="native writer"):
        pub.publish_artifacts(
            "planning-board",
            source,
            unit,
            (ArtifactChange(str(path), artifact, before, acceptance["review_path"]),),
            merge=merge,
            acceptance=acceptance,
            journal=Journal(tmp_path / "journal"),
        )
    assert path.read_bytes() == before
