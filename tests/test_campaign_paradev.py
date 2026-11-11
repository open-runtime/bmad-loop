"""Project-correct workspace allocation and native projection under uncertain APIs."""

import copy
import os
import subprocess
from pathlib import Path

import pytest

from bmad_loop import verify
from bmad_loop.campaign_paradev import ParadevWorkspaceProvider, ReflectionError
from bmad_loop.campaign_titles import navigation_title


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
        self.folder_settings = []
        self.folder_writes = []
        self.lose_folder_reply = False
        self.statuses = [
            {"id": "todo", "type": "unstarted"},
            {"id": "doing", "type": "started"},
            {"id": "done", "type": "completed"},
        ]

    def rpc(self, procedure, payload=None, mutate=False):
        if procedure == "project.list":
            return copy.deepcopy(self.projects)
        if procedure == "workspace.list":
            return copy.deepcopy(self.workspaces)
        if procedure == "terminal.daemon.listSessions":
            return []
        if procedure == "tagFolders.list":
            assert not mutate
            return copy.deepcopy(self.folder_settings)
        if procedure == "tagFolders.upsert":
            assert mutate and set(payload) == {"scope", "tag", "displayName"}
            assert 1 <= len(payload["displayName"].encode("utf-16-le")) // 2 <= 200
            self.folder_writes.append(copy.deepcopy(payload))
            row = next(
                (
                    row
                    for row in self.folder_settings
                    if (row["scope"], row["tag"]) == (payload["scope"], payload["tag"])
                ),
                None,
            )
            if row is None:
                row = {"color": None, "tabOrder": None}
                self.folder_settings.append(row)
            row.update(payload)
            if self.lose_folder_reply:
                raise TimeoutError
            return {"tagSettings": copy.deepcopy(self.folder_settings)}
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
                        "name": args[args.index("--name") + 1],
                    }
                )
            if self.lose_reply or self.no_create:
                raise TimeoutError
            return self.workspaces[-1]
        if args[:2] == ["workspaces", "update"]:
            row = next(r for r in self.workspaces if r["id"] == args[2])
            tags = [args[i + 1] for i, arg in enumerate(args) if arg == "--tag"]
            if any(not tag for tag in tags):
                raise ValueError("Empty --tag is not --clear-tags")
            tags = [tag.strip().lower() for tag in tags]
            if any(not tag or len(tag.encode("utf-16-le")) // 2 > 64 for tag in tags):
                raise ValueError("Native tag must be 1-64 UTF-16 units")
            if len(set(tags)) > 32:
                raise ValueError("Native workspace permits at most 32 tags")
            if "--clear-tags" in args:
                assert not tags
                row["tags"] = ""
            if tags:
                row["tags"] = ",".join(tags)
            if "--task-id" in args:
                row["taskId"] = args[args.index("--task-id") + 1]
            if "--name" in args:
                row["name"] = args[args.index("--name") + 1]
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
            row = next(item for item in self.tasks if item["id"] == args[2])
            row["statusId"] = args[args.index("--status-id") + 1]
            if "--title" in args:
                row["title"] = args[args.index("--title") + 1]
            if "--pr-url" in args:
                row["prUrl"] = args[args.index("--pr-url") + 1]
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


def test_explicit_registered_workspace_link_preserves_native_layout(provider):
    p, host = provider
    allocated = p.allocate("unit", "campaign-unit", "unit", "allocate")
    host.workspaces[0]["name"] = "owner name"
    host.workspaces[0]["tags"] = "aaa-owner,factory-desk-side-tasks"
    host.calls.clear()
    adopted = p.adopt(
        "new-unit", allocated.branch, allocated.workspace_id, allocated.path, "adopt", group="story"
    )
    assert adopted.workspace_id == allocated.workspace_id and adopted.git_dir == allocated.git_dir
    assert adopted.unit_id == "new-unit" and adopted.group == "story"
    assert (
        p.adopt(
            "new-unit",
            allocated.branch,
            allocated.workspace_id,
            allocated.path,
            "adopt",
            group="story",
        )
        == adopted
    )
    assert p.reconcile("adopt") == adopted
    assert not host.calls
    assert host.workspaces[0]["tags"] == "aaa-owner,factory-desk-side-tasks"


def test_registered_adoption_refuses_foreign_uuid_path_or_reused_action(provider):
    p, host = provider
    allocated = p.allocate("unit", "campaign-unit", "unit", "allocate")
    host.calls.clear()
    with pytest.raises(ReflectionError, match="registration differs"):
        p.adopt("unit", allocated.branch, "foreign", allocated.path, "adopt")
    with pytest.raises(ReflectionError, match="registration differs"):
        p.adopt("unit", allocated.branch, allocated.workspace_id, p.allowed_root / "other", "adopt")
    p.adopt("unit", allocated.branch, allocated.workspace_id, allocated.path, "adopt")
    with pytest.raises(ReflectionError, match="action/Git identity changed"):
        p.adopt("other-unit", allocated.branch, allocated.workspace_id, allocated.path, "adopt")
    assert not host.calls


def test_allocate_registered_isolated_workspace_before_agent(provider):
    p, host = provider
    binding = p.allocate("source/epic/unit", "campaign-unit", "unit", "action")
    assert binding.project_id == "project"
    assert Path(binding.path).is_relative_to(p.allowed_root)
    assert binding.git_dir != binding.common_dir
    assert all("--agent" not in args and "--command" not in args for args in host.calls)
    assert p.allocate("source/epic/unit", "campaign-unit", "unit", "action") == binding
    assert sum(call[:2] == ["workspaces", "create"] for call in host.calls) == 1


def test_lost_create_response_reconciles_without_duplicate(provider):
    p, host = provider
    host.lose_reply = True
    with pytest.raises(ReflectionError, match="outcome unknown"):
        p.allocate("unit", "campaign-unit", "unit", "action")
    host.lose_reply = False
    binding = p.allocate("unit", "campaign-unit", "unit", "action")
    assert binding.workspace_id == host.workspaces[0]["id"]
    assert sum(call[:2] == ["workspaces", "create"] for call in host.calls) == 1


def test_no_search_match_is_not_proof_create_failed(provider):
    p, host = provider
    host.no_create = True
    with pytest.raises(ReflectionError):
        p.allocate("unit", "campaign-unit", "unit", "action")
    for _ in range(2):
        with pytest.raises(ReflectionError, match="uncertain"):
            p.allocate("unit", "campaign-unit", "unit", "action")
    assert sum(call[:2] == ["workspaces", "create"] for call in host.calls) == 1


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
    assert sum(call[:2] == ["workspaces", "create"] for call in host.calls) == 1


def test_reflection_retains_operator_tags_and_reports_precedence(provider):
    p, host = provider
    binding = p.allocate("unit", "campaign-unit", "unit", "action", group="story")
    host.workspaces[0]["tags"] += ",aaa-owner"
    data = p.reflect(
        binding, campaign_id="campaign", epic="epic", phase="running", attempt=1, held=True
    )
    assert data["held"]
    assert data["folder_selection"] == "operator-tags-present"
    assert "aaa-owner" in host.workspaces[0]["tags"]
    assert data["group"] == "story"


def test_native_tag_normalization_keeps_mixed_case_group_identity(provider):
    p, host = provider
    binding = p.allocate("unit", "campaign-unit", "unit", "action", group="Epic CART")
    original = host.cli

    def normalized(args):
        result = original(args)
        if args[:2] == ["workspaces", "update"]:
            host.workspaces[0]["tags"] = host.workspaces[0]["tags"].lower()
        return result

    host.cli = normalized
    data = p.reflect(binding, campaign_id="Campaign", epic="Epic CART", phase="running", attempt=1)
    assert data["group"] == "Epic CART"
    assert data["folder"].startswith("bmad-")
    assert data["group_display"]["observed"] == "Epic CART"
    assert data["folder_selection"] == "confirmed"
    before = len(host.calls)
    assert (
        p.reflect(binding, campaign_id="Campaign", epic="Epic CART", phase="review", attempt=1)[
            "folder"
        ]
        == data["folder"]
    )
    assert len(host.calls) == before


def test_navigation_titles_refresh_without_changing_allocation_or_operator_tags(provider):
    p, host = provider
    first = "hcom:pending · E:epic · I:pending · PR:pending · Story"
    binding = p.allocate(
        "unit", "campaign-unit", "Story", "action", group="epic-stable", display_name=first
    )
    assert host.workspaces[0]["name"] == first
    host.workspaces[0]["tags"] += ",factory-desk-side-tasks"
    initial = p.reflect(
        binding,
        campaign_id="Campaign",
        epic="epic",
        phase="running",
        attempt=1,
        title=first,
        group_title=first,
        group_fields={"epics": ("epic",)},
    )
    title = "@tina · E:epic · I:#42 · PR:#18 · Story"
    updated = p.reflect(
        binding,
        campaign_id="Campaign",
        epic="epic",
        phase="reviewing",
        attempt=1,
        title=title,
        group_title=title,
        group_fields={"names": ("tina",), "epics": ("epic",), "issues": ("#42",), "prs": ("#18",)},
    )
    assert host.workspaces[0]["name"] == title
    assert updated["folder"] == initial["folder"]
    assert updated["group_display"]["observed"] == title
    assert initial["folder"] in host.workspaces[0]["tags"]
    assert "factory-desk-side-tasks" in host.workspaces[0]["tags"]
    assert (
        p.allocate(
            "unit", "campaign-unit", "Story", "action", group="epic-stable", display_name=title
        )
        == binding
    )
    assert p.reconcile("action") == binding
    assert sum(call[:2] == ["workspaces", "create"] for call in host.calls) == 1


@pytest.mark.parametrize("epic", ["intake", "epic-m3-generated-outputs-and-contracts", "İ🚀" * 30])
def test_native_tag_budget_preserves_navigation_and_full_titles(provider, epic):
    p, host = provider
    fields = {"epics": (epic,), "issues": ("#529",), "prs": ("#2286",)}
    full = navigation_title(**fields, description="Full story description")
    binding = p.allocate("unit", "campaign-unit", full, "action", group=epic)
    host.workspaces[0]["tags"] = "owner-folder"
    data = p.reflect(
        binding,
        campaign_id="campaign-20261109-034256-b84ae801",
        epic=epic,
        phase="draft",
        attempt=0,
        title=full,
        group_title=navigation_title(**fields),
        group_fields=fields,
    )
    assert len(data["folder"].encode("utf-16-le")) // 2 <= 64
    assert data["folder"].startswith("bmad-")
    assert data["group_display"]["observed"] == navigation_title(**fields)
    assert data["group_title"] == navigation_title(**fields)
    assert host.workspaces[0]["name"] == full
    assert set(host.workspaces[0]["tags"].split(",")) == {"owner-folder", data["folder"]}
    calls = len(host.calls)
    assert (
        p.reflect(
            binding,
            campaign_id="campaign-20261109-034256-b84ae801",
            epic=epic,
            phase="draft",
            attempt=0,
            title=full,
            group_title=navigation_title(**fields),
            group_fields=fields,
        )["folder"]
        == data["folder"]
    )
    assert len(host.calls) == calls


def test_compact_projection_lost_ack_reconciles_same_workspace_without_second_write(provider):
    p, host = provider
    binding = p.allocate("unit", "campaign-unit", "Story", "action", group="epic")
    host.workspaces[0]["tags"] = "owner-folder"
    original = host.cli

    def lose_update(args):
        result = original(args)
        if args[:2] == ["workspaces", "update"]:
            raise TimeoutError("lost acknowledgement")
        return result

    host.cli = lose_update
    kwargs = dict(
        campaign_id="campaign",
        epic="epic",
        phase="draft",
        attempt=0,
        group_fields={"epics": ("epic",)},
    )
    assert p.reflect(binding, **kwargs)["state"] == "confirmed"
    written = host.workspaces[0]["tags"]
    calls = len(host.calls)
    host.cli = original
    data = p.reflect(binding, **kwargs)
    assert len(host.calls) == calls
    assert host.workspaces[0]["tags"] == written
    assert data["workspace_id"] == binding.workspace_id
    assert len(host.workspaces) == 1


def test_native_tag_slot_exhaustion_retains_all_operator_tags(provider):
    p, host = provider
    binding = p.allocate("unit", "campaign-unit", "Story", "action")
    tags = ",".join("owner-" + str(i) for i in range(32))
    host.workspaces[0]["tags"] = tags
    calls = len(host.calls)
    with pytest.raises(ReflectionError, match="no free native tag slot"):
        p.reflect(binding, campaign_id="campaign", epic="epic", phase="draft", attempt=0)
    assert host.workspaces[0]["tags"] == tags
    assert len(host.calls) == calls


def test_task_navigation_refresh_retains_description_and_exact_task(provider):
    p, host = provider
    binding = p.allocate("unit", "campaign-unit", "Story", "action")
    task = p.reflect_task(
        binding,
        campaign_id="campaign",
        title="hcom:pending · E:epic · I:pending · PR:pending",
        state="running",
    )
    host.tasks[0]["description"] += "\nOwner's steering note"
    title = "@nava · E:epic · I:#42 · PR:#18"
    assert (
        p.reflect_task(
            binding,
            campaign_id="campaign",
            title=title,
            state="running",
            links=("https://github.com/owner/repo/pull/18",),
        )
        == task
    )
    assert host.tasks[0]["title"] == title
    assert host.tasks[0]["prUrl"] == "https://github.com/owner/repo/pull/18"
    assert host.tasks[0]["description"].endswith("Owner's steering note")
    assert len(host.tasks) == 1


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
    target = Path(host.calls[-1][3])
    assert target.is_relative_to(Path(binding.path)) and target.read_text() == "completed output"
    assert verify.worktree_clean(Path(binding.path))
    target.write_text("Owner annotated history", encoding="utf-8")
    with pytest.raises(ReflectionError, match="Owner edited"):
        p.history_view(binding, log)
    assert target.read_text() == "Owner annotated history"


def test_allocation_action_cannot_be_reused_for_different_unit(provider):
    p, host = provider
    p.allocate("unit", "campaign-unit", "unit", "action")
    with pytest.raises(ReflectionError, match="identity reused"):
        p.allocate("another-unit", "campaign-unit", "unit", "action")
    assert sum(call[:2] == ["workspaces", "create"] for call in host.calls) == 1


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
    assert sum(call[:2] == ["workspaces", "create"] for call in host.calls) == 1


def test_confirmed_allocation_drops_internal_folder_and_reconciles_pinned_uuid(provider):
    p, host = provider
    binding = p.allocate("unit", "campaign-unit", "Story", "action")
    assert host.workspaces[0]["tags"] == ""
    assert host.calls[-1] == [
        "workspaces",
        "update",
        binding.workspace_id,
        "--local",
        "--clear-tags",
    ]
    host.workspaces[0].update(name="Owner rename", tags="factory-desk-side-tasks,owner-folder")
    assert p.reconcile("action") == binding
    assert host.workspaces[0]["name"] == "Owner rename"
    assert host.workspaces[0]["tags"] == "factory-desk-side-tasks,owner-folder"


def test_lost_clear_ack_is_confirmed_by_readback_without_duplicate(provider, monkeypatch):
    """A cleared marker loses search identity, so retries must use the saved UUID."""
    from bmad_loop.adapters.paradev_runtime import read_private_json

    p, host = provider
    original = host.cli

    def lose_clear_ack(args):
        result = original(args)
        if args[:2] == ["workspaces", "update"]:
            raise TimeoutError("clear applied, response lost")
        return result

    monkeypatch.setattr(host, "cli", lose_clear_ack)
    binding = p.allocate("unit", "campaign-unit", "Story", "action")
    assert host.workspaces[0]["tags"] == ""
    assert read_private_json(p._path("action"))["binding"]["workspace_id"] == binding.workspace_id
    calls = copy.deepcopy(host.calls)
    assert p.allocate("unit", "campaign-unit", "Story", "action") == binding
    assert host.calls == calls
    assert sum(call[:2] == ["workspaces", "create"] for call in host.calls) == 1


@pytest.mark.parametrize("lost_reply", [False, True])
def test_unconfirmed_clear_recovers_pinned_workspace_without_duplicate(
    provider, monkeypatch, lost_reply
):
    """Neither success prose nor timeout proves a clear; reobserve the same workspace."""
    from bmad_loop.adapters.paradev_runtime import read_private_json

    p, host = provider
    original = host.cli

    def no_clear(args):
        if args[:2] == ["workspaces", "update"]:
            host.calls.append(args)
            if lost_reply:
                raise TimeoutError("clear did not apply")
            return {"id": args[2]}
        return original(args)

    monkeypatch.setattr(host, "cli", no_clear)
    with pytest.raises(ReflectionError, match="Temporary create marker removal unconfirmed"):
        p.allocate("unit", "campaign-unit", "Story", "action")
    intent = read_private_json(p._path("action"))
    assert intent["state"] == "confirmed"
    assert intent["tag"] in host.workspaces[0]["tags"]
    monkeypatch.setattr(host, "cli", original)
    binding = p.allocate("unit", "campaign-unit", "Story", "action")
    assert binding.workspace_id == intent["binding"]["workspace_id"]
    assert host.workspaces[0]["tags"] == ""
    assert sum(call[:2] == ["workspaces", "create"] for call in host.calls) == 1


@pytest.mark.parametrize(
    ("code", "detail"),
    [
        ("CLI_FAILED", "HostError (CLI_FAILED)"),
        ("CLI_FAILED credential=private", "HostError"),
        ("CLI_" + "X" * 100, "HostError"),
        (None, "HostError"),
    ],
)
def test_marker_failure_retains_only_bounded_public_host_code(provider, monkeypatch, code, detail):
    from bmad_loop.adapters.paradev_host import HostError

    p, host = provider
    original = host.cli

    def refuse_clear(args):
        if args[:2] == ["workspaces", "update"]:
            raise HostError(code)
        return original(args)

    monkeypatch.setattr(host, "cli", refuse_clear)
    with pytest.raises(ReflectionError) as caught:
        p.allocate("unit", "campaign-unit", "Story", "action")
    assert str(caught.value) == "Temporary create marker removal unconfirmed: " + detail


def test_lost_ack_keeps_marker_until_exact_identity_is_saved(provider):
    p, host = provider
    host.lose_reply = True
    with pytest.raises(ReflectionError):
        p.allocate("unit", "campaign-unit", "Story", "action")
    assert "zz-bmad-action-" in host.workspaces[0]["tags"]
    host.workspaces[0]["tags"] += ",factory-desk-side-tasks,owner-folder"
    host.lose_reply = False
    binding = p.reconcile("action")
    assert binding.workspace_id == host.workspaces[0]["id"]
    assert host.workspaces[0]["tags"] == "factory-desk-side-tasks,owner-folder"
    assert host.calls[-1] == [
        "workspaces",
        "update",
        binding.workspace_id,
        "--local",
        "--tag",
        "factory-desk-side-tasks",
        "--tag",
        "owner-folder",
    ]


def test_pinned_uuid_cannot_be_replaced_by_foreign_row_with_old_marker(provider):
    from bmad_loop.adapters.paradev_runtime import read_private_json

    p, host = provider
    binding = p.allocate("unit", "campaign-unit", "Story", "action")
    host.workspaces[0]["id"] = "foreign-replacement"
    host.workspaces[0]["tags"] = read_private_json(p._path("action"))["tag"]
    with pytest.raises(ReflectionError, match="uncertain"):
        p.reconcile("action")
    assert binding.workspace_id != host.workspaces[0]["id"]


def test_task_projection_confirms_status_and_prefers_named_hold(provider):
    p, host = provider
    host.statuses.append({"id": "held", "name": "Blocked", "type": "started"})
    binding = p.allocate("held-unit", "campaign-held", "held", "allocate-held")
    task_id = p.reflect_task(binding, campaign_id="campaign", title="Held work", state="held")
    assert next(row for row in host.tasks if row["id"] == task_id)["statusId"] == "held"
    assert host.workspaces[0]["taskId"] == task_id


def test_task_success_ack_without_changed_remote_status_is_not_confirmed(provider, monkeypatch):
    p, host = provider
    binding = p.allocate("unit", "campaign-unit", "unit", "allocate")
    original = host.cli

    def no_update(args):
        if args[:2] == ["tasks", "update"]:
            return {"id": args[2]}
        return original(args)

    monkeypatch.setattr(host, "cli", no_update)
    with pytest.raises(ReflectionError, match="status update remains pending"):
        p.reflect_task(binding, campaign_id="campaign", title="Work", state="running")
    monkeypatch.setattr(host, "cli", original)
    assert (
        p.reflect_task(binding, campaign_id="campaign", title="Work", state="running")
        == host.tasks[0]["id"]
    )
    assert len(host.tasks) == 1
