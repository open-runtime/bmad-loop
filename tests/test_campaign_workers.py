"""Single-unit composition, child receipt authority and zero-token worker I/O."""

from __future__ import annotations

import dataclasses
import json
import shutil
import sys
import tempfile
from pathlib import Path

import pytest
from conftest import (
    DEFAULT_TREE,
    git,
    install_bmad_config,
    private_tmux_socket,
    real_mux_e2e,
    write_sprint,
    write_ticket_tree,
)
from test_stories_engine import SPEC_FOLDER, setup_stories

from bmad_loop import campaign_workers as workers
from bmad_loop import policy as policy_mod
from bmad_loop import runs, runsetup, verify
from bmad_loop.adapters.mock import MockAdapter
from bmad_loop.adapters.profile import CLIProfile, HookSpec
from bmad_loop.campaign_execution import CampaignExecution, CampaignExecutionError
from bmad_loop.campaign_model import CampaignError, SourceSelector, UnitSpec
from bmad_loop.engine import Engine, RunSummary
from bmad_loop.journal import load_state, save_state
from bmad_loop.model import Phase, StoryTask
from bmad_loop.stories_engine import StoriesEngine
from bmad_loop.tickets_engine import TicketsEngine


def envelope(repo, directory, policy=None, profiles=None):
    policy = policy or policy_mod.Policy()
    return CampaignExecution(
        "campaign-1",
        "s:project:unit",
        1,
        2,
        3,
        directory,
        policy,
        profiles or runsetup.resolve_profiles(policy, repo),
    )


def worker_request(repo, context, **changes):
    params = {
        "launch_id": "launch-1",
        "run_id": "campaign-stage",
        "workspace": str(repo),
        "branch": git(repo, "branch", "--show-current").strip(),
        "common_dir": str(workers.common_directory(repo)),
        "kind": "verify",
        "commands": [[sys.executable, "-c", "print('zero-token verification')"]],
    }
    return {**params, **changes}


@pytest.mark.parametrize("kind", ["sprint-status", "stories", "tickets"])
def test_real_compose_run_freezes_exact_source_selector_and_one_unit(
    request, tmp_path, monkeypatch, kind
):
    if kind == "tickets":
        project_v7 = request.getfixturevalue("project_v7")
    else:
        project = request.getfixturevalue("project")
    if kind == "tickets":
        repo = project_v7.project
        write_ticket_tree(project_v7, DEFAULT_TREE)
        source = SourceSelector(kind, initiative=project_v7.initiative, id="s")
        unit = UnitSpec("s:project:unit", "s", "epic-cart", "Selected", selector="1.1")
        expected_cls = TicketsEngine
    else:
        repo = project.project
        install_bmad_config(project)
        write_sprint(project, {"1-1-one": "ready-for-dev", "2-1-other": "ready-for-dev"})
        source = SourceSelector(kind, id="s", path=SPEC_FOLDER if kind == "stories" else "")
        selector = "first" if kind == "stories" else "1-1-one"
        unit = UnitSpec("s:project:unit", "s", "1", "Selected", selector=selector)
        if kind == "stories":
            setup_stories(
                project,
                [
                    {"id": "first", "title": "First", "description": "first"},
                    {"id": "other", "title": "Other", "description": "other"},
                ],
            )
        expected_cls = StoriesEngine if kind == "stories" else Engine
    policy = policy_mod.loads(
        '[recovery]\nreload_policy="story-boundary"\n[limits]\nmax_review_cycles=7\n[gates]\nmode="none"'
    )
    context = envelope(repo, tmp_path / "execution", policy)
    composed = []
    original = runsetup.compose_run

    def capture(**kwargs):
        assert kwargs["max_stories"] == 1
        assert kwargs["story_filter"] == unit.selector
        assert kwargs["policy"].recovery.reload_policy == "off"
        assert kwargs["policy"].limits.max_review_cycles == 7
        assert kwargs["campaign_execution"] == context
        assert kwargs["profiles"] == context.profiles
        actual = original(**kwargs)
        composed.append(actual)
        assert isinstance(actual.engine, expected_cls)
        return actual

    adapter = MockAdapter([])
    monkeypatch.setattr(
        runsetup,
        "make_adapters",
        lambda *args, **kwargs: {name: adapter for name in ("dev", "review", "retro", "triage")},
    )
    monkeypatch.setattr(runsetup, "compose_run", capture)

    def no_model_run(self):
        self.state.finished = True
        self.state.tasks[unit.selector] = StoryTask(unit.selector, 1, phase=Phase.DONE)
        save_state(self.run_dir, self.state)
        return RunSummary(self.state.run_id, 1, 0, 0, False, "", 0, 0)

    monkeypatch.setattr(expected_cls, "run", no_model_run)
    path = workers.prepare_launch(
        context,
        worker_request(
            repo,
            context,
            kind="execution",
            source=dataclasses.asdict(source),
            unit=dataclasses.asdict(unit),
            run_id="exact-worker",
        ),
    )
    result = workers._execution(json.loads(path.read_text()), context)
    state = load_state(composed[0].run_dir)
    assert result["outcome"] == "locally_complete"
    assert state.max_stories == 1
    assert state.story_filter == unit.selector
    assert state.source == kind
    if kind == "tickets":
        assert state.ticket_epic_filter == "epic-cart"
    elif kind == "stories":
        assert state.spec_folder == SPEC_FOLDER
    else:
        assert state.epic_filter == 1
    assert adapter.sessions == []


