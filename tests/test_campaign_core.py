"""Campaign graph, persistence, fencing and parallel admission contracts."""

from __future__ import annotations

from dataclasses import asdict, replace

import pytest

from bmad_loop.campaign_model import (
    CampaignError,
    CampaignSnapshot,
    CampaignUnit,
    Hold,
    UnitSpec,
    UnitState,
    WorkerClaim,
    parse_spec,
    scopes_overlap,
    snapshot_from_dict,
    snapshot_to_dict,
    spec_from_dict,
    spec_to_dict,
    validate_graph,
)
from bmad_loop.campaign_scheduler import CapacityObservation, admit, resource_available, select_role
from bmad_loop.campaign_store import (
    CampaignStore,
    CoordinatorMismatch,
    RevisionConflict,
    add_action,
    add_event,
    transition,
)
from bmad_loop.platform_util import LockUnavailableError
from bmad_loop.statemachine import IllegalTransition


def make_snapshot(*, workers=4, scopes=None, epics=None):
    epics = epics or ["a", "a", "b", "b"]
    scopes = scopes or [(f"src/{n}",) for n in range(len(epics))]
    units = [
        UnitSpec(f"source:{epic}:{n}", "source", epic, f"Task {n}", scopes=scopes[n], order=n)
        for n, epic in enumerate(epics)
    ]
    spec = spec_from_dict(
        {
            "schema_version": 1,
            "name": "parallel",
            "sources": [{"kind": "stories", "path": "specs"}],
            "max_workers": workers,
            "max_active_epics": 4,
            "max_in_flight_units": 12,
            "units": [asdict(unit) for unit in units],
        }
    )
    return CampaignSnapshot(
        "test-campaign",
        "example/repo",
        spec,
        {unit.id: CampaignUnit(unit, UnitState.READY) for unit in units},
    )


def claim(unit, *, resource="execution", quiescent=False):
    return WorkerClaim(unit.id, 1, 1, "launch", pid=None, resource=resource, quiescent=quiescent)


def test_spec_snapshot_roundtrip_freezes_roles_and_settings():
    snapshot = make_snapshot()
    raw = spec_to_dict(snapshot.spec)
    raw["role_pools"] = {
        "reasoning": {
            "candidates": [
                {"profile": "codex", "model": "gpt-6-astra"},
                {"profile": "claude", "model": "claude-fable-5-1"},
            ]
        }
    }
    raw["roles"] = {
        "planning": {"pool": "reasoning"},
        "conflict_repair": {
            "profile": "codex",
            "model": "gpt-6-astra",
            "timeout_s": 600,
            "max_tokens": 50000,
        },
    }
    raw["planning"] = {
        "enabled": True,
        "brief": "docs/brief.md",
        "skills": {"decomposition": "tickets-refine"},
    }
    snapshot.spec = spec_from_dict(raw)
    snapshot.units[next(iter(snapshot.units))].hold = Hold("operator", "Wait")
    snapshot.config_snapshot = {"full_policy": {"model": "saved-model"}}
    snapshot.outbox.append(add_action(snapshot.clone(), "issue", "unit", {"head": "abc"}))
    restored = snapshot_from_dict(snapshot_to_dict(snapshot))
    assert restored == snapshot
    assert restored.spec.candidates("planning")[1].model == "claude-fable-5-1"
    assert restored.spec.role("conflict_repair").timeout_s == 600
    assert restored.spec.planning.skills == (("decomposition", "tickets-refine"),)


@pytest.mark.parametrize(
    "change,match",
    [
        ({"schema_version": True}, "schema_version"),
        ({"unknown": 2}, "unknown fields"),
        ({"max_workers": True}, "positive integer"),
        ({"max_workers": 0}, "positive integer"),
        ({"communication": {"mode": "automatic"}}, "not qualified"),
        ({"roles": {"planning": {"pool": "missing"}}}, "unknown pool"),
        ({"roles": {"planning": {"pool": "x", "profile": "codex"}}}, "choose pool"),
        ({"verify_commands": ["echo hi"]}, "array"),
        ({"target": "--all"}, "options"),
        ({"max_in_flight_units": 1}, "cover max_workers"),
    ],
)
def test_spec_rejects_unsafe_or_ambiguous_configuration(change, match):
    raw = spec_to_dict(make_snapshot().spec)
    raw.update(change)
    with pytest.raises(CampaignError, match=match):
        spec_from_dict(raw)


