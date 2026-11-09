"""Native lifecycle reflection joins actor identities without creating workers."""

import dataclasses
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from bmad_loop import envvars
from bmad_loop.adapters.base import SessionHandle, SessionSpec
from bmad_loop.adapters.paradev_runtime import WORKSPACE_KEY, private_json
from bmad_loop.campaign_model import (
    CampaignSnapshot,
    CampaignSpec,
    CampaignUnit,
    CommunicationSpec,
    ParadevSpec,
    UnitSpec,
    WorkerClaim,
)
from bmad_loop.campaign_paradev import ReflectionError, WorkspaceBinding
from bmad_loop.campaign_reflection_flow import (
    compose_context,
    observe_adapter,
    reflection_policy,
    sanitized_worker_env,
    session_env,
    stage_binding,
)
from bmad_loop.policy import Policy


@pytest.fixture
def flow(project, tmp_path):
    if os.name != "posix":
        pytest.skip("Native Paradev context is qualified on POSIX hosts")
    workspace = str(project.project)
    binding = WorkspaceBinding(
        "unit",
        "allocate",
        "workspace",
        "project",
        workspace,
        "branch",
        "common",
        "git-dir",
        "story",
    )
    claim = WorkerClaim(
        "unit",
        1,
        1,
        "launch",
        run_id="globally-unique-run",
        run_dir=str(tmp_path / "worker"),
        workspace=workspace,
        branch="branch",
    )
    unit = CampaignUnit(
        UnitSpec("unit", "source", "epic", "Story"),
        claim=claim,
        bindings={
            "native": dataclasses.asdict(binding),
            "workspace": workspace,
            "branch": "branch",
        },
    )
    spec = CampaignSpec(
        name="qualification",
        paradev=ParadevSpec(reflect=True, project_id="project", worktree_root=str(tmp_path)),
    )
    snapshot = CampaignSnapshot("campaign", "owner/repo", spec, units={"unit": unit}, generation=1)
    run_dir = tmp_path / "adapter-run"
    mux = SimpleNamespace(
        root=tmp_path / "native",
        status=lambda **_: {
            "windows": [
                {
                    "id": "terminal",
                    "workspace": "workspace",
                    "session": "real-session",
                    "state": "active",
                }
            ]
        },
        native_identity=lambda _: {"agentSessionId": "native-session"},
        _daemon=lambda: {"terminal": {"pid": 12}},
    )
    private_json(
        mux.root / "launches" / "terminal" / "launch.json",
        {
            "workspaceId": "workspace",
            "cwd": workspace,
            "env": {"BMAD_LOOP_RUN_DIR": str(run_dir), "BMAD_LOOP_TASK_ID": "task"},
        },
    )
    return snapshot, unit, claim, binding, run_dir, mux


def test_sanitizes_parent_identity_preserving_accounts_and_explicit_room(flow, tmp_path):
    _, _, _, binding, _, _ = flow
    env = sanitized_worker_env(
        {
            "CODEX_HOME": "/account",
            "TEAMROUTER_ACCOUNT": "authorized",
            "SUPERSET_TERMINAL_ID": "parent",
            "HCOM_INSTANCE_NAME": "parent",
            "HCOM_PROCESS_ID": "stale",
            "HCOM_DIR": "/room",
            "BMAD_LOOP_RUN_DIR": "/parent-run",
            "CODEX_THREAD_ID": "parent-thread",
        },
        binding=binding,
        state_dir=tmp_path / "native",
        communication=CommunicationSpec(room="/room"),
    )
    assert env["CODEX_HOME"] == "/account" and env["TEAMROUTER_ACCOUNT"] == "authorized"
    assert env["HCOM_DIR"] == "/room"
    assert env[WORKSPACE_KEY] == "workspace"
    assert env[envvars.MUX_BACKEND] == "paradev"
    assert not any(
        key in env
        for key in (
            "SUPERSET_TERMINAL_ID",
            "HCOM_INSTANCE_NAME",
            "HCOM_PROCESS_ID",
            "BMAD_LOOP_RUN_DIR",
            "CODEX_THREAD_ID",
        )
    )


