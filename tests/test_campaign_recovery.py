"""Real Git history and remote publication qualify append-only recovery."""

import json
import subprocess
from dataclasses import replace

import pytest

from bmad_loop import verify
from bmad_loop.campaign_recovery import (
    CampaignRecovery,
    CheckpointPusher,
    PushObservation,
    Quiescence,
    RecoveryHold,
)


def git(repo, *args):
    return (
        subprocess.run(["git", "-C", str(repo), *args], capture_output=True, check=True)
        .stdout.decode()
        .strip()
    )


def commit(repo, rel, content, message):
    (repo / rel).write_text(content, encoding="utf-8")
    git(repo, "add", "--", rel)
    git(repo, "commit", "-m", message)
    return git(repo, "rev-parse", "HEAD")


def quiet(head, generation=1):
    return Quiescence(head, generation, True, True, "verified-process-receipt")


@pytest.fixture
def case(project, tmp_path):
    repo = project.project
    git(repo, "checkout", "-b", "campaign-unit")
    baseline = commit(repo, "owned.py", "accepted behavior\n", "prior accepted unit")
    remote = tmp_path / "remote.git"
    subprocess.run(
        ["git", "clone", "--bare", str(repo), str(remote)], capture_output=True, check=True
    )
    git(repo, "remote", "add", "campaign-test", str(remote))
    state = tmp_path / "state"
    recovery = CampaignRecovery(repo, state, "campaign-test", "campaign-unit")
    return repo, baseline, remote, recovery


def test_pushed_checkpoint_survives_compensating_retry_and_next_fastforward(case):
    repo, baseline, remote, recovery = case
    rejected = commit(repo, "owned.py", "rejected behavior\n", "rejected attempt checkpoint")
    assert recovery.pusher.push("checkpoint", rejected).state == "confirmed"
    (repo / "owned.py").write_text("dirty attempted fix\n", encoding="utf-8")
    (repo / "scratch.txt").write_text("untracked attempt residue", encoding="utf-8")
    recovery.begin(
        "repair-1",
        expected_head=rejected,
        baseline=baseline,
        owned_paths=("owned.py",),
        quiescence=quiet(rejected),
    )
    with pytest.raises(RecoveryHold, match="fence"):
        recovery.pusher.push("forbidden", rejected)
    receipt = recovery.recover("repair-1", quiescence=quiet(rejected))
    assert receipt.parent == rejected
    assert verify.is_ancestor(repo, rejected, receipt.head)
    assert (repo / "owned.py").read_text(encoding="utf-8") == "accepted behavior\n"
    assert (repo / "scratch.txt").read_text(encoding="utf-8") == "untracked attempt residue"
    assert git(repo, "show", receipt.dirty_ref + ":owned.py") == "dirty attempted fix"
    assert git(repo, "show", receipt.dirty_ref + ":scratch.txt") == "untracked attempt residue"
    completed = recovery.finish("repair-1", quiescence=quiet(receipt.head))
    assert completed.state == "complete"
    assert not recovery.pusher.fenced()
    assert git(remote, "rev-parse", "refs/heads/campaign-unit") == receipt.head
    accepted = commit(repo, "owned.py", "accepted next implementation\n", "next accepted attempt")
    assert recovery.pusher.push("next-checkpoint", accepted).state == "confirmed"
    assert verify.is_ancestor(repo, rejected, accepted)


def test_lost_push_reply_reconciles_success_without_second_push(case):
    repo, baseline, _, recovery = case
    head = commit(repo, "owned.py", "checkpoint\n", "checkpoint")
    calls = []

    def runner(root, *args, **kwargs):
        if args[0] == "push":
            calls.append(args)
            verify.git_bytes(root, *args, **kwargs)
            raise verify.GitError("reply lost")
        return verify.git_bytes(root, *args, **kwargs)

    pusher = CheckpointPusher(
        repo, recovery.root, "campaign-test", "campaign-unit", git_runner=runner
    )
    assert pusher.push("checkpoint", head).state == "confirmed"
    assert pusher.push("checkpoint", head).state == "confirmed"
    assert len(calls) == 1


def divergent_heads(case):
    repo, baseline, _, recovery = case
    head = commit(repo, "local.py", "local work\n", "local continuation")
    git(repo, "checkout", "-b", "review-bot", baseline)
    remote_head = commit(repo, "review.py", "review fix\n", "external review fix")
    git(repo, "push", "campaign-test", remote_head + ":refs/heads/campaign-unit")
    git(repo, "checkout", "campaign-unit")
    return head, remote_head