def test_parse_toml_mutation_free(tmp_path):
    path = tmp_path / "campaign.toml"
    path.write_text(
        'schema_version = 1\nname = "test"\n[[sources]]\nkind = "stories"\npath = "specs"\n'
    )
    before = set(tmp_path.iterdir())
    spec = parse_spec(path)
    assert spec.sources[0].kind == "stories"
    assert set(tmp_path.iterdir()) == before


def test_graph_checks_dependency_and_parent_cycles_and_missing_ids():
    a = UnitSpec("s:e:a", "s", "e", "A", after=("s:e:b",))
    b = UnitSpec("s:e:b", "s", "e", "B", after=("s:e:a",))
    with pytest.raises(CampaignError, match="cycle"):
        validate_graph([a, b])
    with pytest.raises(CampaignError, match="unknown dependency"):
        validate_graph([a])
    with pytest.raises(CampaignError, match="parent cycle"):
        validate_graph([replace(a, after=(), parent=b.id), replace(b, after=(), parent=a.id)])
    with pytest.raises(CampaignError, match="duplicate"):
        validate_graph([replace(a, after=()), replace(a, after=())])


def test_same_epic_independent_units_parallelize_and_epics_rotate():
    snapshot = make_snapshot()
    result = admit(snapshot)
    assert result.units == ("source:a:0", "source:b:2", "source:a:1", "source:b:3")
    assert result.next_epic == "source::b"
    snapshot.last_epic = result.next_epic
    snapshot.spec = replace(snapshot.spec, max_workers=1)
    assert admit(snapshot).units == ("source:a:0",)
    snapshot.last_epic = "source::a"
    assert admit(snapshot).units == ("source:b:2",)


def test_parallel_units_within_one_epic_have_no_implicit_serial_ceiling():
    snapshot = make_snapshot(epics=["a"] * 4)
    assert len(admit(snapshot).units) == 4
    snapshot.spec = replace(snapshot.spec, max_workers_per_epic=2)
    decision = admit(snapshot)
    assert len(decision.units) == 2
    assert set(decision.waits.values()) == {"explicit epic worker capacity"}


def test_dependency_only_releases_after_delivery_not_cancel_or_local_done():
    snapshot = make_snapshot(epics=["a", "b"])
    a, b = snapshot.units.values()
    b.spec = replace(b.spec, after=(a.id,))
    for state in (
        UnitState.RUNNING,
        UnitState.LOCALLY_COMPLETE,
        UnitState.MERGED_PENDING_SYNC,
        UnitState.CANCELED,
    ):
        a.state = state
        assert b.id not in admit(snapshot).units
        assert admit(snapshot).waits[b.id] == "dependency not delivered"
    a.state = UnitState.DELIVERED
    assert b.id in admit(snapshot).units


def test_unrelated_units_continue_while_a_unit_owes_tracking():
    snapshot = make_snapshot()
    a = next(iter(snapshot.units.values()))
    a.state = UnitState.MERGED_PENDING_SYNC
    a.hold = Hold("tracking", "remote issue update failed")
    a.claim = claim(a, quiescent=True)
    assert len(admit(snapshot).units) == 3


def test_uncertain_launch_retains_execution_and_ownership_resources():
    snapshot = make_snapshot(workers=1)
    a = next(iter(snapshot.units.values()))
    a.state = UnitState.CLAIMED
    a.claim = claim(a)
    assert a.claim.pid is None
    assert admit(snapshot).units == ()
    assert set(admit(snapshot).waits.values()) == {"execution capacity"}
    assert not resource_available(snapshot, "execution")
    a.claim = None
    assert admit(snapshot).units == ()
    a.claim = claim(a, quiescent=True)
    assert len(admit(snapshot).units) == 1


def test_overlapping_ownership_and_unknown_scope_serialize():
    snapshot = make_snapshot(scopes=[("src",), ("src/api",), ("other",), ()])
    result = admit(snapshot)
    assert result.units == ("source:a:0", "source:b:2")
    assert set(result.waits.values()) == {"ownership conflict"}
    assert scopes_overlap(("resource:db",), ("resource:db",))
    assert not scopes_overlap(("resource:db",), ("resource:db-shadow",))
    assert not scopes_overlap(("src/api",), ("src/apix",))


