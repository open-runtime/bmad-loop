"""Native presentation separates business work from per-launch actors and bridges."""

import copy
import dataclasses
import json
import time
from types import SimpleNamespace

import pytest
from test_campaign_headless import prepared as prepared
from test_campaign_paradev import provider as provider
from test_campaign_requalification import allocated_campaign as allocated_campaign
from test_campaign_requalification import campaign as campaign

from bmad_loop.campaign_model import (
    CampaignError,
    CampaignSnapshot,
    CampaignSpec,
    CampaignUnit,
    ParadevSpec,
    UnitSpec,
    UnitState,
    WorkerClaim,
)
from bmad_loop.campaign_reflect import refresh
from bmad_loop.campaign_reflection_flow import desired_group_identity
from bmad_loop.campaign_store import CampaignStore, RevisionConflict
from bmad_loop.campaign_titles import group_title, unit_session_title, work_title


def project_display(p, binding, **changes):
    return p.reflect(
        binding,
        **{
            "campaign_id": "campaign",
            "epic": "Supply chain",
            "phase": "draft",
            "attempt": 0,
            "title": "#529 · Benchmark schedule",
            "group_title": "REM · Supply chain",
            **changes,
        },
    )


def test_folder_display_renames_without_changing_tags_or_native_order(provider):
    p, host = provider
    bound = p.allocate("unit", "unit-branch", "Story", "allocation", group="epic")
    host.workspaces[0]["tags"] = "owner-folder"
    first = project_display(p, bound)
    host.folder_settings[0].update(color="purple", tabOrder=17)
    tags = host.workspaces[0]["tags"]
    second = project_display(p, bound, group_title="REM · Renamed epic")
    assert second["folder"] == first["folder"]
    assert host.workspaces[0]["tags"] == tags
    assert host.folder_settings[0] == {
        "scope": "project",
        "tag": first["folder"],
        "displayName": "REM · Renamed epic",
        "color": "purple",
        "tabOrder": 17,
    }
    assert set(host.folder_writes[-1]) == {"scope", "tag", "displayName"}


def test_manual_workspace_and_folder_names_are_preserved_and_reported(provider):
    p, host = provider
    bound = p.allocate("unit", "unit-branch", "Story", "allocation", group="epic")
    first = project_display(p, bound)
    host.workspaces[0]["name"] = "Owner workspace"
    host.folder_settings[0]["displayName"] = "Owner folder"
    writes = len(host.folder_writes)
    result = project_display(p, bound, title="#529 · New title", group_title="New epic")
    assert result["title_state"] == result["group_display"]["state"] == "manual-override"
    assert host.workspaces[0]["name"] == "Owner workspace"
    assert host.folder_settings[0]["displayName"] == "Owner folder"
    assert len(host.folder_writes) == writes and result["folder"] == first["folder"]


def test_native_folder_lost_ack_is_confirmed_once_by_readback(provider):
    p, host = provider
    bound = p.allocate("unit", "unit-branch", "Story", "allocation", group="epic")
    host.lose_folder_reply = True
    assert project_display(p, bound)["group_display"]["state"] == "confirmed"
    assert project_display(p, bound)["group_display"]["state"] == "confirmed"
    assert len(host.folder_writes) == 1


def test_operator_task_title_remains_owned_across_status_and_link_refresh(provider):
    p, host = provider
    bound = p.allocate("unit", "unit-branch", "Story", "allocation")
    task_id = p.reflect_task(bound, campaign_id="campaign", title="Story", state="running")
    host.tasks[0]["title"] = "Owner task name"
    details = {}
    assert (
        p.reflect_task(
            bound,
            campaign_id="campaign",
            title="Changed story",
            state="delivered",
            links=("https://github.com/a/b/pull/1",),
            details=details,
        )
        == task_id
    )
    assert host.tasks[0]["title"] == "Owner task name"
    assert host.tasks[0]["prUrl"] == "https://github.com/a/b/pull/1"
    assert details == {"title_state": "manual-override", "observed_title": "Owner task name"}


