"""Native ownership is scoped to authenticated exact IDs, never inferred fences."""

from bmad_loop.campaign_native_inventory import NativeServiceObserver, _common


class Host:
    def __init__(self, repo):
        self.projects = [{"id": "project", "repoPath": str(repo)}]
        self.workspaces = [
            {
                "id": "workspace",
                "projectId": "project",
                "worktreePath": str(repo),
                "branch": "main",
            },
            {"id": "foreign-workspace", "projectId": "other", "worktreePath": "/foreign"},
        ]
        self.calls = []
        self.fail_daemon = False
        self.automations = [
            {
                "id": "automation",
                "v2ProjectId": "project",
                "v2WorkspaceId": "workspace",
                "enabled": False,
                "updatedAt": "revision",
                "archivedAt": None,
            },
            {
                "id": "foreign-automation",
                "v2ProjectId": "other",
                "enabled": True,
                "updatedAt": "other",
            },
        ]

    def rpc(self, name, payload=None):
        self.calls.append((name, payload))
        if name == "project.list":
            return self.projects
        if name == "workspace.list":
            return self.workspaces
        if name == "terminal.daemon.listSessions":
            if self.fail_daemon:
                raise TimeoutError
            return [
                {"id": "terminal", "pid": 123, "alive": True},
                {"id": "foreign-terminal", "pid": 456, "alive": True},
            ]
        if name == "terminal.list":
            assert payload["workspaceId"] == "workspace"
            return {"sessions": [{"terminalId": "terminal", "workspaceId": "workspace"}]}
        if name == "terminalAgents.get":
            return {
                "terminalId": "terminal",
                "agentSessionId": "native-session",
                "startedAt": "time",
            }
        raise AssertionError(name)

    def cli(self, args):
        self.calls.append(("cli", args))
        assert args in (["automations", "list"], ["automations", "list", "--archived"])
        return [] if "--archived" in args else self.automations


def test_exact_project_workspace_and_daemon_actor_inventory(project):
    host = Host(project.project)
    observed = NativeServiceObserver("project", host=host)(
        project.project, _common(project.project)
    )
    assert observed.complete and not observed.faults
    assert {item.id for item in observed.items} == {
        "paradev:workspace:workspace",
        "paradev:terminal:terminal",
        "paradev:automation:automation",
    }
    actor = next(item for item in observed.items if item.kind == "process")
    assert actor.liveness == "alive" and actor.details["pty_pid"] == 123
    assert actor.details["native"]["agentSessionId"] == "native-session"
    assert actor.details["coding_child_quiescence"] == "unknown"
    assert not any("foreign" in str(call[1]) for call in host.calls if call[0] == "terminal.list")


def test_disabled_automation_is_not_an_admissions_or_drain_receipt(project):
    observed = NativeServiceObserver("project", host=Host(project.project))(
        project.project, _common(project.project)
    )
    automation = next(item for item in observed.items if item.kind == "automation")
    assert automation.state == "disabled"
    assert automation.details["admissions_open"] is None
    assert automation.details["fence_revision"] is None
    assert automation.details["active_workers"] is None
    assert not NativeServiceObserver.capabilities()["admission_fence_readback"]


def test_native_observer_never_claims_empty_success_for_failed_daemon(project):
    host = Host(project.project)
    host.fail_daemon = True
    observed = NativeServiceObserver("project", host=host)(
        project.project, _common(project.project)
    )
    assert not observed.complete and observed.faults


def test_native_project_id_does_not_authorize_another_git_repository(project, tmp_path):
    host = Host(project.project)
    observed = NativeServiceObserver("project", host=host)(project.project, tmp_path / "other.git")
    assert not observed.complete and observed.items == ()
    assert [call[0] for call in host.calls] == ["project.list"]


def test_bounded_inventory_refuses_truncation(project):
    observed = NativeServiceObserver("project", host=Host(project.project), max_items=1)(
        project.project, _common(project.project)
    )
    assert not observed.complete and observed.faults


def test_concrete_manifest_route_withholds_disabled_automation_replacement(project):
    from types import SimpleNamespace

    from bmad_loop.campaign_migration import MigrationManifest, MigrationSelection
    from bmad_loop.campaign_model import CampaignSpec, ParadevSpec
    from bmad_loop.campaign_native_inventory import native_inventory, qualify_native_migration

    host = Host(project.project)
    spec = CampaignSpec("native", paradev=ParadevSpec(project_id="project"))
    observed = native_inventory(
        project.project,
        spec,
        gateway=SimpleNamespace(repository="owner/repo", pulls=lambda: []),
        host=host,
    )
    automation = next(item for item in observed.items if item.kind == "automation")
    manifest = MigrationManifest(
        1,
        observed.common_dir,
        observed.digest,
        "owner-explicit",
        (MigrationSelection(automation.id, automation.revision, "replace"),),
        disable_automations=(automation.id,),
    )
    replacement = {
        "schema_version": 1,
        "common_dir": observed.common_dir,
        "config_digest": "qualified-config",
        "capabilities": {key: {"ready": True} for key in ("planning", "execution", "publication")},
    }
    same, qualification = qualify_native_migration(
        project.project, spec, manifest, observed=observed, replacement=replacement, host=host
    )
    assert same is observed and not qualification.qualified
    assert any("admissions fence not proven" in problem for problem in qualification.problems)
    assert qualification.replaceable_automations == ()