def test_operator_hold_and_retry_only_block_affected_units():
    snapshot = make_snapshot()
    a, b, *_ = snapshot.units.values()
    a.hold = Hold("operator", "takeover")
    b.retry_at = 500
    assert len(admit(snapshot, now=499).units) == 2
    assert len(admit(snapshot, now=500).units) == 3
    a.hold = None
    assert len(admit(snapshot, now=500).units) == 4
    snapshot.phase = "draining"
    assert admit(snapshot).units == ()


def test_phase_resources_do_not_consume_execution_slots():
    snapshot = make_snapshot(workers=1)
    a = next(iter(snapshot.units.values()))
    a.state = UnitState.VERIFYING
    a.claim = claim(a, resource="verify")
    assert not resource_available(snapshot, "verify")
    assert resource_available(snapshot, "execution")
    assert len(admit(snapshot).units) == 1
    a.state = UnitState.REVIEWING
    a.claim.resource = "review"
    assert not resource_available(snapshot, "review")
    assert resource_available(snapshot, "verify")


def test_role_pool_selects_available_fable_without_waiting_on_astra():
    raw = spec_to_dict(make_snapshot().spec)
    raw["role_pools"] = {
        "reasoning": {
            "candidates": [
                {"profile": "codex", "model": "astra"},
                {"profile": "claude", "model": "fable"},
            ]
        }
    }
    raw["roles"] = {
        "planning": {"pool": "reasoning"},
        "conflict_repair": {"profile": "codex", "model": "astra"},
    }
    spec = spec_from_dict(raw)
    capacities = {
        ("codex", "astra"): CapacityObservation(False, reason="quota"),
        ("claude", "fable"): CapacityObservation(True, slots=1, model_access=True),
    }
    assert select_role(spec, "planning", capacities)[0].model == "fable"
    assert select_role(spec, "conflict_repair", capacities)[0] is None
    assert select_role(spec, "planning", {})[0] is None
    capacities[("codex", "astra")] = CapacityObservation(True, slots=1, model_access=True)
    assert select_role(spec, "planning", capacities)[0].model == "astra"
    assert (
        select_role(spec, "planning", capacities, selections={("codex", "astra"): 1})[0].model
        == "fable"
    )


def test_atomic_snapshot_intents_and_events_recover_together(tmp_path):
    store = CampaignStore(tmp_path / "state", clock=lambda: 42)
    snapshot = store.create(make_snapshot())
    unit = next(iter(snapshot.units))
    transition(snapshot, unit, UnitState.CLAIMED)
    action = add_action(snapshot, "create_pr", unit, {"head": "abc"})
    snapshot = store.save(snapshot, expected_revision=0)
    restored = CampaignStore(store.root).load(snapshot.id)
    assert restored.revision == 1
    assert restored.units[unit].state == UnitState.CLAIMED
    assert restored.outbox[0].id == action.id
    assert restored.events[0]["target"] == "claimed"
    projection = store.directory(snapshot.id) / "events.jsonl"
    projection.unlink()
    store.project_events(restored)
    assert '"target":"claimed"' in projection.read_text()
    assert len(store.list()) == 1


def test_stale_snapshot_or_generation_cannot_overwrite_authority(tmp_path):
    store = CampaignStore(tmp_path)
    snapshot = store.create(make_snapshot())
    stale = snapshot.clone()
    snapshot.generation = 1
    store.save(snapshot, 0)
    with pytest.raises(RevisionConflict):
        store.save(stale, 0)
    stale.revision = 1
    with pytest.raises(CoordinatorMismatch):
        store.save(stale, 1)
    latest = store.load(snapshot.id)
    with pytest.raises(CoordinatorMismatch):
        store.save(latest, 1, expected_generation=0)
    assert store.load(snapshot.id).generation == 1


def test_delivery_and_graph_are_not_retroactively_redefined(tmp_path):
    store = CampaignStore(tmp_path)
    snapshot = make_snapshot()
    unit = next(iter(snapshot.units.values()))
    unit.state = UnitState.DELIVERED
    snapshot = store.create(snapshot)
    old = store.load(snapshot.id)
    old.units[unit.id].spec = replace(unit.spec, title="Different delivered intent")
    old.graph_revision += 1
    with pytest.raises(CampaignError, match="landed unit"):
        store.save(old, 0)
    changed = store.load(snapshot.id)
    changed.units[unit.id].state = UnitState.READY
    with pytest.raises(CampaignError, match="landed unit"):
        store.save(changed, 0)
    unchanged = store.load(snapshot.id)
    unchanged.units.pop(unit.id)
    with pytest.raises(CampaignError, match="landed unit"):
        store.save(unchanged, 0)