def test_stopped_task_projection_does_not_create_missing_task(provider):
    from bmad_loop.campaign_paradev import ReflectionError

    p, host = provider
    bound = p.allocate("unit", "unit-branch", "Story", "allocation")
    with pytest.raises(ReflectionError, match="does not create"):
        p.reflect_task(bound, campaign_id="campaign", title="Story", state="draft", create=False)
    assert host.tasks == []


def test_regroup_replaces_only_exact_owned_tag_and_retains_old_settings(provider):
    p, host = provider
    bound = p.allocate("unit", "unit-branch", "Story", "allocation", group="story")
    first = project_display(p, bound)
    foreign = "bmad-someone-elses-tag"
    host.workspaces[0]["tags"] += ",owner-folder," + foreign
    second = project_display(p, dataclasses.replace(bound, group="epic"))
    assert set(host.workspaces[0]["tags"].split(",")) == {second["folder"], "owner-folder", foreign}
    assert second["folder"] != first["folder"]
    assert {row["tag"] for row in host.folder_settings} == {first["folder"], second["folder"]}


@pytest.fixture
def stopped_display(provider, tmp_path):
    p, host = provider
    bound = p.allocate("unit", "unit-branch", "Story", "allocation", group="legacy-story-group")
    spec = UnitSpec(
        "unit", "rem", "Supply chain", "Benchmark schedule", selector="303.9", initiative="REM"
    )
    unit = CampaignUnit(
        spec,
        state=UnitState.DRAFT,
        bindings={
            "native": dataclasses.asdict(bound),
            "workspace": bound.path,
            "branch": bound.branch,
            "issue": {"number": 529},
        },
    )
    state = CampaignSnapshot(
        "campaign",
        "owner/repo",
        CampaignSpec(
            "pilot",
            units=(spec,),
            paradev=ParadevSpec(
                reflect=True, project_id="project", worktree_root=str(p.allowed_root)
            ),
        ),
        root=str(p.repository),
        common_dir=bound.common_dir,
        units={unit.id: unit},
        phase="stopped",
        generation=1,
        config_revision=8,
    )
    store = CampaignStore(tmp_path / "store")
    state = store.create(state)

    def run(**kwargs):
        return refresh(
            store,
            state.id,
            provider_factory=lambda **_: p,
            mux_factory=lambda **_: SimpleNamespace(),
            **kwargs,
        )

    return store, state, p, host, run


def test_stopped_preview_and_apply_use_existing_workspace_without_launch(stopped_display):
    store, state, p, host, run = stopped_display
    calls = copy.deepcopy(host.calls)
    paths = sorted(str(path) for path in p.root.rglob("*"))
    result = run(preview=True, expected_revision=state.revision)
    assert (
        result["workspaces"][0]["workspace"]["requested_title"]
        == "#529 · 303.9 · Benchmark schedule"
    )
    assert store.load(state.id).revision == state.revision
    assert host.calls == calls and not host.folder_writes
    assert sorted(str(path) for path in p.root.rglob("*")) == paths
    applied = run(expected_revision=state.revision)
    assert applied["ok"] and store.load(state.id).phase == "stopped"
    after = store.load(state.id)
    assert after.config_revision == 8 and after.generation == state.generation
    assert after.units["unit"].attempt == 0 and after.units["unit"].claim is None
    assert after.units["unit"].bindings["native"] == state.units["unit"].bindings["native"]
    assert len(host.workspaces) == 1
    assert all(call[:2] == ["workspaces", "update"] for call in host.calls[len(calls) :])
    assert all(
        action.state == "confirmed" and action.kind == "reflection" for action in after.outbox
    )


def test_refresh_refuses_running_and_stale_revision_before_native_writes(stopped_display):
    store, state, _, host, run = stopped_display
    with pytest.raises(RevisionConflict):
        run(expected_revision=state.revision + 1)
    state.phase = "active"
    store.save(state, state.revision)
    with pytest.raises(CampaignError, match="stopped"):
        run()
    assert not host.folder_writes


