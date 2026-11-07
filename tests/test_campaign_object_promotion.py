"""Accepted Git heads survive a live writer changing its private object bank."""

import json
import os
import shutil
import subprocess
import zlib

import pytest
from conftest import git

from bmad_loop import campaign_object_promotion as promotion
from bmad_loop.campaign_execution_sandbox import _object_bank
from bmad_loop.campaign_model import CampaignError


def private_git(repo, private, canonical, *args):
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        env={
            **os.environ,
            "GIT_OBJECT_DIRECTORY": str(private),
            "GIT_ALTERNATE_OBJECT_DIRECTORIES": str(canonical),
        },
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


@pytest.fixture
def bank(project, tmp_path):
    repo = project.project
    baseline = git(repo, "rev-parse", "HEAD").strip()
    branch = git(repo, "symbolic-ref", "--short", "HEAD").strip()
    private, canonical = _object_bank(
        {"git_branch": branch, "config_digest": "frozen"}, tmp_path / "job", repo
    )
    environment = {
        **os.environ,
        "GIT_OBJECT_DIRECTORY": str(private),
        "GIT_ALTERNATE_OBJECT_DIRECTORIES": str(canonical),
    }

    def commit(text):
        (repo / "src.txt").write_text(text)
        for args in (("add", "src.txt"), ("commit", "-qm", text)):
            result = subprocess.run(
                ["git", "-C", str(repo), *args], env=environment, capture_output=True
            )
            assert result.returncode == 0, result.stderr
        return git(repo, "rev-parse", "HEAD").strip()

    return repo, baseline, private, canonical, commit


def test_sealed_checkpoint_survives_existing_descriptor_and_bank_loss(bank):
    repo, baseline, private, canonical, commit = bank
    head = commit("first durable checkpoint")
    blob = private_git(repo, private, canonical, "rev-parse", head + ":src.txt")
    path = private / blob[:2] / blob[2:]
    path.chmod(0o644)
    with path.open("r+b") as writer:
        result = promotion.promote(repo, head, baseline)
        assert result["state"] == "sealed" and result["pack"]
        writer.write(b"corruption through a descriptor opened before acceptance")
        writer.flush()
    shutil.rmtree(private)
    assert git(repo, "show", head + ":src.txt") == "first durable checkpoint"
    assert git(repo, "rev-parse", head + "^{commit}").strip() == head
    again = promotion.promote(repo, head, baseline)
    assert again == {"state": "sealed", "head": head, "baseline": baseline}


def test_new_checkpoint_reuses_only_a_previously_sealed_baseline(bank):
    repo, baseline, private, canonical, commit = bank
    first = commit("first")
    second = commit("second")
    with pytest.raises(CampaignError, match="baseline is not sealed"):
        promotion.promote(repo, second, first)
    promotion.promote(repo, first, baseline)
    assert promotion.promote(repo, second, first)["state"] == "sealed"
    shutil.rmtree(private)
    assert git(repo, "show", second + ":src.txt") == "second"


def test_bank_cannot_replace_a_canonical_packed_object(bank):
    repo, baseline, private, canonical, commit = bank
    git(repo, "repack", "-ad")
    original = {path.name: path.read_bytes() for path in (canonical / "pack").iterdir()}
    blob = git(repo, "rev-parse", baseline + ":src.txt").strip()
    fake = private / blob[:2] / blob[2:]
    fake.parent.mkdir(exist_ok=True)
    fake.write_bytes(zlib.compress(b"blob 6\0poison"))
    head = commit("legitimate next commit")
    promotion.promote(repo, head, baseline)
    for name, contents in original.items():
        assert (canonical / "pack" / name).read_bytes() == contents
    assert git(repo, "show", baseline + ":src.txt") != "poison"
    shutil.rmtree(private)
    assert git(repo, "show", head + ":src.txt") == "legitimate next commit"


def test_corrupted_new_object_cannot_earn_a_sealed_receipt(bank):
    repo, baseline, private, canonical, commit = bank
    head = commit("required exact bytes")
    blob = private_git(repo, private, canonical, "rev-parse", head + ":src.txt")
    path = private / blob[:2] / blob[2:]
    path.chmod(0o644)
    path.write_bytes(zlib.compress(b"blob 6\0poison"))
    with pytest.raises(CampaignError, match="promotion failed"):
        promotion.promote(repo, head, baseline)