def test_no_transition_can_skip_review_merge_or_tracking():
    snapshot = make_snapshot()
    key = next(iter(snapshot.units))
    with pytest.raises(IllegalTransition):
        transition(snapshot, key, UnitState.DELIVERED)
    snapshot.units[key].state = UnitState.LOCALLY_COMPLETE
    with pytest.raises(IllegalTransition):
        transition(snapshot, key, UnitState.MERGED_PENDING_SYNC)
    snapshot.units[key].state = UnitState.MERGED_PENDING_SYNC
    transition(snapshot, key, UnitState.DELIVERED)
    with pytest.raises(IllegalTransition):
        transition(snapshot, key, UnitState.READY)


def test_config_change_requires_revision_and_quiescent_workers(tmp_path):
    store = CampaignStore(tmp_path)
    snapshot = store.create(make_snapshot())
    changed = snapshot.clone()
    changed.config_snapshot["model"] = "another"
    with pytest.raises(CampaignError, match="explicit revision"):
        store.save(changed, 0)
    changed.config_revision += 1
    unit = next(iter(snapshot.units.values()))
    unit.claim = claim(unit)
    snapshot = store.save(snapshot, 0)
    changed.revision = snapshot.revision
    with pytest.raises(CampaignError, match="quiescent"):
        store.save(changed, snapshot.revision)


def test_control_requests_idempotent_and_separate_from_coordinator_state(tmp_path):
    store = CampaignStore(tmp_path)
    snapshot = store.create(make_snapshot())
    a = store.enqueue(
        snapshot.id, "hold", unit="source:a:0", reason="owner", request_id="request-1"
    )
    assert (
        store.enqueue(
            snapshot.id, "hold", unit="source:a:0", reason="owner", request_id="request-1"
        )
        == a
    )
    assert store.load(snapshot.id).revision == 0
    assert store.commands(snapshot.id) == [a]
    assert store.commands(snapshot.id, [a["id"]]) == []
    with pytest.raises(CampaignError, match="reused"):
        store.enqueue(snapshot.id, "release", unit="source:a:0", request_id="request-1")


def test_repository_coordinator_lock_shared_across_state_roots(tmp_path, project):
    # The sandbox is supplied by conftest; no hand-rolled Git repository.
    common = project.project / ".git"
    first = CampaignStore(tmp_path / "one")
    second = CampaignStore(tmp_path / "two")
    with first.coordinator(common, "main", campaign_id="one"):
        with pytest.raises(LockUnavailableError):
            with second.coordinator(common, "other", campaign_id="two"):
                pytest.fail("second coordinator acquired repository ownership")
    with second.coordinator(common, "main", campaign_id="two"):
        assert (common / "bmad-loop-campaign-owner.json").exists()


def test_atomic_write_failure_preserves_previous_authority(tmp_path, monkeypatch):
    store = CampaignStore(tmp_path)
    snapshot = store.create(make_snapshot())
    add_event(snapshot, "prepared", head="abc")
    add_action(snapshot, "push", "source:a:0", {"head": "abc"})
    original_write = store._write

    def fail(path, value):
        if path.name == "snapshot.json":
            raise OSError("disk unavailable")
        original_write(path, value)

    monkeypatch.setattr(store, "_write", fail)
    with pytest.raises(OSError, match="disk unavailable"):
        store.save(snapshot, 0)
    old = store.load(snapshot.id)
    assert old.revision == 0
    assert old.outbox == []
    assert old.events == []


def test_verification_processes_do_not_consume_coding_session_capacity():
    snapshot = make_snapshot(epics=["a"] * 5)
    verifying = next(iter(snapshot.units.values()))
    verifying.state = UnitState.VERIFYING
    verifying.claim = claim(verifying, resource="verify")
    assert len(admit(snapshot).units) == 4


def test_role_budget_unknown_or_exhausted_is_visible():
    raw = spec_to_dict(make_snapshot().spec)
    raw["roles"] = {"planning": {"profile": "codex", "model": "astra", "budget_usd": 10}}
    spec = spec_from_dict(raw)
    capacity = {("codex", "astra"): CapacityObservation(True, slots=1, model_access=True)}
    assert select_role(spec, "planning", capacity, spent_usd=9)[0].model == "astra"
    for spent in [10, 11, None]:
        result, reason = select_role(spec, "planning", capacity, spent_usd=spent)
        assert result is None
        assert "budget" in reason