def test_policy_and_stage_hook_environment_bound_to_actual_run(flow, tmp_path):
    snapshot, unit, claim, _, run_dir, _ = flow
    pol = Policy()
    assert reflection_policy(pol, True).mux.backend == "paradev"
    assert reflection_policy(pol, False) is pol
    env = session_env(
        snapshot, unit, claim, run_dir=run_dir, task_id="task", native_state_dir=tmp_path / "native"
    )
    assert env["BMAD_LOOP_RUN_DIR"] == str(run_dir)
    assert env["BMAD_LOOP_TASK_ID"] == "task"
    assert env[WORKSPACE_KEY] == "workspace"


def test_native_binding_uses_run_environment_not_terminal_title(flow):
    snapshot, unit, claim, _, run_dir, mux = flow
    observed = stage_binding(snapshot, unit, claim, mux=mux, run_directories=(run_dir,))
    assert observed["state"] == "observed"
    assert observed["windows"][0]["terminal_id"] == "terminal"
    assert observed["windows"][0]["attribution"] == "confirmed"
    assert observed["windows"][0]["phase"] == "unknown"
    assert not observed["actors_quiescent"]
    wrong = stage_binding(snapshot, unit, claim, mux=mux, run_directories=(run_dir / "different",))
    assert wrong["state"] == "pending" and wrong["windows"] == []
    assert not wrong["actors_quiescent"]


def test_reflection_projects_enabled_tasks_through_normal_flow(flow):
    from bmad_loop.campaign_reflection_flow import reflect

    snapshot, unit, claim, _, run_dir, mux = flow
    snapshot.spec = dataclasses.replace(
        snapshot.spec, paradev=dataclasses.replace(snapshot.spec.paradev, tasks=True)
    )
    calls = []
    provider = SimpleNamespace(
        reflect=lambda *args, **kwargs: {"state": "observed"},
        reflect_task=lambda *args, **kwargs: calls.append(kwargs) or "task-id",
        bind_terminal=lambda *args, **kwargs: {},
    )
    result = reflect(snapshot, unit, claim, provider=provider, mux=mux, run_directories=(run_dir,))
    assert result["task"] == {"state": "confirmed", "task_id": "task-id"}
    assert calls[0]["title"] == "hcom:pending · E:epic · I:pending · PR:pending · Story"
    assert calls[0]["state"] == unit.state.value


@pytest.mark.parametrize("active", [False, True])
def test_reflection_uses_shared_native_issue_for_all_navigation_and_task_links(flow, active):
    from bmad_loop.campaign_reflection_flow import reflect
    from bmad_loop.campaign_runtime import CampaignRuntime

    snapshot, unit, claim, _, run_dir, mux = flow
    unit.claim = claim if active else None
    unit.bindings["hierarchy_ready"] = False
    snapshot.spec = dataclasses.replace(
        snapshot.spec, paradev=dataclasses.replace(snapshot.spec.paradev, tasks=True)
    )
    before = CampaignRuntime._reflection_fingerprint(unit, snapshot)
    snapshot.bindings["github_hierarchy"] = {
        "story:"
        + unit.id: {
            "kind": "issue",
            "number": 1582,
            "database_id": 6128563360,
            "url": "https://github.com/owner/repo/issues/1582",
        }
    }
    assert CampaignRuntime._reflection_fingerprint(unit, snapshot) != before
    workspaces, tasks, terminals = [], [], []
    provider = SimpleNamespace(
        reflect=lambda *args, **kwargs: workspaces.append(kwargs) or {"state": "observed"},
        reflect_task=lambda *args, **kwargs: tasks.append(kwargs) or "task-id",
        bind_terminal=lambda *args, **kwargs: terminals.append(kwargs) or {},
    )
    result = reflect(
        snapshot, unit, unit.claim, provider=provider, mux=mux, run_directories=(run_dir,)
    )
    prefix = "hcom:pending · E:epic · I:#1582 · PR:pending"
    assert workspaces[0]["title"] == prefix + " · Story"
    assert workspaces[0]["group_title"] == prefix
    assert workspaces[0]["group_fields"]["issues"] == ("#1582",)
    assert tasks[0]["title"] == prefix + " · Story"
    assert tasks[0]["links"] == ("https://github.com/owner/repo/issues/1582",)
    assert len(terminals) == (1 if active else 0)
    if active:
        assert terminals[0]["navigation_prefix"] == prefix
        assert terminals[0]["title"].startswith(prefix + " · Story")
    assert result["task"] == {"state": "confirmed", "task_id": "task-id"}
    assert unit.bindings["hierarchy_ready"] is False and "issue" not in unit.bindings