def test_stopped_display_reconciles_exact_unknown_effect_without_duplicate(
    stopped_display, monkeypatch
):
    store, state, _, host, run = stopped_display
    original = host.rpc
    unavailable = True

    def rpc(method, value, **kwargs):
        nonlocal unavailable
        if method == "tagFolders.upsert" and unavailable:
            unavailable = False
            raise OSError("native settings temporarily unavailable")
        return original(method, value, **kwargs)

    monkeypatch.setattr(host, "rpc", rpc)
    first = run()
    assert first["ok"] is False
    pending = store.load(state.id)
    action = next(a for a in pending.outbox if a.kind == "reflection")
    assert action.state == "sent_unknown" and not pending.units["unit"].bindings.get("reflection")
    second = run(expected_revision=pending.revision)
    final = store.load(state.id)
    assert second["ok"]
    assert [(a.id, a.state) for a in final.outbox] == [(action.id, "confirmed")]
    assert final.units["unit"].bindings["reflection"]["workspace"]["effect_id"] == action.id
    assert len(host.workspaces) == 1 and len(host.folder_settings) == 1


def test_private_display_projection_requires_exact_durable_or_pending_effect(stopped_display):
    from bmad_loop.adapters.paradev_runtime import private_json, read_private_json

    store, state, provider, _, run = stopped_display
    assert run()["ok"]
    bound = store.load(state.id).units["unit"].bindings["native"]
    path = provider.root / "workspaces" / (bound["workspace_id"] + ".json")
    changed = read_private_json(path)
    changed["effect_id"] = "other-effect"
    private_json(path, changed)
    result = run()
    assert result["ok"] is False
    assert "durable effect ownership" in result["workspaces"][0]["error"]


def test_legacy_membership_changes_only_under_explicit_regroup_revision(stopped_display):
    _, state, _, _, _ = stopped_display
    unit = state.units["unit"]
    assert desired_group_identity(state, unit) == "legacy-story-group"
    state.bindings["paradev_regroup"] = {
        "config_revision": 9,
        "from_group_by": "story",
        "to_group_by": "epic",
        "previous_groups": {unit.id: "legacy-story-group"},
    }
    assert desired_group_identity(state, unit) == "legacy-story-group"
    state.config_revision = 9
    assert desired_group_identity(state, unit) != "legacy-story-group"
    peer = CampaignUnit(
        dataclasses.replace(unit.spec, id="peer"), bindings=copy.deepcopy(unit.bindings)
    )
    peer.bindings["native"]["unit_id"] = peer.id
    state.bindings["paradev_regroup"]["previous_groups"][peer.id] = "other-story"
    assert desired_group_identity(state, peer) == desired_group_identity(state, unit)


def test_parallel_sessions_and_closed_bridge_never_become_workspace_identity(stopped_display):
    _, state, _, _, _ = stopped_display
    unit = state.units["unit"]
    unit.claim = WorkerClaim(unit.id, 1, 1, "launch")
    unit.bindings["managed_communication"] = {
        "name": "bole",
        "kind": "bridge",
        "state": "accepting",
        "target": {"unit_id": unit.id, "launch_id": "launch", "generation": 1},
    }
    a = {"phase": "develop", "hook": {"hcom_name": "tina"}, "native": {"definitionId": "codex"}}
    b = {"phase": "review", "hook": {"hcom_name": "nava"}, "native": {"definitionId": "claude"}}
    assert "native @tina" in unit_session_title(unit, a, snapshot=state)
    assert "native @nava" in unit_session_title(unit, b, snapshot=state)
    assert "control @bole" in unit_session_title(unit, b, snapshot=state)
    assert "past control @bole" in unit_session_title(unit, b, snapshot=state, historical=True)
    unit.bindings["managed_communication"]["state"] = "closed"
    assert "closed control @bole" in unit_session_title(unit, b, snapshot=state, historical=True)
    assert "@" not in work_title(unit, snapshot=state) and "@" not in group_title(state, unit)
    unit.claim = dataclasses.replace(unit.claim, launch_id="successor", generation=2)
    assert "bole" not in unit_session_title(unit, a, snapshot=state)