def test_prepared_launch_is_immutable_and_duplicate_spawn_is_not_guessed(project, tmp_path):
    context = envelope(project.project, tmp_path / "execution")
    initial = worker_request(project.project, context)
    path = workers.prepare_launch(context, initial)
    assert workers.prepare_launch(context, initial) == path
    with pytest.raises(CampaignExecutionError, match="replace a prepared launch"):
        workers.prepare_launch(context, {**initial, "launch_id": "other"})
    (context.directory / "startup.json").write_text("{}")
    with pytest.raises(CampaignError, match="startup binding"):
        workers.spawn(path)


def test_unacknowledged_launch_and_missing_pid_identity_remain_unknown(tmp_path, monkeypatch):
    assert workers.observe(tmp_path, expected={})["state"] == "unknown"
    metadata = {"campaign_id": "c", "unit_id": "u", "attempt": 1, "generation": 1, "launch_id": "l"}
    (tmp_path / "startup.json").write_text(json.dumps(metadata))
    monkeypatch.setattr(runs, "read_named_pid_identity", lambda path: (None, None))
    assert workers.observe(tmp_path, expected=metadata)["state"] == "unknown"


def observed_files(tmp_path, project):
    startup = {
        "campaign_id": "c",
        "unit_id": "u",
        "attempt": 1,
        "generation": 1,
        "launch_id": "l",
        "workspace": str(project.project),
        "common_dir": str(workers.common_directory(project.project)),
        "branch": git(project.project, "branch", "--show-current").strip(),
        "schema_version": 1,
    }
    receipt = {**startup, "outcome": "verified", "head": verify.rev_parse_head(project.project)}
    (tmp_path / "startup.json").write_text(json.dumps(startup))
    (tmp_path / "receipt.json").write_text(json.dumps(receipt))
    return startup, receipt


def test_alive_or_uncertain_actor_cannot_be_finished_by_early_receipt(
    tmp_path, project, monkeypatch
):
    startup, _ = observed_files(tmp_path, project)
    monkeypatch.setattr(runs, "read_named_pid_identity", lambda path: (123, 456))
    monkeypatch.setattr(runs, "probe_liveness", lambda *args: "alive")
    assert workers.observe(tmp_path, expected=startup)["state"] == "running"
    monkeypatch.setattr(runs, "probe_liveness", lambda *args: "unknown")
    assert workers.observe(tmp_path, expected=startup)["state"] == "unknown"


def test_dead_worker_requires_matching_final_receipt_and_unchanged_workspace(
    tmp_path, project, monkeypatch
):
    startup, receipt = observed_files(tmp_path, project)
    monkeypatch.setattr(runs, "read_named_pid_identity", lambda path: (123, 456))
    monkeypatch.setattr(runs, "probe_liveness", lambda *args: "dead")
    assert workers.observe(tmp_path, expected=startup)["state"] == "finished"
    (tmp_path / "receipt.json").unlink()
    assert workers.observe(tmp_path, expected=startup)["state"] == "interrupted"
    (tmp_path / "receipt.json").write_text(json.dumps({**receipt, "generation": 0}))
    with pytest.raises(CampaignError, match="receipt binding"):
        workers.observe(tmp_path, expected=startup)
    (tmp_path / "receipt.json").write_text(json.dumps(receipt))
    (project.project / "checkpoint").write_text("new head")
    git(project.project, "add", "checkpoint")
    git(project.project, "commit", "-qm", "new checkpoint")
    with pytest.raises(CampaignError, match="head changed"):
        workers.observe(tmp_path, expected=startup)


