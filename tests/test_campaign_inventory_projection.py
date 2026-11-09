"""Native ownership stays bounded independently of cached discovery/history."""

import json
from contextlib import contextmanager
from dataclasses import asdict

import pytest
from test_campaign_migration import observed

from bmad_loop.campaign_database import LEGACY_MARKER, CampaignDatabase
from bmad_loop.campaign_jobs import JobLimits, JobRequest, prepare
from bmad_loop.campaign_model import (
    CampaignError,
    CampaignSnapshot,
    CampaignSpec,
    CampaignUnit,
    Hold,
    ReasoningSpec,
    UnitSpec,
    WorkerClaim,
)
from bmad_loop.campaign_store import CampaignStore, add_action

BOUND = 32768


def native_state(project, identity="current", *, phase="active"):
    unit_spec = UnitSpec("s:project:u", "s", "epic", "Unit", selector="authored-ticket")
    snapshot = CampaignSnapshot(
        identity,
        "example/repo",
        CampaignSpec("Existing", units=(unit_spec,), reasoning=ReasoningSpec(enabled=True)),
        {unit_spec.id: CampaignUnit(unit_spec)},
        common_dir=str((project.project / ".git").resolve()),
        root=str(project.project),
        phase=phase,
    )
    unit = snapshot.units[unit_spec.id]
    unit.claim = WorkerClaim(
        unit.id, 1, 1, "worker-launch", pid=123, process_identity="birth-1", run_dir="/owned/run"
    )
    unit.hold = Hold("operator", "owner hold")
    unit.bindings.update(workspace="/owned/workspace", pr={"number": 9})
    row = prepare(
        snapshot,
        JobRequest(
            "reasoning-job",
            "graph_review",
            "planning",
            {"graph_revision": 1, "config_revision": 1, "head": "a", "base": "b"},
            (),
            "Review the graph",
            {"type": "object", "properties": {}, "additionalProperties": False},
            JobLimits(100, 100),
            unit=unit.id,
        ),
    )
    row.update(
        state="running",
        claim={
            "launch_id": "reasoning-launch",
            "generation": 1,
            "quiescent": False,
            "run_dir": "/owned/reasoning",
            "native_id": "session-identity",
            "presentation": {"terminal_id": "terminal-identity"},
            "managed_communication": {"endpoint_id": "endpoint-identity"},
        },
    )
    return snapshot


@pytest.mark.parametrize("phase", ["active", "stopped"])
@pytest.mark.parametrize("schema", [2, 3])
def test_large_self_inventory_keeps_all_claims_without_reading_caches(
    project, tmp_path, monkeypatch, phase, schema
):
    snapshot = native_state(project, phase=phase)
    snapshot.bindings["inventory"] = {"old-observations": "x" * BOUND}
    snapshot.config_snapshot["unrelated"] = "x" * BOUND
    add_action(snapshot, "inventory", "", {"cached-observation": "x" * BOUND})
    store = CampaignStore(tmp_path / "native")
    store.create(snapshot)
    directory = store.directory(snapshot.id)
    database = CampaignDatabase(directory)
    if schema == 2:
        with database.connect(write=True) as connection:
            connection.execute("PRAGMA user_version=2")
        (directory / "snapshot.json").write_text(json.dumps(LEGACY_MARKER))
    with pytest.raises(CampaignError, match="authority exceeds byte bound"):
        database.read(max_bytes=BOUND)
    before = {path.name: path.read_bytes() for path in directory.iterdir() if path.is_file()}

    def full_read_forbidden(*args, **kwargs):
        raise AssertionError("ownership inventory loaded complete authority/jobs")

    monkeypatch.setattr(CampaignDatabase, "read", full_read_forbidden)
    result = observed(project, campaign_root=store.root, max_bytes=BOUND)
    assert result.complete, result.unavailable
    items = {item.kind: item for item in result.items if item.kind.startswith("native-")}
    unit = items["native-unit"]
    job = items["native-job"]
    source_unit = snapshot.units["s:project:u"]
    assert source_unit.claim is not None and source_unit.hold is not None
    assert unit.details is not None and job.details is not None
    assert unit.details["claim"] == asdict(source_unit.claim)
    assert unit.details["hold"] == asdict(source_unit.hold)
    assert unit.details["workspace"] == "/owned/workspace"
    assert unit.details["pr"] == {"number": 9}
    assert job.details["claim"] == snapshot.jobs["reasoning-job"]["claim"]
    assert job.units == unit.units == ("s:project:u",)
    assert job.ownership == unit.ownership == "owned"
    assert job.liveness == unit.liveness == "unknown"
    assert job.details["admissions_open"] is (phase == "active")
    assert {
        path.name: path.read_bytes() for path in directory.iterdir() if path.is_file()
    } == before


