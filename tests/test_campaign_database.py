"""Campaign authority survives migration/crashes without reading cold history."""

import json
from pathlib import Path

import pytest

from bmad_loop.campaign_database import MARKER, CampaignDatabase, EventJournal
from bmad_loop.campaign_model import (
    CampaignError,
    CampaignSnapshot,
    CampaignSpec,
    CampaignUnit,
    UnitSpec,
    WorkerClaim,
    snapshot_from_dict,
    snapshot_to_dict,
)
from bmad_loop.campaign_store import CampaignStore, add_action, add_event


def state(common=""):
    spec = UnitSpec("source:epic:story", "source", "epic", "Story", scopes=("src/",))
    return CampaignSnapshot(
        "database-test",
        "owner/repo",
        CampaignSpec("Test", units=(spec,)),
        {spec.id: CampaignUnit(spec)},
        common_dir=str(common),
    )


def test_hot_save_and_clone_do_not_scan_persisted_history(tmp_path, monkeypatch):
    store = CampaignStore(tmp_path)
    initial = state()
    initial.events = [{"kind": "old", "index": index} for index in range(10000)]
    current = store.create(initial)
    assert isinstance(current.events, EventJournal)
    assert not current.events.cache
    original = CampaignDatabase.events
    queries = []

    def bounded(self, **kwargs):
        queries.append(kwargs)
        assert kwargs["after"] >= 10000, "hot save traversed cold journal"
        return original(self, **kwargs)

    monkeypatch.setattr(CampaignDatabase, "events", bounded)
    candidate = current.clone()
    add_event(candidate, "new")
    saved = store.save(candidate, 0)
    assert saved.revision == 1 and len(saved.events) == 10001
    assert saved.events.cache == {}
    assert queries
    raw, count = CampaignDatabase(store.directory(saved.id)).read()
    assert raw["events"] == [] and count == 10001


def test_transaction_failure_rolls_back_events_and_intents(tmp_path, monkeypatch):
    store = CampaignStore(tmp_path)
    current = store.create(state())
    add_event(current, "prepared")
    add_action(current, "push", "source:epic:story", {"head": "abc"})
    append = CampaignDatabase._append

    def fail(connection, offset, events):
        append(connection, offset, events)
        raise OSError("disk unavailable")

    monkeypatch.setattr(CampaignDatabase, "_append", staticmethod(fail))
    with pytest.raises(OSError, match="disk unavailable"):
        store.save(current, 0)
    restored = store.load(current.id)
    assert restored.revision == 0 and restored.events == [] and restored.outbox == []


def test_persisted_event_mutation_is_rejected(tmp_path):
    store = CampaignStore(tmp_path)
    current = state()
    add_event(current, "original", nested={"approved": False})
    current = store.create(current)
    current.events[0]["nested"]["approved"] = True
    with pytest.raises(CampaignError, match="append-only"):
        store.save(current, current.revision)
    assert store.load(current.id).events[0]["nested"] == {"approved": False}


def test_projection_recovers_a_partial_append_without_duplicate_events(tmp_path):
    store = CampaignStore(tmp_path)
    current = store.create(state())
    add_event(current, "first")
    current = store.save(current, 0)
    path = store.directory(current.id) / "events.jsonl"
    with path.open("a") as stream:
        stream.write('{"partial":')
    add_event(current, "second")
    current = store.save(current, 1)
    rows = [json.loads(row) for row in path.read_text().splitlines()]
    assert [row["kind"] for row in rows] == ["first", "second"]
    assert [row["sequence"] for row in rows] == [1, 2]
    assert store.history(current.id, after=1) == [rows[1]]


def test_migration_preserves_bytes_and_fences_older_readers(tmp_path, project):
    store = CampaignStore(tmp_path, storage="json")
    current = state(project.project / ".git")
    add_event(current, "original")
    current = store.create(current)
    directory = store.directory(current.id)
    original = (directory / "snapshot.json").read_bytes()
    migrated = store.migrate_state(current.id)
    assert isinstance(migrated.events, EventJournal)
    assert snapshot_to_dict(migrated) == snapshot_to_dict(current)
    assert (directory / "snapshot.v1.backup.json").read_bytes() == original
    assert json.loads((directory / "snapshot.json").read_text()) == MARKER
    with pytest.raises(CampaignError, match="unsupported.*schema"):
        snapshot_from_dict(MARKER)
    assert store.migrate_state(current.id).revision == current.revision


def test_migration_retries_after_crash_before_marker_switch(tmp_path, project, monkeypatch):
    store = CampaignStore(tmp_path, storage="json")
    current = store.create(state(project.project / ".git"))
    write = store._write

    def fail(path, value):
        if value == MARKER:
            raise OSError("crash before switch")
        write(path, value)

    monkeypatch.setattr(store, "_write", fail)
    with pytest.raises(OSError, match="crash before switch"):
        store.migrate_state(current.id)
    assert isinstance(store.load(current.id).events, list)
    monkeypatch.setattr(store, "_write", write)
    assert isinstance(store.migrate_state(current.id).events, EventJournal)