def test_startup_binding_rejects_reused_generation_and_foreign_worktree(
    tmp_path, project, monkeypatch
):
    startup, receipt = observed_files(tmp_path, project)
    with pytest.raises(CampaignError, match="startup generation"):
        workers.observe(tmp_path, expected={**startup, "generation": 2})
    monkeypatch.setattr(runs, "read_named_pid_identity", lambda path: (123, 456))
    monkeypatch.setattr(runs, "probe_liveness", lambda *args: "dead")
    (tmp_path / "startup.json").write_text(json.dumps({**startup, "branch": "foreign"}))
    with pytest.raises(CampaignError, match="branch binding changed"):
        workers.observe(tmp_path, expected=startup)


def test_module_child_verification_receipt_has_real_process_and_git_binding(project, tmp_path):
    context = envelope(project.project, tmp_path / "execution")
    path = workers.prepare_launch(context, worker_request(project.project, context))
    process = workers.spawn(path)
    assert process.wait(timeout=20) == 0
    observed = workers.observe(context.directory, expected=json.loads(path.read_text()))
    assert observed["state"] == "finished"
    assert observed["receipt"]["outcome"] == "verified"
    assert observed["receipt"]["commands"][0]["returncode"] == 0
    assert "zero-token verification" in observed["receipt"]["commands"][0]["stdout"]
    assert observed["receipt"]["head"] == verify.rev_parse_head(project.project)
    pid, identity = runs.read_named_pid_identity(context.directory / "engine.pid")
    assert pid == process.pid
    assert identity is not None


def test_child_failed_verification_is_not_an_acceptance_receipt(project, tmp_path):
    context = envelope(project.project, tmp_path / "execution")
    path = workers.prepare_launch(
        context,
        worker_request(
            project.project, context, commands=[[sys.executable, "-c", "raise SystemExit(17)"]]
        ),
    )
    process = workers.spawn(path)
    assert process.wait(timeout=20) == 1
    observed = workers.observe(context.directory, expected=json.loads(path.read_text()))
    assert observed["state"] == "finished"
    assert observed["receipt"]["outcome"] == "verification_failed"
    assert observed["receipt"]["commands"][0]["returncode"] == 17


def test_verification_observes_ignored_story_sources_outside_output_folder(project, tmp_path):
    install_bmad_config(project)
    repo = project.project
    private = repo / "hidden_sources"
    private.mkdir()
    product = private / "story.md"
    product.write_text("accepted source", encoding="utf-8")
    with (repo / ".gitignore").open("a", encoding="utf-8") as handle:
        handle.write("\n/hidden_sources/\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-m", "private source configuration")
    context = envelope(repo, tmp_path / "execution")
    request = worker_request(
        repo,
        context,
        source={"kind": "stories", "path": "hidden_sources"},
        commands=[
            [
                sys.executable,
                "-c",
                "from pathlib import Path; Path('hidden_sources/story.md').write_text('mutated')",
            ]
        ],
    )
    with pytest.raises(CampaignError, match="changed candidate content"):
        workers._verify(request, context)


def test_append_only_finalization_retains_private_checkpoint_chain(project):
    baseline = verify.rev_parse_head(project.project)
    checkpoints = []
    for number in range(3):
        (project.project / "checkpoint.txt").write_text(str(number))
        git(project.project, "add", "checkpoint.txt")
        git(project.project, "commit", "-qm", f"agent checkpoint {number}")
        checkpoints.append(verify.rev_parse_head(project.project))
    (project.project / "bookkeeping.txt").write_text("verified state")
    result = verify.finalize_commit(project.project, baseline, "finish", append_only=True)
    assert result == verify.rev_parse_head(project.project)
    for head in checkpoints:
        assert verify.is_ancestor(project.project, head, result)
    assert git(project.project, "rev-list", "--count", f"{baseline}..{result}").strip() == "4"


