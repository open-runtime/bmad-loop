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


def test_sanitizes_parent_identity_preserving_accounts_and_room(flow, tmp_path):
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
        communication=CommunicationSpec(),
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
