"""A provider handoff in legacy review retains product and owes a fresh review."""

import dataclasses
import hashlib
import json
import os

import pytest
from conftest import git, install_bmad_config, review_effect, write_spec, write_sprint
from test_engine import make_engine

from bmad_loop import policy as policy_mod
from bmad_loop import runs, runsetup, verify
from bmad_loop.campaign_continuation import compose_review, validate_resume
from bmad_loop.campaign_execution import CampaignExecution, bind_run
from bmad_loop.campaign_finalization import _source_roots
from bmad_loop.campaign_fingerprint import capture
from bmad_loop.campaign_interruptions import execution_interruption
from bmad_loop.campaign_model import CampaignError
from bmad_loop.campaign_workers import _execution, common_directory, prepare_launch
from bmad_loop.engine import RunPaused
from bmad_loop.journal import load_state, save_state
from bmad_loop.model import Phase, SessionRecord, StoryTask, TokenUsage


def archive(root, value):
    data = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    digest = hashlib.sha256(data).hexdigest()
    path = root / digest
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return str(path), digest


@pytest.fixture
def review_handoff(project, tmp_path, request):
    repo = project.project
    install_bmad_config(project)
    write_sprint(project, {"1-1-owned": "done"})
    (repo / "owned.py").write_text("original\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "baseline")
    baseline = verify.rev_parse_head(repo)
    git(repo, "checkout", "-b", "campaign-review")
    path = project.implementation_artifacts / "spec-1-1-owned.md"
    write_spec(path, "done", baseline)
    (repo / "owned.py").write_text("accepted development\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "development checkpoint")
    policy = policy_mod.loads(
        '[gates]\nmode="none"\n[review]\nenabled=true\ntrigger="always"\n'
        "[notify]\ndesktop=false\nfile=false\n[scm]\nrollback_on_failure=true\n"
        "[recovery]\nmax_session_resumes=2\n[limits]\nmax_dev_attempts=1\nmax_review_cycles=1\n"
    )
    original_context = CampaignExecution(
        "campaign",
        "source:project:1-1-owned",
        1,
        2,
        1,
        tmp_path / "old-worker",
        policy,
        runsetup.resolve_profiles(policy, repo),
    )
    engine, adapter = make_engine(
        project,
        [review_effect(project, "1-1-owned", True)],
        policy=policy,
        campaign_execution=original_context,
    )
    task = StoryTask(
        "1-1-owned",
        1,
        phase=Phase.ESCALATED,
        baseline_commit=baseline,
        spec_file=str(path),
        attempt=2,
        review_cycle=3,
        tokens=TokenUsage(input_tokens=123),
        sessions=[
            SessionRecord("dev", "dev", "completed", session_id="old-dev"),
            SessionRecord("review", "review", "crashed", session_id="old-review", ended="pool"),
        ],
    )
    engine.state.tasks[task.story_key] = task
    engine.state.record_pause("pool unavailable", "escalation", task.story_key)
    engine.state.campaign_binding = bind_run(repo, engine.run_dir, original_context)
    save_state(engine.run_dir, engine.state)
    child = dataclasses.replace(
        original_context,
        attempt=2,
        generation=3,
        directory=tmp_path / "new-worker",
        policy=dataclasses.replace(
            policy,
            limits=dataclasses.replace(
                policy.limits, max_dev_attempts=getattr(request, "param", 1)
            ),
        ),
    )
    predecessor = {
        "unit": child.unit_id,
        "attempt": 1,
        "generation": 2,
        "launch_id": "old-launch",
        "workspace": str(repo),
        "branch": "campaign-review",
        "run_id": engine.state.run_id,
        "run_dir": str(original_context.directory),
        "quiescent": True,
    }
    sessions = [row.to_dict() for row in task.sessions]
    receipt = {
        "campaign_id": child.campaign_id,
        "unit_id": child.unit_id,
        "attempt": 1,
        "generation": 2,
        "launch_id": "old-launch",
        "run_id": engine.state.run_id,
        "run_dir": str(engine.run_dir),
        "sessions": sessions,
        "interruption": execution_interruption(sessions),
        "outcome": "paused",
    }
    state_path, state_hash = archive(tmp_path / "archive", engine.state.to_dict())
    receipt_path, receipt_hash = archive(tmp_path / "archive", receipt)
    roots = _source_roots(repo, project, engine.state)
    handoff = {
        "capture_digest": "captured",
        "native_role": "review",
        "original_run_id": engine.state.run_id,
        "original_run_dir": str(engine.run_dir),
        "original_state_path": state_path,
        "original_state_sha256": state_hash,
        "predecessor_receipt_path": receipt_path,
        "predecessor_receipt_sha256": receipt_hash,
        "source_roots": [str(path.relative_to(repo)) for path in roots],
        "fingerprint": capture(repo, source_roots=roots).to_dict(),
    }
    request = {
        "workspace": str(repo),
        "branch": "campaign-review",
        "common_dir": str(common_directory(repo)),
        "launch_id": "new-launch",
        "run_id": "review-successor",
        "kind": "execution",
        "source": {"kind": "sprint-status", "id": "source"},
        "unit": {
            "id": child.unit_id,
            "source": "source",
            "epic": "1",
            "title": "Owned",
            "selector": task.story_key,
        },
        "handoff_id": "handoff",
        "handoff_state": handoff,
        "native_predecessor": predecessor,
        "handoff_packet": {"read_only": False},
        "review_candidate": {
            "profile": policy.adapter.review.name,
            "model": policy.adapter.review.model,
            "effort": policy.adapter.review.effort,
        },
    }
    request = json.loads(prepare_launch(child, request).read_text())
    return project, engine, adapter, child, request


def compose(case):
    _, _, adapter, envelope, request = case
    return compose_review(
        request,
        envelope,
        make_adapters=lambda *a, **k: {"dev": adapter, "review": adapter, "retro": adapter},
    )


@pytest.mark.parametrize("review_handoff", [0, 1], indirect=True)
def test_normal_execution_handoff_reviews_done_source_without_restarting_development(
    review_handoff, monkeypatch
):
    project, old, adapter, child, request = review_handoff
    original = (old.run_dir / "state.json").read_bytes()
    request["initial_source"] = {
        "path": "_bmad-output/implementation-artifacts/spec-1-1-owned.md",
        "sha256": "old-ready-for-dev-source",
    }
    monkeypatch.setattr(
        "bmad_loop.campaign_workers._adapters",
        lambda *a, **k: {"dev": adapter, "review": adapter, "retro": adapter},
    )
    result = _execution(request, child)
    assert result["outcome"] == "locally_complete", result
    state = load_state(runs.run_dir_for(project.project, request["run_id"]))
    task = state.tasks["1-1-owned"]
    assert task.phase == Phase.DONE
    assert task.attempt == 2 and task.review_cycle == 4
    assert task.sessions[:2] == old.state.tasks[task.story_key].sessions
    assert task.tokens.input_tokens == 133
    assert state.replay_of == old.state.run_id
    from bmad_loop.journal import Journal

    entries = Journal(runs.run_dir_for(project.project, request["run_id"]), pin=state).entries()
    handoff = next(row for row in entries if row["kind"] == "campaign-review-handoff")
    assert handoff["predecessor"] == old.state.run_id
    assert handoff["handoff_id"] == request["handoff_id"]
    assert any(row["kind"] == "campaign-review-continuation" for row in entries)
    assert (old.run_dir / "state.json").read_bytes() == original
    assert len(adapter.sessions) == 1 and adapter.sessions[0].role == "review"
    assert not adapter.sessions[0].resume_session_id
    assert (project.project / "owned.py").read_text() == "accepted development\n"
    assert verify.is_ancestor(
        project.project,
        request["handoff_state"]["fingerprint"]["head"],
        verify.rev_parse_head(project.project),
    )


@pytest.mark.parametrize(
    "change",
    [
        "tree",
        "private_source",
        "archive",
        "receipt",
        "live",
        "role",
        "config",
        "quiescence",
        "candidate",
        "allowance",
        "claim",
    ],
)
def test_review_handoff_refuses_changed_or_unproven_boundary(review_handoff, change):
    project, old, adapter, child, request = review_handoff
    if change == "tree":
        (project.project / "owned.py").write_text("unobserved changes\n")
    elif change == "private_source":
        (project.implementation_artifacts / "spec-1-1-owned.md").write_text("changed source")
    elif change in {"archive", "receipt"}:
        key = "original_state" if change == "archive" else "predecessor_receipt"
        from pathlib import Path

        Path(request["handoff_state"][key + "_path"]).write_text("{}")
    elif change == "live":
        runs.write_named_pid(child.directory.parent / "old-worker" / "engine.pid", os.getpid())
    elif change == "role":
        request["handoff_state"]["native_role"] = "dev"
    elif change == "config":
        child = dataclasses.replace(child, config_revision=2)
    elif change == "quiescence":
        request["native_predecessor"]["quiescent"] = False
    elif change == "candidate":
        request["review_candidate"]["model"] = "different"
    elif change == "claim":
        request["native_predecessor"]["run_id"] = "different-run"
    else:
        child = dataclasses.replace(
            child,
            policy=dataclasses.replace(
                child.policy, limits=dataclasses.replace(child.policy.limits, max_review_cycles=0)
            ),
        )
    with pytest.raises(CampaignError):
        compose((project, old, adapter, child, request))
    assert not adapter.sessions


def test_permit_cannot_survive_candidate_mutation_or_be_replaced_by_boolean(review_handoff):
    project, old, adapter, child, request = review_handoff
    composed = compose(review_handoff)
    task = composed.state.tasks["1-1-owned"]
    assert validate_resume(composed.engine, task)
    (project.project / "owned.py").write_text("changed after composition\n")
    assert not validate_resume(composed.engine, task)
    composed.engine._campaign_review_permit = True
    assert not validate_resume(composed.engine, task)
    with pytest.raises(RunPaused, match="campaign recovery"):
        composed.engine._finish_inflight()
    assert not adapter.sessions