def test_divergent_checkpoint_is_durable_unsent_debt_then_settles_by_descendant(case):
    repo, _, remote, recovery = case
    head, remote_head = divergent_heads(case)
    pushes = []

    def runner(root, *args, **kwargs):
        if args[0] == "push":
            pushes.append(args)
        return verify.git_bytes(root, *args, **kwargs)

    pusher = CheckpointPusher(
        repo, recovery.root, "campaign-test", "campaign-unit", git_runner=runner
    )
    observed = pusher.push("checkpoint", head)
    assert observed == PushObservation("checkpoint", head, "remote_changed", remote_head)
    journal = json.loads(pusher._path("checkpoint").read_text())
    assert journal["submission"] == "not-submitted"
    assert journal["head"] == head
    assert pusher.push("checkpoint", head) == observed
    assert pusher.reconcile("checkpoint") == observed
    assert git(remote, "rev-parse", "campaign-unit") == remote_head
    assert pushes == []

    git(repo, "merge", "--no-edit", remote_head)
    combined = git(repo, "rev-parse", "HEAD")
    assert verify.is_ancestor(repo, head, combined)
    assert verify.is_ancestor(repo, remote_head, combined)
    with pytest.raises(RecoveryHold, match="reused"):
        pusher.push("checkpoint", combined)
    assert pusher.push("combined-checkpoint", combined).state == "confirmed"
    assert pusher.reconcile("checkpoint") == PushObservation(
        "checkpoint", head, "confirmed", combined
    )
    assert len(pushes) == 1
    assert json.loads(pusher._path("checkpoint").read_text())["submission"] == "not-submitted"


def test_remote_ahead_confirms_backup_but_exposes_different_candidate(case):
    repo, baseline, _, recovery = case
    remote_head = commit(repo, "review.py", "external continuation\n", "review fix")
    git(repo, "push", "campaign-test", remote_head + ":refs/heads/campaign-unit")
    observed = recovery.pusher.push("old-checkpoint", baseline)
    assert observed == PushObservation("old-checkpoint", baseline, "confirmed", remote_head)


def test_divergent_remote_only_commit_is_fetched_for_older_ancestry_endpoint(case):
    repo, baseline, remote, recovery = case
    head = commit(repo, "local.py", "local work\n", "local continuation")
    remote_head = git(
        remote,
        "-c",
        "user.name=Scripted Bot",
        "-c",
        "user.email=bot@example.test",
        "commit-tree",
        baseline + "^{tree}",
        "-p",
        baseline,
        "-m",
        "remote-only continuation",
    )
    git(remote, "update-ref", "refs/heads/campaign-unit", remote_head, baseline)
    assert verify.git_bytes(repo, "cat-file", "-e", remote_head).returncode != 0
    before_refs = git(repo, "for-each-ref", "--format=%(refname) %(objectname)")
    observed = recovery.pusher.push("remote-only", head)
    assert observed == PushObservation("remote-only", head, "remote_changed", remote_head)
    assert git(repo, "for-each-ref", "--format=%(refname) %(objectname)") == before_refs
    assert git(repo, "rev-parse", "HEAD") == head
    assert git(remote, "rev-parse", "campaign-unit") == remote_head


def test_recovery_retains_remote_commit_and_settles_prior_unsent_debt(case):
    repo, baseline, _, recovery = case
    head, remote_head = divergent_heads(case)
    assert recovery.pusher.push("checkpoint", head).state == "remote_changed"
    git(repo, "merge", "--no-edit", remote_head)
    combined = git(repo, "rev-parse", "HEAD")
    recovery.begin(
        "repair-after-integration",
        expected_head=combined,
        baseline=baseline,
        owned_paths=("local.py",),
        quiescence=quiet(combined),
    )
    corrected = recovery.recover("repair-after-integration", quiescence=quiet(combined))
    result = recovery.finish("repair-after-integration", quiescence=quiet(corrected.head))
    assert result.state == "complete"
    assert verify.is_ancestor(repo, remote_head, result.head)
    assert verify.is_ancestor(repo, head, result.head)
    assert (repo / "review.py").read_text() == "review fix\n"
    assert recovery.pusher.reconcile("checkpoint").state == "confirmed"


