"""Trusted Engine retry handoffs recover through real remote descendant commits."""

import dataclasses
import hashlib
import json
import subprocess

import pytest
from conftest import git, install_bmad_config, write_spec, write_sprint
from test_engine import make_engine

from bmad_loop import policy as policy_mod
from bmad_loop import runsetup, sprintstatus, verify
from bmad_loop.campaign_execution import CampaignExecution
from bmad_loop.campaign_internal_recovery import recover
from bmad_loop.campaign_recovery import CampaignRecovery, CheckpointPusher, Quiescence, RecoveryHold
from bmad_loop.campaign_workers import common_directory, prepare_launch
from bmad_loop.engine import RunPaused
from bmad_loop.model import Phase, StoryTask


def quiet(head, generation=2):
    return Quiescence(head, generation, True, True, "worker-and-sessions-terminal")


@pytest.fixture
def handoff(project, tmp_path):
    repo = project.project
    install_bmad_config(project)
    write_sprint(project, {"1-1-owned": "ready-for-dev", "1-2-other": "ready-for-dev"})
    (repo / "owned.py").write_text("accepted behavior\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "accepted baseline")
    baseline = verify.rev_parse_head(repo)
    git(repo, "checkout", "-b", "campaign-unit")
    remote = tmp_path / "remote.git"
    subprocess.run(
        ["git", "clone", "--bare", str(repo), str(remote)], capture_output=True, check=True
    )
    git(repo, "remote", "add", "campaign-test", str(remote))
    (repo / "pre-existing-note.txt").write_text("owner note\n")
    policy = policy_mod.loads("[scm]\nrollback_on_failure=true")
    context = CampaignExecution(
        "campaign-1",
        "source:project:1-1-owned",
        1,
        2,
        1,
        tmp_path / "worker",
        policy,
        runsetup.resolve_profiles(policy, repo),
    )
    prepare_launch(
        context,
        {
            "launch_id": "launch-1",
            "workspace": str(repo),
            "branch": "campaign-unit",
            "common_dir": str(common_directory(repo)),
            "kind": "execution",
            "run_id": "child-run",
            "source": {"kind": "sprint-status", "id": "source"},
            "unit": {
                "id": context.unit_id,
                "source": "source",
                "epic": "1",
                "title": "Owned",
                "selector": "1-1-owned",
            },
        },
    )
    (repo / "owned.py").write_text("rejected attempt\n")
    sprintstatus.advance(project.sprint_status, "1-1-owned", "done")
    git(repo, "add", "owned.py", str(project.sprint_status))
    git(repo, "commit", "-qm", "published agent checkpoint")
    checkpoint = verify.rev_parse_head(repo)
    pusher = CheckpointPusher(repo, tmp_path / "recovery-state", "campaign-test", "campaign-unit")
    assert pusher.push("checkpoint-1", checkpoint).state == "confirmed"
    task = StoryTask(
        "1-1-owned",
        1,
        phase=Phase.DEV_VERIFY,
        baseline_commit=baseline,
        baseline_untracked=["pre-existing-note.txt"],
    )
    engine, _ = make_engine(project, [], policy=policy, campaign_execution=context)
    engine.state.tasks[task.story_key] = task
    return project, context, engine, task, pusher, checkpoint, baseline, remote


def invoke(case, *, action="retry-1", quiescence=None, allowed=("owned.py",), request_path=None):
    project, context, engine, task, pusher, checkpoint, baseline, remote = case
    return recover(
        project.project,
        project.project,
        pusher.root,
        request_path or context.directory / "recovery-request.json",
        action,
        "campaign-test",
        "campaign-unit",
        quiescence or quiet(verify.rev_parse_head(project.project)),
        allowed,
    )


def request(case):
    project, context, engine, task, *_ = case
    with pytest.raises(RunPaused, match="campaign recovery"):
        engine._roll_back_rejected_attempt(task, replayed=False)


def test_internal_rollback_after_pushed_checkpoint_is_append_only_and_next_push_is_fast_forward(
    handoff,
):
    project, context, engine, task, pusher, checkpoint, baseline, remote = handoff
    # Independent source progress that predates recovery remains untouched.
    sprintstatus.advance(project.sprint_status, "1-2-other", "in-progress")
    request(handoff)
    receipt = invoke(handoff)
    assert receipt["state"] == "complete"
    assert receipt["head"] != checkpoint
    assert verify.is_ancestor(project.project, checkpoint, receipt["head"])
    assert (project.project / "owned.py").read_text() == "accepted behavior\n"
    assert (project.project / "pre-existing-note.txt").read_text() == "owner note\n"
    assert sprintstatus.story_status(project.sprint_status, "1-1-owned") == "ready-for-dev"
    assert sprintstatus.story_status(project.sprint_status, "1-2-other") == "in-progress"
    (project.project / "owned.py").write_text("accepted next attempt\n")
    git(project.project, "add", "owned.py")
    git(project.project, "commit", "-qm", "next attempt")
    next_head = verify.rev_parse_head(project.project)
    assert pusher.push("next-attempt", next_head).state == "confirmed"
    assert git(remote, "rev-parse", "campaign-unit").strip() == next_head


def test_internal_recovery_ledger_cas_and_corrected_spec_preserve_same_transaction(handoff):
    project, context, engine, task, pusher, checkpoint, baseline, remote = handoff
    spec = project.implementation_artifacts / "owned-spec.md"
    write_spec(spec, "ready-for-dev", baseline)
    correction = spec.read_bytes() + b"\nOwner-corrected acceptance\n"
    spec.write_bytes(correction)
    task.dispatched_spec_file = str(spec)
    task.dispatched_spec_snapshot = correction
    task.spec_file = str(spec)
    task.resolved_redrive = True
    spec.write_bytes(correction.replace(b"ready-for-dev", b"done") + b"attempt body change\n")
    ledger = project.deferred_work
    ledger.write_text("engine harvest\n")
    task.pre_harvest_ledger_captured = True
    task.pre_harvest_ledger = "original ledger\n"
    task.post_engine_ledger_digest = hashlib.sha256(ledger.read_bytes()).hexdigest()
    request(handoff)
    receipt = invoke(handoff)
    assert receipt["source_restored"] is True
    assert ledger.read_text() == "original ledger\n"
    assert spec.read_bytes() == correction
    assert verify.is_ancestor(project.project, checkpoint, receipt["head"])


def test_divergent_ledger_is_held_without_retracting_another_writers_work(handoff):
    project, context, engine, task, *_ = handoff
    project.deferred_work.write_text("engine harvest\n")
    task.pre_harvest_ledger_captured = True
    task.pre_harvest_ledger = "original\n"
    task.post_engine_ledger_digest = hashlib.sha256(project.deferred_work.read_bytes()).hexdigest()
    request(handoff)
    project.deferred_work.write_text("another writer\n")
    with pytest.raises(RecoveryHold, match="ledger diverged"):
        invoke(handoff)
    assert project.deferred_work.read_text() == "another writer\n"


def test_completed_internal_recovery_is_idempotent_after_lost_ack(handoff):
    request(handoff)
    first = invoke(handoff)
    second = invoke(handoff, quiescence=quiet(first["head"]))
    assert second == first


def test_internal_recovery_requires_exact_quiescence_and_frozen_binding(handoff):
    request(handoff)
    project, context, engine, task, pusher, checkpoint, *_ = handoff
    with pytest.raises(RecoveryHold, match="quiescent"):
        invoke(handoff, quiescence=dataclasses.replace(quiet(checkpoint), sessions_terminal=False))
    raw = json.loads((context.directory / "recovery-request.json").read_text())
    raw["generation"] = 99
    (context.directory / "recovery-request.json").write_text(json.dumps(raw))
    with pytest.raises(RecoveryHold, match="generation differs"):
        invoke(handoff)
    assert verify.rev_parse_head(project.project) == checkpoint


def test_post_commit_crash_adopts_existing_compensation_without_repeating_it(handoff, monkeypatch):
    request(handoff)
    original = CampaignRecovery.finish
    monkeypatch.setattr(
        CampaignRecovery,
        "finish",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("crash after recovery commit")),
    )
    with pytest.raises(OSError, match="crash after recovery commit"):
        invoke(handoff)
    head = verify.rev_parse_head(handoff[0].project)
    monkeypatch.setattr(CampaignRecovery, "finish", original)
    receipt = invoke(handoff, quiescence=quiet(head))
    assert receipt["head"] == head
    assert receipt["state"] == "complete"


