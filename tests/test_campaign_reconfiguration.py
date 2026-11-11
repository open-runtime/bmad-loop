"""Startup edits preserve history and cannot silently change delivery authority."""

import copy
from dataclasses import replace
from pathlib import Path

import pytest
from test_campaign_requalification import allocated_campaign as allocated_campaign
from test_campaign_requalification import campaign as campaign
from test_campaign_requalification import prepared as prepared

from bmad_loop import campaign_reconfiguration as config
from bmad_loop.campaign_execution import CampaignExecution
from bmad_loop.campaign_model import (
    CampaignError,
    RoleSpec,
    UnitState,
    snapshot_from_dict,
    snapshot_to_dict,
    spec_from_dict,
    spec_to_dict,
)
from bmad_loop.campaign_store import RevisionConflict


@pytest.fixture(autouse=True)
def installation(monkeypatch):
    monkeypatch.setattr(
        config,
        "installation",
        lambda: {
            "qualified": True,
            "commit": "b" * 40,
            "installation_kind": "git",
        },
    )


def change(case, **kwargs):
    store, before, _, _ = case
    proposed = kwargs.pop(
        "proposed",
        replace(before.spec, paradev=replace(before.spec.paradev, group_by="story")),
    )
    return config.reconfigure(
        store,
        before.id,
        proposed,
        expected_revision=kwargs.pop("expected_revision", before.revision),
        reason="Use story folders without changing work",
        **kwargs,
    )


def test_preview_has_no_published_revision_or_files(campaign):
    store, before, _, _ = campaign
    directory = store.directory(before.id)
    files = {p.relative_to(directory): p.read_bytes() for p in directory.rglob("*.json")}
    result = change(campaign, preview=True)
    assert result["preview"] is True and result["group_by"] == "story"
    assert store.load(before.id).revision == before.revision
    assert {p.relative_to(directory): p.read_bytes() for p in directory.rglob("*.json")} == files
    assert not (directory / "configuration-revisions").exists()


def test_group_revision_preserves_old_template_work_holds_and_receipts(campaign):
    store, before, template, _ = campaign
    old_path = Path(before.bindings["execution_template"]["path"])
    old_bytes = old_path.read_bytes()
    jobs = copy.deepcopy(before.jobs)
    after = change(campaign)
    assert after.config_revision == before.config_revision + 1
    assert after.revision == before.revision + 1
    assert after.phase == "stopped" and after.generation == before.generation
    assert after.units == before.units
    assert after.spec.paradev.group_by == "story"
    assert old_path.read_bytes() == old_bytes
    binding = after.bindings["execution_template"]
    new_template = CampaignExecution.load(Path(binding["path"]), binding["digest"])
    assert new_template.policy == template.policy and new_template.grants == template.grants
    retained_jobs = {row["id"]: row for row in store.jobs(after.id)}
    for key, row in jobs.items():
        assert retained_jobs[key]["request"] == row["request"]
        assert retained_jobs[key]["state"] == "stale"
    record = after.bindings["configuration_revisions"][0]
    assert spec_from_dict(record["previous_spec"]) == before.spec
    assert record["previous_template"] == before.bindings["execution_template"]


@pytest.mark.parametrize("allocated_campaign", [(False, False), (True, True)], indirect=True)
def test_group_change_reuses_confirmed_workspaces_without_native_calls(allocated_campaign):
    case, key, _, host = allocated_campaign
    calls = copy.deepcopy(host.calls)
    before = case[1]
    after = change(case)
    assert after.units[key] == before.units[key]
    assert host.calls == calls
    assert after.units[key].state == UnitState.DRAFT


@pytest.mark.parametrize(
    "field,value",
    [
        ("max_workers", 2),
        ("target", "other"),
        ("verify_commands", (("echo", "skip"),)),
    ],
)
def test_cannot_change_scope_policy_or_global_limits(campaign, field, value):
    with pytest.raises(CampaignError, match="scope, policy and limits"):
        change(campaign, proposed=replace(campaign[1].spec, **{field: value}))
    assert campaign[0].load(campaign[1].id).revision == campaign[1].revision


def test_cannot_raise_role_limits(campaign):
    with pytest.raises(CampaignError, match="role limits"):
        change(
            campaign,
            proposed=replace(campaign[1].spec, roles=(("triage", RoleSpec(max_sessions=99)),)),
        )


def test_cannot_remove_existing_native_route_to_fall_back_to_legacy(campaign, prepared):
    _, before, template, _ = campaign
    candidate = prepared[3]
    spec = replace(
        before.spec,
        roles=(
            (
                "triage",
                RoleSpec(
                    profile=candidate.profile,
                    model=candidate.model,
                    effort=candidate.effort,
                ),
            ),
        ),
    )
    previous_routes = before.config_snapshot["reasoning_capabilities"]
    with pytest.raises(CampaignError, match="exact native qualification"):
        config._profiles(spec, spec, template, Path(before.root), {}, previous_routes)


