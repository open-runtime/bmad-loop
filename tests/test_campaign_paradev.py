"""Project-correct workspace allocation and native projection under uncertain APIs."""

import copy
import os
import subprocess
from pathlib import Path

import pytest

from bmad_loop.campaign_paradev import ParadevWorkspaceProvider, ReflectionError


class Host:
    def __init__(self, repo, root):
        self.repo, self.root = repo, root
        self.projects = [{"id": "project", "repoPath": str(repo), "worktreeBaseDir": str(root)}]
        self.workspaces = []
        self.calls = []
        self.lose_reply = False
        self.no_create = False
        self.rewrite_branch = False
        self.tasks = []
        self.statuses = [
            {"id": "todo", "type": "unstarted"},
            {"id": "doing", "type": "started"},
            {"id": "done", "type": "completed"},
        ]

    def rpc(self, procedure, payload=None):
        if procedure == "project.list":
            return copy.deepcopy(self.projects)
        if procedure == "workspace.list":
            return copy.deepcopy(self.workspaces)
        if procedure == "terminal.daemon.listSessions":
            return []
        raise AssertionError(procedure)

    def cli(self, args):
        self.calls.append(args)
        if args[:2] == ["workspaces", "create"]:
            if not self.no_create:
                branch = args[args.index("--branch") + 1]
                checkout = self.root / ("unit-" + str(len(self.workspaces)))
                subprocess.run(
                    ["git", "-C", str(self.repo), "worktree", "add", "-b", branch, str(checkout)],
                    check=True,
                    capture_output=True,
                )
                self.workspaces.append(
                    {
                        "id": "workspace-" + str(len(self.workspaces)),
                        "projectId": "project",
                        "worktreePath": str(checkout),
                        "branch": branch if not self.rewrite_branch else "rewritten",
                        "tags": args[args.index("--tag") + 1],
                    }
                )
            if self.lose_reply or self.no_create:
                raise TimeoutError
            return self.workspaces[-1]
        if args[:2] == ["workspaces", "update"]:
            row = next(r for r in self.workspaces if r["id"] == args[2])
            tags = [args[i + 1] for i, arg in enumerate(args) if arg == "--tag"]
            if tags:
                row["tags"] = ",".join(tags)
            if "--task-id" in args:
                row["taskId"] = args[args.index("--task-id") + 1]
            return row
        if args[:2] == ["tasks", "create"]:
            row = {
                "id": "task-" + str(len(self.tasks)),
                "title": args[args.index("--title") + 1],
                "description": args[args.index("--description") + 1],
            }
            self.tasks.append(row)
            if self.lose_reply:
                raise TimeoutError
            return row
        if args[:2] == ["tasks", "list"]:
            return self.tasks
        if args[:3] == ["tasks", "statuses", "list"]:
            return self.statuses
        if args[:2] == ["tasks", "update"]:
            return {"id": args[2]}
        if args[:3] == ["panes", "open", "file"]:
            return {"opened": True}
        raise AssertionError(args)


@pytest.fixture
def provider(project, tmp_path):
    if os.name != "posix":
        pytest.skip("Native Paradev context is qualified on POSIX hosts")
    root = tmp_path / "worktrees"
    root.mkdir()
    host = Host(project.project, root)
    provider = ParadevWorkspaceProvider(
        project_id="project",
        allowed_root=root,
        repository=project.project,
        state_dir=tmp_path / "reflection",
        host=host,
    )
    return provider, host


def test_preflight_has_no_mutations(provider):
    p, host = provider
    assert p.preflight()["ready"]
    assert not host.calls


def test_allocate_registered_isolated_workspace_before_agent(provider):
    p, host = provider
    binding = p.allocate("source/epic/unit", "campaign-unit", "unit", "action")
    assert binding.project_id == "project"
    assert Path(binding.path).is_relative_to(p.allowed_root)
    assert binding.git_dir != binding.common_dir
    assert all("--agent" not in args and "--command" not in args for args in host.calls)
    assert p.allocate("source/epic/unit", "campaign-unit", "unit", "action") == binding
    assert len(host.calls) == 1


def test_lost_create_response_reconciles_without_duplicate(provider):
    p, host = provider
    host.lose_reply = True
    with pytest.raises(ReflectionError, match="outcome unknown"):
        p.allocate("unit", "campaign-unit", "unit", "action")
    host.lose_reply = False
    binding = p.allocate("unit", "campaign-unit", "unit", "action")
    assert binding.workspace_id == host.workspaces[0]["id"]
    assert len(host.calls) == 1