def test_reasoning_terminal_presentation_uses_shared_story_without_borrowing_actor(flow):
    from bmad_loop.campaign_reasoning_flow import presentation

    snapshot, unit, *_ = flow
    snapshot.bindings["github_hierarchy"] = {
        "story:"
        + unit.id: {
            "kind": "issue",
            "number": 529,
            "database_id": 6090790724,
            "url": "https://github.com/owner/repo/issues/529",
        }
    }
    unit.bindings["reflection"] = {
        "stage": {
            "launch_id": unit.claim.launch_id,
            "windows": [{"state": "active", "hook": {"hcom_name": "tina"}}],
        }
    }
    view = presentation(snapshot, SimpleNamespace(unit=unit.id))
    assert view["navigation_prefix"] == "hcom:pending · E:epic · I:#529 · PR:pending"


def test_sibling_prompt_is_scoped_and_bounded(flow):
    from bmad_loop.campaign_model import UnitState

    snapshot, unit, *_ = flow
    unit.spec = dataclasses.replace(unit.spec, scopes=("own/",))
    snapshot.spec = dataclasses.replace(
        snapshot.spec, communication=CommunicationSpec(max_context_events=1)
    )
    for index in range(3):
        peer = CampaignUnit(UnitSpec(f"peer-{index}", "source", "epic", "Peer", scopes=("other/",)))
        snapshot.units[peer.id] = peer
    snapshot.units["done"] = CampaignUnit(
        UnitSpec("done", "source", "epic", "Done"), state=UnitState.DELIVERED
    )
    context = json.loads(compose_context(snapshot, unit).split("(advisory):\n", 1)[1])
    assert len(context["siblings"]) == 1
    assert context["omitted_siblings"] == 2
    assert context["coverage"] == "bounded"


def test_communication_collects_events_and_transcripts_without_reflection(flow, tmp_path):
    from bmad_loop.campaign_communication import Observation, PeerBinding
    from bmad_loop.campaign_reflection_flow import observe_communication

    snapshot, unit, claim, *_ = flow
    snapshot.spec = dataclasses.replace(
        snapshot.spec,
        paradev=ParadevSpec(),
        communication=CommunicationSpec(mode="observe", room=str(tmp_path), max_context_events=2),
    )
    peer = PeerBinding(
        snapshot.id, unit.id, claim.attempt, "nava", "pid", "session", "term", str(tmp_path)
    )
    unit.bindings["hcom"] = dataclasses.asdict(peer)
    calls = []
    observer = SimpleNamespace(
        roster=lambda bindings: Observation(True),
        events=lambda binding, **kwargs: calls.append(kwargs)
        or Observation(True, events=({"id": 5, "data": {"session": "session"}},), cursor=5),
        transcript=lambda binding, **kwargs: {"messages": ["bounded transcript"]},
    )
    first = observe_communication(snapshot, observer=observer)
    assert first["available"] and first["events"][0]["id"] == 5
    assert first["transcripts"][unit.id]["messages"] == ["bounded transcript"]
    snapshot.bindings["communication"] = first
    observe_communication(snapshot, observer=observer)
    assert calls[0] == {"cursor": 0, "limit": 2}
    assert calls[1]["cursor"] == 5