def test_unqualified_preferred_candidate_remains_visible_with_qualified_fallback(
    campaign, prepared
):
    from bmad_loop.campaign_model import RoleCandidate

    _, before, template, _ = campaign
    candidate = prepared[3]
    missing = RoleCandidate(candidate.profile, "unqualified-preferred-model", candidate.effort)
    proposed = replace(
        before.spec,
        roles=(("triage", RoleSpec(pool="fallback")),),
        role_pools=(("fallback", (missing, candidate)),),
    )
    routes = before.config_snapshot["reasoning_capabilities"]
    profiles, exclusions = config._profiles(
        before.spec, proposed, template, Path(before.root), routes, routes
    )
    assert "campaign:" + candidate.profile in profiles
    assert len(exclusions["triage"]) == 1 and missing.model in exclusions["triage"][0]
    assert proposed.candidates("triage") == (missing, candidate)


@pytest.mark.parametrize("mutation", ["running", "attempt", "publication"])
def test_requires_unstarted_quiescent_units(campaign, mutation):
    store, before, template, manifest = campaign
    if mutation == "running":
        before.phase = "active"
    else:
        unit = next(iter(before.units.values()))
        if mutation == "attempt":
            unit.attempt = 1
        else:
            unit.bindings["pr"] = {"number": 9}
    before = store.save(before, before.revision)
    with pytest.raises(CampaignError, match="stopped|prior work"):
        change((store, before, template, manifest))
    assert store.load(before.id).revision == before.revision


def test_stale_revision_cannot_change_config(campaign):
    with pytest.raises(RevisionConflict):
        change(campaign, expected_revision=campaign[1].revision - 1)


@pytest.mark.parametrize("allocated_campaign", [(True, False)], indirect=True)
def test_initiative_group_requires_frozen_identity(allocated_campaign):
    case, _, _, _ = allocated_campaign
    before = case[1]
    proposed = replace(
        before.spec,
        paradev=replace(before.spec.paradev, group_by="initiative"),
    )
    with pytest.raises(CampaignError, match="frozen initiative identity"):
        change(case, proposed=proposed)
    assert case[0].load(before.id).config_revision == before.config_revision


def test_reconfigure_cli_preview_and_apply(campaign, tmp_path, monkeypatch, capsys):
    import json

    from bmad_loop import campaign_runtime, cli

    store, before, _, _ = campaign
    path = tmp_path / "proposed.toml"
    path.write_text(
        'schema_version = 1\nname = "Startup"\n'
        '[[units]]\nid = "s:project:u"\nsource = "s"\nepic = "epic"\ntitle = "Story"\n'
        'acceptance = ["Keep all obligations"]\n'
        '[reasoning]\nenabled = true\n[paradev]\ngroup_by = "story"\n'
    )
    monkeypatch.setattr(campaign_runtime, "store_for", lambda *_: store)
    args = [
        "campaign",
        "reconfigure",
        before.id,
        "--spec",
        str(path),
        "--expected-revision",
        str(before.revision),
        "--reason",
        "Owner grouping change",
        "--json",
    ]
    assert cli.main([*args, "--preview"]) == 0
    preview = json.loads(capsys.readouterr().out)["proposal"]
    assert preview["preview"] and preview["config_revision"] == before.config_revision + 1
    assert store.load(before.id).revision == before.revision
    assert cli.main(args) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["campaign"]["phase"] == "stopped"
    assert store.load(before.id).config_revision == before.config_revision + 1


def test_legacy_snapshot_default_and_new_spec_default_differ(campaign):
    raw = snapshot_to_dict(campaign[1])
    raw["spec"]["paradev"].pop("group_by")
    assert snapshot_from_dict(raw).spec.paradev.group_by == "story"
    assert spec_from_dict(raw["spec"]).paradev.group_by == "epic"


@pytest.mark.parametrize("limit", [0, -1, True, 1.5, None, 1024 * 1024 * 1024 + 1])
def test_history_budget_is_finite_and_positive(campaign, limit):
    raw = spec_to_dict(campaign[1].spec)
    raw["reasoning"]["history_max_bytes"] = limit
    with pytest.raises(CampaignError, match="history_max_bytes"):
        spec_from_dict(raw)


def test_history_budget_roundtrips_and_cannot_change_at_startup(campaign):
    before = campaign[1]
    assert (
        spec_from_dict(spec_to_dict(before.spec)).reasoning.history_max_bytes == 512 * 1024 * 1024
    )
    with pytest.raises(CampaignError, match="scope, policy and limits"):
        change(
            campaign,
            proposed=replace(
                before.spec,
                reasoning=replace(
                    before.spec.reasoning,
                    history_max_bytes=1024 * 1024 * 1024,
                ),
            ),
        )


def test_stage_pool_mapping_roundtrips_and_requires_complete_known_pools(campaign):
    raw = spec_to_dict(campaign[1].spec)
    raw["role_pools"] = {"summary": {"candidates": [{"profile": "codex", "model": "gpt-6.1-sol"}]}}
    routes = {
        stage: "summary"
        for stage in (
            "leaf",
            "intermediate",
            "final",
            "constraint",
            "recovery",
            "audit",
            "constraint_audit",
            "recovery_audit",
        )
    }
    raw["reasoning"]["summary_routes"] = routes
    spec = spec_from_dict(raw)
    assert spec_to_dict(spec)["reasoning"]["summary_routes"] == routes
    raw["reasoning"]["summary_routes"] = {"leaf": "summary"}
    with pytest.raises(CampaignError, match="eight"):
        spec_from_dict(raw)
    raw["reasoning"]["summary_routes"] = {**routes, "final": "missing"}
    with pytest.raises(CampaignError, match="unknown pool"):
        spec_from_dict(raw)