def test_finalization_handoff_never_reverts_already_accepted_code(handoff):
    project, context, engine, task, pusher, checkpoint, *_ = handoff
    context.recovery_request(task, checkpoint, cause="finalization-failed")
    with pytest.raises(RecoveryHold, match="preserved-candidate re-verification"):
        invoke(handoff)
    assert verify.rev_parse_head(project.project) == checkpoint
    assert (project.project / "owned.py").read_text() == "rejected attempt\n"


def test_pending_and_lost_reply_checkpoint_heads_settle_as_recovered_ancestors(handoff):
    project, context, engine, task, pusher, checkpoint, *_ = handoff
    for name, state in (("pending-old-head", "prepared"), ("lost-ack-old-head", "sent_unknown")):
        path = pusher._path(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "action_id": name,
                    "head": checkpoint,
                    "state": state,
                    "recovery_action": None,
                }
            )
        )
    request(handoff)
    receipt = invoke(handoff)
    assert pusher.reconcile("pending-old-head").state == "confirmed"
    assert pusher.reconcile("lost-ack-old-head").state == "confirmed"
    assert pusher.remote_head() == receipt["head"]
    assert verify.is_ancestor(project.project, checkpoint, receipt["head"])


def test_staged_and_unstaged_attempt_residue_are_preserved_before_compensation(handoff):
    project, context, engine, task, pusher, checkpoint, *_ = handoff
    (project.project / "owned.py").write_text("staged rejected residue\n")
    git(project.project, "add", "owned.py")
    (project.project / "owned.py").write_text("unstaged rejected residue\n")
    request(handoff)
    receipt = invoke(handoff)
    assert receipt["index_ref"]
    assert receipt["dirty_ref"]
    assert (
        git(project.project, "show", receipt["index_ref"] + ":owned.py").strip()
        == "staged rejected residue"
    )
    assert (
        git(project.project, "show", receipt["dirty_ref"] + ":owned.py").strip()
        == "unstaged rejected residue"
    )
    assert (project.project / "owned.py").read_text() == "accepted behavior\n"