def test_legacy_repository_does_not_create_bank_or_pack(project):
    repo = project.project
    head = git(repo, "rev-parse", "HEAD").strip()
    assert promotion.promote(repo, head, "") == {"state": "not_required", "head": head}
    assert not (repo / ".git" / "bmad-loop-object-banks").exists()


def test_checkpoint_monitor_and_controller_read_seal_before_git_validation(
    bank, tmp_path, monkeypatch
):
    from bmad_loop import campaign_checkpoint as checkpoint

    repo, baseline, private, canonical, commit = bank
    request = {
        "campaign_id": "campaign",
        "unit_id": "story",
        "attempt": 1,
        "generation": 1,
        "launch_id": "launch",
        "workspace": str(repo),
        "branch": git(repo, "symbolic-ref", "--short", "HEAD").strip(),
        "checkpoint_interval_s": 20,
        "checkpoint_paths": ["src.txt"],
        "checkpoint_base": baseline,
    }
    monkeypatch.setattr(checkpoint, "_native", lambda *_: {})
    directory = tmp_path / "progress"
    directory.mkdir()
    monitor = checkpoint.ProgressMonitor(request, directory, clock=lambda: 100)
    first = commit("monitor seals new objects")
    observed = monitor.observe()
    assert observed["state"] == "observed" and observed["head"] == first
    assert git(repo, "show", first + ":src.txt") == "monitor seals new objects"
    second = commit("controller seals a newer immutable receipt")
    receipt = {**observed, "head": second}
    accepted = checkpoint.read(directory, request, now=100, max_age_s=30, receipt=receipt)
    assert accepted["head"] == second
    shutil.rmtree(private)
    assert git(repo, "show", second + ":src.txt") == "controller seals a newer immutable receipt"


@pytest.mark.parametrize("corrupt", [False, True])
def test_terminal_worker_seals_objects_or_holds_without_advancing_accepted_head(
    bank, tmp_path, monkeypatch, corrupt
):
    from bmad_loop import campaign_runtime
    from bmad_loop.campaign_model import (
        CampaignSnapshot,
        CampaignSpec,
        CampaignUnit,
        OutboxAction,
        UnitSpec,
        UnitState,
        WorkerClaim,
    )
    from bmad_loop.campaign_store import CampaignStore

    repo, baseline, private, canonical, commit = bank
    head = commit("terminal worker candidate")
    if corrupt:
        blob = private_git(repo, private, canonical, "rev-parse", head + ":src.txt")
        path = private / blob[:2] / blob[2:]
        path.chmod(0o644)
        path.write_bytes(zlib.compress(b"blob 6\0poison"))
    unit = CampaignUnit(UnitSpec("unit", "source", "epic", "Unit"), UnitState.VERIFYING)
    unit.bindings.update(workspace=str(repo), base=baseline, head=baseline)
    unit.claim = WorkerClaim(unit.id, 1, 0, "worker", role="verify", writes=True)
    snapshot = CampaignSnapshot(
        "promotion", "org/repo", CampaignSpec("promotion", units=(unit.spec,)), {unit.id: unit}
    )
    runtime = campaign_runtime.CampaignRuntime(repo, CampaignStore(tmp_path / "state"))
    directory = runtime.store.directory(snapshot.id) / "workers" / unit.claim.launch_id
    directory.mkdir(parents=True)
    (directory / "request.json").write_text(
        json.dumps({"kind": "verify", "checkpoint_base": baseline})
    )
    monkeypatch.setattr(
        campaign_runtime,
        "observe",
        lambda *_a, **_k: {
            "state": "finished",
            "receipt": {"head": head, "outcome": "verified"},
        },
    )
    action = OutboxAction("observe", "observe-worker", unit.id, {"launch_id": "worker"}, 0)
    observation = runtime._perform_observe_worker(snapshot, action)
    assert observation.state == "confirmed"
    runtime._apply_worker(snapshot, unit, action, observation.data)
    assert unit.claim.quiescent
    if corrupt:
        assert unit.bindings["head"] == baseline
        assert unit.hold.kind == "ownership_uncertain"
        assert unit.evidence["failure"]["code"] == "object_promotion_failed"
    else:
        assert unit.bindings["head"] == head and unit.state == UnitState.REVIEWING
        shutil.rmtree(private)
        assert git(repo, "show", head + ":src.txt") == "terminal worker candidate"