def test_issued_unknown_push_never_becomes_unsent_on_remote_divergence(case):
    repo, baseline, _, recovery = case
    head = commit(repo, "local.py", "local work\n", "local continuation")
    pushes = []

    def runner(root, *args, **kwargs):
        if args[0] == "push":
            pushes.append(args)
            raise verify.GitError("lost reply; publication unknown")
        return verify.git_bytes(root, *args, **kwargs)

    pusher = CheckpointPusher(
        repo, recovery.root, "campaign-test", "campaign-unit", git_runner=runner
    )
    assert pusher.push("checkpoint", head).state == "sent_unknown"
    git(repo, "checkout", "-b", "review-bot", baseline)
    remote_head = commit(repo, "review.py", "review fix\n", "external review fix")
    git(repo, "push", "campaign-test", remote_head + ":refs/heads/campaign-unit")
    git(repo, "checkout", "campaign-unit")
    assert pusher.reconcile("checkpoint").state == "sent_unknown"
    assert pusher.push("checkpoint", head).state == "sent_unknown"
    journal = json.loads(pusher._path("checkpoint").read_text())
    assert journal["last_observed_remote"] == remote_head
    assert journal.get("submission") != "not-submitted"
    assert len(pushes) == 1


def test_remote_push_racing_checkpoint_has_definitive_porcelain_refusal(case):
    repo, baseline, remote, recovery = case
    head = commit(repo, "local.py", "local work\n", "local continuation")
    git(repo, "checkout", "-b", "review-bot", baseline)
    remote_head = commit(repo, "review.py", "review fix\n", "external review fix")
    git(repo, "checkout", "campaign-unit")
    pushes = []

    def runner(root, *args, **kwargs):
        if args[0] == "push":
            pushes.append(args)
            git(repo, "push", "campaign-test", remote_head + ":refs/heads/campaign-unit")
        return verify.git_bytes(root, *args, **kwargs)

    pusher = CheckpointPusher(
        repo, recovery.root, "campaign-test", "campaign-unit", git_runner=runner
    )
    assert pusher.push("racing-checkpoint", head) == PushObservation(
        "racing-checkpoint", head, "remote_changed", remote_head
    )
    journal = json.loads(pusher._path("racing-checkpoint").read_text())
    assert journal["submission"] == "not-submitted"
    assert journal["reason"] == "non_fast_forward_refusal"
    assert pusher.push("racing-checkpoint", head).state == "remote_changed"
    assert len(pushes) == 1
    assert git(remote, "rev-parse", "campaign-unit") == remote_head


@pytest.mark.parametrize(
    "output,returncode",
    [
        ("!\\t{head}:refs/heads/another-branch\\t[rejected] (fetch first)\\n", 1),
        ("!\\tHEAD:refs/heads/campaign-unit\\t[rejected] (fetch first)\\n", 1),
        (
            "!\\t{head}:refs/heads/campaign-unit\\t[remote rejected] (pre-receive hook declined)\\n",
            1,
        ),
        ("!\\t{head}:refs/heads/campaign-unit\\t[rejected] (fetch first)\\n", 128),
        (
            "!\\t{head}:refs/heads/campaign-unit\\t[rejected] (fetch first)\\n!\\tother:refs/heads/other\\t[rejected] (fetch first)\\n",
            1,
        ),
        ("fatal: non-fast-forward push rejected", 1),
    ],
)
def test_unqualified_push_failures_remain_uncertain(case, output, returncode):
    repo, _, _, recovery = case
    head = commit(repo, "local.py", "local work\n", "local continuation")
    pushes = []

    def runner(root, *args, **kwargs):
        if args[0] == "push":
            pushes.append(args)
            raw = output.format(head=head).replace("\\t", "\t").replace("\\n", "\n").encode()
            return subprocess.CompletedProcess(args, returncode, raw, b"fetch first; rejected")
        return verify.git_bytes(root, *args, **kwargs)

    pusher = CheckpointPusher(
        repo, recovery.root, "campaign-test", "campaign-unit", git_runner=runner
    )
    assert pusher.push("uncertain-checkpoint", head).state == "sent_unknown"
    assert pusher.push("uncertain-checkpoint", head).state == "sent_unknown"
    assert len(pushes) == 1
    assert (
        json.loads(pusher._path("uncertain-checkpoint").read_text()).get("submission")
        != "not-submitted"
    )