def test_post_commit_validator_failure_never_rewinds_append_only_history(project):
    baseline = verify.rev_parse_head(project.project)
    (project.project / "work.txt").write_text("accepted agent work")
    git(project.project, "add", "work.txt")
    git(project.project, "commit", "-qm", "agent checkpoint")
    checkpoint = verify.rev_parse_head(project.project)
    (project.project / "bookkeeping.txt").write_text("bookkeeping")

    def reject(head, snapshot):
        raise ValueError("post-commit evidence changed")

    with pytest.raises(verify.GitError, match="history retained"):
        verify.finalize_commit(
            project.project,
            baseline,
            "finish",
            append_only=True,
            staged_validator=lambda: "accepted-index",
            committed_validator=reject,
        )
    head = verify.rev_parse_head(project.project)
    assert head != checkpoint
    assert verify.is_ancestor(project.project, checkpoint, head)
    assert git(project.project, "show", "HEAD:bookkeeping.txt").strip() == "bookkeeping"


@pytest.fixture
def private_tmux_server():
    # macOS's per-user TMPDIR is long enough to overflow sockaddr_un once the
    # pytest path is included. This contains only a socket, never a worktree.
    with tempfile.TemporaryDirectory(prefix="bmad-campaign-", dir="/tmp") as root:
        with private_tmux_socket(Path(root)) as socket:
            yield socket


FAKE_CLI = """import json,os,time
from pathlib import Path
run=Path(os.environ["BMAD_LOOP_RUN_DIR"])
task=os.environ["BMAD_LOOP_TASK_ID"]
events=Path(os.environ["BMAD_LOOP_EVENTS_DIR"])
events.mkdir(parents=True,exist_ok=True)
folder=run/"tasks"/task
folder.mkdir(parents=True,exist_ok=True)
(folder/"result.json").write_text(json.dumps({"status":"done","artifact":{"zero_tokens":True}}))
now=time.time_ns()
for offset,kind in enumerate(["SessionStart","Stop"]):
    record={"ts":now+offset,"event":kind,"task_id":task,"session_id":"scripted-zero-token"}
    (events/f"{now+offset}-{task}-{kind}.json").write_text(json.dumps(record))
"""


@pytest.mark.skipif(
    sys.platform == "win32" or shutil.which("tmux") is None,
    reason="scripted CLI transport qualification needs POSIX tmux",
)
@real_mux_e2e
@pytest.mark.usefixtures("private_tmux_server")
def test_child_module_runs_scripted_coding_cli_through_real_mux_zero_tokens(project, tmp_path):
    install_bmad_config(project)
    git(project.project, "add", "-A")
    git(project.project, "commit", "-qm", "fixture config")
    script = tmp_path / "fake_cli.py"
    script.write_text(FAKE_CLI)
    profile = CLIProfile(
        "scripted-zero-token",
        sys.executable,
        HookSpec(
            "claude-settings-json",
            ".claude/settings.json",
            {"SessionStart": "SessionStart", "Stop": "Stop"},
        ),
        launch_args=(str(script),),
        usage_parser="none",
    )
    policy = policy_mod.loads('[mux]\nbackend="tmux"\n[notify]\ndesktop=false')
    profiles = runsetup.resolve_profiles(policy, project.project)
    profiles["scripted-zero-token"] = profile
    context = envelope(project.project, tmp_path / "execution", policy, profiles)
    path = workers.prepare_launch(
        context,
        worker_request(
            project.project,
            context,
            kind="stage",
            role="planning_review",
            candidate={"profile": "scripted-zero-token", "model": ""},
            profile_key="scripted-zero-token",
            prompt="Return the zero-token scripted artifact.",
            timeout_s=10,
            read_only=True,
        ),
    )
    process = workers.spawn(path)
    code = process.wait(timeout=25)
    assert code == 0, (context.directory / "receipt.json").read_text()
    observed = workers.observe(context.directory, expected=json.loads(path.read_text()))
    assert observed["receipt"]["outcome"] == "stage_complete"
    assert observed["receipt"]["artifact"]["artifact"]["zero_tokens"] is True
    assert observed["receipt"]["usage"] is None


def test_local_completion_receipt_requires_a_persisted_legacy_run(tmp_path, project, monkeypatch):
    startup, receipt = observed_files(tmp_path, project)
    monkeypatch.setattr(runs, "read_named_pid_identity", lambda path: (123, 456))
    monkeypatch.setattr(runs, "probe_liveness", lambda *args: "dead")
    (tmp_path / "receipt.json").write_text(json.dumps({**receipt, "outcome": "locally_complete"}))
    with pytest.raises(CampaignError, match="legacy run|run identity|run_dir"):
        workers.observe(tmp_path, expected=startup)
