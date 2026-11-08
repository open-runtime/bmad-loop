"""Production adapter composition keeps native control state outside writable trees."""

import sys
from pathlib import Path

import pytest
from conftest import git, install_bmad_config
from test_campaign_codex_scoped_tools import scoped_writer as scoped_writer
from test_campaign_execution_qualification import fixture_writer_capability
from test_campaign_execution_qualification import writer as writer
from test_campaign_headless import prepared as prepared
from test_campaign_workers import envelope

from bmad_loop import campaign_headless
from bmad_loop import campaign_workers as workers
from bmad_loop.adapters import multiplexer
from bmad_loop.adapters.base import SessionSpec
from bmad_loop.campaign_execution_qualification import qualify
from bmad_loop.campaign_headless import read_json
from bmad_loop.campaign_model import CampaignError
from bmad_loop.policy import AdapterPolicy, Policy


@pytest.mark.skipif(sys.platform != "darwin", reason="native macOS scoped tools")
def test_worker_composition_runs_scoped_writer_with_external_control_state(
    project, tmp_path, monkeypatch, prepared, scoped_writer
):
    runner, _, job, candidate, _ = prepared
    proposal, _, _ = scoped_writer
    report = qualify(
        proposal,
        tmp_path / "proof",
        input_bytes=4096,
        output_tokens=4096,
        timeout_s=30,
        runner=runner,
    )
    assert report["ok"], report
    cap = read_json(Path(report["capability_path"]))
    canonical = project.project
    install_bmad_config(project)
    (canonical / "src").mkdir(exist_ok=True)
    (canonical / "src/owned.txt").write_text("initial\n")
    git(canonical, "add", "-f", "_bmad/bmm/config.yaml", "src/owned.txt")
    git(canonical, "commit", "-qm", "prepare worker composition")
    baseline = git(canonical, "rev-parse", "HEAD").strip()
    workspace = tmp_path / "worker"
    git(canonical, "worktree", "add", "-b", "scoped-production", str(workspace))
    policy = Policy(
        adapter=AdapterPolicy(
            name="codex",
            model=candidate.model,
            effort=candidate.effort,
        )
    )
    context = envelope(workspace, tmp_path / "controller", policy)
    run_dir = workspace / ".bmad-loop/runs/production-run"
    run_dir.mkdir(parents=True)
    monkeypatch.setattr(multiplexer, "get_multiplexer", lambda: runner.mux)
    monkeypatch.setattr(multiplexer, "mux_usable", lambda mux: True)
    adapters = workers._adapters(
        {
            "kind": "execution",
            "headless_capabilities": {"codex:" + candidate.model: {"execution": cap}},
            "headless_coordinates": job.coordinates,
            "headless_grants": {
                "schema_version": 1,
                "workspace": str(workspace),
                "scopes": ["src"],
                "git_branch": "scoped-production",
                "config_digest": "frozen",
                "generation": 1,
            },
        },
        context,
        workspace,
        run_dir,
        policy,
    )
    adapter = adapters["dev"]
    spec = SessionSpec(
        "production-scoped",
        "dev",
        "QUALIFICATION NONCE " + "a" * 32,
        workspace,
        model=candidate.model,
        effort=candidate.effort,
        timeout_s=30,
    )
    result = adapter.run(spec)
    request = adapter.requests[spec.task_id]
    assert result.status == "completed", (request.parent / "stderr.log").read_text()
    assert not request.resolve().is_relative_to(workspace.resolve())
    assert request.is_relative_to(context.directory)
    assert run_dir.name in request.parts
    assert adapter.run_dir == run_dir  # Preserve the engine journal and hook contract.
    assert (
        workspace / "src/owned.txt"
    ).read_text() == "native writer qualification: " + "a" * 32 + "\n"
    assert git(workspace, "log", "-1", "--format=%s").strip() == "native tool qualification"
    assert git(canonical, "rev-parse", "HEAD").strip() == baseline
    assert not git(workspace, "status", "--porcelain", "--", "src").strip()


@pytest.mark.parametrize("placement", ["product", "runtime-parent", "runtime-child"])
def test_prepare_refuses_writable_native_control_records(project, tmp_path, prepared, placement):
    runner, _, job, candidate, basecap = prepared
    cap = fixture_writer_capability(tmp_path, basecap, candidate)
    workspace = project.project.resolve()
    directory = workspace / "control" if placement == "product" else tmp_path / "control"
    grants = {
        "schema_version": 1,
        "workspace": str(workspace),
        "scopes": ["."],
        "config_digest": "frozen",
        "generation": 1,
    }
    if placement == "runtime-parent":
        grants["runtime_write_paths"] = [str(directory.parent)]
    elif placement == "runtime-child":
        grants["runtime_write_paths"] = [str(directory / "request.json")]
    with pytest.raises(CampaignError, match="control records"):
        runner.prepare(
            directory,
            job,
            candidate,
            cap,
            launch_id="unsafe",
            generation=1,
            permissions="workspace-tools",
            grants=grants,
        )
    assert not (directory / "request.json").exists()


def test_launch_rejects_previously_prepared_unsafe_control_layout(
    project, tmp_path, prepared, monkeypatch
):
    runner, _, job, candidate, basecap = prepared
    cap = fixture_writer_capability(tmp_path, basecap, candidate)
    grants = {
        "schema_version": 1,
        "workspace": str(project.project.resolve()),
        "scopes": ["."],
        "config_digest": "frozen",
        "generation": 1,
    }
    # Model a persisted launch from the old layout; current preparation must
    # reject it, and recovery must also refuse to launch its existing bytes.
    with monkeypatch.context() as old_layout:
        old_layout.setattr(campaign_headless, "validate_control_directory", lambda *args: None)
        request = runner.prepare(
            project.project / "control",
            job,
            candidate,
            cap,
            launch_id="old-layout",
            generation=1,
            permissions="workspace-tools",
            grants=grants,
        )
    with pytest.raises(CampaignError, match="control records"):
        runner.launch(request)
    assert not runner.mux.processes
    assert not (request.parent / "launch-intent.json").exists()