def test_ticket_lifecycle_preimage_uses_existing_sole_restore_writer(
    project_v7, tmp_path, monkeypatch
):
    from conftest import write_plan

    from bmad_loop.tickets_source import ticket_binding
    from bmad_loop.tickettree import TicketTree

    repo = project_v7.project
    plan = write_plan(project_v7, "epic-cart", "story-cart-model-plan", 1, "ready-for-dev")
    preimage = plan.read_bytes()
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "ticket baseline")
    baseline = verify.rev_parse_head(repo)
    git(repo, "checkout", "-b", "campaign-ticket")
    remote = tmp_path / "ticket-remote.git"
    subprocess.run(
        ["git", "clone", "--bare", str(repo), str(remote)], capture_output=True, check=True
    )
    git(repo, "remote", "add", "campaign-test", str(remote))
    binding = ticket_binding(TicketTree(repo).find("1.1"))
    plan.write_bytes(preimage.replace(b"ready-for-dev", b"in-progress") + b"child body changes\n")
    (repo / "src.txt").write_text("rejected code\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "ticket checkpoint")
    checkpoint = verify.rev_parse_head(repo)
    policy = policy_mod.Policy()
    context = CampaignExecution(
        "campaign-ticket",
        "tickets:initiative:1.1",
        1,
        2,
        1,
        tmp_path / "ticket-worker",
        policy,
        runsetup.resolve_profiles(policy, repo),
    )
    prepare_launch(
        context,
        {
            "launch_id": "ticket-launch",
            "workspace": str(repo),
            "branch": "campaign-ticket",
            "common_dir": str(common_directory(repo)),
            "kind": "execution",
            "run_id": "ticket-child",
            "source": {"kind": "tickets", "id": "tickets", "initiative": project_v7.initiative},
            "unit": {
                "id": context.unit_id,
                "source": "tickets",
                "epic": "epic-cart",
                "title": "Cart",
                "selector": "1.1",
            },
        },
    )
    task = StoryTask(
        "1.1",
        1,
        phase=Phase.DEV_VERIFY,
        baseline_commit=baseline,
        baseline_untracked=[],
        dispatched_spec_file=str(plan),
        dispatched_spec_snapshot=preimage,
        ticket=binding,
    )
    context.recovery_request(task, checkpoint, cause="rejected-attempt")
    original = TicketTree.restore_preimage
    calls = []

    def restored(self, target, data, journal):
        calls.append((target, data))
        return original(self, target, data, journal)

    monkeypatch.setattr(TicketTree, "restore_preimage", restored)
    state_dir = tmp_path / "ticket-recovery"
    CheckpointPusher(repo, state_dir, "campaign-test", "campaign-ticket").push(
        "ticket-checkpoint", checkpoint
    )
    receipt = recover(
        repo,
        repo,
        state_dir,
        context.directory / "recovery-request.json",
        "ticket-retry",
        "campaign-test",
        "campaign-ticket",
        quiet(checkpoint),
        ("src.txt",),
    )
    assert calls == [(plan, preimage)]
    assert plan.read_bytes() == preimage
    assert (repo / "src.txt").read_text() == "original\n"
    assert verify.is_ancestor(repo, checkpoint, receipt["head"])
