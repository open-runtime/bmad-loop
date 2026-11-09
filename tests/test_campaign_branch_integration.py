"""Real Git bot/worker branches retain every parent and the frozen path authority."""

import dataclasses

import pytest
from conftest import git
from test_campaign_workers import envelope

from bmad_loop import campaign_workers as workers
from bmad_loop import verify
from bmad_loop.campaign_model import CampaignError, UnitSpec


def commit(repo, path, value, message):
    destination = repo / path
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(value)
    git(repo, "add", "--", path)
    git(repo, "commit", "-qm", message)
    return git(repo, "rev-parse", "HEAD")


@pytest.fixture
def branch_case(project, tmp_path):
    repo = project.project
    git(repo, "branch", "-M", "main")
    baseline = commit(repo, "src/owned/shared.txt", "accepted\n", "accepted product")
    git(repo, "checkout", "-b", "owned-unit")
    remote, bot, main = (tmp_path / name for name in ("remote.git", "bot", "main"))
    git(tmp_path, "clone", "--bare", str(repo), str(remote))
    git(repo, "remote", "add", "origin", str(remote))
    git(repo, "fetch", "origin")
    for checkout, branch in ((bot, "owned-unit"), (main, "main")):
        git(tmp_path, "clone", "--branch", branch, str(remote), str(checkout))
        git(checkout, "config", "user.name", "External Test")
        git(checkout, "config", "user.email", "external@example.invalid")
    context = envelope(repo, tmp_path / "execution")
    unit = UnitSpec("s:project:unit", "s", "project", "Owned", scopes=("src/owned",))

    def request():
        return {
            "workspace": str(repo),
            "launch_id": "integrate-owned-remote",
            "branch": "owned-unit",
            "remote": "origin",
            "target": "main",
            "unit": dataclasses.asdict(unit),
            "remote_head_sync": {
                "schema_version": 1,
                "id": "observed-remote-change",
                "repository": "owner/repo",
                "pr_number": 12,
                "pr_node_id": "PR_owned",
                "branch": "owned-unit",
                "target": "main",
                "anchor": baseline,
                "head": git(remote, "rev-parse", "owned-unit"),
                "local_head": git(repo, "rev-parse", "HEAD"),
                "graph_revision": 1,
                "config_revision": context.config_revision,
                "source_revision": "source-revision",
            },
        }

    return repo, remote, bot, main, baseline, context, request


@pytest.mark.parametrize("diverged", [False, True])
def test_owned_remote_and_fresh_main_are_both_preserved(branch_case, diverged):
    repo, remote, bot, main, baseline, context, request = branch_case
    local = (
        commit(repo, "src/owned/local.txt", "worker\n", "worker continuation")
        if diverged
        else baseline
    )
    remote_head = commit(bot, "src/owned/bot.txt", "bot\n", "bot correction")
    git(bot, "push", "origin", "owned-unit")
    target = commit(main, "outside-main.txt", "canonical\n", "accepted main")
    git(main, "push", "origin", "main")
    original_tracking = git(repo, "rev-parse", "refs/remotes/origin/main")
    incoming = request()
    receipt = workers._integrate(incoming, context)
    assert receipt["outcome"] == "integrated"
    assert receipt["base"] == target
    assert receipt["remote_head_sync"] == incoming["remote_head_sync"]
    assert set(receipt["preserved_heads"]) == {local, remote_head, target}
    assert all(
        verify.is_ancestor(repo, head, receipt["head"]) for head in (local, remote_head, target)
    )
    assert [git(repo, "rev-parse", ref) for ref in receipt["preserved_refs"]] == receipt[
        "preserved_heads"
    ]
    assert (repo / "src/owned/bot.txt").read_text() == "bot\n"
    assert (repo / "outside-main.txt").read_text() == "canonical\n"
    assert git(remote, "rev-parse", "owned-unit") == remote_head  # integration never pushes
    assert git(repo, "rev-parse", "refs/remotes/origin/main") == original_tracking
    assert receipt["external_contribution"]["remote_head"] == remote_head