def test_migration_retries_after_failure_inside_initial_database_transaction(
    tmp_path, project, monkeypatch
):
    store = CampaignStore(tmp_path, storage="json")
    current = store.create(state(project.project / ".git"))
    append = CampaignDatabase._append

    def fail(connection, offset, events):
        append(connection, offset, events)
        raise OSError("initial transaction interrupted")

    monkeypatch.setattr(CampaignDatabase, "_append", staticmethod(fail))
    with pytest.raises(OSError, match="initial transaction interrupted"):
        store.migrate_state(current.id)
    assert isinstance(store.load(current.id).events, list)
    assert not CampaignDatabase(store.directory(current.id)).path.exists()
    monkeypatch.setattr(CampaignDatabase, "_append", staticmethod(append))
    assert isinstance(store.migrate_state(current.id).events, EventJournal)


@pytest.mark.parametrize("uncertain", ["worker", "effect"])
def test_migration_refuses_live_or_uncertain_ownership(tmp_path, project, uncertain):
    store = CampaignStore(tmp_path, storage="json")
    initial = state(project.project / ".git")
    unit = next(iter(initial.units.values()))
    if uncertain == "worker":
        unit.claim = WorkerClaim(unit.id, 1, 1, "launch")
    else:
        add_action(initial, "push", unit.id, {}).state = "sent_unknown"
    current = store.create(initial)
    with pytest.raises(CampaignError, match="requires"):
        store.migrate_state(current.id)
    assert not (store.directory(current.id) / "campaign.sqlite").exists()


def test_database_backup_includes_committed_wal_and_rejects_symlink(tmp_path):
    store = CampaignStore(tmp_path / "state")
    current = store.create(state())
    add_event(current, "committed")
    current = store.save(current, 0)
    database = CampaignDatabase(store.directory(current.id))
    backup_dir = tmp_path / "backup"
    backup_dir.mkdir()
    database.backup(backup_dir / "campaign.sqlite")
    raw, count = CampaignDatabase(backup_dir).read()
    assert raw["revision"] == 1 and count == 1
    sidecar = Path(str(database.path) + "-wal")
    sidecar.unlink(missing_ok=True)
    sidecar.symlink_to(backup_dir / "campaign.sqlite")
    with pytest.raises(CampaignError, match="regular"):
        store.load(current.id)


def test_bounded_history_uses_sequence_cursor_across_filters(tmp_path):
    store = CampaignStore(tmp_path)
    current = state()
    for index in range(6):
        add_event(current, "even" if index % 2 == 0 else "odd", unit=str(index % 3))
    current = store.create(current)
    first = store.history(current.id, kind="even", limit=2)
    assert [row["sequence"] for row in first] == [1, 3]
    assert [row["sequence"] for row in store.history(current.id, kind="even", after=3)] == [5]


def test_fast_authority_document_preserves_schema_and_nested_isolation(tmp_path):
    from bmad_loop.campaign_store import _state_document

    original = state()
    original.bindings["nested"] = {"rows": [1, {"unicode": "résumé"}]}
    add_action(
        original, "push", "source:epic:story", {"paths": ("a", "b"), "nested": {"head": "old"}}
    )
    expected = json.loads(json.dumps(snapshot_to_dict(original, include_events=False)))
    assert _state_document(original) == expected
    store = CampaignStore(tmp_path)
    created = store.create(original)
    original.bindings["nested"]["rows"][1]["unicode"] = "caller changed"
    assert created.bindings["nested"]["rows"][1]["unicode"] == "résumé"
    saved = store.save(created, created.revision)
    created.outbox[0].payload["nested"]["head"] = "caller changed"
    assert saved.outbox[0].payload["nested"]["head"] == "old"
    assert store.load(saved.id).outbox[0].payload["nested"]["head"] == "old"


def test_fast_normalization_canonicalizes_role_and_pool_order(tmp_path):
    from dataclasses import replace

    from bmad_loop.campaign_model import RoleCandidate, RoleSpec

    original = state()
    original.spec = replace(
        original.spec,
        roles=(("triage", RoleSpec(pool="z-pool")), ("planning", RoleSpec(pool="a-pool"))),
        role_pools=(
            ("z-pool", (RoleCandidate("codex", "z"),)),
            ("a-pool", (RoleCandidate("claude", "a"),)),
        ),
    )
    store = CampaignStore(tmp_path)
    created = store.create(original)
    assert created.spec == store.load(created.id).spec
    saved = store.save(created, created.revision)
    assert saved.spec == created.spec and saved.config_revision == created.config_revision