def test_runtime_reflects_completed_tasks_and_persists_history(flow, tmp_path, monkeypatch):
    from bmad_loop import campaign_paradev
    from bmad_loop.campaign_cli import status_document
    from bmad_loop.campaign_runtime import CampaignRuntime
    from bmad_loop.campaign_store import CampaignStore, add_action

    snapshot, unit, claim, _, _, mux = flow
    unit.claim = dataclasses.replace(claim, quiescent=True)
    snapshot.spec = dataclasses.replace(
        snapshot.spec,
        units=(unit.spec,),
        paradev=dataclasses.replace(snapshot.spec.paradev, tasks=True),
    )
    mux.status = lambda **kwargs: {"windows": []}
    calls = []
    provider = SimpleNamespace(
        reflect=lambda *args, **kwargs: {"state": "observed"},
        reflect_task=lambda *args, **kwargs: calls.append(kwargs) or "native-task",
    )
    monkeypatch.setattr(campaign_paradev, "ParadevWorkspaceProvider", lambda **kwargs: provider)
    store = CampaignStore(tmp_path / "runtime-state")
    snapshot = store.create(snapshot)
    runtime = CampaignRuntime(Path(claim.workspace), store, clock=lambda: 100)
    monkeypatch.setattr(runtime, "_native_mux", lambda snapshot: mux)
    monkeypatch.setattr(
        runtime,
        "_lifecycle_bridge",
        lambda *args, **kwargs: SimpleNamespace(observe=lambda *a, **k: {"state": "observed"}),
    )
    proposal = next(row for row in runtime.observe_workers(snapshot) if row.kind == "reflection")
    action = add_action(snapshot, proposal.kind, proposal.unit, proposal.payload)
    snapshot = store.save(snapshot, snapshot.revision)
    action = next(row for row in snapshot.outbox if row.id == action.id)
    result = runtime.perform(snapshot, action)
    assert result.state == "confirmed"
    action.state, action.receipt = result.state, result.data
    runtime.apply(snapshot, action, result)
    saved = store.save(snapshot, snapshot.revision)
    assert calls and Path(saved.units[unit.id].bindings["reflection"]["history_path"]).is_file()
    assert (
        status_document(saved)["units"][0]["operator"]["reflection"]["task"]["task_id"]
        == "native-task"
    )