def test_prepared_push_detects_divergence_without_issuing_after_restart(case, monkeypatch):
    import bmad_loop.campaign_recovery as module

    repo, baseline, _, recovery = case
    head = commit(repo, "local.py", "local work\n", "local continuation")
    save = module._save

    def crash(path, row):
        save(path, row)
        if row.get("state") == "prepared":
            raise RuntimeError("stopped before submission")

    monkeypatch.setattr(module, "_save", crash)
    with pytest.raises(RuntimeError, match="before submission"):
        recovery.pusher.push("checkpoint", head)
    monkeypatch.setattr(module, "_save", save)
    git(repo, "checkout", "-b", "review-bot", baseline)
    remote_head = commit(repo, "review.py", "review fix\n", "external review fix")
    git(repo, "push", "campaign-test", remote_head + ":refs/heads/campaign-unit")
    git(repo, "checkout", "campaign-unit")

    def no_push(root, *args, **kwargs):
        assert args[0] != "push", "divergent checkpoint must not submit a Git push"
        return verify.git_bytes(root, *args, **kwargs)

    pusher = CheckpointPusher(
        repo, recovery.root, "campaign-test", "campaign-unit", git_runner=no_push
    )
    assert pusher.push("checkpoint", head).state == "remote_changed"
    assert json.loads(pusher._path("checkpoint").read_text())["submission"] == "not-submitted"


@pytest.mark.parametrize("method", ["push", "observe_existing"])
def test_dangling_push_journal_is_not_treated_as_missing(case, method):
    repo, baseline, _, recovery = case
    path = recovery.pusher._path("checkpoint")
    path.parent.mkdir(parents=True)
    path.symlink_to(path.parent / "missing-target")
    with pytest.raises(RecoveryHold, match="bounded regular"):
        getattr(recovery.pusher, method)("checkpoint", baseline)
    assert path.is_symlink()
    assert not (path.parent / "missing-target").exists()


def test_dangling_recovery_fence_is_not_treated_as_absent(case):
    _, baseline, _, recovery = case
    path = recovery.pusher.scope / "fence.json"
    path.symlink_to(path.parent / "missing-fence")
    with pytest.raises(RecoveryHold, match="bounded regular"):
        recovery.pusher.push("checkpoint", baseline)
    assert path.is_symlink()
    assert not recovery.pusher._path("checkpoint").exists()


def test_unknown_inflight_push_ancestor_can_be_settled_by_recovery(case):
    repo, baseline, _, recovery = case
    head = commit(repo, "owned.py", "rejected\n", "attempt")
    pending = PushObservation("lost-push", head, "sent_unknown")
    recovery.begin(
        "repair",
        expected_head=head,
        baseline=baseline,
        owned_paths=("owned.py",),
        pending_pushes=(pending,),
        quiescence=quiet(head),
    )
    receipt = recovery.recover("repair", quiescence=quiet(head))
    completed = recovery.finish("repair", quiescence=quiet(receipt.head))
    assert completed.state == "complete"
    assert verify.is_ancestor(repo, head, completed.head)


def test_pending_push_from_another_lineage_fences_without_edits(case):
    repo, baseline, _, recovery = case
    head = commit(repo, "owned.py", "attempt\n", "attempt")
    git(repo, "checkout", "-b", "foreign", baseline)
    other = commit(repo, "owned.py", "foreign\n", "foreign")
    git(repo, "checkout", "campaign-unit")
    with pytest.raises(RecoveryHold, match="potentially published"):
        recovery.begin(
            "repair",
            expected_head=head,
            baseline=baseline,
            owned_paths=("owned.py",),
            pending_pushes=(PushObservation("unknown", other, "in_flight"),),
            quiescence=quiet(head),
        )
    assert recovery.pusher.fenced() == "repair"
    assert git(repo, "rev-parse", "HEAD") == head
    assert (repo / "owned.py").read_text(encoding="utf-8") == "attempt\n"


def test_crash_after_commit_adopts_once_by_parent_tree_action(case, monkeypatch):
    repo, baseline, _, recovery = case
    head = commit(repo, "owned.py", "attempt\n", "attempt")
    recovery.begin(
        "repair",
        expected_head=head,
        baseline=baseline,
        owned_paths=("owned.py",),
        quiescence=quiet(head),
    )
    adopt = recovery._adopt
    monkeypatch.setattr(
        recovery, "_adopt", lambda *_: (_ for _ in ()).throw(RuntimeError("crash after commit"))
    )
    with pytest.raises(RuntimeError, match="crash"):
        recovery.recover("repair", quiescence=quiet(head))
    corrected = verify.rev_parse_head(repo)
    monkeypatch.setattr(recovery, "_adopt", adopt)
    resumed = recovery.recover("repair", quiescence=quiet(corrected))
    assert resumed.head == corrected
    assert git(repo, "rev-list", "--count", head + "..HEAD") == "1"