@pytest.mark.parametrize("conflict_source", ["remote", "target"])
def test_conflict_preserves_all_three_heads_for_existing_repair(branch_case, conflict_source):
    repo, _, bot, main, _, context, request = branch_case
    local = commit(repo, "src/owned/shared.txt", "worker\n", "worker conflict side")
    remote_head = commit(
        bot,
        "src/owned/shared.txt" if conflict_source == "remote" else "src/owned/bot.txt",
        "bot\n",
        "bot side",
    )
    git(bot, "push", "origin", "owned-unit")
    target = commit(main, "src/owned/shared.txt", "main\n", "canonical conflict side")
    git(main, "push", "origin", "main")
    receipt = workers._integrate(request(), context)
    assert receipt["outcome"] == "integration_conflict"
    assert receipt["conflict_source"] == conflict_source
    assert set(receipt["preserved_heads"]) == {local, remote_head, target}
    assert set(git(repo, "rev-parse", ref) for ref in receipt["preserved_refs"]) == {
        local,
        remote_head,
        target,
    }
    assert receipt["conflicts"] == ["src/owned/shared.txt"]
    assert not verify.is_ancestor(repo, target, receipt["head"])


@pytest.mark.parametrize(
    "mutation",
    ["remote_changed", "local_changed", "dirty", "rewritten_anchor", "local_missing_anchor"],
)
def test_changed_authority_is_refused_before_candidate_mutation(branch_case, mutation):
    repo, _, bot, _, baseline, context, request = branch_case
    commit(bot, "src/owned/bot.txt", "first\n", "first remote update")
    git(bot, "push", "origin", "owned-unit")
    incoming = request()
    if mutation == "remote_changed":
        commit(bot, "src/owned/bot.txt", "second\n", "second remote update")
        git(bot, "push", "origin", "owned-unit")
    elif mutation == "local_changed":
        commit(repo, "src/owned/local.txt", "later\n", "later local update")
    elif mutation == "dirty":
        (repo / "src/owned/shared.txt").write_text("uncommitted owner work\n")
    elif mutation == "rewritten_anchor":
        unrelated = git(repo, "commit-tree", baseline + "^{tree}", "-m", "unrelated root")
        incoming["remote_head_sync"]["anchor"] = unrelated
    else:
        incoming["remote_head_sync"]["anchor"] = incoming["remote_head_sync"]["head"]
    before = git(repo, "rev-parse", "HEAD")
    if mutation in {"remote_changed", "rewritten_anchor", "local_missing_anchor"}:
        receipt = workers._integrate(incoming, context)
        assert receipt["outcome"] == (
            "remote_head_changed" if mutation == "remote_changed" else "ownership_uncertain"
        )
        assert receipt["head"] == before
    else:
        with pytest.raises(CampaignError):
            workers._integrate(incoming, context)
    assert git(repo, "rev-parse", "HEAD") == before
    assert not verify.ref_exists_checked(
        repo, f"refs/bmad-campaign/{context.campaign_id}/integration/{incoming['launch_id']}/remote"
    )


def test_remote_rename_checks_deleted_source_and_preserves_unowned_file(branch_case):
    repo, _, bot, _, _, context, request = branch_case
    git(bot, "mv", "src.txt", "src/owned/imported.txt")
    git(bot, "commit", "-qm", "bot moves unowned file into owned scope")
    git(bot, "push", "origin", "owned-unit")
    before = git(repo, "rev-parse", "HEAD")
    receipt = workers._integrate(request(), context)
    assert receipt["outcome"] == "ownership_uncertain"
    assert "escaped declared ownership: src.txt" in receipt["error"]
    assert git(repo, "rev-parse", "HEAD") == before and (repo / "src.txt").exists()


@pytest.mark.parametrize("kind", ["policy", "artifact"])
def test_remote_cannot_change_protected_inputs_even_with_broad_scope(branch_case, kind):
    repo, _, bot, _, _, context, request = branch_case
    path = ".bmad-loop/policy.toml" if kind == "policy" else "src/owned/story.md"
    commit(bot, path, "remote changed authority\n", "unauthorized source change")
    git(bot, "push", "origin", "owned-unit")
    incoming = request()
    incoming["unit"]["scopes"] = ["*"]
    incoming["source_artifacts"] = [{"path": "src/owned/story.md", "review_path": "docs/story.md"}]
    before = git(repo, "rev-parse", "HEAD")
    receipt = workers._integrate(incoming, context)
    assert receipt["outcome"] == "ownership_uncertain"
    assert "protected source or policy" in receipt["error"]
    assert git(repo, "rev-parse", "HEAD") == before


def test_exact_main_carry_forward_does_not_expand_bot_scope(branch_case):
    repo, _, bot, main, _, context, request = branch_case
    target = commit(main, "outside-main.txt", "canonical\n", "accepted main")
    git(main, "push", "origin", "main")
    git(bot, "fetch", "origin", "main")
    git(bot, "merge", "--no-edit", target)
    commit(bot, "src/owned/bot.txt", "owned\n", "bot within product scope")
    git(bot, "push", "origin", "owned-unit")
    result = workers._integrate(request(), context)
    assert result["outcome"] == "integrated" and verify.is_ancestor(repo, target, result["head"])
    assert (repo / "outside-main.txt").read_text() == "canonical\n"


