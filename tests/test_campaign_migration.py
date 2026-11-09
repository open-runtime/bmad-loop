"""Migration never mistakes observation for release of a legacy actor's lease."""

import json
from dataclasses import asdict, replace

import pytest
from conftest import git

from bmad_loop import campaign_migration as migration
from bmad_loop.campaign_model import CampaignSnapshot, CampaignSpec, CampaignUnit, Hold, UnitSpec
from bmad_loop.campaign_store import CampaignStore


class Gateway:
    repository = "example/repo"

    def pulls(self):
        return [
            {
                "number": 9,
                "head": {"sha": "abc", "ref": "work"},
                "base": {"sha": "def"},
                "draft": True,
                "state": "open",
                "html_url": "https://github.com/example/repo/pull/9",
            }
        ]


def service(project, common):
    return migration.ServiceInventory("paradev", str(common), 100, "service-1", ())


def observed(project, **kwargs):
    return migration.inventory(
        project.project, gateway=Gateway(), observers=(service,), clock=lambda: 100, **kwargs
    )


def replacement(inv):
    return {
        "schema_version": 1,
        "common_dir": inv.common_dir,
        "config_digest": "qualified-config",
        "capabilities": {
            area: {"ready": True} for area in ("planning", "execution", "publication")
        },
    }


def manifest(inv, *selections, disable=()):
    return migration.MigrationManifest(
        1, inv.common_dir, inv.digest, "authenticated-owner-request", tuple(selections), disable
    )


def item(kind="automation", **kwargs):
    details = {
        "admissions_open": False,
        "fence_revision": "fence-2",
        "active_workers": 0,
        "campaign_conflict": True,
    }
    details.update(kwargs.pop("details", {}))
    return migration.MigrationItem(
        "auto:legacy",
        kind,
        "r1",
        "paradev",
        state="stopped",
        liveness="dead",
        details=details,
        **kwargs,
    )


def with_service(inv, actor):
    receipt = migration.ServiceInventory("paradev", inv.common_dir, 100, "r1", (actor,))
    return replace(inv, items=(*inv.items, actor), services=(receipt,))


def select(actor, disposition="replace", units=()):
    return migration.MigrationSelection(actor.id, actor.revision, disposition, units)


def test_read_only_inventory_covers_worktrees_legacy_holds_pr_and_pid(project, monkeypatch):
    run_dir = project.project / ".bmad-loop/runs/legacy-run"
    run_dir.mkdir(parents=True)
    state = {
        "run_id": "legacy-run",
        "project": str(project.project),
        "source": "stories",
        "paused_reason": "owner decision",
        "tasks": {"story-1": {"phase": "dev", "branch": "legacy"}},
    }
    (run_dir / "state.json").write_text(json.dumps(state))
    (run_dir / "engine.pid").write_text("123 44.1")
    monkeypatch.setattr(migration.runs, "engine_liveness", lambda path: "alive")
    before = {p: p.read_bytes() for p in run_dir.iterdir()}
    head = git(project.project, "rev-parse", "HEAD")
    result = observed(project)
    assert result.complete
    legacy = next(row for row in result.items if row.kind == "legacy-run")
    assert legacy.liveness == "alive" and legacy.ownership == "owned"
    assert legacy.units == ("story-1",)
    assert legacy.details["hold"] == "owner decision"
    assert legacy.details["launcher_preservation_required"] is True
    assert any(row.kind == "worktree" and row.ownership == "unknown" for row in result.items)
    assert any(row.id == "pr:example/repo:9" for row in result.items)
    assert git(project.project, "rev-parse", "HEAD") == head
    assert {p: p.read_bytes() for p in run_dir.iterdir()} == before