@pytest.mark.parametrize("storage", ["json", "sqlite"])
def test_other_campaign_claims_survive_inventory_and_do_not_grant_adoption(
    project, tmp_path, storage
):
    store = CampaignStore(tmp_path / "native", storage=storage)
    for identity in ("current", "other"):
        store.create(native_state(project, identity, phase="stopped"))
    result = observed(project, campaign_root=store.root)
    assert result.complete
    jobs = [item for item in result.items if item.kind == "native-job"]
    assert {(item.details or {})["campaign_id"] for item in jobs} == {"current", "other"}
    assert all(item.ownership == "owned" and item.liveness == "unknown" for item in jobs)
    from test_campaign_migration import manifest, replacement, select

    from bmad_loop.campaign_migration import validate_manifest

    qualification = validate_manifest(
        result, manifest(result, select(jobs[1], "adopt")), replacement(result), now=100
    )
    assert not qualification.qualified
    assert qualification.held_units == ("s:project:u",)
    assert any("adoption ownership/preservation not proven" in p for p in qualification.problems)


@pytest.mark.parametrize("oversized", ["unit", "job", "combined", "legacy"])
def test_actual_ownership_or_legacy_overflow_is_visible(project, tmp_path, oversized):
    snapshot = native_state(project)
    if oversized == "unit":
        snapshot.units["s:project:u"].bindings["workspace"] = "x" * BOUND
    elif oversized in {"job", "combined"}:
        snapshot.jobs["reasoning-job"]["claim"]["run_dir"] = "x" * BOUND
        if oversized == "combined":
            snapshot.jobs["reasoning-job"]["claim"]["run_dir"] = "x" * (BOUND - 1000)
    else:
        snapshot.bindings["inventory"] = "x" * BOUND
    store = CampaignStore(
        tmp_path / "native", storage="json" if oversized == "legacy" else "sqlite"
    )
    store.create(snapshot)
    result = observed(project, campaign_root=store.root, max_bytes=BOUND)
    assert not result.complete
    assert any(
        "current:" in value and "exceeds byte bound" in value for value in result.unavailable
    )
    assert not any(item.kind.startswith("native-") for item in result.items)


def test_native_items_have_a_total_bound(project, tmp_path):
    store = CampaignStore(tmp_path / "native")
    store.create(native_state(project))
    # The worktree consumes one slot; the unit and reasoning claim need two more.
    result = observed(project, campaign_root=store.root, max_items=2)
    assert not result.complete
    assert any("ownership projection exceeds item bound" in value for value in result.unavailable)