def test_wrong_recovery_commit_never_adopted(case, monkeypatch):
    repo, baseline, _, recovery = case
    head = commit(repo, "owned.py", "attempt\n", "attempt")
    recovery.begin(
        "repair",
        expected_head=head,
        baseline=baseline,
        owned_paths=("owned.py",),
        quiescence=quiet(head),
    )
    monkeypatch.setattr(recovery, "_adopt", lambda *_: (_ for _ in ()).throw(RuntimeError("crash")))
    with pytest.raises(RuntimeError):
        recovery.recover("repair", quiescence=quiet(head))
    monkeypatch.undo()
    foreign = commit(repo, "other.py", "extra unrelated change\n", "foreign continuation")
    with pytest.raises(RecoveryHold, match="parent, tree or action"):
        recovery.recover("repair", quiescence=quiet(foreign))


def test_no_content_correction_does_not_make_empty_commit(case):
    repo, baseline, _, recovery = case
    recovery.begin(
        "noop",
        expected_head=baseline,
        baseline=baseline,
        owned_paths=("owned.py",),
        quiescence=quiet(baseline),
    )
    receipt = recovery.recover("noop", quiescence=quiet(baseline))
    assert receipt.head == baseline
    assert recovery.recover("noop", quiescence=quiet(baseline)).head == baseline
    assert recovery.finish("noop", quiescence=quiet(baseline)).state == "complete"


def test_source_sole_writer_required_and_preserved_spec_correction_retained(case):
    repo, baseline, _, recovery = case
    (repo / "ledger.yaml").write_text("initial\n", encoding="utf-8")
    git(repo, "add", "ledger.yaml")
    git(repo, "commit", "-m", "ledger baseline")
    baseline = verify.rev_parse_head(repo)
    head = commit(repo, "owned.py", "authorized spec correction\n", "spec correction")
    commit(repo, "ledger.yaml", "attempt state\n", "attempt")
    head = verify.rev_parse_head(repo)
    recovery.begin(
        "repair",
        expected_head=head,
        baseline=baseline,
        owned_paths=("owned.py",),
        preserve_paths=("owned.py",),
        source_preimages={"ledger.yaml": b"initial\n"},
        quiescence=quiet(head),
    )
    with pytest.raises(RecoveryHold, match="sole-writer"):
        recovery.recover("repair", quiescence=quiet(head))
    calls = []

    def writer(preimages, action):
        calls.append(action)
        (repo / "ledger.yaml").write_bytes(preimages["ledger.yaml"])

    receipt = recovery.recover("repair", quiescence=quiet(head), restore_sources=writer)
    assert calls == ["repair"]
    assert (repo / "owned.py").read_text(encoding="utf-8") == "authorized spec correction\n"
    with pytest.raises(RecoveryHold, match="source state"):
        recovery.finish("repair", quiescence=quiet(receipt.head))
    assert (
        recovery.finish(
            "repair",
            quiescence=quiet(receipt.head),
            verify_sources=lambda images: (
                (repo / "ledger.yaml").read_bytes() == images["ledger.yaml"]
            ),
        ).state
        == "complete"
    )


def test_unproven_quiescence_refuses_before_fence_or_mutation(case):
    repo, baseline, _, recovery = case
    head = commit(repo, "owned.py", "attempt\n", "attempt")
    with pytest.raises(RecoveryHold, match="quiescent"):
        recovery.begin(
            "repair",
            expected_head=head,
            baseline=baseline,
            owned_paths=("owned.py",),
            quiescence=replace(quiet(head), sessions_terminal=False),
        )
    assert recovery.pusher.fenced() is None
    assert verify.rev_parse_head(repo) == head


def test_preexisting_untracked_file_never_deleted(case):
    repo, baseline, _, recovery = case
    (repo / "owner.txt").write_text("owner data", encoding="utf-8")
    with pytest.raises(RecoveryHold, match="pre-existing"):
        recovery.begin(
            "repair",
            expected_head=baseline,
            baseline=baseline,
            owned_paths=("owner.txt",),
            baseline_untracked=("owner.txt",),
            quiescence=quiet(baseline),
        )
    assert (repo / "owner.txt").read_text(encoding="utf-8") == "owner data"