def test_missing_observers_and_symlinked_run_are_visible_not_empty_success(project, tmp_path):
    missing = migration.inventory(project.project, clock=lambda: 100)
    assert not missing.complete
    assert "external-schedulers:observer-not-configured" in missing.unavailable
    run_dir = project.project / ".bmad-loop/runs/legacy"
    run_dir.mkdir(parents=True)
    external = tmp_path / "external.json"
    external.write_text(json.dumps({"project": str(project.project)}))
    (run_dir / "state.json").symlink_to(external)
    result = observed(project)
    assert any("run:" in fault for fault in result.unavailable)
    assert external.read_text() == json.dumps({"project": str(project.project)})


def test_native_claim_inventory_uses_exact_repository_and_preserves_snapshot(project, tmp_path):
    inv = observed(project)
    unit = UnitSpec("s:project:u", "s", "epic", "Unit")
    snapshot = CampaignSnapshot(
        "existing",
        "example/repo",
        CampaignSpec("Existing", units=(unit,)),
        {unit.id: CampaignUnit(unit)},
        common_dir=inv.common_dir,
        root=inv.project,
    )
    root = tmp_path / "native-state"
    CampaignStore(root).create(snapshot)
    path = root / "existing/snapshot.json"
    before = path.read_bytes()
    result = observed(project, campaign_root=root)
    entry = next(row for row in result.items if row.kind == "native-unit")
    assert entry.units == (unit.id,) and entry.details["admissions_open"] is True
    assert path.read_bytes() == before


@pytest.mark.parametrize("fault", ["stale", "wrong-common", "partial", "duplicate"])
def test_service_observation_must_be_fresh_bound_complete_unambiguous(project, fault):
    def observer(root, common):
        return migration.ServiceInventory(
            "paradev",
            "/other" if fault == "wrong-common" else str(common),
            0 if fault == "stale" else 100,
            "r1",
            (item(), item()) if fault == "duplicate" else (),
            complete=fault != "partial",
        )

    result = migration.inventory(
        project.project,
        gateway=Gateway(),
        observers=(observer,),
        clock=lambda: 200 if fault == "stale" else 100,
    )
    assert not result.complete
    assert result.unavailable


def test_explicit_fenced_automation_replacement_keeps_unrelated_job(project):
    actor = item()
    inv = with_service(observed(project), actor)
    unrelated = replace(item(), id="auto:unrelated", details={"campaign_conflict": False})
    inv = replace(
        inv,
        items=(*inv.items, unrelated),
        services=(replace(inv.services[0], items=(actor, unrelated)),),
    )
    selection = manifest(inv, select(actor), disable=(actor.id,))
    result = migration.validate_manifest(inv, selection, replacement(inv), now=100)
    assert result.qualified
    assert result.replaceable_automations == (actor.id,)
    assert not result.held_units
    assert unrelated.id not in result.replaceable_automations


@pytest.mark.parametrize(
    "change,expected",
    [
        ({"admissions_open": True}, "fence not proven"),
        ({"fence_revision": ""}, "fence not proven"),
        ({"active_workers": 1}, "in-flight"),
        ({"active_workers": None}, "in-flight"),
    ],
)
def test_live_or_unknown_scheduler_never_qualifies_disabling(project, change, expected):
    actor = item(details=change, units=("s:project:u",))
    inv = with_service(observed(project), actor)
    result = migration.validate_manifest(
        inv, manifest(inv, select(actor), disable=(actor.id,)), replacement(inv), now=100
    )
    assert not result.qualified
    assert result.replaceable_automations == ()
    assert result.held_units == ("s:project:u",)
    assert any(expected in problem for problem in result.problems)


def test_local_fake_fence_cannot_replace_an_authenticated_readback(project):
    actor = item()
    inv = observed(project)
    inv = replace(inv, items=(*inv.items, actor))
    result = migration.validate_manifest(
        inv, manifest(inv, select(actor), disable=(actor.id,)), replacement(inv), now=100
    )
    assert not result.qualified
    assert any("fence not proven" in problem for problem in result.problems)