def test_no_search_match_is_not_proof_create_failed(provider):
    p, host = provider
    host.no_create = True
    with pytest.raises(ReflectionError):
        p.allocate("unit", "campaign-unit", "unit", "action")
    for _ in range(2):
        with pytest.raises(ReflectionError, match="uncertain"):
            p.allocate("unit", "campaign-unit", "unit", "action")
    assert len(host.calls) == 1


def test_unapproved_root_and_foreign_repository_refuse_before_create(provider, tmp_path):
    p, host = provider
    host.projects[0]["worktreeBaseDir"] = str(tmp_path / "elsewhere")
    with pytest.raises(ReflectionError, match="allowed root"):
        p.allocate("unit", "campaign-unit", "unit", "action")
    assert not host.calls


def test_rewritten_branch_refuses_adoption(provider):
    p, host = provider
    host.rewrite_branch = True
    with pytest.raises(ReflectionError, match="returned branch"):
        p.allocate("unit", "campaign-unit", "unit", "action")
    assert len(host.calls) == 1


def test_reflection_retains_operator_tags_and_reports_precedence(provider):
    p, host = provider
    binding = p.allocate("unit", "campaign-unit", "unit", "action", group="story")
    host.workspaces[0]["tags"] += ",aaa-owner"
    data = p.reflect(
        binding, campaign_id="campaign", epic="epic", phase="running", attempt=1, held=True
    )
    assert data["held"]
    assert data["folder_selection"] == "operator-tag-precedence"
    assert "aaa-owner" in host.workspaces[0]["tags"]
    assert data["group"] == "story"


def test_optional_task_explicit_tracker_preserves_human_text(provider):
    p, host = provider
    binding = p.allocate("unit", "campaign-unit", "unit", "action")
    task = p.reflect_task(
        binding, campaign_id="campaign", title="story", state="running", links=("PR: test",)
    )
    host.tasks[0]["description"] += "\nOwner note"
    assert p.reflect_task(binding, campaign_id="campaign", title="story", state="delivered") == task
    assert len(host.tasks) == 1
    assert host.tasks[0]["description"].endswith("Owner note")
    assert all("superset" in args for args in host.calls if args[0] == "tasks")


def test_lost_task_create_reconciles_marker_without_duplicate(provider):
    p, host = provider
    binding = p.allocate("unit", "campaign-unit", "unit", "action")
    host.lose_reply = True
    with pytest.raises(ReflectionError, match="Task creation outcome unknown"):
        p.reflect_task(binding, campaign_id="campaign", title="story", state="running")
    host.lose_reply = False
    assert (
        p.reflect_task(binding, campaign_id="campaign", title="story", state="running") == "task-0"
    )
    assert len(host.tasks) == 1


def test_history_view_opens_file_without_terminal(provider, tmp_path):
    p, host = provider
    binding = p.allocate("unit", "campaign-unit", "unit", "action")
    log = tmp_path / "phase.log"
    log.write_text("completed output", encoding="utf-8")
    assert p.history_view(binding, log)["opened"]
    assert host.calls[-1][:3] == ["panes", "open", "file"]
    assert "--background" in host.calls[-1]


def test_allocation_action_cannot_be_reused_for_different_unit(provider):
    p, host = provider
    p.allocate("unit", "campaign-unit", "unit", "action")
    with pytest.raises(ReflectionError, match="identity reused"):
        p.allocate("another-unit", "campaign-unit", "unit", "action")
    assert len(host.calls) == 1


def test_native_registration_window_expires_without_duplicate_actor(provider):
    from types import SimpleNamespace

    p, host = provider
    binding = p.allocate("unit", "campaign-unit", "unit", "action")
    now = [100.0]
    p.clock = lambda: now[0]
    mux = SimpleNamespace(
        _address=lambda _: {"workspace": binding.workspace_id, "cwd": binding.path},
        set_display_title=lambda *_: None,
        _ensure_pane=lambda *_: "pane",
        native_identity=lambda _: None,
    )
    assert (
        p.bind_terminal(binding, mux, "terminal", phase="dev", attempt=1, agent="codex")[
            "attribution"
        ]
        == "pending"
    )
    now[0] += 31
    result = p.bind_terminal(binding, mux, "terminal", phase="dev", attempt=1, agent="codex")
    assert result["attribution"] == "unknown"
    assert "bounded window" in result["detail"]
    assert len(host.calls) == 1