def test_only_owned_paths_revert_and_prior_accepted_work_preserved(case):
    repo, baseline, _, recovery = case
    head = commit(repo, "owned.py", "bad\n", "attempt")
    head = commit(repo, "unrelated.py", "keep this accepted correction\n", "authorized unrelated")
    recovery.begin(
        "repair",
        expected_head=head,
        baseline=baseline,
        owned_paths=("owned.py",),
        quiescence=quiet(head),
    )
    receipt = recovery.recover("repair", quiescence=quiet(head))
    assert verify.is_ancestor(repo, head, receipt.head)
    assert (repo / "unrelated.py").read_text(encoding="utf-8") == "keep this accepted correction\n"


def test_later_remote_change_keeps_recovery_fence(case):
    repo, baseline, remote, recovery = case
    head = commit(repo, "owned.py", "bad\n", "attempt")
    recovery.begin(
        "repair",
        expected_head=head,
        baseline=baseline,
        owned_paths=("owned.py",),
        quiescence=quiet(head),
    )
    receipt = recovery.recover("repair", quiescence=quiet(head))
    # Server already contains a descendant beyond the recovered candidate.
    descendant = commit(repo, "unrelated.py", "later work\n", "external continuation")
    git(repo, "push", "campaign-test", descendant + ":refs/heads/campaign-unit")
    with pytest.raises(RecoveryHold, match="head changed"):
        recovery.finish("repair", quiescence=quiet(receipt.head))
    assert recovery.pusher.fenced() == "repair"


@pytest.mark.parametrize("stage", ["preserved", "correction_prepared", "committed"])
def test_crash_resume_does_not_repeat_correction(case, monkeypatch, stage):
    import bmad_loop.campaign_recovery as module

    repo, baseline, _, recovery = case
    head = commit(repo, "owned.py", "rejected\n", "attempt")
    recovery.begin(
        "repair",
        expected_head=head,
        baseline=baseline,
        owned_paths=("owned.py",),
        quiescence=quiet(head),
    )
    save = module._save
    failed = []

    def crashing(path, row):
        save(path, row)
        if path.name == "action.json" and row.get("state") == stage and not failed:
            failed.append(True)
            raise RuntimeError("process lost")

    monkeypatch.setattr(module, "_save", crashing)
    with pytest.raises(RuntimeError, match="process lost"):
        recovery.recover("repair", quiescence=quiet(head))
    monkeypatch.setattr(module, "_save", save)
    current = verify.rev_parse_head(repo)
    receipt = recovery.recover("repair", quiescence=quiet(current))
    assert git(repo, "rev-list", "--count", head + ".." + receipt.head) == "1"
    assert recovery.finish("repair", quiescence=quiet(receipt.head)).state == "complete"


def test_crash_after_complete_receipt_releases_fence_on_resume(case, monkeypatch):
    import bmad_loop.campaign_recovery as module

    repo, baseline, _, recovery = case
    head = commit(repo, "owned.py", "rejected\n", "attempt")
    recovery.begin(
        "repair",
        expected_head=head,
        baseline=baseline,
        owned_paths=("owned.py",),
        quiescence=quiet(head),
    )
    receipt = recovery.recover("repair", quiescence=quiet(head))
    save = module._save

    def crashing(path, row):
        save(path, row)
        if path.name == "action.json" and row.get("state") == "complete":
            raise RuntimeError("lost before fence release")

    monkeypatch.setattr(module, "_save", crashing)
    with pytest.raises(RuntimeError, match="fence release"):
        recovery.finish("repair", quiescence=quiet(receipt.head))
    assert recovery.pusher.fenced() == "repair"
    monkeypatch.setattr(module, "_save", save)
    assert recovery.finish("repair", quiescence=quiet(receipt.head)).state == "complete"
    assert recovery.pusher.fenced() is None


def test_prepared_push_after_crash_can_issue_once(case, monkeypatch):
    import bmad_loop.campaign_recovery as module

    repo, _, remote, recovery = case
    head = commit(repo, "owned.py", "checkpoint\n", "attempt")
    save = module._save

    def crashing(path, row):
        save(path, row)
        if row.get("state") == "prepared":
            raise RuntimeError("before issuing push")

    monkeypatch.setattr(module, "_save", crashing)
    with pytest.raises(RuntimeError, match="before issuing"):
        recovery.pusher.push("checkpoint", head)
    monkeypatch.setattr(module, "_save", save)
    assert recovery.pusher.push("checkpoint", head).state == "confirmed"
    assert git(remote, "rev-parse", "campaign-unit") == head