def test_exact_reserved_intake_issue_labels_control_workspace(stopped_display):
    _, state, _, _, _ = stopped_display
    unit = state.units["unit"]
    unit.bindings.pop("issue")
    unit.bindings["intake_control"] = {"input_versions": ["v1"]}
    record = {
        "control_unit": unit.id,
        "observation": {
            "kind": "issue",
            "repository": "owner/repo",
            "number": 529,
            "url": "https://github.com/owner/repo/issues/529",
        },
    }
    state.bindings["intake_inbox"] = {"records": {"v1": record}}
    assert work_title(unit, snapshot=state).startswith("#529 ·")
    record["control_unit"] = "other"
    assert not work_title(unit, snapshot=state).startswith("#529")


def test_cli_stopped_reflection_preview_is_real_dispatch(stopped_display, monkeypatch, capsys):
    from bmad_loop import campaign_reflect, campaign_runtime, cli

    store, state, p, _, _ = stopped_display
    original = campaign_reflect.refresh
    monkeypatch.setattr(campaign_runtime, "store_for", lambda _: store)
    monkeypatch.setattr(
        campaign_reflect,
        "refresh",
        lambda *a, **k: original(
            *a, **k, provider_factory=lambda **_: p, mux_factory=lambda **_: SimpleNamespace()
        ),
    )
    assert cli.main(["campaign", "reflect", state.id, "--preview", "--json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["command"] == "campaign.reflect" and result["preview"] is True
    assert store.load(state.id).revision == state.revision


def test_ended_portfolio_view_uses_exact_attempt_and_retains_earned_receipt(
    stopped_display, prepared
):
    from bmad_loop.campaign_headless import read_json, write_json
    from bmad_loop.campaign_model import OutboxAction
    from bmad_loop.campaign_reflect import _historical_views

    store, state, provider, _, _ = stopped_display
    unit = state.units["unit"]
    runner, _, original, candidate, capability = prepared
    request = dataclasses.replace(original, id="portfolio", unit="")
    directory = store.directory(state.id)
    path = runner.prepare(
        directory / "jobs" / request.id / "1",
        request,
        candidate,
        capability,
        launch_id="portfolio-launch",
        generation=1,
        presentation={
            "workspace": unit.bindings["workspace"],
            "workspace_id": unit.bindings["native"]["workspace_id"],
        },
    )
    runner.mux.set_display_title = lambda _, title, **kwargs: title
    runner.launch(path)
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        ended = runner.observe(path)
        if ended["state"] == "finished":
            break
        time.sleep(0.02)
    assert ended["state"] == "finished"
    envelope = read_json(path)
    coordinates = {key: envelope[key] for key in ("launch_id", "generation", "request_digest")}
    coordinates["attempt"] = 1
    payload = {"job_id": request.id, "job_binding": coordinates, "candidate": envelope["selection"]}
    state.outbox.extend(
        [
            OutboxAction("portfolio-launch", "reasoning-launch", "", payload, 1, state="confirmed"),
            OutboxAction(
                "portfolio-observe",
                "reasoning-observe",
                "",
                payload,
                1,
                state="confirmed",
                receipt={**ended, "effect_binding": coordinates},
            ),
        ]
    )
    runner.mux._address = lambda _: {
        "workspace": unit.bindings["native"]["workspace_id"],
        "cwd": unit.bindings["workspace"],
    }
    runner.mux.native_identity = lambda _: {"agentSessionId": "native-session"}
    runner.mux._ensure_pane = lambda *_: pytest.fail("History must not create panes")
    before = (path.parent / "receipt.json").read_bytes()
    preview = _historical_views(state, unit, directory, provider, runner.mux, preview=True)
    assert preview[0]["state"] == "preview" and "history" in preview[0]["title"]
    assert "Portfolio" in preview[0]["title"] and "#529" not in preview[0]["title"]
    result = _historical_views(state, unit, directory, provider, runner.mux, preview=False)
    assert result[0]["state"] == "confirmed"
    assert result[0]["view"]["native"] == {"agentSessionId": "native-session"}
    assert (path.parent / "receipt.json").read_bytes() == before
    envelope["generation"] = 2
    write_json(path, envelope)
    refused = _historical_views(state, unit, directory, provider, runner.mux, preview=False)
    assert refused[0]["state"] == "unavailable"
    assert "envelope differs" in refused[0]["reason"]


@pytest.mark.parametrize("allocated_campaign", [(True, False)], indirect=True)
def test_pending_regroup_survives_second_configuration_and_proof_renewal(
    allocated_campaign, monkeypatch
):
    from bmad_loop import campaign_reconfiguration as config
    from bmad_loop import campaign_requalification as renewal

    case, key, _, host = allocated_campaign
    store, state, _, manifest = case
    monkeypatch.setattr(config, "installation", renewal.installation)
    assert refresh(store, state.id, mux_factory=lambda **_: SimpleNamespace())["ok"]
    state = store.load(state.id)
    original = copy.deepcopy(state.units[key].bindings["native"])
    old_group = state.units[key].bindings["reflection"]["workspace"]["group"]
    state = config.reconfigure(
        store,
        state.id,
        dataclasses.replace(
            state.spec, paradev=dataclasses.replace(state.spec.paradev, group_by="story")
        ),
        expected_revision=state.revision,
        reason="Regroup existing work",
    )
    unrelated = state.clone()
    unrelated.bindings["paradev_regroup"]["previous_groups"][key] = "unrelated-group"
    with pytest.raises(CampaignError, match="prepared reflection differs"):
        renewal._startup_only(unrelated, store.directory(state.id))
    state = config.reconfigure(
        store,
        state.id,
        dataclasses.replace(
            state.spec,
            reasoning=dataclasses.replace(state.spec.reasoning, capabilities=str(manifest)),
        ),
        expected_revision=state.revision,
        reason="Renew selected route manifest while display is pending",
    )
    state = renewal.requalify(
        store,
        state.id,
        manifest,
        expected_revision=state.revision,
        reason="Renew installed proof before display applies",
    )
    assert state.bindings["paradev_regroup"]["config_revision"] == state.config_revision
    assert desired_group_identity(state, state.units[key]) != old_group
    assert refresh(store, state.id, mux_factory=lambda **_: SimpleNamespace())["ok"]
    state = store.load(state.id)
    projected = state.units[key].bindings["reflection"]["workspace"]
    assert projected["group"] != old_group
    state = renewal.requalify(
        store,
        state.id,
        manifest,
        expected_revision=state.revision,
        reason="Renew installed proof after display applies",
    )
    assert state.units[key].bindings["native"] == original
    assert state.units[key].bindings["reflection"]["workspace"] == projected
    assert state.units[key].attempt == 0 and len(host.workspaces) == 1


def test_new_allocation_and_display_share_frozen_effective_initiative(
    campaign, tmp_path, monkeypatch
):
    from bmad_loop.campaign_reflection_flow import new_group_identity

    store, state, template, manifest = campaign
    key = next(iter(state.units))
    state.bindings["source_observations"] = {key: {"effective_initiative": "Resolved initiative"}}
    state = store.save(state, state.revision)
    # Reuse the real-Git production preparation harness with an imported source
    # observation, as supplied before allocation by the source importer.
    case, key, _, host = allocated_campaign.__wrapped__(
        (store, state, template, manifest),
        tmp_path,
        monkeypatch,
        SimpleNamespace(param=(True, False)),
    )
    state = case[1]
    unit = state.units[key]
    assert not unit.spec.initiative
    assert unit.bindings["native"]["group"] == new_group_identity(state, unit)
    assert host.workspaces[0]["name"] == work_title(unit, snapshot=state)
    assert refresh(store, state.id, mux_factory=lambda **_: SimpleNamespace())["ok"]
    projected = store.load(state.id).units[key].bindings["reflection"]["workspace"]
    assert projected["group"] == unit.bindings["native"]["group"]
    assert projected["group_title"].startswith("Resolved initiative ·")