@pytest.mark.parametrize(
    "fault", ["foreign", "identity", "claim", "unit", "schema", "symlink", "shape", "worker"]
)
def test_broken_native_store_is_visible_and_does_not_hide_other_owners(project, tmp_path, fault):
    store = CampaignStore(tmp_path / "native")
    for identity in ("a-broken", "z-other"):
        store.create(native_state(project, identity))
    directory = store.directory("a-broken")
    database = CampaignDatabase(directory)
    if fault == "symlink":
        target = tmp_path / "moved-store"
        directory.rename(target)
        directory.symlink_to(target, target_is_directory=True)
    else:
        with database.connect(write=True) as connection:
            with connection:
                if fault == "shape":
                    connection.execute("UPDATE authority SET payload='null' WHERE id=1")
                elif fault == "worker":
                    connection.execute(
                        "UPDATE authority SET payload=json_set(payload,"
                        "'$.units.\"s:project:u\".claim.unit','another-unit') WHERE id=1"
                    )
                elif fault in {"foreign", "identity", "schema"}:
                    key, value = {
                        "foreign": ("common_dir", "/another/common"),
                        "identity": ("id", "wrong-id"),
                        "schema": ("schema_version", 999),
                    }[fault]
                    connection.execute(
                        "UPDATE authority SET payload=json_set(payload,?,?) WHERE id=1",
                        ("$." + key, value),
                    )
                else:
                    key = "$.claim.quiescent" if fault == "claim" else "$.request.unit"
                    connection.execute(
                        "UPDATE jobs SET payload=json_set(payload,?,?)", (key, "invalid")
                    )
    result = observed(project, campaign_root=store.root)
    assert not result.complete
    assert any("native-campaigns:a-broken:" in value for value in result.unavailable)
    native = [item for item in result.items if item.kind.startswith("native-")]
    assert {(item.details or {})["campaign_id"] for item in native} == {"z-other"}
    assert {item.kind for item in native} == {"native-unit", "native-job"}


def test_terminal_job_with_uncertain_claim_remains_visible(project, tmp_path):
    store = CampaignStore(tmp_path / "native")
    store.create(native_state(project))
    database = CampaignDatabase(store.directory("current"))
    with database.connect(write=True) as connection:
        with connection:
            connection.execute(
                "UPDATE jobs SET state='stale',payload=json_set(payload,'$.state','stale')"
            )
    result = observed(project, campaign_root=store.root)
    assert result.complete
    job = next(item for item in result.items if item.kind == "native-job")
    assert job.state == "stale" and job.ownership == "owned" and job.liveness == "unknown"


def test_confirmed_terminal_history_does_not_consume_current_ownership_bound(project, tmp_path):
    snapshot = native_state(project)
    row = snapshot.jobs["reasoning-job"]
    row.update(state="stale", result={"old": "x" * BOUND})
    row["claim"].update(quiescent=True, run_dir="x" * BOUND)
    store = CampaignStore(tmp_path / "native")
    store.create(snapshot)
    result = observed(project, campaign_root=store.root, max_bytes=BOUND)
    assert result.complete
    assert [item.kind for item in result.items if item.kind.startswith("native-")] == [
        "native-unit"
    ]


def test_unit_and_reasoning_claims_share_one_read_transaction(project, tmp_path, monkeypatch):
    store = CampaignStore(tmp_path / "native")
    store.create(native_state(project))
    database = CampaignDatabase(store.directory("current"))
    connect = CampaignDatabase.connect
    changes = []

    class ReadConnection:
        def __init__(self, connection):
            self.connection = connection

        def execute(self, query, *args):
            if query.startswith("WITH projection(id,value)"):
                with connect(database, write=True) as writer:
                    with writer:
                        writer.execute(
                            "UPDATE authority SET payload=json_set(payload,"
                            "'$.units.\"s:project:u\".claim.launch_id','next-worker') WHERE id=1"
                        )
                        writer.execute(
                            "UPDATE jobs SET payload=json_set(payload,"
                            "'$.claim.launch_id','next-reasoning')"
                        )
                changes.append("committed-between-queries")
            return self.connection.execute(query, *args)

    @contextmanager
    def concurrent_read(self, *, write=False):
        with connect(self, write=write) as connection:
            yield connection if write else ReadConnection(connection)

    monkeypatch.setattr(CampaignDatabase, "connect", concurrent_read)
    projection = database.read_inventory(max_bytes=BOUND, max_items=10)
    assert changes == ["committed-between-queries"]
    assert projection["units"]["s:project:u"]["claim"]["launch_id"] == "worker-launch"
    assert projection["jobs"]["reasoning-job"]["claim"]["launch_id"] == "reasoning-launch"
    current, _ = database.read()
    assert current["units"]["s:project:u"]["claim"]["launch_id"] == "next-worker"
    assert current["jobs"]["reasoning-job"]["claim"]["launch_id"] == "next-reasoning"