def test_preserve_disposition_does_not_bypass_a_conflicting_open_scheduler(project):
    actor = item("scheduler", details={"admissions_open": True})
    inv = with_service(observed(project), actor)
    result = migration.validate_manifest(
        inv, manifest(inv, select(actor, "preserve")), replacement(inv), now=100
    )
    assert not result.qualified
    assert any("no verified fence" in problem for problem in result.problems)


@pytest.mark.parametrize(
    "mode", ["unselected", "wrong-revision", "stale-inventory", "replacement-unready", "other-repo"]
)
def test_migration_rejects_changed_or_unqualified_authority(project, mode):
    actor = item()
    inv = with_service(observed(project), actor)
    choice = select(actor)
    if mode == "wrong-revision":
        choice = replace(choice, revision="other")
    selected = manifest(inv, *(() if mode == "unselected" else (choice,)), disable=(actor.id,))
    if mode == "stale-inventory":
        selected = replace(selected, inventory_digest="old")
    ready = replacement(inv)
    if mode == "replacement-unready":
        ready["capabilities"]["publication"]["ready"] = False
    if mode == "other-repo":
        ready["common_dir"] = "/other/repo"
    result = migration.validate_manifest(inv, selected, ready, now=100)
    assert not result.qualified and not result.replaceable_automations


def test_idle_dead_worktree_is_not_authority_to_adopt(project):
    inv = observed(project)
    worktree = next(row for row in inv.items if row.kind == "worktree")
    result = migration.validate_manifest(
        inv, manifest(inv, select(worktree, "adopt", ("s:project:u",))), replacement(inv), now=100
    )
    assert not result.qualified
    assert result.held_units == ("s:project:u",)


def test_adopt_requires_service_release_exact_preserved_binding(project):
    actor = migration.MigrationItem(
        "workspace:old",
        "workspace",
        "r1",
        "paradev",
        "clean",
        "dead",
        "unowned",
        ("s:project:u",),
        {
            "preserved_clean": True,
            "resumable_binding_verified": True,
            "head": "a" * 40,
            "binding_revision": "b1",
        },
    )
    inv = with_service(observed(project), actor)
    result = migration.validate_manifest(
        inv, manifest(inv, select(actor, "adopt")), replacement(inv), now=100
    )
    assert result.qualified and result.adopted_units == actor.units
    assert result.replaceable_automations == ()


def test_drain_keeps_live_units_reserved_and_never_releases_operator_hold(project):
    actor = item("process", units=("s:project:u",), details={})
    actor = replace(actor, liveness="alive", state="unfinished")
    inv = with_service(observed(project), actor)
    result = migration.validate_manifest(
        inv, manifest(inv, select(actor, "drain")), replacement(inv), now=100
    )
    unit = UnitSpec("s:project:u", "s", "e", "Unit")
    snapshot = CampaignSnapshot(
        "native",
        "example/repo",
        CampaignSpec("Native", units=(unit,)),
        {unit.id: CampaignUnit(unit, hold=Hold("operator", "wait", id="owner-hold"))},
        common_dir=inv.common_dir,
    )
    migration.record_qualification(snapshot, result)
    assert snapshot.units[unit.id].hold.id == "owner-hold"
    assert snapshot.bindings["migration"]["held_units"] == (unit.id,)
    assert snapshot.events[-1]["kind"] == "migration-qualified"
    assert actor.liveness == "alive"


def test_manifest_cannot_embed_fence_or_change_unknown_fields(project):
    inv = observed(project)
    raw = asdict(manifest(inv))
    raw["admissions_open"] = False
    with pytest.raises(migration.MigrationError, match="fields/version"):
        migration.manifest_from_dict(raw)
    raw.pop("admissions_open")
    assert migration.manifest_from_dict(raw) == manifest(inv)


def test_unavailable_inventory_cannot_claim_no_competing_work(project):
    inv = observed(project, max_items=1)
    assert not inv.complete and inv.truncated
    result = migration.validate_manifest(inv, manifest(inv), replacement(inv), now=100)
    assert not result.qualified