def test_runtime_group_refreshes_quiescent_siblings_after_actor_and_pr_discovery(
    flow, tmp_path, monkeypatch
):
    from bmad_loop import campaign_paradev, campaign_reflection_flow
    from bmad_loop.campaign_runtime import CampaignRuntime
    from bmad_loop.campaign_store import CampaignStore
    from bmad_loop.campaign_supervisor import Observation

    snapshot, unit, claim, binding, _, mux = flow
    unit.claim = dataclasses.replace(claim, quiescent=True)
    peer = CampaignUnit(
        dataclasses.replace(unit.spec, id="peer", title="Other story"),
        claim=dataclasses.replace(claim, unit="peer", launch_id="peer-launch", quiescent=True),
        bindings={
            "workspace": binding.path,
            "branch": binding.branch,
            "native": dataclasses.asdict(
                dataclasses.replace(binding, unit_id="peer", workspace_id="peer-workspace")
            ),
        },
    )
    snapshot.units[peer.id] = peer
    snapshot.spec = dataclasses.replace(
        snapshot.spec, paradev=dataclasses.replace(snapshot.spec.paradev, group_by="epic")
    )
    runtime = CampaignRuntime(
        Path(claim.workspace), CampaignStore(tmp_path / "refresh-store"), clock=lambda: 100
    )
    for work in snapshot.units.values():
        work.bindings["reflection"] = {
            "fingerprint": runtime._reflection_fingerprint(work, snapshot),
            "observed_at": 100,
        }
    assert not [row for row in runtime.observe_workers(snapshot) if row.kind == "reflection"]
    unit.bindings["issue"] = {"number": 42}
    unit.bindings["pr"] = {"number": 18}
    unit.bindings["reflection"]["stage"] = {
        "launch_id": claim.launch_id,
        "windows": [{"state": "gone", "hook": {"hcom_name": "tina"}}],
    }
    proposed = [row for row in runtime.observe_workers(snapshot) if row.kind == "reflection"]
    assert {row.unit for row in proposed} == {unit.id, peer.id}
    captured = []
    provider = SimpleNamespace(reflect=lambda *args, **kwargs: captured.append(kwargs) or kwargs)
    monkeypatch.setattr(campaign_paradev, "ParadevWorkspaceProvider", lambda **kwargs: provider)
    monkeypatch.setattr(runtime, "_native_mux", lambda _: mux)
    monkeypatch.setattr(
        runtime,
        "_lifecycle_bridge",
        lambda *args, **kwargs: SimpleNamespace(observe=lambda *a, **k: {}),
    )
    monkeypatch.setattr(
        campaign_reflection_flow,
        "stage_binding",
        lambda *args, **kwargs: {
            "state": "observed",
            "launch_id": peer.claim.launch_id,
            "windows": [],
        },
    )
    from bmad_loop.campaign_store import add_action

    action = add_action(snapshot, "reflection", peer.id, {})
    result = runtime.perform(snapshot, action)
    assert isinstance(result, Observation) and result.state == "confirmed"
    assert captured[0]["group_title"].startswith("@tina · E:epic · I:#42 · PR:#18")
    assert captured[0]["group_fields"] == {
        "names": ("tina",),
        "epics": ("epic",),
        "issues": ("#42",),
        "prs": ("#18",),
    }
    assert captured[0]["title"].startswith("hcom:pending · E:epic · I:pending · PR:pending")


def test_runtime_persists_hcom_cursor_with_reflection_disabled(flow, tmp_path, monkeypatch):
    from bmad_loop.campaign_communication import Observation, PeerBinding
    from bmad_loop.campaign_runtime import CampaignRuntime
    from bmad_loop.campaign_store import CampaignStore, add_action

    snapshot, unit, claim, *_ = flow
    snapshot.spec = dataclasses.replace(
        snapshot.spec,
        units=(unit.spec,),
        paradev=ParadevSpec(),
        communication=CommunicationSpec(mode="observe", room=str(tmp_path)),
    )
    unit.bindings["hcom"] = dataclasses.asdict(
        PeerBinding(
            snapshot.id, unit.id, claim.attempt, "nava", "pid", "session", "term", str(tmp_path)
        )
    )
    observer = SimpleNamespace(
        roster=lambda bindings: Observation(True),
        events=lambda *a, **k: Observation(True, cursor=5),
        transcript=lambda *a, **k: {"messages": []},
    )
    store = CampaignStore(tmp_path / "runtime-state")
    snapshot = store.create(snapshot)
    runtime = CampaignRuntime(Path(claim.workspace), store, clock=lambda: 100)
    monkeypatch.setattr(runtime, "_hcom_observer", lambda snapshot: observer)
    proposal = next(row for row in runtime.observe_workers(snapshot) if row.kind == "communication")
    action = add_action(snapshot, proposal.kind, proposal.unit, proposal.payload)
    snapshot = store.save(snapshot, snapshot.revision)
    action = next(row for row in snapshot.outbox if row.id == action.id)
    result = runtime.perform(snapshot, action)
    action.state, action.receipt = result.state, result.data
    runtime.apply(snapshot, action, result)
    saved = store.save(snapshot, snapshot.revision)
    assert list(saved.bindings["communication"]["cursors"].values()) == [5]
    assert not any(row.kind == "communication" for row in runtime.observe_workers(saved))
    runtime.clock = lambda: 116
    assert any(row.kind == "communication" for row in runtime.observe_workers(saved))