@pytest.mark.parametrize("bound", ["bytes", "count"])
def test_large_changed_path_list_is_visible_refusal_without_partial_merge(
    branch_case, monkeypatch, bound
):
    from bmad_loop import campaign_branch_integration as integration

    repo, _, bot, _, _, context, request = branch_case
    commit(bot, "src/owned/a.txt", "one\n", "first")
    commit(bot, "src/owned/b.txt", "two\n", "second")
    git(bot, "push", "origin", "owned-unit")
    monkeypatch.setattr(
        integration, "MAX_CHANGED_PATH_BYTES" if bound == "bytes" else "MAX_CHANGED_PATHS", 1
    )
    before = git(repo, "rev-parse", "HEAD")
    result = workers._integrate(request(), context)
    assert result["outcome"] == "ownership_uncertain"
    assert "bound exceeded" in result["error"]
    assert git(repo, "rev-parse", "HEAD") == before


def test_preserved_input_cannot_be_rebound_before_integration(branch_case):
    repo, _, bot, _, baseline, context, request = branch_case
    commit(bot, "src/owned/bot.txt", "bot\n", "bot")
    git(bot, "push", "origin", "owned-unit")
    incoming = request()
    ref = f"refs/bmad-campaign/{context.campaign_id}/integration/{incoming['launch_id']}/remote"
    verify.create_ref(repo, ref, baseline)
    with pytest.raises(CampaignError, match="preservation reference changed"):
        workers._integrate(incoming, context)
    assert git(repo, "rev-parse", "HEAD") == baseline and git(repo, "rev-parse", ref) == baseline


def test_local_owner_change_during_observation_is_preserved_not_merged(branch_case, monkeypatch):
    repo, _, bot, _, _, context, request = branch_case
    commit(bot, "src/owned/bot.txt", "bot\n", "bot")
    git(bot, "push", "origin", "owned-unit")
    incoming = request()
    original = workers.git
    changed = []

    def observed(workspace, *args):
        result = original(workspace, *args)
        if args[0] == "fetch" and args[-1].endswith("/observed-target"):
            changed.append(commit(repo, "src/owned/owner.txt", "owner\n", "intervening owner"))
        return result

    monkeypatch.setattr(workers, "git", observed)
    with pytest.raises(CampaignError, match="candidate changed while observing"):
        workers._integrate(incoming, context)
    assert changed and git(repo, "rev-parse", "HEAD") == changed[0]
    assert not (repo / "src/owned/bot.txt").exists()


def test_absent_main_path_does_not_authorize_deleting_unowned_branch_work(branch_case):
    repo, _, bot, _, _, context, request = branch_case
    anchor = commit(repo, "branch-only.txt", "accepted unrelated work\n", "existing branch work")
    git(repo, "push", "origin", "owned-unit")
    git(bot, "fetch", "origin", "owned-unit")
    git(bot, "merge", "--ff-only", "origin/owned-unit")
    git(bot, "rm", "branch-only.txt")
    git(bot, "commit", "-qm", "bot deletes outside its scope")
    git(bot, "push", "origin", "owned-unit")
    incoming = request()
    incoming["remote_head_sync"]["anchor"] = anchor
    result = workers._integrate(incoming, context)
    assert result["outcome"] == "ownership_uncertain"
    assert "escaped declared ownership: branch-only.txt" in result["error"]
    assert (repo / "branch-only.txt").read_text() == "accepted unrelated work\n"


def test_previously_incorporated_remote_retains_original_author_history(branch_case):
    repo, _, bot, _, baseline, context, request = branch_case
    remote_head = commit(bot, "src/owned/bot.txt", "bot\n", "bot contribution")
    git(bot, "push", "origin", "owned-unit")
    git(repo, "fetch", "origin", "owned-unit")
    git(repo, "merge", "--ff-only", "origin/owned-unit")
    incoming = request()
    incoming["remote_head_sync"]["provenance_before"] = baseline
    result = workers._integrate(incoming, context)
    assert result["outcome"] == "integrated"
    assert result["head"] == remote_head
    assert result["external_contribution"]["provenance_before"] == baseline
    assert result["external_contribution"]["commit_count"] == 1
