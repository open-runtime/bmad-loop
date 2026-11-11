"""A permanent archived release enters the ordinary adoption/preparation path."""

import copy
import dataclasses
from pathlib import Path

import pytest
import test_campaign_transfer as core
from conftest import git

from bmad_loop import campaign_adoption as adoption
from bmad_loop import campaign_native_inventory as native
from bmad_loop.campaign_model import CampaignError
from bmad_loop.campaign_paradev import ReflectionError
from bmad_loop.campaign_runtime import CampaignRuntime
from bmad_loop.campaign_store import add_action

adoption_case = core.adoption_case
case = core.case


@pytest.fixture
def transferred(case, monkeypatch):
    core.invoke(case)
    original_cli = case.host.cli

    def cli(args):
        if args[:2] == ["workspaces", "create"]:
            case.host.calls.append(args)
            if not case.host.no_create:
                branch = args[args.index("--branch") + 1]
                checkout = case.root / "recipient"
                git(case.repo, "worktree", "add", str(checkout), branch)
                case.host.workspaces.append(
                    {
                        "id": "recipient-workspace",
                        "projectId": "project",
                        "worktreePath": str(checkout),
                        "branch": branch,
                        "tags": args[args.index("--tag") + 1],
                        "name": args[args.index("--name") + 1],
                    }
                )
            if case.host.no_create or case.host.lose_reply:
                raise TimeoutError("lost native create reply")
            return case.host.workspaces[-1]
        return original_cli(args)

    monkeypatch.setattr(case.host, "cli", cli)
    monkeypatch.setattr(native, "Host", lambda: case.host)
    case.resolver = lambda name, **kwargs: adoption.load_provider(name, store=case.store, **kwargs)
    case.snapshot = case.store.load("successor")
    case.pr["draft"] = True
    proposed = adoption.propose(
        case.repo,
        case.snapshot,
        case.unit.id,
        7,
        "campaign",
        gateway=case.gateway,
        resolver=case.resolver,
    )
    assert proposed["ready_for_owner_authorization"], proposed["problems"]
    manifest = adoption.manifest_from_dict({**proposed["manifest"], "authorized_by": "owner-test"})
    identity, _ = case.resolver("campaign", snapshot=case.snapshot)
    qualified = adoption.qualify_pr(
        case.repo, case.snapshot, manifest, identity, case.gateway, resolver=case.resolver
    )
    adoption.begin(
        case.snapshot,
        case.unit.id,
        {
            "manifest": dataclasses.asdict(manifest),
            "provider": dataclasses.asdict(identity),
            "qualification": dataclasses.asdict(qualified),
        },
        command_id="owner-adopt",
    )
    case.runtime = CampaignRuntime(
        case.repo, case.store, gateway=case.gateway, adoption_resolver=case.resolver
    )
    # Drive the existing draft acknowledgement, then normal prepare-unit producer.
    action = add_action(case.snapshot, "adopt-draft", case.unit.id, {})
    result = case.runtime.perform(case.snapshot, action)
    case.runtime.apply(case.snapshot, action, result)
    action.state, action.receipt = "confirmed", result.data
    assert case.snapshot.units[case.unit.id].bindings["adoption"]["state"] == "draft_confirmed"
    case.action = add_action(case.snapshot, "prepare-unit", case.unit.id, {"adoption": True})
    case.snapshot = case.store.save(case.snapshot, case.snapshot.revision)
    case.action = next(row for row in case.snapshot.outbox if row.id == case.action.id)
    return case


def test_release_prepares_new_native_uuid_on_preserved_branch_and_keeps_fence(transferred):
    from bmad_loop.campaign_reflection_flow import new_group_identity

    case = transferred
    before = copy.deepcopy(case.host.workspaces[0])
    original = case.store.load(case.before.id)
    result = case.runtime._perform_prepare_unit(case.snapshot, case.action)
    assert result.state == "confirmed", result.reason
    case.runtime.apply(case.snapshot, case.action, result)
    case.action.state, case.action.receipt = "confirmed", result.data
    case.snapshot = case.store.save(case.snapshot, case.snapshot.revision)
    unit = case.snapshot.units[case.unit.id]
    assert unit.bindings["native"]["workspace_id"] == "recipient-workspace"
    assert unit.bindings["native"]["workspace_id"] != before["id"]
    assert unit.bindings["native"]["group"] == new_group_identity(case.snapshot, unit)
    assert unit.bindings["pr"]["number"] == 7
    assert unit.bindings["branch"] == before["branch"]
    assert (
        git(
            Path(unit.bindings["workspace"]),
            "merge-base",
            "--is-ancestor",
            before["headSha"],
            "HEAD",
        )
        == ""
    )
    assert case.host.workspaces[0] == before
    assert case.store.load(original.id) == original
    case.pr["head"]["sha"] = unit.bindings["head"]
    adoption.guard_owned(case.runtime, case.snapshot, case.unit.id)
    assert sum(args[:2] == ["workspaces", "create"] for args in case.host.calls) == 2