def test_actual_adapter_handle_proves_phase_and_cli_pin(flow):
    snapshot, unit, claim, _, run_dir, mux = flow

    class Adapter:
        def start_session(self, spec):
            self.spec = spec
            return SessionHandle(
                spec.task_id, "terminal", launched_ns=100, pinned_session_id="cli-session"
            )

    adapter = Adapter()
    observe_adapter(
        adapter,
        directory=Path(claim.run_dir),
        run_dir=run_dir,
        claim=claim,
        campaign_id=snapshot.id,
        environment={"WORKER": "own"},
        context="\ncontext",
    )
    adapter.start_session(
        SessionSpec("task", "publication_review", "Review", Path(claim.workspace))
    )
    assert adapter.spec.env["WORKER"] == "own" and adapter.spec.prompt.endswith("context")
    observed = stage_binding(snapshot, unit, claim, mux=mux, run_directories=(run_dir,))
    assert observed["windows"][0]["phase"] == "publication_review"


def test_handle_observation_failure_does_not_strand_started_actor(flow, monkeypatch):
    import bmad_loop.campaign_reflection_flow as module

    snapshot, unit, claim, _, run_dir, _ = flow
    adapter = SimpleNamespace(start_session=lambda spec: SessionHandle(spec.task_id, "terminal"))
    observe_adapter(
        adapter,
        directory=Path(claim.run_dir),
        run_dir=run_dir,
        claim=claim,
        campaign_id=snapshot.id,
        environment={},
    )
    monkeypatch.setattr(
        module, "atomic_write_text", lambda *a, **k: (_ for _ in ()).throw(OSError("full disk"))
    )
    assert (
        adapter.start_session(SessionSpec("task", "dev", "build", Path(claim.workspace))).native_id
        == "terminal"
    )
    assert adapter.reflection_errors


def test_stale_request_never_adopted(flow):
    snapshot, unit, claim, _, run_dir, mux = flow
    request = Path(claim.run_dir) / "request.json"
    request.parent.mkdir(parents=True)
    request.write_text(json.dumps({"campaign_id": "other-campaign"}), encoding="utf-8")
    with pytest.raises(ReflectionError, match="differs"):
        stage_binding(snapshot, unit, claim, mux=mux, run_directories=(run_dir,))


def test_recorded_handle_does_not_follow_wrong_run_or_claim(flow):
    snapshot, unit, claim, _, run_dir, mux = flow
    handle = Path(claim.run_dir) / "native-sessions" / "task.json"
    handle.parent.mkdir(parents=True)
    handle.write_text(
        json.dumps({"campaign_id": "foreign", "native_id": "terminal"}), encoding="utf-8"
    )
    observed = stage_binding(snapshot, unit, claim, mux=mux, run_directories=(run_dir,))
    assert observed["state"] == "unknown"
    assert not observed["actors_quiescent"]


def test_sibling_context_works_without_hcom_and_has_actual_links(flow):
    snapshot, unit, _, _, _, _ = flow
    snapshot.units["peer"] = CampaignUnit(
        UnitSpec("peer", "source", "epic", "Other", scopes=("src/api",)),
        bindings={"pr": {"url": "https://github.com/owner/repo/pull/3"}},
    )
    context = compose_context(snapshot, unit)
    data = json.loads(context.split("advisory):\n")[1])
    assert data["advisory_only"]
    assert data["siblings"][0]["pr_url"].endswith("/pull/3")
    assert data["siblings"][0]["ownership"] == ["src/api"]
    assert not data["automatic_peer_delivery"]


