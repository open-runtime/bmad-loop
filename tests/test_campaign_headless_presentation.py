"""Native terminal placement stays distinct from headless input and tool grants."""

import time
from dataclasses import asdict, replace

from test_campaign_headless import prepared as prepared
from test_campaign_reasoning_jobs import case

from bmad_loop.campaign_headless import read_json
from bmad_loop.campaign_paradev import WorkspaceBinding
from bmad_loop.campaign_titles import work_title


def test_registered_terminal_runs_owned_child_with_external_no_tools_state(prepared, tmp_path):
    runner, _, request, candidate, capability = prepared
    workspace = tmp_path / "registered-workspace"
    workspace.mkdir()
    view = {"workspace": str(workspace), "workspace_id": "owned-workspace"}
    observed = []
    launch = runner.mux.new_window

    def new_window(session, name, cwd, env, command):
        observed.append((cwd, env))
        return launch(session, name, cwd, env, command)

    runner.mux.new_window = new_window
    titles = []
    runner.mux.set_display_title = lambda terminal, title, **kwargs: titles.append(
        (terminal, title)
    )
    path = runner.prepare(
        tmp_path / "external-state",
        request,
        candidate,
        capability,
        launch_id="native-presentation",
        generation=1,
        presentation=view,
    )
    runner.launch(path)
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        result = runner.observe(path)
        if result["state"] == "finished":
            break
        time.sleep(0.02)
    assert result["state"] == "finished" and result["receipt"]["outcome"] == "completed"
    assert observed == [(workspace, {"BMAD_LOOP_PARADEV_WORKSPACE_ID": "owned-workspace"})]
    envelope = read_json(path)
    assert envelope["presentation"] == view
    assert envelope["workspace"] == str(path.parent)
    assert envelope["permissions"] == "no-tools" and envelope["grants"] == {}
    assert titles == [("1", f"summary · gpt-test · Portfolio · {request.id}")]
    assert result["receipt"]["presentation_status"]["state"] == "confirmed"


def test_reasoning_waits_for_workspace_then_freezes_its_native_binding(tmp_path, prepared):
    snapshot, runtime, supervisor, _ = case(tmp_path, prepared, count=1)
    snapshot.spec = replace(snapshot.spec, paradev=replace(snapshot.spec.paradev, reflect=True))
    snapshot.config_revision += 1
    # A configuration revision owns its own frozen launcher envelope.
    from test_campaign_route_readiness import freeze_routes

    from bmad_loop.adapters.profile import get_profile

    freeze_routes(
        snapshot, tmp_path / "reflected-configuration", {"campaign:codex": get_profile("codex")}
    )
    job = snapshot.jobs["job-0"]
    job["request"]["coordinates"]["config_revision"] = snapshot.config_revision
    waiting = runtime.observe_workers(snapshot)
    assert [row.kind for row in waiting] == ["reasoning-wait"]
    assert job["attempt"] == 0 and job["claim"] is None
    unit = snapshot.units[job["request"]["unit"]]
    workspace = tmp_path / "registered-workspace"
    workspace.mkdir()
    unit.bindings.update(workspace=str(workspace), branch="owned-branch")
    binding = WorkspaceBinding(
        unit.id,
        "allocate",
        "owned-workspace",
        "owned-project",
        str(workspace),
        "owned-branch",
        "common",
        "worktree-git",
        "qualified-epic",
    )
    unit.bindings["native"] = asdict(binding)
    proposals = tuple(p for p in runtime.observe_workers(snapshot) if p.kind == "reasoning-launch")
    assert len(proposals) == 1
    # Preparation uses the same production control handler as the supervisor;
    # the fixture's initially persisted spec intentionally predates reflection.
    from bmad_loop.campaign_store import add_action

    proposal = proposals[0]
    action = add_action(snapshot, proposal.kind, proposal.unit, proposal.payload)
    runtime.control(snapshot, {"kind": "prepare-effect", "action": asdict(action)})
    expected = {
        "workspace": str(workspace),
        "workspace_id": "owned-workspace",
        "navigation_prefix": work_title(unit, snapshot=snapshot),
    }
    assert job["claim"]["presentation"] == expected
    unit.bindings["native"]["workspace_id"] = "later-workspace"
    assert job["claim"]["presentation"] == expected


def test_reasoning_navigation_uses_own_unknown_actor_and_updates_parent_pr(tmp_path, prepared):
    from bmad_loop.campaign_jobs import request_from_dict
    from bmad_loop.campaign_reasoning_flow import presentation

    snapshot, _, _, _ = case(tmp_path, prepared, count=1)
    snapshot.spec = replace(snapshot.spec, paradev=replace(snapshot.spec.paradev, reflect=True))
    request = request_from_dict(snapshot.jobs["job-0"]["request"])
    unit = snapshot.units[request.unit]
    unit.bindings.update(workspace=str(tmp_path), branch="branch")
    unit.bindings["native"] = asdict(
        WorkspaceBinding(
            unit.id,
            "allocate",
            "owned-workspace",
            "project",
            str(tmp_path),
            "branch",
            "common",
            "git",
            "epic",
        )
    )
    unit.bindings["hcom"] = {"name": "tina"}
    unit.bindings["issue"] = {"number": 42}
    unit.bindings["pr"] = {"number": 18}
    view = presentation(snapshot, request)
    assert view["navigation_prefix"] == work_title(unit, snapshot=snapshot)
    assert "tina" not in view["navigation_prefix"]
    runner, _, standalone, candidate, capability = prepared
    runner.mux.set_display_title = lambda terminal, title, **kwargs: title
    path = runner.prepare(
        tmp_path / "title-state",
        standalone,
        candidate,
        capability,
        launch_id="title-refresh",
        generation=1,
        presentation=view,
    )
    runner.launch(path)
    before = path.read_bytes()
    prefix = view["navigation_prefix"].replace("PR #18", "PR #19")
    status = runner.refresh_presentation(path, prefix)
    assert prefix in status["title"]
    assert status["identity"]["launch_id"] == "title-refresh"
    assert path.read_bytes() == before


def test_headless_restores_only_verified_own_native_hook_context(monkeypatch):
    import pytest

    from bmad_loop import campaign_headless_context
    from bmad_loop.adapters.paradev_runtime import CONTEXT_FILE
    from bmad_loop.campaign_model import CampaignError

    context = {
        "SUPERSET_TERMINAL_ID": "worker",
        "SUPERSET_WORKSPACE_ID": "own-workspace",
        "SUPERSET_ACCOUNT_ATTRIBUTION_TOKEN": "owned-test-token",
    }
    verdict = {"verified": True, "context": context, "checks": {"lineage": "ok"}}
    monkeypatch.setattr(campaign_headless_context, "verify_context", lambda _: verdict)
    clean, receipt = campaign_headless_context.restore(
        {"CODEX_HOME": "/account"},
        {CONTEXT_FILE: "/owned/context.json"},
        {"workspace_id": "own-workspace"},
    )
    assert clean == {"CODEX_HOME": "/account", **context}
    assert receipt["terminal_id"] == "worker"
    assert "owned-test-token" not in str(receipt)
    with pytest.raises(CampaignError, match="frozen workspace"):
        campaign_headless_context.restore(
            {}, {CONTEXT_FILE: "/owned/context.json"}, {"workspace_id": "foreign"}
        )
    verdict["verified"] = False
    with pytest.raises(CampaignError, match="frozen workspace"):
        campaign_headless_context.restore(
            {}, {CONTEXT_FILE: "/owned/context.json"}, {"workspace_id": "own-workspace"}
        )