@pytest.mark.parametrize("created", [False, True])
def test_uncertain_preserved_allocation_never_replays_create(transferred, created):
    case = transferred
    case.host.lose_reply = True
    case.host.no_create = not created
    with pytest.raises(ReflectionError, match="outcome unknown"):
        case.runtime._perform_prepare_unit(case.snapshot, case.action)
    creates = sum(args[:2] == ["workspaces", "create"] for args in case.host.calls)
    case.host.lose_reply = False
    case.host.no_create = False
    if created:
        result = case.runtime._perform_prepare_unit(case.snapshot, case.action)
        assert result.state == "confirmed"
    else:
        with pytest.raises(ReflectionError, match="uncertain"):
            case.runtime._perform_prepare_unit(case.snapshot, case.action)
    assert sum(args[:2] == ["workspaces", "create"] for args in case.host.calls) == creates


@pytest.mark.parametrize("fault", ["live", "unknown", "automation", "archive", "foreign-project"])
def test_fresh_selected_native_authority_cannot_be_replaced_by_archive_label(case, fault):
    from bmad_loop.campaign_migration import MigrationItem

    record = core.invoke(case, preview=True)["record"]
    observed = case.observer(case.repo, Path(case.snapshot.common_dir))
    if fault in {"live", "unknown"}:
        extra = MigrationItem(
            "actor",
            "process",
            "rev",
            "native",
            liveness="alive" if fault == "live" else "unknown",
            details={"workspace_id": case.host.workspaces[0]["id"]},
        )
        observed = dataclasses.replace(observed, items=(*observed.items, extra))
    elif fault == "automation":
        extra = MigrationItem(
            "automation",
            "automation",
            "rev",
            "native",
            details={"v2WorkspaceId": case.host.workspaces[0]["id"], "enabled": False},
        )
        observed = dataclasses.replace(observed, items=(*observed.items, extra))
    else:
        row = observed.items[0]
        changed = {
            **row.details,
            ("archivedAt" if fault == "archive" else "projectId"): (
                None if fault == "archive" else "foreign"
            ),
        }
        observed = dataclasses.replace(
            observed, items=(dataclasses.replace(row, details=changed), *observed.items[1:])
        )
    with pytest.raises(CampaignError):
        native.validate_transfer_observation(
            record,
            case.before,
            case.successor,
            project=case.repo,
            native_inventory=observed,
            store=case.store,
        )


@pytest.mark.parametrize("overlap", [True, False])
def test_paused_other_native_worktree_is_scoped_without_global_drain(case, overlap):
    other = case.root / "unrelated"
    git(case.repo, "worktree", "add", "-b", "other-owner", str(other), "main")
    (other / ("src.txt" if overlap else "unrelated.txt")).write_text("paused unsaved work\n")
    case.host.workspaces.append(
        {"id": "other", "projectId": "project", "worktreePath": str(other), "branch": "other-owner"}
    )
    if overlap:
        with pytest.raises(CampaignError, match="overlapping"):
            core.invoke(case)
    else:
        assert core.invoke(case)["record"]["successor"] == "successor"


def test_selected_archived_terminal_is_actually_queried_and_unbound_daemon_refuses(
    case, monkeypatch
):
    original = case.host.rpc
    calls = []

    def rpc(procedure, payload=None, mutate=False):
        calls.append((procedure, payload))
        if procedure == "terminal.daemon.listSessions":
            return [{"id": "orphan", "alive": True, "workspaceId": case.host.workspaces[0]["id"]}]
        return original(procedure, payload, mutate=mutate)

    monkeypatch.setattr(case.host, "rpc", rpc)
    with pytest.raises(CampaignError):
        core.invoke(case)
    assert ("terminal.list", {"workspaceId": case.host.workspaces[0]["id"]}) in calls