def test_hook_readback_keeps_parent_cli_and_rejects_nested_session(flow):
    snapshot, unit, claim, _, run_dir, mux = flow

    class Adapter:
        def start_session(self, spec):
            return SessionHandle(
                spec.task_id, "terminal", launched_ns=100, pinned_session_id="parent-cli"
            )

    adapter = Adapter()
    observe_adapter(
        adapter,
        directory=Path(claim.run_dir),
        run_dir=run_dir,
        claim=claim,
        campaign_id=snapshot.id,
        environment={},
    )
    adapter.start_session(SessionSpec("task", "dev", "Build", Path(claim.workspace)))
    events = run_dir / "events"
    events.mkdir(parents=True)
    records = [
        {"event": "SessionStart", "task_id": "task", "session_id": "nested", "ts": 101},
        {
            "event": "SessionStart",
            "task_id": "task",
            "session_id": "parent-cli",
            "ts": 102,
            "hcom_name": "nava",
            "hcom_dir": "/room",
            "transcript_path": "/retained/parent.jsonl",
        },
        {"event": "Stop", "task_id": "task", "session_id": "nested", "ts": 103},
        {"event": "Stop", "task_id": "task", "session_id": "parent-cli", "ts": 104},
    ]
    for index, event in enumerate(records):
        (events / f"{index}.json").write_text(json.dumps(event), encoding="utf-8")
    private_json(
        mux.root / "launches" / "terminal" / "launch.json",
        {
            "workspaceId": "workspace",
            "cwd": claim.workspace,
            "env": {
                "BMAD_LOOP_RUN_DIR": str(run_dir),
                "BMAD_LOOP_TASK_ID": "task",
                "BMAD_LOOP_EVENTS_DIR": str(events),
            },
        },
    )
    observed = stage_binding(snapshot, unit, claim, mux=mux, run_directories=(run_dir,))
    hook = observed["windows"][0]["hook"]
    assert hook["cli_session_id"] == "parent-cli"
    assert hook["hcom_name"] == "nava"
    assert hook["transcript_path"] == "/retained/parent.jsonl"
    assert not observed[
        "actors_quiescent"
    ]  # Stop text never completes the campaign or proves teardown.


def test_hcom_binding_requires_same_cli_process_terminal_and_room(flow, tmp_path):
    from bmad_loop.campaign_communication import Observation
    from bmad_loop.campaign_reflection_flow import discover_peer_binding

    snapshot, unit, claim, _, _, _ = flow
    snapshot.spec = dataclasses.replace(
        snapshot.spec, communication=CommunicationSpec(mode="observe", room=str(tmp_path))
    )
    actor = {
        "name": "nava",
        "session_id": "cli-parent",
        "launch_context": {"process_id": "process", "env": {"SUPERSET_TERMINAL_ID": "terminal"}},
    }
    observer = SimpleNamespace(
        room=tmp_path.resolve(),
        _read=lambda args: [actor],
        roster=lambda bindings: Observation(True),
    )
    window = {
        "terminal_id": "terminal",
        "hook": {"hcom_name": "nava", "hcom_room": str(tmp_path), "cli_session_id": "cli-parent"},
    }
    binding = discover_peer_binding(snapshot, unit, claim, window, observer=observer)
    assert binding.process_id == "process" and binding.session_id == "cli-parent"
    actor["session_id"] = "reused-session"
    with pytest.raises(ReflectionError, match="different native"):
        discover_peer_binding(snapshot, unit, claim, window, observer=observer)


def test_hcom_off_never_queries_supplied_observer(flow):
    from bmad_loop.campaign_reflection_flow import discover_peer_binding

    snapshot, unit, claim, _, _, _ = flow
    observer = SimpleNamespace(
        _read=lambda _: (_ for _ in ()).throw(AssertionError("unexpected hcom read"))
    )
    assert discover_peer_binding(snapshot, unit, claim, {}, observer=observer) is None


def test_nonreflecting_actor_drops_parent_room_and_claude_nesting_flags():
    from bmad_loop.campaign_reflection_flow import sanitized_actor_env

    env = sanitized_actor_env(
        {
            "HCOM_DIR": "/parent",
            "HCOM_NAME": "parent",
            "HCOM_CONFIG": "/parent/config",
            "CLAUDECODE": "1",
            "CLAUDE_CODE_ENTRYPOINT": "sdk",
            "CLAUDE_CONFIG_DIR": "/account",
            "CODEX_SESSION_ID": "parent",
        },
        communication=CommunicationSpec(),
    )
    assert env == {"CLAUDE_CONFIG_DIR": "/account"}