def test_capacity_commands_and_queue_context_persist_in_resolved_roles():
    raw = spec_to_dict(make_snapshot().spec)
    command = ["project-tool", "capacity", "--json"]
    raw["role_pools"] = {
        "reasoning": {
            "candidates": [{"profile": "codex", "model": "astra", "capacity_command": command}]
        }
    }
    raw["roles"] = {
        "planning": {"pool": "reasoning"},
        "conflict_repair": {"profile": "codex", "model": "astra", "capacity_command": command},
    }
    raw["github"]["candidate_check_context"] = "campaign-candidate-review"
    spec = spec_from_dict(raw)
    assert spec.candidates("planning")[0].capacity_command == tuple(command)
    assert spec.candidates("conflict_repair")[0].capacity_command == tuple(command)
    assert spec_from_dict(spec_to_dict(spec)) == spec
    assert spec.github.candidate_check_context == "campaign-candidate-review"


def test_ownership_overrides_normalize_paths_and_roundtrip():
    raw = spec_to_dict(make_snapshot().spec)
    raw["ownership"] = {"source:a:0": ["./src/./api/", "resource:test-db"]}
    spec = spec_from_dict(raw)
    assert dict(spec.ownership)["source:a:0"] == ("src/api", "resource:test-db")
    assert spec_from_dict(spec_to_dict(spec)) == spec
    assert scopes_overlap(("src/./api",), ("src/api/routes",))
    for scopes in ([], ["../escape"], ["/absolute"]):
        raw["ownership"] = {"source:a:0": scopes}
        with pytest.raises(CampaignError, match="ownership|scope"):
            spec_from_dict(raw)


@pytest.mark.parametrize("method", ["squash", "rebase"])
def test_campaign_merge_method_preserves_every_checkpoint_commit(method):
    raw = spec_to_dict(make_snapshot().spec)
    raw["github"]["merge_method"] = method
    with pytest.raises(CampaignError, match="preserve all checkpoint commits"):
        spec_from_dict(raw)


def test_new_intake_requires_explicit_scope_and_bounded_limits():
    raw = spec_to_dict(make_snapshot().spec)
    assert spec_from_dict(raw).intake.allow_new_units is False
    raw["intake"] = {"allow_new_units": True}
    with pytest.raises(CampaignError, match="explicit allowed_scopes"):
        spec_from_dict(raw)
    raw["intake"] = {
        "allow_new_units": True,
        "allowed_scopes": ["src/api"],
        "max_new_units": 20,
        "max_triage_attempts": 2,
    }
    spec = spec_from_dict(raw)
    assert spec.intake.allowed_scopes == ("src/api",)
    assert spec_from_dict(spec_to_dict(spec)) == spec
    for key in ("max_new_units", "max_triage_attempts"):
        invalid = spec_to_dict(spec)
        invalid["intake"][key] = 0
        with pytest.raises(CampaignError, match="positive integer"):
            spec_from_dict(invalid)


def test_offline_dependencies_defer_only_configured_source_identity():
    raw = spec_to_dict(make_snapshot().spec)
    raw["sources"] = [
        {"kind": "stories", "id": "imported", "path": "specs", "initiative": "project"}
    ]
    raw["units"][0]["after"] = ["imported:project:prerequisite"]
    assert spec_from_dict(raw).units[0].after == ("imported:project:prerequisite",)
    for unknown in [
        "other:project:prerequisite",
        "imported:wrong-initiative:prerequisite",
        "prerequisite",
    ]:
        raw["units"][0]["after"] = [unknown]
        with pytest.raises(CampaignError, match="unknown dependency"):
            spec_from_dict(raw)


def test_versioned_source_publication_has_explicit_remote_branch_configuration():
    raw = spec_to_dict(make_snapshot().spec)
    raw["sources"] = [
        {"kind": "tickets", "id": "tickets", "store_remote": "origin", "store_branch": "main"}
    ]
    spec = spec_from_dict(raw)
    assert spec.sources[0].store_remote == "origin"
    assert spec.sources[0].store_branch == "main"
    assert spec_from_dict(spec_to_dict(spec)) == spec
    for configuration in [
        {"store_remote": "origin"},
        {"store_remote": "-unsafe", "store_branch": "main"},
        {"store_remote": "origin", "store_branch": "main\nother"},
    ]:
        raw["sources"] = [{"kind": "tickets", **configuration}]
        with pytest.raises(CampaignError, match="source publication"):
            spec_from_dict(raw)