def test_pending_allocation_cannot_change_release_or_restore_old_uuid(transferred):
    from bmad_loop.adapters.paradev_runtime import private_json, read_private_json
    from bmad_loop.campaign_paradev import ParadevWorkspaceProvider

    case = transferred
    case.host.lose_reply = True
    with pytest.raises(ReflectionError):
        case.runtime._perform_prepare_unit(case.snapshot, case.action)
    provider = ParadevWorkspaceProvider(
        project_id="project",
        allowed_root=case.root,
        repository=case.repo,
        state_dir=case.store.directory("successor") / "reflection",
        host=case.host,
    )
    path = provider._path(case.action.id)
    intent = read_private_json(path)
    private_json(path, {**intent, "release_id": "a" * 64})
    creates = len(case.host.calls)
    with pytest.raises(CampaignError, match="journal.*release"):
        case.runtime._perform_prepare_unit(case.snapshot, case.action)
    assert len(case.host.calls) >= creates  # Readback is allowed; no second create.
    assert sum(args[:2] == ["workspaces", "create"] for args in case.host.calls) == 2
    private_json(path, intent)
    case.host.workspaces[-1]["id"] = intent["archived_workspace_id"]
    case.host.workspaces = [case.host.workspaces[-1]]
    with pytest.raises(ReflectionError, match="restored archived"):
        provider.reconcile(case.action.id)


@pytest.mark.parametrize(
    "fault",
    [
        "",
        "foreign-terminal",
        "generation",
        "request-digest",
        "coordinates",
        "quiescent",
        "replaced-pty",
        "missing-bootstrap",
        "ended-bootstrap",
        "registry-session",
        "transport-root",
    ],
)
def test_only_exact_current_claim_can_explain_recipient_native_actor(transferred, fault):
    import json

    from test_paradev_transport import HostFixture

    from bmad_loop import envvars
    from bmad_loop.adapters.paradev_backend import ParadevMultiplexer
    from bmad_loop.adapters.paradev_runtime import (
        private_json,
        read_private_json,
        worker_environment,
    )
    from bmad_loop.campaign_headless import digest
    from bmad_loop.campaign_migration import MigrationItem
    from bmad_loop.campaign_model import WorkerClaim

    case = transferred
    result = case.runtime._perform_prepare_unit(case.snapshot, case.action)
    case.runtime.apply(case.snapshot, case.action, result)
    case.action.state, case.action.receipt = "confirmed", result.data
    case.snapshot = case.store.save(case.snapshot, case.snapshot.revision)
    unit = case.snapshot.units[case.unit.id]
    root = case.store.directory("successor") / "workers" / "current-launch"
    root.mkdir(parents=True)
    coordinates = {"head": unit.bindings["head"], "config_revision": 1, "graph_revision": 1}
    transport = case.store.directory("successor") / "native-transport"
    outer = {
        "campaign_id": "successor",
        "unit_id": unit.id,
        "launch_id": "current-launch",
        "generation": 1,
        "headless_coordinates": coordinates,
        "reflection_environment": {
            envvars.PARADEV_STATE_DIR: str(transport if fault != "transport-root" else root)
        },
    }
    (root / "request.json").write_text(json.dumps(outer))
    inner = root / "native" / "run" / "headless" / "job"
    inner.mkdir(parents=True)
    request = {"coordinates": coordinates}
    envelope = {
        "launch_id": "native-launch",
        "generation": 1,
        "request": request,
        "request_digest": digest(request),
        "presentation": {"workspace_id": "recipient-workspace"},
        "session": "owned-session",
        "window_name": "owned-window",
    }
    if fault == "generation":
        envelope["generation"] = 2
    elif fault == "request-digest":
        envelope["request_digest"] = "foreign"
    elif fault == "coordinates":
        envelope["request"] = {"coordinates": {**coordinates, "head": "f" * 40}}
        envelope["request_digest"] = digest(envelope["request"])
    (inner / "request.json").write_text(json.dumps(envelope))
    unit.claim = WorkerClaim(
        unit.id,
        1,
        1,
        "current-launch",
        run_dir=str(root),
        workspace=unit.bindings["workspace"],
        branch=unit.bindings["branch"],
        quiescent=fault == "quiescent",
    )
    # Exercise the maintained registry/bootstrap producers against a fake native host.
    # No real terminal or provider is launched.
    checkout = Path(unit.bindings["workspace"])
    host = HostFixture(checkout)
    host.workspaces[0]["id"] = "recipient-workspace"
    mux = ParadevMultiplexer(host=host, state_dir=transport)
    mux.new_session("owned-session", checkout)
    terminal = mux.new_window("owned-session", "owned-window", checkout, {}, "true")
    launch_dir = transport / "launches" / terminal
    worker_environment(
        launch_dir,
        {
            "SUPERSET_TERMINAL_ID": terminal,
            "SUPERSET_WORKSPACE_ID": "recipient-workspace",
            "SUPERSET_WORKSPACE_PATH": str(checkout),
        },
        pid=123,
    )
    (inner / "window.json").write_text(
        json.dumps({"launch_id": "native-launch", "native_id": terminal})
    )
    if fault == "registry-session":
        registry_path = transport / "windows" / (terminal + ".json")
        registry = read_private_json(registry_path)
        private_json(registry_path, {**registry, "session": "foreign"})
    if fault == "missing-bootstrap":
        (launch_dir / "context.json").unlink()
    if fault == "ended-bootstrap":
        private_json(launch_dir / "exit.json", {"exit_code": 0})
    record = core.transfer.read_release(case.store, case.before.id, unit.id)
    observed = native.NativeServiceObserver(
        "project", host=case.host, archived_workspaces=(case.host.workspaces[0]["id"],)
    )(case.repo, Path(case.snapshot.common_dir))
    actor = MigrationItem(
        "current-native",
        "process",
        "rev",
        "native",
        liveness="alive",
        details={
            "workspace_id": "recipient-workspace",
            "terminal_id": "foreign" if fault == "foreign-terminal" else terminal,
            "pty_pid": 456 if fault == "replaced-pty" else 123,
        },
    )
    observed = dataclasses.replace(observed, items=(*observed.items, actor))

    def check():
        return native.validate_transfer_observation(
            record,
            case.store.load(case.before.id),
            case.snapshot,
            project=case.repo,
            native_inventory=observed,
            store=case.store,
        )

    if fault:
        with pytest.raises(CampaignError):
            check()
    else:
        assert "current-native" in check()["scoped_native_items"]