def test_unknown_unissued_push_is_not_repeated_but_descendant_recovery_settles(case):
    repo, baseline, _, recovery = case
    head = commit(repo, "owned.py", "bad\n", "attempt")
    calls = []

    def failing(root, *args, **kwargs):
        if args[0] == "push":
            calls.append(args)
            raise verify.GitError("unknown before network write")
        return verify.git_bytes(root, *args, **kwargs)

    pusher = CheckpointPusher(
        repo, recovery.root, "campaign-test", "campaign-unit", git_runner=failing
    )
    assert pusher.push("checkpoint", head).state == "sent_unknown"
    assert pusher.push("checkpoint", head).state == "sent_unknown"
    assert len(calls) == 1
    recovery.begin(
        "repair",
        expected_head=head,
        baseline=baseline,
        owned_paths=("owned.py",),
        quiescence=quiet(head),
    )
    receipt = recovery.recover("repair", quiescence=quiet(head))
    assert recovery.finish("repair", quiescence=quiet(receipt.head)).state == "complete"
    assert recovery.pusher.reconcile("checkpoint").state == "confirmed"


def test_ignored_private_source_restored_without_tracking_it(case):
    repo, baseline, _, recovery = case
    private = repo / ".bmad-loop" / "campaign-source" / "ledger.yaml"
    exclude = repo / ".git" / "info" / "exclude"
    exclude.parent.mkdir(exist_ok=True)
    exclude.write_text(".bmad-loop/campaign-source/\n", encoding="utf-8")
    private.parent.mkdir(parents=True)
    private.write_text("attempt status\n", encoding="utf-8")
    head = commit(repo, "owned.py", "bad\n", "attempt")
    rel = private.relative_to(repo).as_posix()
    assert git(repo, "check-ignore", rel) == rel
    recovery.begin(
        "repair",
        expected_head=head,
        baseline=baseline,
        owned_paths=("owned.py",),
        source_preimages={rel: b"prior status\n"},
        quiescence=quiet(head),
    )

    def writer(images, action):
        private.write_bytes(images[rel])

    receipt = recovery.recover("repair", quiescence=quiet(head), restore_sources=writer)
    assert private.read_text(encoding="utf-8") == "prior status\n"
    assert git(repo, "ls-tree", receipt.head, "--", rel) == ""
    assert git(repo, "show", receipt.dirty_ref + ":" + rel) == "attempt status"


def test_staged_only_residue_preserved_without_empty_commit_or_owner_index_loss(case):
    repo, baseline, _, recovery = case
    (repo / "owned.py").write_text("staged attempt\n", encoding="utf-8")
    git(repo, "add", "owned.py")
    (repo / "owned.py").write_text("accepted behavior\n", encoding="utf-8")
    (repo / "operator.txt").write_text("operator staged edit\n", encoding="utf-8")
    git(repo, "add", "operator.txt")
    recovery.begin(
        "index-repair",
        expected_head=baseline,
        baseline=baseline,
        owned_paths=("owned.py",),
        quiescence=quiet(baseline),
    )
    receipt = recovery.recover("index-repair", quiescence=quiet(baseline))
    assert receipt.head == baseline
    assert git(repo, "show", receipt.index_ref + ":owned.py") == "staged attempt"
    assert git(repo, "diff", "--cached", "--name-only") == "operator.txt"
    assert (repo / "operator.txt").read_text(encoding="utf-8") == "operator staged edit\n"


def test_conflicting_checkpoint_receipts_cannot_hide_published_head(case):
    repo, baseline, _, recovery = case
    head = commit(repo, "owned.py", "bad\n", "attempt")
    recovery.pusher.push("checkpoint", head)
    with pytest.raises(RecoveryHold, match="conflicting published head"):
        recovery.begin(
            "repair",
            expected_head=head,
            baseline=baseline,
            owned_paths=("owned.py",),
            pending_pushes=(PushObservation("checkpoint", baseline, "prepared"),),
            quiescence=quiet(head),
        )
    assert verify.rev_parse_head(repo) == head