def test_unmapped_predecessor_allocation_live_terminal_prevents_whole_retirement(case, monkeypatch):
    from bmad_loop.campaign_model import CampaignUnit, UnitState

    previous = case.store.load(case.before.id)
    unit = CampaignUnit(
        dataclasses.replace(case.unit, id="unmapped-control"), state=UnitState.PLANNING
    )
    previous.units[unit.id] = unit
    previous.graph_revision += 1
    action = add_action(previous, "prepare-unit", unit.id, {})
    previous = case.store.save(previous, previous.revision)
    action = next(item for item in previous.outbox if item.id == action.id)
    runtime = CampaignRuntime(case.repo, case.store, gateway=case.gateway)
    result = runtime._perform_prepare_unit(previous, action)
    runtime.apply(previous, action, result)
    action.state, action.receipt = "confirmed", result.data
    previous = case.store.save(previous, previous.revision)
    unit = previous.units[unit.id]
    git(case.repo, "worktree", "remove", unit.bindings["workspace"])
    row = case.host.workspaces[-1]
    row.update(
        archivedAt="deleted-unmapped",
        archiveReason="deleted",
        worktreeExists=False,
        headSha=unit.bindings["head"],
    )
    original = case.host.rpc

    def rpc(procedure, payload=None, mutate=False):
        if procedure == "terminal.daemon.listSessions":
            return [{"id": "unmapped-live", "alive": True, "workspaceId": row["id"]}]
        if procedure == "terminal.list" and payload == {"workspaceId": row["id"]}:
            return {"sessions": [{"terminalId": "unmapped-live", "workspaceId": row["id"]}]}
        if procedure == "terminalAgents.get":
            return None
        return original(procedure, payload, mutate=mutate)

    monkeypatch.setattr(case.host, "rpc", rpc)
    case.observer = native.NativeServiceObserver(
        "project",
        host=case.host,
        archived_workspaces=core.transfer.archived_workspace_ids(previous),
        clock=lambda: 100,
    )
    case.request = dataclasses.replace(case.request, expected_revision=previous.revision)
    with pytest.raises(CampaignError, match="live or uncertain"):
        core.invoke(case)
    assert not core.transfer.retired(case.store.load(previous.id))


def test_workspace_overlap_unions_every_best_merge_base(tmp_path, monkeypatch):
    from types import SimpleNamespace

    first, second = "a" * 40, "b" * 40
    calls = []

    def git_bytes(path, *args, **kwargs):
        calls.append(args)
        if args[:2] == ("merge-base", "--all"):
            output = (first + "\n" + second + "\n").encode()
        elif second in args:
            output = b"src.txt\0"
        else:
            output = b""
        return SimpleNamespace(returncode=0, stdout=output)

    monkeypatch.setattr(native, "git_bytes", git_bytes)
    assert native._transfer_workspace_overlap(
        {"worktreePath": str(tmp_path)}, {"base": first}, ("src.txt",)
    )
    assert any(second in args and "--no-renames" in args for args in calls)


@pytest.mark.parametrize("diverged", [False, True])
def test_peer_scope_excludes_accepted_main_progress_but_retains_exclusive_work(case, diverged):
    record = core.invoke(case)["record"]
    (case.repo / "src.txt").write_text("accepted main change\n")
    git(case.repo, "add", "src.txt")
    git(case.repo, "commit", "-qm", "advance current main")
    git(case.repo, "push", "origin", "main")
    peer = case.root / "current-main-peer"
    git(case.repo, "worktree", "add", "-b", "new-peer", str(peer), "main")
    if diverged:
        (peer / "src.txt").write_text("unmerged peer change\n")
        git(peer, "add", "src.txt")
        git(peer, "commit", "-qm", "peer contribution")
    case.host.workspaces.append(
        {"id": "new-peer", "projectId": "project", "worktreePath": str(peer), "branch": "new-peer"}
    )
    observed = case.observer(case.repo, Path(case.snapshot.common_dir))

    def check():
        return native.validate_transfer_observation(
            record,
            case.store.load(case.before.id),
            case.successor,
            project=case.repo,
            native_inventory=observed,
            store=case.store,
        )

    if diverged:
        with pytest.raises(CampaignError, match="overlapping"):
            check()
    else:
        assert check()["native_revision"] == observed.revision
        assert (
            core.transfer.read_release(case.store, case.before.id, case.unit.id)["base"]
            == record["base"]
        )


@pytest.mark.parametrize("actor", ["none", "dead", "alive", "unknown"])
def test_fresh_claim_before_request_needs_no_grant_until_native_actor_exists(transferred, actor):
    from bmad_loop.campaign_migration import MigrationItem
    from bmad_loop.campaign_model import WorkerClaim

    case = transferred
    result = case.runtime._perform_prepare_unit(case.snapshot, case.action)
    case.runtime.apply(case.snapshot, case.action, result)
    case.action.state, case.action.receipt = "confirmed", result.data
    case.snapshot = case.store.save(case.snapshot, case.snapshot.revision)
    unit = case.snapshot.units[case.unit.id]
    root = case.store.directory("successor") / "workers" / "new-claim"
    assert not root.exists()
    unit.claim = WorkerClaim(
        unit.id,
        1,
        1,
        "new-claim",
        run_dir=str(root),
        workspace=unit.bindings["workspace"],
        branch=unit.bindings["branch"],
    )
    record = core.transfer.read_release(case.store, case.before.id, unit.id)
    observed = case.observer(case.repo, Path(case.snapshot.common_dir))
    if actor != "none":
        item = MigrationItem(
            "native-actor",
            "process",
            "rev",
            "native",
            liveness=actor,
            details={"workspace_id": "recipient-workspace", "terminal_id": "unproven"},
        )
        observed = dataclasses.replace(observed, items=(*observed.items, item))

    def check():
        return native.validate_transfer_observation(
            record,
            case.store.load(case.before.id),
            case.snapshot,
            project=case.repo,
            native_inventory=observed,
            store=case.store,
        )

    if actor in {"alive", "unknown"}:
        with pytest.raises(CampaignError, match="Qualified file is unavailable"):
            check()
    else:
        assert check()["native_revision"] == observed.revision
